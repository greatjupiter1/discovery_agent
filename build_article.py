#!/usr/bin/env python3
"""Scientific Discovery Agent — v1 pipeline (fail-closed).

One production run:
  1. Read state.json; refuse to duplicate an existing (field, topic, language).
  2. Retrieve candidate works from FREE indexes only (OpenAlex, Crossref,
     arXiv, Semantic Scholar — no API keys).
  3. VERIFICATION LAW: a reference is kept only if its DOI resolves through
     Crossref, or its arXiv page exists, or its publisher landing page
     answers HTTP 200. Anything unverifiable is discarded. Nothing is ever
     invented: no reference, no number, no result.
  4. Extract a structured card per kept source (title, authors, year, venue,
     abstract).
  5. Gemini writes a review/synthesis article in the standard structure,
     synthesising ONLY from the extracted cards, citing them as [n]. The
     reference list itself is formatted by this script from the verified
     cards — never by the model.
  6. Deliver to Telegram: header message + article chunks + the full article
     as a .md document.
  7. Append the production to state.json (the workflow commits it back).

If fewer than 5 sources survive verification, the run FAILS LOUDLY instead
of writing an article on sand.

Retry law (Guyton p.6 lesson, hard rule): EVERY transient failure — network
errors, timeouts, HTTP 429 and 5xx — is retried with backoff, up to 6
attempts, with a 180-second read timeout. Transport exceptions retry exactly
like HTTP error statuses.

Inputs come from the environment (set by .github/workflows/discovery.yml):
  FIELD, TOPIC, LANGUAGE (default "fa"), MODE (default "review"),
  GEMINI_MODEL (default "gemini-3.8-flash")
Secrets (names only, set in the repo's Actions secrets):
  GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
MIN_SOURCES = 5
MAX_VERIFIED = 20
MAX_CANDIDATES = 80
READ_TIMEOUT = 180
MAX_ATTEMPTS = 6

LANGUAGE_NAMES = {
    "fa": "Persian (Farsi)", "en": "English", "ar": "Arabic", "zh": "Chinese",
    "fr": "French", "de": "German", "es": "Spanish", "ru": "Russian",
    "tr": "Turkish", "ur": "Urdu",
}


class Fail(Exception):
    """A fail-closed stop: the run ends with this message and no output."""


# --------------------------------------------------------------------------
# HTTP with the full retry law
# --------------------------------------------------------------------------

def http_request(url, method="GET", data=None, headers=None, timeout=READ_TIMEOUT):
    """One logical HTTP request with retries on ALL transient failures."""
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", "discovery-agent/1.0 (research; mailto:research@example.org)")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            if e.code == 429 or 500 <= e.code < 600:
                last_error = Fail(f"HTTP {e.code} from {url.split('?')[0]}: {body[:200]!r}")
            else:
                # A definitive answer (e.g. 404) is not transient: return it.
                return e.code, body
        except Exception as e:  # timeouts, DNS, resets — all transient
            last_error = e
        if attempt < MAX_ATTEMPTS:
            time.sleep(30 * attempt)
    raise Fail(f"request failed after {MAX_ATTEMPTS} attempts: {url.split('?')[0]} ({last_error})")


def http_json(url, **kw):
    status, raw = http_request(url, **kw)
    if status != 200:
        raise Fail(f"HTTP {status} from {url.split('?')[0]}")
    return json.loads(raw.decode("utf-8", "replace"))


def page_exists(url):
    """True if the URL answers with a 2xx/3xx status (used by the
    verification law for arXiv / publisher pages)."""
    try:
        status, _ = http_request(url, timeout=60)
        return 200 <= status < 400
    except Fail:
        return False


# --------------------------------------------------------------------------
# Retrieval — free indexes only, no keys
# --------------------------------------------------------------------------

def reconstruct_abstract(inverted):
    if not inverted:
        return ""
    positions = []
    for word, idxs in inverted.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)


def search_openalex(query):
    out = []
    url = ("https://api.openalex.org/works?per-page=25&search="
           + urllib.parse.quote(query))
    data = http_json(url)
    for w in data.get("results", []):
        authors = [a.get("author", {}).get("display_name", "")
                   for a in w.get("authorships", [])]
        venue = ""
        loc = w.get("primary_location") or {}
        src = loc.get("source") or {}
        venue = src.get("display_name", "") or ""
        out.append({
            "title": (w.get("title") or "").strip(),
            "authors": [a for a in authors if a],
            "year": w.get("publication_year"),
            "venue": venue,
            "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
            "url": (loc.get("landing_page_url") or w.get("id") or ""),
            "abstract": reconstruct_abstract(w.get("abstract_inverted_index")),
            "arxiv_id": None,
            "index": "OpenAlex",
        })
    return out


def search_crossref(query):
    out = []
    url = ("https://api.crossref.org/works?rows=25&query="
           + urllib.parse.quote(query))
    data = http_json(url)
    for it in data.get("message", {}).get("items", []):
        titles = it.get("title") or []
        authors = []
        for a in it.get("author", []):
            name = " ".join(x for x in [a.get("given", ""), a.get("family", "")] if x).strip()
            if name:
                authors.append(name)
        year = None
        for key in ("published-print", "published-online", "issued", "created"):
            parts = ((it.get(key) or {}).get("date-parts") or [[None]])[0]
            if parts and parts[0]:
                year = parts[0]
                break
        abstract = re.sub(r"<[^>]+>", " ", it.get("abstract") or "")
        abstract = re.sub(r"\s+", " ", abstract).strip()
        out.append({
            "title": (titles[0] if titles else "").strip(),
            "authors": authors,
            "year": year,
            "venue": (it.get("container-title") or [""])[0],
            "doi": it.get("DOI"),
            "url": it.get("URL", ""),
            "abstract": abstract,
            "arxiv_id": None,
            "index": "Crossref",
        })
    return out


def search_arxiv(query):
    out = []
    url = ("https://export.arxiv.org/api/query?start=0&max_results=20"
           "&search_query=" + urllib.parse.quote(f'all:"{query}"'))
    status, raw = http_request(url)
    if status != 200:
        return out
    ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    root = ET.fromstring(raw.decode("utf-8", "replace"))
    for entry in root.findall("a:entry", ns):
        abs_url = (entry.findtext("a:id", "", ns) or "").strip()
        m = re.search(r"arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", abs_url)
        arxiv_id = m.group(1) if m else None
        doi_el = entry.find("arxiv:doi", ns)
        published = entry.findtext("a:published", "", ns) or ""
        out.append({
            "title": re.sub(r"\s+", " ", entry.findtext("a:title", "", ns) or "").strip(),
            "authors": [re.sub(r"\s+", " ", a.findtext("a:name", "", ns) or "").strip()
                        for a in entry.findall("a:author", ns)],
            "year": int(published[:4]) if published[:4].isdigit() else None,
            "venue": "arXiv",
            "doi": doi_el.text.strip() if doi_el is not None and doi_el.text else None,
            "url": abs_url,
            "abstract": re.sub(r"\s+", " ", entry.findtext("a:summary", "", ns) or "").strip(),
            "arxiv_id": arxiv_id,
            "index": "arXiv",
        })
    return out


def search_semantic_scholar(query):
    out = []
    fields = "title,abstract,authors,year,venue,externalIds,url"
    url = ("https://api.semanticscholar.org/graph/v1/paper/search?limit=25&fields="
           + fields + "&query=" + urllib.parse.quote(query))
    data = http_json(url)
    for p in data.get("data", []) or []:
        ext = p.get("externalIds") or {}
        arxiv_id = ext.get("ArXiv")
        out.append({
            "title": (p.get("title") or "").strip(),
            "authors": [a.get("name", "") for a in p.get("authors", []) if a.get("name")],
            "year": p.get("year"),
            "venue": p.get("venue") or "",
            "doi": ext.get("DOI"),
            "url": p.get("url", "") or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""),
            "abstract": p.get("abstract") or "",
            "arxiv_id": arxiv_id,
            "index": "Semantic Scholar",
        })
    return out


SEARCHERS = [search_openalex, search_crossref, search_arxiv, search_semantic_scholar]


def normalise_title(t):
    return re.sub(r"[^a-z0-9\u0600-\u06FF]+", " ", (t or "").lower()).strip()


def retrieve(field, topic, queries):
    candidates, seen = [], set()
    for query in queries:
        for searcher in SEARCHERS:
            try:
                results = searcher(query)
            except Fail as e:
                print(f"WARNING: {searcher.__name__} failed for query {query!r}: {e}")
                continue
            for c in results:
                if not c["title"]:
                    continue
                key = (c["doi"].lower() if c["doi"] else None) or normalise_title(c["title"])
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(c)
                if len(candidates) >= MAX_CANDIDATES:
                    return candidates
    return candidates


# --------------------------------------------------------------------------
# Verification law
# --------------------------------------------------------------------------

def verify(candidate):
    """A reference survives ONLY with independent proof it exists."""
    doi = candidate.get("doi")
    if doi:
        status, _ = http_request(
            "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe=""),
            timeout=60)
        if status == 200:
            return True
        # DOI present but Crossref has no record: fall through to page check.
    if candidate.get("arxiv_id"):
        if page_exists(f"https://arxiv.org/abs/{candidate['arxiv_id']}"):
            return True
    url = candidate.get("url") or ""
    if url.startswith("http") and "openalex.org" not in url:
        if page_exists(url):
            return True
    return False


# --------------------------------------------------------------------------
# Gemini writing
# --------------------------------------------------------------------------

def gemini(prompt, api_key, model):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 8192},
    }).encode("utf-8")
    status, raw = http_request(url, method="POST", data=body, headers={
        "Content-Type": "application/json", "x-goog-api-key": api_key})
    if status != 200:
        raise Fail(f"Gemini HTTP {status}: {raw[:300]!r}")
    data = json.loads(raw.decode("utf-8", "replace"))
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except Exception:
        raise Fail(f"Gemini returned an unexpected shape: {raw[:300]!r}")
    if not text:
        raise Fail("Gemini returned empty text")
    return text


def translate_query_to_english(text, api_key, model):
    """Best-effort: an English search query for the indexes. Never fatal."""
    try:
        out = gemini(
            "Translate this research topic into a short English search query "
            "(keywords only, no quotes, no explanation):\n" + text,
            api_key, model)
        return out.strip().strip('"')[:200]
    except Exception as e:
        print(f"WARNING: query translation failed ({e}); continuing without it")
        return None


def source_cards_text(sources):
    lines = []
    for i, s in enumerate(sources, 1):
        authors = ", ".join(s["authors"][:6]) or "Unknown authors"
        lines.append(
            f"[{i}] {s['title']}\n"
            f"    Authors: {authors}\n"
            f"    Year: {s['year'] or 'n.d.'} | Venue: {s['venue'] or 'n/a'} | "
            f"DOI: {s['doi'] or 'none'} | URL: {s['url']}\n"
            f"    Abstract: {(s['abstract'] or '(no abstract available)')[:1500]}")
    return "\n\n".join(lines)


def format_reference(i, s):
    authors = ", ".join(s["authors"][:6])
    if len(s["authors"]) > 6:
        authors += ", et al."
    ref = f"[{i}] {authors + '. ' if authors else ''}{s['title']}."
    if s["venue"]:
        ref += f" {s['venue']}"
    if s["year"]:
        ref += f", {s['year']}"
    ref += "."
    if s["doi"]:
        ref += f" https://doi.org/{s['doi']}"
    elif s["url"]:
        ref += f" {s['url']}"
    return ref


def write_article(field, topic, language, sources, api_key, model):
    lang_name = LANGUAGE_NAMES.get(language.lower(), language)
    cards = source_cards_text(sources)
    hard_rules = (
        "HARD RULES — follow exactly:\n"
        "- Use ONLY the numbered sources below. Never mention any work, author, "
        "number, journal or result that is not in these sources.\n"
        "- Every factual claim must carry its source number like [3] at the end "
        "of the sentence.\n"
        "- Synthesise in your own words; never copy a sentence from an abstract.\n"
        "- If the sources do not answer something, say so plainly instead of "
        "filling the gap.\n"
        f"- Write the ENTIRE output in {lang_name}.\n")
    common = (f"Field: {field}\nTopic: {topic}\n\n{hard_rules}\n"
              f"THE ONLY SOURCES YOU MAY USE:\n{cards}\n")

    part1 = gemini(common +
        "\nWrite PART 1 of a scholarly review article:\n"
        "1) A precise title line (no exaggeration), prefixed with 'TITLE: '\n"
        "2) '## Abstract' — a structured abstract with labelled parts: "
        "Background, Objective, Method, Findings, Conclusion — based only on "
        "the sources.\n"
        "3) '## Keywords' — 5 to 7 keywords on one line.\n"
        "4) '## Introduction' — a funnel: broad context, narrowing to the "
        "specific topic, the gap visible in these sources, and this review's aim.",
        api_key, model)
    progress_update(55, "✍️ نگارش بخش اول")
    part2 = gemini(common +
        "\nWrite PART 2 of the same review article: the body. Organise the "
        "synthesis into 3 to 5 thematic sections, each with a '## ' heading. "
        "Compare the sources with each other, note agreements and "
        "contradictions, and keep every claim cited as [n]. Do NOT repeat the "
        "title, abstract or introduction.",
        api_key, model)
    progress_update(75, "✍️ نگارش بخش دوم")
    part3 = gemini(common +
        "\nWrite PART 3 of the same review article:\n"
        "1) '## Discussion' — what the combined evidence means, and how it "
        "compares across the sources.\n"
        "2) '## Conclusion' — the takeaways that the sources actually support.\n"
        "3) '## Limitations' — honest limits of this review (sources are "
        "abstract-level, free-access only, etc.).\n"
        "Do NOT write a reference list; it is added separately.",
        api_key, model)
    progress_update(90, "✍️ نگارش بخش پایانی")

    title = ""
    m = re.search(r"TITLE:\s*(.+)", part1)
    if m:
        title = m.group(1).strip().strip("#").strip()
        part1 = part1.replace(m.group(0), "", 1).strip()
    if not title:
        title = f"{topic} — a review of the literature"
    references = "\n".join(format_reference(i, s) for i, s in enumerate(sources, 1))
    article = (f"# {title}\n\n{part1}\n\n{part2}\n\n{part3}\n\n"
               f"## References\n\n{references}\n")
    return title, article


# --------------------------------------------------------------------------
# Production progress (§13) — fail-soft: progress reporting must NEVER
# fail the build. If ARTICLE_CHAT_ID is set (the requesting user's chat,
# passed by the Worker through the workflow), one message in that chat is
# edited in place with monotonic percents + a Persian stage label.
# --------------------------------------------------------------------------

_progress = {"chat_id": "", "token": "", "message_id": None,
             "pct": -1, "stage": "شروع"}


def _progress_text(pct, stage):
    if pct >= 100:
        return "✅ مقاله در کانال Discovery منتشر شد\nپیشرفت تولید: 100%"
    return f"⏳ تولید مقاله\nمرحله: {stage}\nپیشرفت تولید: {pct}%"


def _progress_send_or_edit(pct, stage):
    """Best-effort: first call sends the message, later calls edit it.
    Monotonic: a percent never goes down. Never raises."""
    try:
        if not _progress["chat_id"] or not _progress["token"]:
            return
        if pct <= _progress["pct"]:
            return
        _progress["pct"] = pct
        _progress["stage"] = stage
        text = _progress_text(pct, stage)
        if _progress["message_id"] is None:
            r = tg_call("sendMessage", _progress["token"],
                        {"chat_id": _progress["chat_id"], "text": text})
            _progress["message_id"] = r["message_id"]
        else:
            tg_call("editMessageText", _progress["token"], {
                "chat_id": _progress["chat_id"],
                "message_id": _progress["message_id"],
                "text": text,
            })
    except Exception as e:
        print(f"WARNING: progress update {pct}% failed (ignored): {e}")


def progress_start(chat_id, token):
    _progress["chat_id"] = (chat_id or "").strip()
    _progress["token"] = (token or "").strip()
    if _progress["chat_id"]:
        print(f"Progress reporting on for chat {_progress['chat_id']}")
        _progress_send_or_edit(0, "شروع")


def progress_update(pct, stage):
    _progress_send_or_edit(pct, stage)


def progress_fail():
    """Best-effort: mark the progress message as stopped. Never raises."""
    try:
        if not _progress["chat_id"] or _progress["message_id"] is None:
            return
        tg_call("editMessageText", _progress["token"], {
            "chat_id": _progress["chat_id"],
            "message_id": _progress["message_id"],
            "text": f"❌ تولید متوقف شد — مرحله: {_progress['stage']}",
        })
    except Exception as e:
        print(f"WARNING: progress fail-mark failed (ignored): {e}")


# --------------------------------------------------------------------------
# Telegram delivery
# --------------------------------------------------------------------------

def tg_call(method, token, payload):
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(payload).encode("utf-8")
    status, raw = http_request(url, method="POST", data=body,
                               headers={"Content-Type": "application/json"})
    data = json.loads(raw.decode("utf-8", "replace"))
    if status != 200 or not data.get("ok"):
        raise Fail(f"Telegram {method} failed: HTTP {status}: {raw[:300]!r}")
    return data["result"]


def tg_send_document(token, chat_id, filename, content_bytes, caption=""):
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    boundary = "----discovery" + uuid.uuid4().hex
    parts = []
    def field_part(name, value):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f"name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8"))
    field_part("chat_id", str(chat_id))
    if caption:
        field_part("caption", caption[:1000])
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
        f"filename=\"{filename}\"\r\nContent-Type: text/markdown\r\n\r\n".encode("utf-8")
        + content_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    status, raw = http_request(url, method="POST", data=body, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    data = json.loads(raw.decode("utf-8", "replace"))
    if status != 200 or not data.get("ok"):
        raise Fail(f"Telegram sendDocument failed: HTTP {status}: {raw[:300]!r}")
    return data["result"]


def split_text(text, limit=3900):
    chunks, current = [], ""
    for para in text.split("\n\n"):
        piece = para if not current else current + "\n\n" + para
        if len(piece) <= limit:
            current = piece
        else:
            if current:
                chunks.append(current)
            while len(para) > limit:
                cut = para.rfind("\n", 0, limit)
                if cut < limit // 2:
                    cut = limit
                chunks.append(para[:cut])
                para = para[cut:].lstrip("\n")
            current = para
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"productions": []}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    field = os.environ.get("FIELD", "").strip()
    topic = os.environ.get("TOPIC", "").strip()
    language = (os.environ.get("LANGUAGE") or "fa").strip()
    mode = (os.environ.get("MODE") or "review").strip().lower()
    model = (os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash").strip()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()

    if not field or not topic:
        raise Fail("FIELD and TOPIC inputs are required.")
    if mode != "review":
        raise Fail(f"Mode '{mode}' is not in the v1 scope (v1 = review/synthesis only).")
    missing = [n for n, v in [("GEMINI_API_KEY", api_key),
                              ("TELEGRAM_BOT_TOKEN", tg_token),
                              ("CHAT_ID", chat_id)] if not v]
    if missing:
        raise Fail("Missing required Actions secret(s): " + ", ".join(missing))

    state = load_state()
    for p in state.get("productions", []):
        if (norm(p.get("field")) == norm(field)
                and norm(p.get("topic")) == norm(topic)
                and norm(p.get("language")) == norm(language)):
            print(f"NOTE: '{topic}' ({field}, {language}) is already in "
                  "state.json — refusing to duplicate. Exiting cleanly.")
            return 0

    # Queries: the topic as given (its own language) + field+topic, plus a
    # best-effort English query for the global indexes.
    queries = []
    for q in (topic, f"{field} {topic}"):
        if q not in queries:
            queries.append(q)
    if any(ord(ch) > 127 for ch in topic):
        english_q = translate_query_to_english(f"{field} {topic}", api_key, model)
        if english_q and english_q not in queries:
            queries.append(english_q)
    print(f"Queries: {queries}")

    progress_start(os.environ.get("ARTICLE_CHAT_ID", ""), tg_token)
    candidates = retrieve(field, topic, queries)
    print(f"Candidates collected: {len(candidates)}")
    progress_update(15, "🔎 جست‌وجوی منابع")

    verified = []
    for c in candidates:
        if len(verified) >= MAX_VERIFIED:
            break
        try:
            ok = verify(c)
        except Fail as e:
            print(f"WARNING: verification error for {c['title'][:60]!r}: {e}")
            ok = False
        if ok:
            verified.append(c)
            print(f"  verified [{len(verified)}] {c['title'][:80]}")
    print(f"Verified sources: {len(verified)}")
    progress_update(35, "📚 راستی‌آزمایی منابع")
    if len(verified) < MIN_SOURCES:
        raise Fail(
            f"Only {len(verified)} verifiable sources found for "
            f"'{topic}' (minimum {MIN_SOURCES}). Refusing to write an article "
            "on unverified material. Try a broader or different topic.")

    title, article = write_article(field, topic, language, verified, api_key, model)

    header = (f"📚 Scientific Discovery Agent — new article\n"
              f"Field: {field}\nTopic: {topic}\nLanguage: {language}\n"
              f"Mode: {mode}\nVerified sources: {len(verified)}\n\n{title}")
    message_ids = []
    r = tg_call("sendMessage", tg_token, {"chat_id": chat_id, "text": header})
    message_ids.append(r["message_id"])
    for chunk in split_text(article):
        r = tg_call("sendMessage", tg_token, {"chat_id": chat_id, "text": chunk})
        message_ids.append(r["message_id"])
        time.sleep(0.3)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", topic)[:40].strip("-") or "article"
    r = tg_send_document(tg_token, chat_id, f"article-{safe}.md",
                         article.encode("utf-8"), caption=title)
    message_ids.append(r["message_id"])

    state.setdefault("productions", []).append({
        "field": field, "topic": topic, "language": language, "mode": mode,
        "title": title,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": len(verified),
        "message_ids": message_ids,
    })
    save_state(state)
    progress_update(100, "انتشار در کانال")
    print(f"Done. Article delivered, message_ids={message_ids}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        progress_fail()
        print(f"FAILED (fail-closed): {e}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        progress_fail()
        raise
