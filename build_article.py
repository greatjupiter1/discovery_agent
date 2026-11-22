#!/usr/bin/env python3
"""Scientific Discovery Agent — v1 pipeline (fail-closed).

One production run:
  1. Read state.json; refuse to duplicate an existing (field, topic, language).
  2. Retrieve candidate works from FREE indexes only (OpenAlex, Crossref,
     OpenAIRE, HAL, zbMATH, Europe PMC, Internet Archive, Google Books,
     DOAJ, arXiv, Semantic Scholar, Anna's Archive — no API keys, no
     registration).
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
  LLM_PROVIDER (default "gemini"; "github" selects GitHub Models,
  "glm" selects GLM on z.ai),
  GH_MODELS_MODEL (default "openai/gpt-4o"),
  GLM_MODEL (default "glm-4.7-flash", the free z.ai Flash model)
  GLM_MAX_TOKENS (default 8000, the GLM path's output budget)
Secrets (names only, set in the repo's Actions secrets):
  GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID
  GH_MODELS_TOKEN (optional; the GitHub path falls back to GITHUB_TOKEN)
  GLM_API_KEY (the GLM path's key, from z.ai)
"""

import email.utils
import html as html_lib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Pacing: at least this many seconds between successive calls to one host.
HOST_PACE_SECONDS = 0.5
_HOST_LAST_CALL = {}


def _pace(host):
    """Sleep just enough that calls to the same host stay >=0.5s apart."""
    if not host:
        return
    now = time.monotonic()
    last = _HOST_LAST_CALL.get(host)
    if last is not None:
        wait = HOST_PACE_SECONDS - (now - last)
        if wait > 0:
            time.sleep(wait)
    _HOST_LAST_CALL[host] = time.monotonic()


def _retry_after_seconds(headers):
    """Parse a Retry-After header (seconds or HTTP-date); None if absent
    or unparseable."""
    if headers is None:
        return None
    raw = headers.get("Retry-After")
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        pass
    return None


def http_request(url, method="GET", data=None, headers=None, timeout=READ_TIMEOUT):
    """One logical HTTP request with retries on ALL transient failures.
    Honours Retry-After (capped at 120s) when the server sends one, and
    paces successive calls to the same host ~0.5s apart."""
    last_error = None
    host = urllib.parse.urlparse(url).netloc
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _pace(host)
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", "discovery-agent/1.0 (research; mailto:research@example.org)")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        wait = 30 * attempt
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            if e.code == 429 or 500 <= e.code < 600:
                last_error = Fail(f"HTTP {e.code} from {url.split('?')[0]}: {body[:200]!r}")
                ra = _retry_after_seconds(getattr(e, "headers", None))
                if ra is not None:
                    wait = min(ra, 120)
                    print(f"NOTE: {host} sent Retry-After; sleeping "
                          f"{wait:.0f}s before attempt {attempt + 1}")
            else:
                # A definitive answer (e.g. 404) is not transient: return it.
                return e.code, body
        except Exception as e:  # timeouts, DNS, resets — all transient
            last_error = e
        if attempt < MAX_ATTEMPTS:
            time.sleep(wait)
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
    # mailto puts us in OpenAlex's polite pool (higher, steadier limits).
    url = ("https://api.openalex.org/works?per-page=25&search="
           + urllib.parse.quote(query)
           + "&mailto=" + CROSSREF_MAILTO)
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


CROSSREF_MAILTO = "discoverybot@users.noreply.github.com"


def search_crossref(query):
    out = []
    url = ("https://api.crossref.org/works?rows=25&mailto=" + CROSSREF_MAILTO
           + "&query=" + urllib.parse.quote(query))
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


def search_internet_archive(query):
    """Internet Archive advancedsearch — especially valuable for classical
    Arabic/Persian books that the journal indexes barely cover."""
    out = []
    url = ("https://archive.org/advancedsearch.php?q=" + urllib.parse.quote(query)
           + "&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=creator&fl%5B%5D=date"
           + "&rows=10&page=1&output=json")
    data = http_json(url)
    for doc in (data.get("response") or {}).get("docs", []) or []:
        ident = str(doc.get("identifier") or "").strip()
        if not ident:
            continue
        creator = doc.get("creator") or []
        if isinstance(creator, str):
            creator = [creator]
        date = str(doc.get("date") or "")
        out.append({
            "title": str(doc.get("title") or "").strip(),
            "authors": [str(a).strip() for a in creator if str(a).strip()],
            "year": int(date[:4]) if date[:4].isdigit() else None,
            "venue": "Internet Archive",
            "doi": None,
            "url": f"https://archive.org/details/{ident}",
            "abstract": "",
            "arxiv_id": None,
            "index": "Internet Archive",
        })
    return out


def search_google_books(query):
    out = []
    url = ("https://www.googleapis.com/books/v1/volumes?maxResults=10&q="
           + urllib.parse.quote(query))
    data = http_json(url)
    for item in data.get("items", []) or []:
        info = item.get("volumeInfo") or {}
        published = str(info.get("publishedDate") or "")
        out.append({
            "title": (info.get("title") or "").strip(),
            "authors": [a for a in info.get("authors", []) if a],
            "year": int(published[:4]) if published[:4].isdigit() else None,
            "venue": info.get("publisher") or "Google Books",
            "doi": None,
            "url": info.get("infoLink") or item.get("selfLink") or "",
            "abstract": (info.get("description") or "")[:1500],
            "arxiv_id": None,
            "index": "Google Books",
        })
    return out


def search_doaj(query):
    out = []
    url = ("https://doaj.org/api/search/articles/"
           + urllib.parse.quote(query, safe="") + "?pageSize=10")
    data = http_json(url)
    for r in data.get("results", []) or []:
        bib = r.get("bibjson") or {}
        doi = None
        for ident in bib.get("identifier", []) or []:
            if (ident.get("type") or "").lower() == "doi" and ident.get("id"):
                doi = str(ident["id"]).replace("https://doi.org/", "") or None
                break
        link = ""
        for lnk in bib.get("link", []) or []:
            if lnk.get("url"):
                link = lnk["url"]
                if (lnk.get("type") or "").lower() == "fulltext":
                    break
        year = bib.get("year")
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        out.append({
            "title": (bib.get("title") or "").strip(),
            "authors": [a.get("name", "") for a in bib.get("author", [])
                        if a.get("name")],
            "year": year,
            "venue": (bib.get("journal") or {}).get("title") or "DOAJ",
            "doi": doi,
            "url": link,
            "abstract": bib.get("abstract") or "",
            "arxiv_id": None,
            "index": "DOAJ",
        })
    return out


def _openaire_text(node):
    """OpenAIRE wraps scalars as {"$": value} and repeats elements as
    lists; return the first plain string found."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        v = node.get("$")
        return v if isinstance(v, str) else ""
    if isinstance(node, list):
        for item in node:
            t = _openaire_text(item)
            if t:
                return t
    return ""


def _openaire_texts(node):
    """All plain strings in an OpenAIRE wrapped node (e.g. creators)."""
    if isinstance(node, str):
        return [node] if node.strip() else []
    if isinstance(node, dict):
        v = node.get("$")
        return [v] if isinstance(v, str) and v.strip() else []
    if isinstance(node, list):
        out = []
        for item in node:
            out.extend(_openaire_texts(item))
        return out
    return []


def search_openaire(query):
    """OpenAIRE — the EU's open-access index (no key, no registration).
    Broad coverage of repository-held books and papers that the journal
    indexes miss."""
    out = []
    url = ("https://api.openaire.eu/search/publications?format=json"
           "&size=25&page=1&title=" + urllib.parse.quote(query))
    data = http_json(url)
    results = (((data.get("response") or {}).get("results") or {})
               .get("result")) or []
    if isinstance(results, dict):
        results = [results]
    for r in results:
        md = (((r.get("metadata") or {}).get("oaf:entity") or {})
              .get("oaf:result")) or {}
        if not md:
            continue
        pids = md.get("pid") or []
        if isinstance(pids, dict):
            pids = [pids]
        doi = None
        for pid in pids:
            if (isinstance(pid, dict)
                    and (pid.get("@classid") or "").lower() == "doi"):
                doi = (pid.get("$") or "").replace(
                    "https://doi.org/", "").strip() or None
                if doi:
                    break
        fulltexts = md.get("fulltext") or []
        if isinstance(fulltexts, dict):
            fulltexts = [fulltexts]
        landing = ""
        for ft in fulltexts:
            u = _openaire_text(ft)
            if u.startswith("http"):
                landing = u
                break
        if not landing and doi:
            landing = "https://doi.org/" + doi
        date = _openaire_text(md.get("dateofacceptance"))
        out.append({
            "title": _openaire_text(md.get("title")).strip(),
            "authors": [a.strip() for a in
                        _openaire_texts(md.get("creator")) if a.strip()],
            "year": int(date[:4]) if date[:4].isdigit() else None,
            "venue": _openaire_text(md.get("publisher")).strip() or "OpenAIRE",
            "doi": doi,
            "url": landing,
            "abstract": _openaire_text(md.get("description")).strip(),
            "arxiv_id": None,
            "index": "OpenAIRE",
        })
    return out


def search_europe_pmc(query):
    """Europe PMC — the EBI's life-sciences index (no key, no
    registration); the natural home of PubMed-indexed work."""
    out = []
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
           "?format=json&resultType=core&pageSize=25&query="
           + urllib.parse.quote('TITLE:"' + query.replace('"', "") + '"'))
    data = http_json(url)
    for r in (data.get("resultList") or {}).get("result", []) or []:
        authors = [a.get("fullName", "") for a in
                   ((r.get("authorList") or {}).get("author") or [])
                   if a.get("fullName")]
        if not authors and r.get("authorString"):
            authors = [r["authorString"]]
        year = r.get("pubYear")
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        rid = r.get("id") or ""
        journal = ((r.get("journalInfo") or {}).get("journal") or {}).get(
            "title") or ""
        out.append({
            "title": (r.get("title") or "").strip(),
            "authors": authors,
            "year": year,
            "venue": journal or "Europe PMC",
            "doi": (r.get("doi") or "").strip() or None,
            "url": (f"https://europepmc.org/article/{r.get('source') or 'MED'}"
                    f"/{rid}") if rid else "",
            "abstract": r.get("abstractText") or "",
            "arxiv_id": None,
            "index": "Europe PMC",
        })
    return out


def search_hal(query):
    """HAL — the French national open archive (no key, no registration);
    strong for European scholarship and history of science."""
    out = []
    url = ("https://api.archives-ouvertes.fr/search/?wt=json&rows=25&q="
           + urllib.parse.quote(query)
           + "&fl=title_s,authFullName_s,producedDateY_i,doiId_s,uri_s")
    data = http_json(url)
    for doc in (data.get("response") or {}).get("docs", []) or []:
        titles = doc.get("title_s") or []
        year = doc.get("producedDateY_i")
        out.append({
            "title": (titles[0] if titles else "").strip(),
            "authors": [a for a in doc.get("authFullName_s", []) if a],
            "year": year if isinstance(year, int) else None,
            "venue": "HAL",
            "doi": (doc.get("doiId_s") or "").strip() or None,
            "url": doc.get("uri_s") or "",
            "abstract": "",
            "arxiv_id": None,
            "index": "HAL",
        })
    return out


def search_zbmath(query):
    """zbMATH Open — the mathematics reviewing index (no key, no
    registration); the natural home for mathematics and the history of
    mathematics. (The live route is the singular /document/_search.)"""
    out = []
    url = ("https://api.zbmath.org/v1/document/_search?page=0"
           "&results_per_page=25&search_string="
           + urllib.parse.quote(query))
    data = http_json(url)
    for r in data.get("result") or []:
        title_node = r.get("title") or {}
        if isinstance(title_node, dict):
            title = title_node.get("title") or ""
            subtitle = title_node.get("subtitle")
            if subtitle:
                title = (title + " " + subtitle).strip()
        else:
            title = str(title_node)
        contributors = r.get("contributors") or {}
        authors = [a.get("name", "") for a in
                   contributors.get("authors", []) if a.get("name")]
        if not authors:
            authors = [a.get("name", "") for a in
                       contributors.get("editors", []) if a.get("name")]
        doi = None
        for lnk in r.get("links") or []:
            if ((lnk.get("type") or "").lower() == "doi"
                    and lnk.get("identifier")):
                doi = str(lnk["identifier"]).replace(
                    "https://doi.org/", "") or None
                break
        year = r.get("year")
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        venue = ((r.get("source") or {}).get("source") or "").strip()
        out.append({
            "title": title.strip(),
            "authors": authors,
            "year": year,
            "venue": venue[:200] or "zbMATH",
            "doi": doi,
            "url": r.get("zbmath_url") or "",
            "abstract": "",
            "arxiv_id": None,
            "index": "zbMATH",
        })
    return out


# ---------------------------------------------------------------------------
# Anna's Archive — book index with strong Persian/Arabic coverage (no key).
# FAIL-SOFT BY LAW: domains rotate and mirrors come and go, so this source
# must never fail or stall a run — total mirror failure logs one line and
# returns []. Metadata candidates only; NO file downloading here.
# ---------------------------------------------------------------------------

ANNAS_ARCHIVE_MIRRORS = ["annas-archive.org", "annas-archive.li",
                         "annas-archive.se", "annas-archive.gl",
                         "annas-archive.pm"]
AA_MAX_RESULTS = 25
AA_TIMEOUT = 30

_AA_TITLE_RE = re.compile(
    r'<a\b([^>]*href="/md5/([0-9a-f]{32})"[^>]*)>(.*?)</a>', re.S)
_AA_SEARCH_LINK_RE = re.compile(
    r'<a\b[^>]*href="/search\?q=[^"]*"[^>]*>(.*?)</a>', re.S)
_AA_BOTTOM_ROW_RE = re.compile(
    r'<div class="[^"]*font-semibold text-sm[^"]*">(.*?)</div>', re.S)
_AA_DOI_RE = re.compile(r'10\.\d{4,9}/[^\s"\'<>\]]+')
_AA_YEAR_RE = re.compile(r'^(1\d\d\d|20\d\d)$')
_AA_MONO_RE = re.compile(r'<div class="[^"]*font-mono[^"]*">(.*?)</div>',
                         re.S)
_AA_FILE_EXT_RE = re.compile(r'\.(pdf|epub|djvu|mobi|azw3|zip)$', re.I)


def _aa_strip_tags(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def _aa_get(url):
    """One bounded fetch for Anna's Archive: 30s read timeout, at most 2
    attempts, so a sick mirror fails over fast instead of eating the
    run's time budget the way the full retry law would."""
    for attempt in range(2):
        try:
            req = urllib.request.Request(url)
            req.add_header(
                "User-Agent",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
            with urllib.request.urlopen(req, timeout=AA_TIMEOUT) as resp:
                if resp.status == 200:
                    return resp.read().decode("utf-8", "replace")
                return None
        except Exception:
            if attempt == 0:
                time.sleep(3)
    return None


def _parse_annas_archive(page, domain):
    """Parse AA search-results HTML (list mode) into candidates. Rows are
    anchored on the title link (class js-vim-focus, href /md5/<hash>);
    author and publisher/year live in the /search?q= links that follow
    it, marked by mdi--user-edit / mdi--company icons; the bottom row is
    a '·'-separated metadata string that may carry the year. Rows served
    inside HTML comments (AA's lazy-render mode) are found too, because
    the scan runs over the raw page text."""
    out, seen_md5 = [], set()
    matches = [m for m in _AA_TITLE_RE.finditer(page)
               if "js-vim-focus" in m.group(1)]
    for i, m in enumerate(matches):
        md5 = m.group(2)
        if md5 in seen_md5:
            continue
        seen_md5.add(md5)
        title = _aa_strip_tags(m.group(3))
        if not title:
            continue
        # The row's own segment ends at the next row's furniture (its
        # title link, filename line or row container), so metadata from
        # neighbouring rows can never leak into this one.
        cut = min(len(page), m.end() + 6000)
        for marker in ("js-vim-focus", "font-mono", "pt-3 pb-3"):
            j = page.find(marker, m.end())
            if j != -1:
                cut = min(cut, j)
        segment = page[m.end():cut]
        authors, venue, year = [], "", None
        for link in _AA_SEARCH_LINK_RE.finditer(segment):
            inner = link.group(1)
            text = _aa_strip_tags(inner)
            if not text:
                continue
            if "mdi--user-edit" in inner:
                if not authors:
                    authors = [text]
            elif "mdi--company" in inner:
                ym = re.search(r'(1\d\d\d|20\d\d)\s*$', text)
                if ym:
                    year = int(ym.group(1))
                    venue = text[:ym.start()].rstrip(", ").strip()
                else:
                    venue = text
        bottom_text = ""
        bottom = _AA_BOTTOM_ROW_RE.search(segment)
        if bottom:
            bottom_text = _aa_strip_tags(bottom.group(1))
            if year is None:
                for token in bottom_text.split("·"):
                    token = token.strip()
                    if _AA_YEAR_RE.match(token):
                        year = int(token)
                        break
        # A DOI counts only when THIS row exposes it: in the row's own
        # segment, or in its filename line (printed just BEFORE the
        # title link, so it sits in the text since the previous row's
        # title link — take the last font-mono div there).
        doi = None
        dm = _AA_DOI_RE.search(_aa_strip_tags(segment))
        if not dm:
            prev_end = matches[i - 1].end() if i else 0
            mono = list(_AA_MONO_RE.finditer(page[prev_end:m.start()]))
            if mono:
                dm = _AA_DOI_RE.search(_aa_strip_tags(mono[-1].group(1)))
        if dm:
            doi = _AA_FILE_EXT_RE.sub("", dm.group(0).rstrip(".,;)")) or None
        ext = ""
        for token in bottom_text.split("·"):
            t = token.strip().lower()
            if t in ("pdf", "epub", "djvu", "mobi", "azw3", "fb2", "txt",
                     "rtf", "doc", "docx", "chm"):
                ext = t
                break
        out.append({
            "title": title,
            "authors": authors,
            "year": year,
            "venue": venue or "Anna's Archive",
            "doi": doi,
            "url": f"https://{domain}/md5/{md5}",
            "abstract": "",
            "arxiv_id": None,
            "index": "annas_archive",
            # Internal fields for the LibGen bridge — never cited.
            "md5": md5,
            "ext": ext,
        })
        if len(out) >= AA_MAX_RESULTS:
            break
    return out


def search_annas_archive(query):
    """Try each mirror in order; the first that serves a real results
    page wins. All mirrors down/blocked -> log and return [] (the run
    continues without this source)."""
    for domain in ANNAS_ARCHIVE_MIRRORS:
        try:
            page = _aa_get(f"https://{domain}/search?q="
                           + urllib.parse.quote(query))
        except Exception:
            page = None
        if not page:
            continue
        if "js-aarecord-list-outer" not in page and "/md5/" not in page:
            continue  # parked or block page — not the real index
        return _parse_annas_archive(page, domain)
    print("annas_archive: unreachable (all mirrors failed)")
    return []


# --------------------------------------------------------------------------
# Source registries (§17) — credibility / whitelist SIGNAL only.
# Four curated registries sit beside this script in the repo:
#   sources_registry.json        (registry 1, registry schema)
#   sources_registry_500_more.json (500 DOAJ journals, compact schema:
#                                   {id, name, url, type, fields,
#                                    languages, country, checked,
#                                    http_status} — no verified/api)
#   sources_registry_2.json      (registry 2, registry schema)
#   thesis_registry.json         (thesis-HOSTING sources, registry
#                                   schema — repositories and ETD
#                                   archives, not individual theses)
# Both schemas normalise into one record {name, url, domain, type,
# fields, languages, registry}. A registry match is a ranking SIGNAL
# ONLY: it never verifies a source (the DOI / Crossref / publisher
# verification law is unchanged) and never counts toward the source
# bar. Loading is fail-soft: a missing or unreadable registry file
# logs one line and the run continues with the rest.
# --------------------------------------------------------------------------

REGISTRY_FILES = ["sources_registry.json",
                  "sources_registry_500_more.json",
                  "sources_registry_2.json",
                  "thesis_registry.json"]
_REGISTRY_DIR = os.path.dirname(os.path.abspath(__file__))


def _registry_norm_url(url):
    """Normalised URL key for dedupe: lowercase host, leading www.
    stripped, default ports (80/443) stripped, trailing slash
    stripped from the path."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urllib.parse.urlsplit(raw)
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        port = parts.port
    except ValueError:
        return raw.lower().rstrip("/")
    if not host:
        return raw.lower().rstrip("/")
    netloc = host
    if port and port not in (80, 443):
        netloc = f"{host}:{port}"
    path = (parts.path or "").rstrip("/")
    out = netloc + path
    if parts.query:
        out += "?" + parts.query
    return out


def _registry_domain(url):
    """Normalised domain (host) of a URL, or '' when unparsable."""
    key = _registry_norm_url(url)
    return key.split("/")[0].split("?")[0] if key else ""


def load_source_registry(base_dir=None):
    """Load all registry files; returns (domain_index, url_index,
    stats). domain_index / url_index map the normalised domain / URL
    to the normalised record (first record wins). Fail-soft per
    file: a missing or unreadable registry is logged and skipped."""
    base = base_dir or _REGISTRY_DIR
    domain_index, url_index = {}, {}
    stats = {"loaded": 0, "unique_urls": 0, "unique_domains": 0,
             "duplicates": 0, "files_ok": 0}
    for fname in REGISTRY_FILES:
        path = os.path.join(base, fname)
        try:
            with open(path, encoding="utf-8") as f:
                entries = json.load(f)
            if not isinstance(entries, list):
                raise ValueError("registry root is not a list")
        except Exception as e:
            print(f"registry: {fname} unavailable ({e}) — continuing "
                  "without it")
            continue
        stats["files_ok"] += 1
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = (entry.get("url") or "").strip()
            if not url:
                continue
            stats["loaded"] += 1
            record = {
                "name": str(entry.get("name") or "").strip(),
                "url": url,
                "domain": _registry_domain(url),
                "type": str(entry.get("type") or "").strip(),
                "fields": [str(x) for x in
                           (entry.get("fields") or [])
                           if x is not None],
                "languages": [str(x) for x in
                              (entry.get("languages") or [])
                              if x is not None],
                "registry": fname,
            }
            key = _registry_norm_url(url)
            if key in url_index:
                stats["duplicates"] += 1
                continue
            url_index[key] = record
            if record["domain"] and \
                    record["domain"] not in domain_index:
                domain_index[record["domain"]] = record
    stats["unique_urls"] = len(url_index)
    stats["unique_domains"] = len(domain_index)
    print(f"registry: loaded {stats['loaded']} entries from "
          f"{stats['files_ok']} files, {stats['unique_urls']} unique "
          f"URLs, {stats['unique_domains']} unique domains, "
          f"{stats['duplicates']} duplicate URLs dropped")
    return domain_index, url_index, stats


_REGISTRY_CACHE = None


def get_registry():
    """Load the registries once per run (fail-soft) and cache them."""
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = load_source_registry()
    return _REGISTRY_CACHE


def registry_match_for(candidate):
    """The registry record matching a candidate, or None. Exact
    normalised-URL match first, then domain match, over the
    candidate's landing-page URLs. doi.org links are skipped (the
    DOI resolver domain says nothing about the publisher)."""
    if not isinstance(candidate, dict):
        return None
    domain_index, url_index, _stats = get_registry()
    urls = []
    for key in ("url", "landing_url", "landing_page_url",
                "publisher_url"):
        u = candidate.get(key)
        if u and u not in urls:
            urls.append(u)
    for u in urls:
        rec = url_index.get(_registry_norm_url(u))
        if rec:
            return rec
    for u in urls:
        dom = _registry_domain(u)
        if dom and dom not in ("doi.org", "dx.doi.org"):
            rec = domain_index.get(dom)
            if rec:
                return rec
    return None


def registry_tag(candidate):
    """Tag a candidate in place with its registry match (display
    string) and return the record or None. Signal only — this never
    touches verification or counting."""
    rec = registry_match_for(candidate)
    if rec:
        candidate["registry_match"] = (
            f"{rec['name']} ({rec['type']}, {rec['registry']})")
    return rec



SEARCHERS = [search_openalex, search_crossref, search_openaire,
             search_hal, search_zbmath, search_europe_pmc,
             search_internet_archive, search_google_books,
             search_annas_archive, search_doaj, search_arxiv,
             search_semantic_scholar]


def normalise_title(t):
    return re.sub(r"[^a-z0-9\u0600-\u06FF]+", " ", (t or "").lower()).strip()


def _counts_line(label, found, kept):
    parts = []
    for name in sorted(set(found) | set(kept)):
        parts.append(f"{name}: found={found.get(name, 0)} kept={kept.get(name, 0)}")
    return f"{label} " + ("; ".join(parts) if parts else "(none)")


# --------------------------------------------------------------------------
# Speed pass: parallel retrieval waves + parallel verification.
# - One wave = one query x all active sources, run concurrently; results
#   merge deterministically (SEARCHERS order, then the source's own
#   ranking), so outputs stay reproducible.
# - Per-source time budget: a source whose cumulative in-run time passes
#   SOURCE_TIME_BUDGET is benched for the rest of the run.
# - Early stop: once the verified pool reaches EARLY_STOP_VERIFIED, no
#   new waves are launched.
# --------------------------------------------------------------------------

RETRIEVAL_WORKERS = 8
VERIFY_WORKERS = 10
SOURCE_TIME_BUDGET = 90  # seconds per source per run
EARLY_STOP_VERIFIED = 15
SEARCHER_LABEL = {
    "search_openalex": "OpenAlex", "search_crossref": "Crossref",
    "search_openaire": "OpenAIRE", "search_hal": "HAL",
    "search_zbmath": "zbMATH", "search_europe_pmc": "Europe PMC",
    "search_internet_archive": "Internet Archive",
    "search_google_books": "Google Books",
    "search_annas_archive": "annas_archive", "search_doaj": "DOAJ",
    "search_arxiv": "arXiv", "search_semantic_scholar": "Semantic Scholar",
}

VERIFY_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "verify_cache.json")
VERIFY_CACHE_MAX_AGE_DAYS = 180


def load_verify_cache():
    try:
        with open(VERIFY_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_verify_cache(cache):
    try:
        with open(VERIFY_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    except OSError as e:
        print(f"WARNING: verify cache save failed: {e}")


def _cache_lookup(cache, doi):
    """Fresh Crossref-confirmed cache entry for this DOI, or None.
    Entries older than VERIFY_CACHE_MAX_AGE_DAYS count as absent (they
    are re-verified live)."""
    if not doi:
        return None
    entry = cache.get(doi.lower())
    if not entry:
        return None
    try:
        when = datetime.strptime(entry["verified_at"],
                                 "%Y-%m-%d").date()
    except Exception:
        return None
    age = (datetime.now(timezone.utc).date() - when).days
    return entry if age <= VERIFY_CACHE_MAX_AGE_DAYS else None


def _run_searcher(searcher, query):
    t0 = time.monotonic()
    try:
        return searcher(query), time.monotonic() - t0, None
    except Fail as e:
        return [], time.monotonic() - t0, e
    except Exception as e:  # a searcher must never kill the wave
        return [], time.monotonic() - t0, Fail(str(e))


def _verify_one(candidate, cache):
    """(ok, via) for one candidate: fresh cache hit first (no network),
    else the live verification law. Crossref confirmations are written
    back into the cache."""
    doi = candidate.get("doi")
    if doi and _cache_lookup(cache, doi):
        return True, "cache"
    try:
        ok, via = verify_detailed(candidate)
    except Fail as e:
        print(f"WARNING: verification error for "
              f"{candidate['title'][:60]!r}: {e}")
        return False, None
    if ok and via == "crossref" and doi:
        cache[doi.lower()] = {
            "title": candidate.get("title", ""),
            "authors": candidate.get("authors", []),
            "year": candidate.get("year"),
            "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    return ok, via


def _new_retrieval_state():
    """Shared retrieval machinery state for collect_verified(): the
    dedupe set, the per-source time budgets / bench set, the verify
    cache and the cumulative candidate count. Passing the SAME state
    to a second call (wave 2, §16.2) makes that call respect wave 1's
    bench decisions and never re-fetch a wave-1 candidate."""
    return {"seen": set(), "source_time": {}, "benched": set(),
            "cache": None, "total_candidates": 0}


def collect_verified(queries, state=None, searcher_order=None):
    """Waves of parallel retrieval + parallel verification until the
    pool is full (MAX_VERIFIED), the early-stop bar is reached, the
    candidate cap is hit, or the queries run out. `state` (see
    _new_retrieval_state) shares the dedupe/bench/cache machinery
    across calls; `searcher_order` overrides the deterministic merge
    priority (wave 2 puts the book sources first)."""
    if state is None:
        state = _new_retrieval_state()
    seen = state["seen"]
    source_time, benched = state["source_time"], state["benched"]
    if state.get("cache") is None:
        state["cache"] = load_verify_cache()
    cache = state["cache"]
    order = list(searcher_order) if searcher_order else list(SEARCHERS)
    verified, verified_by = [], {}
    found_by, kept_by = {}, {}
    total_candidates = state["total_candidates"]
    retrieval_t = verification_t = 0.0
    for query in queries:
        if len(verified) >= EARLY_STOP_VERIFIED:
            break
        if total_candidates >= MAX_CANDIDATES:
            break
        active = [s for s in order if s.__name__ not in benched]
        wave_results = {}
        t0 = time.monotonic()
        if active:
            with ThreadPoolExecutor(max_workers=RETRIEVAL_WORKERS) as ex:
                fut_map = {ex.submit(_run_searcher, s, query): s
                           for s in active}
                for fut in as_completed(fut_map):
                    s = fut_map[fut]
                    res, dt, err = fut.result()
                    wave_results[s.__name__] = (res, err)
                    source_time[s.__name__] = \
                        source_time.get(s.__name__, 0.0) + dt
                    if (source_time[s.__name__] > SOURCE_TIME_BUDGET
                            and s.__name__ not in benched):
                        benched.add(s.__name__)
                        print(f"source benched: "
                              f"{SEARCHER_LABEL.get(s.__name__, s.__name__)}")
        retrieval_t += time.monotonic() - t0
        wave_candidates = []
        for s in order:  # deterministic merge: source priority order
            if s.__name__ not in wave_results:
                continue
            res, err = wave_results[s.__name__]
            if err is not None:
                print(f"WARNING: {s.__name__} failed for query "
                      f"{query!r}: {err}")
                continue
            for c in res:
                idx = c.get("index") or s.__name__
                found_by[idx] = found_by.get(idx, 0) + 1
                if not c["title"]:
                    continue
                key = (c["doi"].lower() if c["doi"] else None) \
                    or normalise_title(c["title"])
                if key in seen:
                    continue
                seen.add(key)
                kept_by[idx] = kept_by.get(idx, 0) + 1
                wave_candidates.append(c)
                total_candidates += 1
                if total_candidates >= MAX_CANDIDATES:
                    break
            if total_candidates >= MAX_CANDIDATES:
                break
        t0 = time.monotonic()
        if wave_candidates:
            with ThreadPoolExecutor(max_workers=VERIFY_WORKERS) as ex:
                outcomes = list(ex.map(
                    lambda c: _verify_one(c, cache), wave_candidates))
            for c, (ok, _via) in zip(wave_candidates, outcomes):
                if not ok:
                    continue
                if len(verified) >= MAX_VERIFIED:
                    break
                verified.append(c)
                idx = c.get("index") or "?"
                verified_by[idx] = verified_by.get(idx, 0) + 1
                print(f"  verified [{len(verified)}] {c['title'][:80]}")
        verification_t += time.monotonic() - t0
    state["total_candidates"] = total_candidates
    save_verify_cache(cache)
    print(_counts_line("Candidates by source:", found_by, kept_by))
    print(f"Candidates collected: {total_candidates}")
    print(f"Verified sources: {len(verified)}")
    print("Verified by source: " + ("; ".join(
        f"{name}={n}" for name, n in sorted(verified_by.items())) or "(none)"))
    print(f"timing: retrieval {retrieval_t:.1f}s")
    print(f"timing: verification {verification_t:.1f}s")
    return verified


# --------------------------------------------------------------------------
# Verification law
# --------------------------------------------------------------------------

def verify_detailed(candidate):
    """(ok, via): a reference survives ONLY with independent proof it
    exists — its DOI resolving through Crossref directly (one call, no
    doi.org redirect chain), its arXiv page existing, or its landing
    page answering 2xx/3xx. via names the proof that worked."""
    doi = candidate.get("doi")
    if doi:
        status, _ = http_request(
            "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
            + "?mailto=" + CROSSREF_MAILTO,
            timeout=60)
        if status == 200:
            return True, "crossref"
        # DOI present but Crossref has no record: fall through to page check.
    if candidate.get("arxiv_id"):
        if page_exists(f"https://arxiv.org/abs/{candidate['arxiv_id']}"):
            return True, "arxiv"
    url = candidate.get("url") or ""
    if url.startswith("http") and "openalex.org" not in url:
        if page_exists(url):
            return True, "page"
    return False, None


def verify(candidate):
    return verify_detailed(candidate)[0]


# --------------------------------------------------------------------------
# Abstract enrichment (§16.1) — after verification, BEFORE the content
# gate: a verified record whose index gave no usable abstract gets
# another chance BY DOI, from OpenAlex, then Europe PMC, then
# Semantic Scholar. DOI-based only (no-DOI records are skipped and
# logged in the summary). Fail-soft per source and per record; a host
# that fails ENRICH_BENCH_LIMIT times in a run is benched for the rest
# of the enrichment stage. Lookups run in parallel across records,
# through the existing http helper with its retry law.
# --------------------------------------------------------------------------

ENRICH_WORKERS = 8
ENRICH_BENCH_LIMIT = 3
_ENRICH_FAILURES = {}


def _enrich_benched(label):
    return _ENRICH_FAILURES.get(label, 0) >= ENRICH_BENCH_LIMIT


def _enrich_note(label, ok):
    if ok:
        _ENRICH_FAILURES[label] = 0
    else:
        _ENRICH_FAILURES[label] = _ENRICH_FAILURES.get(label, 0) + 1


def _abstract_via_openalex(doi):
    url = ("https://api.openalex.org/works/doi:"
           + urllib.parse.quote(doi, safe="/")
           + "?mailto=" + CROSSREF_MAILTO)
    data = http_json(url)
    return reconstruct_abstract(data.get("abstract_inverted_index"))


def _abstract_via_europepmc(doi):
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
           "?format=json&resultType=core&pageSize=5&query="
           + urllib.parse.quote(f'DOI:"{doi}"'))
    data = http_json(url)
    for r in (data.get("resultList") or {}).get("result", []) or []:
        if (r.get("doi") or "").strip().lower() == doi.lower():
            return r.get("abstractText") or ""
    return ""


def _abstract_via_semanticscholar(doi):
    url = ("https://api.semanticscholar.org/graph/v1/paper/DOI:"
           + urllib.parse.quote(doi, safe="/") + "?fields=abstract")
    data = http_json(url)
    return data.get("abstract") or ""


_ENRICHERS = (("openalex", _abstract_via_openalex),
              ("europepmc", _abstract_via_europepmc),
              ("semanticscholar", _abstract_via_semanticscholar))


def _enrich_one(candidate):
    """Fill candidate['abstract'] from the first enricher returning a
    usable abstract; returns that enricher's label, or None. Never
    raises."""
    doi = (candidate.get("doi") or "").strip()
    if not doi:
        return None
    for label, fn in _ENRICHERS:
        if _enrich_benched(label):
            continue
        try:
            abstract = (fn(doi) or "").strip()
        except Exception:
            _enrich_note(label, False)
            continue
        _enrich_note(label, True)
        if len(abstract) >= CONTENT_MIN_ABSTRACT_CHARS:
            candidate["abstract"] = abstract
            return label
    return None


def enrich_abstracts(candidates):
    """Fill missing abstracts by DOI, in parallel across records.
    Returns the counts dict and logs the summary line."""
    _t0 = time.monotonic()
    targets, skipped = [], 0
    for c in candidates:
        if len((c.get("abstract") or "").strip()) \
                >= CONTENT_MIN_ABSTRACT_CHARS:
            continue
        if (c.get("doi") or "").strip():
            targets.append(c)
        else:
            skipped += 1
    counts = {"filled": 0, "openalex": 0, "europepmc": 0,
              "semanticscholar": 0, "skipped_no_doi": skipped}
    if targets:
        with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as ex:
            labels = list(ex.map(_enrich_one, targets))
        for label in labels:
            if label:
                counts["filled"] += 1
                counts[label] += 1
    print(f"enrichment: filled {counts['filled']} abstracts "
          f"(openalex {counts['openalex']}, "
          f"europepmc {counts['europepmc']}, "
          f"semanticscholar {counts['semanticscholar']})")
    print(f"timing: enrichment {time.monotonic() - _t0:.1f}s")
    return counts


# --------------------------------------------------------------------------
# Full-text bridge — grounding excerpts for VERIFIED books only.
# Route order (first success wins, every step fail-soft):
#   (a) Internet Archive ready-extracted text (<id>_djvu.txt) — a plain
#       text file, never the book itself;
#   (b) LibGen download — ONLY into a temp file in the runner workspace,
#       extracted, then deleted. Book files are NEVER stored or
#       committed anywhere; the only thing kept is the capped extracted
#       text, cached under fulltext/<md5-or-ia-id>.txt (committed back).
# --------------------------------------------------------------------------

FULLTEXT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fulltext")
FULLTEXT_MAX_BOOKS = 3
FULLTEXT_EXCERPT_CHARS = 120_000
FULLTEXT_CARD_CHARS = 12_000
FULLTEXT_CACHE_BYTES = 200 * 1024
LIBGEN_MAX_BYTES = 60 * 1024 * 1024
IA_TEXT_MAX_BYTES = 2 * 1024 * 1024
LIBGEN_PAGE_MIRRORS = ["library.lol"]
LIBGEN_GET_MIRRORS = ["libgen.is", "libgen.rs", "libgen.st", "libgen.gs"]
_LG_HREF_RE = re.compile(r'href="([^"]+)"')


def _fetch_capped(url, cap, timeout=60):
    """GET up to cap bytes; None on any failure. Never raises."""
    try:
        req = urllib.request.Request(url)
        req.add_header(
            "User-Agent",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return resp.read(cap + 1)[:cap]
    except Exception:
        return None


def _download_capped(url, path, cap, timeout=60):
    """Stream url to path, aborting past cap. (ok, reason); never raises."""
    try:
        req = urllib.request.Request(url)
        req.add_header(
            "User-Agent",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
        total = 0
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            with open(path, "wb") as f:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > cap:
                        return False, f"over size cap ({cap} bytes)"
                    f.write(chunk)
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:120]


def _cache_file(key):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(key))[:120]
    return os.path.join(FULLTEXT_DIR, safe + ".txt")


def _read_cache(key):
    try:
        p = _cache_file(key)
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                t = f.read()
            if len(t.strip()) >= 200:
                return t
    except OSError:
        pass
    return None


def _write_cache(key, text):
    try:
        os.makedirs(FULLTEXT_DIR, exist_ok=True)
        data = text.encode("utf-8")
        if len(data) > FULLTEXT_CACHE_BYTES:
            text = data[:FULLTEXT_CACHE_BYTES].decode("utf-8", "ignore")
        with open(_cache_file(key), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        print(f"fulltext: cache write failed: {e}")


def _ia_id_from_url(url):
    m = re.search(r"archive\.org/details/([^/?#]+)", url or "")
    return m.group(1) if m else None


def _ia_find_by_title(title):
    """Strong-title-match lookup on Internet Archive (mediatype:texts)."""
    try:
        q = ("title:(" + title.replace(")", " ") + ")"
             " AND mediatype:texts")
        url = ("https://archive.org/advancedsearch.php?q="
               + urllib.parse.quote(q)
               + "&fl%5B%5D=identifier&fl%5B%5D=title&rows=5&output=json")
        data = http_json(url)
        want = normalise_title(title)
        for doc in (data.get("response") or {}).get("docs", []) or []:
            if (normalise_title(doc.get("title", "")) == want
                    and doc.get("identifier")):
                return doc["identifier"]
    except Exception as e:
        print(f"fulltext: IA title lookup failed: {e}"[:140])
    return None


def _ia_djvu_text(ia_id):
    """The item's ready-extracted full text, or None (a miss — e.g.
    lending-restricted items — is never an error). The real file name
    comes from the metadata API; it is not always <id>_djvu.txt."""
    fname = None
    try:
        meta = http_json("https://archive.org/metadata/"
                         + urllib.parse.quote(ia_id, safe=""))
        for f in meta.get("files", []) or []:
            if (f.get("name") or "").endswith("_djvu.txt"):
                fname = f["name"]
                break
    except Exception:
        fname = None
    if not fname:
        fname = ia_id + "_djvu.txt"
    url = ("https://archive.org/download/"
           + urllib.parse.quote(ia_id, safe="") + "/"
           + urllib.parse.quote(fname))
    data = _fetch_capped(url, IA_TEXT_MAX_BYTES, timeout=60)
    if not data:
        return None
    text = data.decode("utf-8", "replace")
    if len(text.strip()) < 200 or "<html" in text[:300].lower():
        return None
    return text


def _extract_epub(path):
    import zipfile
    texts = []
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if n.lower().endswith((".xhtml", ".html", ".htm")):
                raw = z.read(n).decode("utf-8", "replace")
                t = _aa_strip_tags(raw)
                if t:
                    texts.append(t)
    return "\n".join(texts)


def _extract_pdf(path, md5):
    exe = shutil.which("pdftotext")
    if not exe:
        print(f"libgen: {md5} pdftotext not available; PDF skipped")
        return None
    try:
        r = subprocess.run([exe, "-q", path, "-"], capture_output=True,
                           timeout=180)
    except Exception as e:
        print(f"libgen: {md5} pdftotext failed: {e}"[:140])
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace")


def _extract_book_text(path, ext, md5):
    with open(path, "rb") as f:
        magic = f.read(12)
    if magic.startswith(b"%PDF"):
        return _extract_pdf(path, md5)
    if magic.startswith(b"PK"):
        try:
            return _extract_epub(path)
        except Exception as e:
            print(f"libgen: {md5} epub extraction failed: {e}"[:140])
            return None
    if ext == "djvu" or magic[8:12] == b"DJVU":
        print(f"libgen: {md5} djvu — extraction skipped")
        return None
    print(f"libgen: {md5} unrecognised file type — extraction skipped")
    return None


def _libgen_get_link(md5):
    """The GET (download) link from the library.lol record page."""
    for mirror in LIBGEN_PAGE_MIRRORS:
        page = _aa_get(f"https://{mirror}/main/{md5}")
        if not page:
            continue
        for href in _LG_HREF_RE.findall(page):
            if "get.php?md5=" in href or "/ipfs/" in href:
                return href
    return None


def _libgen_text(md5, ext):
    """Download via LibGen into a TEMP file, extract, delete the file.
    Returns the text or None; every failure is logged and soft."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="libgen-", suffix=".book")
        os.close(fd)
        ok, reason = False, "no GET link found"
        link = _libgen_get_link(md5)
        if link:
            ok, reason = _download_capped(link, tmp, LIBGEN_MAX_BYTES)
        if not ok:
            for mirror in LIBGEN_GET_MIRRORS:
                ok, reason = _download_capped(
                    f"https://{mirror}/get.php?md5={md5}", tmp,
                    LIBGEN_MAX_BYTES)
                if ok:
                    break
        if not ok:
            print(f"libgen: {md5} download failed ({reason})")
            return None
        text = _extract_book_text(tmp, ext, md5)
        if not text or len(text.strip()) < 200:
            print(f"libgen: {md5} no usable text extracted")
            return None
        return text
    except Exception as e:
        print(f"libgen: {md5} {type(e).__name__}: {e}"[:160])
        return None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


FULLTEXT_WORKERS = 4


def _attach_fulltext_one(s):
    """Fetch one book's grounding excerpt (committed cache -> IA
    ready text -> LibGen on the runner). Sets s['fulltext_excerpt']
    and returns (fetched, from_cache). Fail-soft: never raises."""
    try:
        md5 = s.get("md5") or None
        ia_id = (_ia_id_from_url(s.get("url", ""))
                 if s.get("index") == "Internet Archive" else None)
        text = route = None
        from_cache = False
        for key in (md5, ia_id):
            if key and text is None:
                t = _read_cache(key)
                if t:
                    text, route, from_cache = t, "cache", True
        if text is None:
            if ia_id is None:
                ia_id = _ia_find_by_title(s["title"])
            if ia_id:
                t = _read_cache(ia_id)
                if t:
                    text, route, from_cache = t, "cache", True
                else:
                    t = _ia_djvu_text(ia_id)
                    if t:
                        text, route = t, "ia-text"
        if text is None and md5:
            t = _libgen_text(md5, s.get("ext") or "")
            if t:
                text, route = t, "libgen"
        if text:
            if not from_cache:
                _write_cache(md5 or ia_id, text)
            s["fulltext_excerpt"] = text[:FULLTEXT_EXCERPT_CHARS]
            print(f"fulltext route: {route}")
            return True, from_cache
        print(f"fulltext: no text for {s['title'][:60]!r}")
        return False, False
    except Exception as e:
        print(f"fulltext: failed for {(s.get('title') or '')[:60]!r}: "
              f"{e}")
        return False, False


def attach_fulltexts(verified):
    """Attach a grounding excerpt to the top verified BOOK records.
    Fail-soft throughout: if nothing can be fetched, the records are
    exactly as before. Only extracted text is ever cached/committed.
    The eligible books are fetched CONCURRENTLY (§16.5,
    FULLTEXT_WORKERS); ex.map keeps the outcome order aligned with
    the selection order, so the results match the old serial walk."""
    selected = []
    for s in verified:
        md5 = s.get("md5") or None
        ia_id = (_ia_id_from_url(s.get("url", ""))
                 if s.get("index") == "Internet Archive" else None)
        if not md5 and not ia_id:
            continue
        ext = s.get("ext") or ""
        if ext and ext not in ("pdf", "epub", "djvu"):
            continue  # known non-book format (e.g. azw3)
        selected.append(s)
        if len(selected) >= FULLTEXT_MAX_BOOKS:
            break
    _t0 = time.monotonic()
    fetched = cached = 0
    if selected:
        with ThreadPoolExecutor(max_workers=FULLTEXT_WORKERS) as ex:
            outcomes = list(ex.map(_attach_fulltext_one, selected))
        for got, was_cached in outcomes:
            if got:
                fetched += 1
                if was_cached:
                    cached += 1
    print(f"libgen: fetched {fetched}/{len(selected)} books "
          f"({cached} from cache)")
    print(f"timing: full-text bridge {time.monotonic() - _t0:.1f}s")


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


# --------------------------------------------------------------------------
# LLM providers — Gemini (default), GitHub Models or GLM / z.ai (both
# OpenAI-compatible), selected by env LLM_PROVIDER. EVERY model call
# goes through call_llm.
# --------------------------------------------------------------------------

GH_MODELS_URL = "https://models.github.ai/inference/chat/completions"
GH_MODELS_DEFAULT_MODEL = "openai/gpt-4o"
GH_MODELS_MAX_TOKENS = 4000

GLM_URL = "https://api.z.ai/api/paas/v4/chat/completions"
GLM_DEFAULT_MODEL = "glm-4.7-flash"
GLM_DEFAULT_MAX_TOKENS = 8000


def llm_provider():
    return (os.environ.get("LLM_PROVIDER") or "gemini").strip().lower()


def _brain_model(name):
    """The model brain `name` calls with: its own env override, else
    its default. Each brain resolves its OWN model, so a backup brain
    never receives the primary brain's model name."""
    if name == "glm":
        return (os.environ.get("GLM_MODEL")
                or GLM_DEFAULT_MODEL).strip()
    if name == "github":
        return (os.environ.get("GH_MODELS_MODEL")
                or GH_MODELS_DEFAULT_MODEL).strip()
    return (os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash").strip()


def llm_model():
    return _brain_model(llm_provider())


def _github_models_once(prompt, temperature=0.4, json_mode=False):
    """One single GitHub Models chat completion attempt (no retries —
    call_llm's brain chain owns retrying and failover). Token:
    GH_MODELS_TOKEN if set, else GITHUB_TOKEN. A truncated reply
    (finish_reason "length") raises Fail like any other failed
    attempt: truncated text never silently enters an article."""
    token = (os.environ.get("GH_MODELS_TOKEN") or "").strip() \
        or (os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        raise Fail("GitHub Models brain has no model token available: "
                   "set the GH_MODELS_TOKEN repo secret or pass GITHUB_TOKEN.")
    body = json.dumps({
        "model": _brain_model("github"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": GH_MODELS_MAX_TOKENS,
        **({"response_format": {"type": "json_object"}} if json_mode else {}),
    }).encode("utf-8")
    status, raw = http_request(GH_MODELS_URL, method="POST", data=body,
                               headers={
                                   "Content-Type": "application/json",
                                   "Authorization": f"Bearer {token}"})
    if status != 200:
        raise Fail(f"GitHub Models HTTP {status}: {raw[:300]!r}")
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise Fail(f"GitHub Models returned a non-JSON body "
                   f"(HTTP {status}): {raw[:300]!r}")
    try:
        choice = data["choices"][0]
        text = (choice["message"]["content"] or "").strip()
        finish = choice.get("finish_reason")
    except Exception:
        raise Fail(f"GitHub Models returned an unexpected shape: {raw[:300]!r}")
    if finish == "length":
        raise Fail(f"GitHub Models reply truncated at "
                   f"{GH_MODELS_MAX_TOKENS} tokens (finish_reason=length)")
    if not text:
        raise Fail("GitHub Models returned empty text")
    return text


def glm_max_tokens():
    """The GLM path's output budget: env GLM_MAX_TOKENS overrides the
    8000-token default (an invalid value falls back to the default).
    glm-4.7-flash is a reasoning model whose hidden thinking shares
    this budget; thinking is disabled on our calls (see glm()), but
    long structured outputs still need more than the old 4000."""
    raw = (os.environ.get("GLM_MAX_TOKENS") or "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
        print(f"WARNING: ignoring invalid GLM_MAX_TOKENS {raw!r}; "
              f"using {GLM_DEFAULT_MAX_TOKENS}")
    return GLM_DEFAULT_MAX_TOKENS


def _glm_once(prompt, temperature=0.4, json_mode=False):
    """One single GLM (z.ai) chat completion attempt (no retries —
    call_llm's brain chain owns retrying and failover). Endpoint and
    auth verified against the official docs (docs.z.ai): POST
    https://api.z.ai/api/paas/v4/chat/completions with an
    "Authorization: Bearer <key>" header. Key: GLM_API_KEY. The default
    model glm-4.7-flash is z.ai's free tier (GLM_MODEL overrides it).
    Deep thinking is DISABLED on every call ("thinking":
    {"type": "disabled"} — the docs.z.ai Deep Thinking page supports
    the GLM-4.7 series and reserves the disabled-rejection for
    GLM-5.3 / GLM-5.3-FLASH only): all our call sites are structured
    generation/extraction, where the reasoning model's hidden thinking
    only burns the shared output budget and truncates the visible
    reply. A truncated reply (finish_reason "length") raises Fail like
    any other failed attempt: truncated text never silently enters an
    article."""
    api_key = (os.environ.get("GLM_API_KEY") or "").strip()
    if not api_key:
        raise Fail("GLM brain has no GLM_API_KEY available: "
                   "set the GLM_API_KEY repo secret.")
    max_tokens = glm_max_tokens()
    body = json.dumps({
        "model": _brain_model("glm"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},
        **({"response_format": {"type": "json_object"}} if json_mode else {}),
    }).encode("utf-8")
    status, raw = http_request(GLM_URL, method="POST", data=body,
                               headers={
                                   "Content-Type": "application/json",
                                   "Authorization": f"Bearer {api_key}"})
    if status != 200:
        raise Fail(f"GLM HTTP {status}: {raw[:300]!r}")
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise Fail(f"GLM returned a non-JSON body "
                   f"(HTTP {status}): {raw[:300]!r}")
    try:
        choice = data["choices"][0]
        text = (choice["message"]["content"] or "").strip()
        finish = choice.get("finish_reason")
    except Exception:
        raise Fail(f"GLM returned an unexpected shape: {raw[:300]!r}")
    if finish == "length":
        raise Fail(f"GLM reply truncated at {max_tokens} tokens "
                   "(finish_reason=length)")
    if not text:
        raise Fail("GLM returned empty text")
    return text

# ----------------------------------------------------------------------
# Brain chain: every logical LLM call walks an ordered list of brains
# (env LLM_CHAIN, comma-separated; default: LLM_PROVIDER, then glm,
# then gemini — the retired "github" service is never in the default
# chain). Each brain gets CHAIN_ATTEMPTS attempts with a
# CHAIN_BACKOFF_SECONDS x attempt pause between them; a brain whose
# key is missing is skipped. Only when every brain is skipped or
# exhausted does the call fail — fail-closed, as before.
# ----------------------------------------------------------------------

CHAIN_ATTEMPTS = 3
CHAIN_BACKOFF_SECONDS = 15
KNOWN_BRAINS = ("glm", "gemini", "github")


def llm_chain():
    """The ordered brain chain for this run. LLM_CHAIN (e.g.
    "glm,gemini") wins when set; otherwise the chain is LLM_PROVIDER
    (when set) followed by glm and gemini, deduplicated, order
    preserved. "github" is excluded from the DEFAULT chain (GitHub
    Models is retired); it is honoured only when named explicitly in
    LLM_CHAIN."""
    raw = (os.environ.get("LLM_CHAIN") or "").strip()
    if raw:
        names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    else:
        names = []
        primary = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
        if primary and primary != "github":
            names.append(primary)
        names.extend(["glm", "gemini"])
    chain = []
    for name in names:
        if name not in chain:
            chain.append(name)
    return chain


def _brain_key_available(name, api_key):
    """True when brain `name` has the credential it needs. The Gemini
    key is the api_key argument call_llm already receives (with the
    GEMINI_API_KEY env var as fallback)."""
    if name == "glm":
        return bool((os.environ.get("GLM_API_KEY") or "").strip())
    if name == "gemini":
        return bool((api_key or "").strip()
                    or (os.environ.get("GEMINI_API_KEY") or "").strip())
    if name == "github":
        return bool((os.environ.get("GH_MODELS_TOKEN") or "").strip()
                    or (os.environ.get("GITHUB_TOKEN") or "").strip())
    return False


def _call_brain(name, prompt, api_key, model, temperature, json_mode):
    """One single attempt on brain `name`. The Gemini path is the
    original gemini() above, unchanged (its temperature is built in);
    the GitHub Models and GLM paths honour the temperature / json_mode
    of the call site."""
    if name == "glm":
        return _glm_once(prompt, temperature=temperature,
                         json_mode=json_mode)
    if name == "github":
        return _github_models_once(prompt, temperature=temperature,
                                   json_mode=json_mode)
    return gemini(prompt, api_key, model)


# The brain that served the most recent successful call_llm()
# (§16.4: the relevance screen's second judge must differ from the
# first judge, so the dispatcher records which brain served).
_LAST_BRAIN = {"name": None}


def call_llm(prompt, api_key, model, temperature=0.4, json_mode=False,
             provider=None):
    """One logical LLM call, served by the brain chain (llm_chain()):
    brains are tried in order, each for up to CHAIN_ATTEMPTS attempts
    with a CHAIN_BACKOFF_SECONDS x attempt pause between attempts.
    Truncation, HTTP errors, transport errors and empty replies all
    count as failed attempts. A brain whose key is missing is skipped.
    The call raises Fail only when every brain in the chain was
    skipped or exhausted — fail-closed, exactly as the single-brain
    path was. `provider` pins the call to ONE named brain (the
    relevance screen's second judge, §16.4): the pinned brain gets
    the same attempts, and a missing key is a Fail rather than a
    skip, so the caller knows the pinned call never ran."""
    if provider is not None:
        provider = provider.strip().lower()
        if provider not in KNOWN_BRAINS:
            raise Fail(f"Unknown LLM provider {provider!r} "
                       f"(expected one of: {', '.join(KNOWN_BRAINS)})")
        if not _brain_key_available(provider, api_key):
            raise Fail(f"Pinned brain {provider!r} has no key available")
        chain = [provider]
    else:
        chain = llm_chain()
    failures = []
    for name in chain:
        if name not in KNOWN_BRAINS:
            raise Fail(f"Unknown LLM provider {name!r} in chain "
                       f"{chain} (expected one of: "
                       f"{', '.join(KNOWN_BRAINS)})")
        if not _brain_key_available(name, api_key):
            print(f"brain skipped: {name} (no key)")
            failures.append(f"{name} (no key)")
            continue
        last_error = None
        for attempt in range(1, CHAIN_ATTEMPTS + 1):
            try:
                text = _call_brain(name, prompt, api_key, model,
                                   temperature, json_mode)
            except Exception as e:
                last_error = e
                print(f"WARNING: brain {name} failed attempt "
                      f"({attempt}/{CHAIN_ATTEMPTS}): {e}")
                if attempt < CHAIN_ATTEMPTS:
                    time.sleep(CHAIN_BACKOFF_SECONDS * attempt)
                continue
            print(f"brain ok: {name}")
            _LAST_BRAIN["name"] = name
            return text
        print(f"brain switch: {name} failed after {CHAIN_ATTEMPTS} "
              "attempts -> next brain")
        failures.append(f"{name} ({last_error})")
    raise Fail(f"All brains failed: chain {chain} exhausted — "
               + "; ".join(failures))


def translate_query(text, api_key, model, target_language):
    """Best-effort: a search query for `text` in another language, via the
    Gemini call path (with its retries). Never fatal — returns None."""
    try:
        out = call_llm(
            f"Translate this research topic into a short {target_language} "
            "search query (keywords only, no quotes, no explanation):\n" + text,
            api_key, model)
        q = out.strip().strip('"')[:200]
        if q:
            print(f"Query variant ({target_language}): {q!r}")
            return q
        return None
    except Exception as e:
        print(f"WARNING: query translation to {target_language} failed "
              f"({e}); continuing without it")
        return None


def translate_query_to_english(text, api_key, model):
    """Best-effort: an English search query for the indexes. Never fatal."""
    return translate_query(text, api_key, model, "English")


def source_cards_text(sources):
    lines = []
    for i, s in enumerate(sources, 1):
        authors = ", ".join(s["authors"][:6]) or "Unknown authors"
        card = (
            f"[{i}] {s['title']}\n"
            f"    Authors: {authors}\n"
            f"    Year: {s['year'] or 'n.d.'} | Venue: {s['venue'] or 'n/a'} | "
            f"DOI: {s['doi'] or 'none'} | URL: {s['url']}\n"
            f"    Abstract: {(s['abstract'] or '(no abstract available)')[:1500]}")
        if s.get("registry_match"):
            card += (f"\n    Registry match (curated open-access "
                     f"registry, credibility signal): "
                     f"{s['registry_match']}")
        if s.get("fulltext_excerpt"):
            card += (f"\n    Full-text excerpt from this verified source "
                     f"itself (grounding for [{i}]):\n"
                     + s["fulltext_excerpt"][:FULLTEXT_CARD_CHARS])
        lines.append(card)
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


# --------------------------------------------------------------------------
# Quality laws (§15) — relevance first, credible tradition, content required
# --------------------------------------------------------------------------

CONTENT_MIN_ABSTRACT_CHARS = 50

_SCREEN_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "over", "under",
    "between", "among", "that", "this", "these", "those", "have", "has",
    "are", "was", "were", "will", "would", "can", "could", "its",
    "der", "die", "das", "und", "den", "des", "les", "une", "est", "sur",
    "aux", "dans", "par", "qui", "que",
}


def _screen_tokens(text):
    return {t for t in re.findall(r"[^\W_]+", (text or "").lower())
            if len(t) >= 3 and t not in _SCREEN_STOPWORDS}


def _script_families(text):
    fams = set()
    for ch in text or "":
        if "\u0600" <= ch <= "\u06FF":
            fams.add("arab")
        elif ch.isascii() and ch.isalpha():
            fams.add("latin")
    return fams


def _parse_json_value(text):
    """Best-effort: extract the first JSON value (list or object) from
    an LLM reply that may carry code fences or prose around it."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    starts = [i for i in (t.find("["), t.find("{")) if i >= 0]
    if not starts:
        raise ValueError("no JSON value in reply")
    start = min(starts)
    end = max(t.rfind("]"), t.rfind("}"))
    if end <= start:
        raise ValueError("no JSON value in reply")
    return json.loads(t[start:end + 1])


def has_content(source):
    """Content-required law (§15): a source counts only with real
    content — a fetched full-text excerpt or a non-trivial abstract.
    A bare title/DOI record never counts."""
    if (source.get("fulltext_excerpt") or "").strip():
        return True
    return len((source.get("abstract") or "").strip()) \
        >= CONTENT_MIN_ABSTRACT_CHARS


def _llm_relevance_screen(field, topic, borderline, api_key, model,
                          provider=None):
    """One batched brain-chain call judging the borderline candidates.
    Returns {1-based index: (keep, reason, drop_kind)} where drop_kind
    is "irrelevant" (fails the about-the-topic test) or "tradition"
    (relevant but outside the EN/FR/DE scholarly tradition). Any
    failure returns {} — unjudged borderline candidates are then
    dropped (fail-closed). `provider` pins the call to one brain
    (§16.4 second judge)."""
    lines = []
    for j, s in enumerate(borderline, 1):
        content = (s.get("abstract") or "").strip()
        if not content and s.get("fulltext_excerpt"):
            content = s["fulltext_excerpt"][:800]
        lines.append(
            f"[{j}] Title: {s.get('title', '')}\n"
            f"    Authors: {', '.join((s.get('authors') or [])[:4])} | "
            f"Year: {s.get('year') or 'n.d.'} | "
            f"Venue: {s.get('venue') or 'n/a'} | "
            f"Index: {s.get('index') or '?'}\n"
            f"    Content: {content[:800] or '(no abstract available)'}")
    prompt = (
        "You are screening candidate sources for a scholarly article.\n"
        f"Field: {field}\nTopic: {topic}\n\n"
        "For EACH candidate decide keep=true ONLY if BOTH hold:\n"
        "1) It is GENUINELY about the topic — its title/content "
        "addresses the topic itself, not a keyword coincidence (a "
        "shared word used in a different sense does not count).\n"
        "2) It is credible scholarship in English, French or German — "
        "the European / American / Indian research tradition. Exclude "
        "Persian or Arabic keyword-noise (school textbooks, government "
        "decrees, unrelated matches) UNLESS the item is a primary text "
        "in a scholarly edition or translation of that tradition.\n"
        "Give a one-line reason in English for every verdict.\n"
        'When keep=false, also set "drop" to "irrelevant" if it '
        'fails test 1, or "tradition" if it passes test 1 but fails '
        "test 2.\n"
        "Reply with JSON only, a list like: "
        '[{"i": 1, "keep": true, "reason": "..."}, '
        '{"i": 2, "keep": false, "drop": "irrelevant", '
        '"reason": "..."}]\n\n'
        "CANDIDATES:\n" + "\n".join(lines))
    try:
        raw = call_llm(prompt, api_key, model, temperature=0.1,
                       json_mode=True, provider=provider)
        data = _parse_json_value(raw)
        verdicts = {}
        for item in data or []:
            i = int(item.get("i"))
            kind = str(item.get("drop") or "").strip().lower()
            if kind not in ("irrelevant", "tradition"):
                kind = "irrelevant"
            verdicts[i] = (bool(item.get("keep")),
                           str(item.get("reason") or "").strip()
                           or "LLM relevance screen",
                           kind)
        return verdicts
    except Exception as e:
        print(f"WARNING: LLM relevance screen failed ({e}); borderline "
              "candidates will be dropped (fail-closed)")
        return {}


def screen_sources(field, topic, sources, api_key, model):
    """Relevance filter + content-required gate (§15) with a second
    judge for borderline candidates (§16.4). Returns (counted, stats):
    counted = the sources that COUNT (genuinely relevant AND
    content-bearing); stats carries the funnel numbers — input,
    relevant, counted, and drops by reason (irrelevant / tradition /
    judge disagreement / no content). Deterministic token/concept
    overlap decides the clear cases; a batched LLM call decides the
    borderline ones, and a borderline candidate is kept only if a
    SECOND judge — the next keyed brain in the chain, different from
    the first — agrees. Every verdict is logged, and the fail-closed
    bar is applied to the result."""
    stats = {"input": len(sources), "relevant": 0, "counted": 0,
             "drop_irrelevant": 0, "drop_tradition": 0,
             "drop_disagreement": 0, "drop_no_content": 0,
             "registry_matched": 0}
    # Registry signal (§17): tag every candidate; the tag never
    # changes a relevance verdict — it only re-orders the counted
    # list at the end (matched first, stable otherwise).
    for s in sources:
        registry_tag(s)
    terms = _screen_tokens(topic) | _screen_tokens(field)
    base = _screen_tokens(topic) or terms
    kept, borderline = [], []
    for s in sources:
        title = s.get("title") or ""
        title_tokens = _screen_tokens(title)
        body_tokens = _screen_tokens(
            title + " " + (s.get("abstract") or "") + " "
            + (s.get("fulltext_excerpt") or "")[:2000])
        cov_title = (len(base & title_tokens) / len(base)) if base else 0.0
        cov_body = (len(base & body_tokens) / len(base)) if base else 0.0
        same_script = bool(_script_families(topic)
                           & _script_families(title))
        if base and cov_title >= 0.6:
            s["relevance_reason"] = (
                f"deterministic keep: {cov_title:.0%} of topic terms "
                "in the title")
            kept.append(s)
            print(f"relevance: keep {title[:70]} — "
                  f"{s['relevance_reason']}")
        elif base and same_script and cov_body >= 0.6:
            s["relevance_reason"] = (
                f"deterministic keep: {cov_body:.0%} of topic terms "
                "in title+abstract")
            kept.append(s)
            print(f"relevance: keep {title[:70]} — "
                  f"{s['relevance_reason']}")
        elif base and same_script and cov_body == 0.0:
            stats["drop_irrelevant"] += 1
            print(f"relevance: drop {title[:70]} — deterministic drop: "
                  "no topic-term overlap in title or abstract")
        else:
            borderline.append(s)
    if borderline:
        verdicts = _llm_relevance_screen(field, topic, borderline,
                                         api_key, model)
        first_brain = _LAST_BRAIN["name"]
        first_keeps = [s for j, s in enumerate(borderline, 1)
                       if verdicts.get(j, (False, "", ""))[0]]
        # Second judge (§16.4): the next keyed chain brain that
        # differs from the first judge re-screens ONLY the first
        # judge's keeps. A borderline candidate counts as relevant
        # only if BOTH judges keep it.
        second_brain, second = None, {}
        if first_keeps:
            for name in llm_chain():
                if (name != first_brain and name in KNOWN_BRAINS
                        and _brain_key_available(name, api_key)):
                    second_brain = name
                    break
            if second_brain is None:
                print("second judge: unavailable (no other keyed "
                      "brain in the chain) — first-judge verdicts "
                      "stand")
            else:
                print(f"second judge: {second_brain} reviews "
                      f"{len(first_keeps)} borderline keep(s) of "
                      f"first judge {first_brain}")
                raw2 = _llm_relevance_screen(field, topic, first_keeps,
                                             api_key, model,
                                             provider=second_brain)
                for k, s2 in enumerate(first_keeps, 1):
                    if k in raw2:
                        second[id(s2)] = raw2[k]
        for j, s in enumerate(borderline, 1):
            keep, reason, kind = verdicts.get(
                j, (False, "no verdict from the relevance screen — "
                           "dropped", "irrelevant"))
            title = s.get("title") or ""
            if not keep:
                if kind == "tradition":
                    stats["drop_tradition"] += 1
                else:
                    stats["drop_irrelevant"] += 1
                print(f"relevance: drop {title[:70]} — {reason}")
                continue
            if second_brain is not None:
                v2 = second.get(id(s))
                if not (v2 and v2[0]):
                    stats["drop_disagreement"] += 1
                    reason2 = (v2[1] if v2 else
                               "no verdict from the second judge")
                    print(f"relevance: drop (judge disagreement) "
                          f"{title[:70]} — second judge "
                          f"({second_brain}): {reason2}")
                    continue
                s["relevance_reason"] = (
                    f"LLM screen (two judges agree): {reason}")
                kept.append(s)
                print(f"relevance: keep {title[:70]} — "
                      f"{s['relevance_reason']}")
            else:
                s["relevance_reason"] = f"LLM screen: {reason}"
                kept.append(s)
                print(f"relevance: keep {title[:70]} — "
                      f"LLM screen: {reason}")
    stats["relevant"] = len(kept)
    counted = []
    for s in kept:
        if has_content(s):
            counted.append(s)
        else:
            stats["drop_no_content"] += 1
            print("content: not counted (no abstract/full text) — "
                  f"{(s.get('title') or '')[:70]}")
    # Registry boost (§17): relevant, content-bearing sources that
    # match a curated registry sort ahead of non-matched ones
    # (stable sort — relative order otherwise unchanged).
    counted.sort(key=lambda s: 0 if s.get("registry_match") else 1)
    stats["registry_matched"] = sum(1 for s in counted
                                    if s.get("registry_match"))
    stats["counted"] = len(counted)
    print(f"Relevance + content screen: {len(sources)} verified -> "
          f"{len(kept)} relevant -> {len(counted)} counted "
          f"(registry-matched: {stats['registry_matched']})")
    return counted, stats


# Title law (§15): narrow, claim-fitted titles only.
TITLE_SPECIFICITY_MARKERS = (
    "روش", "اندازه‌گیری", "اندازه گیری", "بازسازی", "خطای", "خطا",
    "ارزیابی", "سنجش", "محاسبه", "حساسیت", "مطالعهٔ موردی",
    "method", "measurement", "measuring", "reconstruction", "error",
    "assessment", "evaluation", "sensitivity", "determination",
    "computation", "case study",
)


def title_is_specific(title, field, topic):
    """Deterministic specificity check: rejects broad field-label
    titles. A title passes only if it is long enough to say something
    specific, is not just the field/topic labels re-arranged, and
    names a specific element (a method, object, measurement, error,
    assessment...)."""
    t = norm(title)
    if len(t.split()) < 5:
        return False
    if t == norm(topic) or t == norm(field):
        return False
    label_tokens = _screen_tokens(field) | _screen_tokens(topic)
    title_tokens = _screen_tokens(title)
    if title_tokens and title_tokens <= label_tokens:
        return False
    return any(m in t for m in TITLE_SPECIFICITY_MARKERS)


def generate_titles(field, topic, language, part1, part3, api_key,
                    model):
    """After the body exists: 5–6 narrow title candidates + the best
    index + 3 narrow follow-up topic suggestions, via the brain chain.
    Returns (candidates, best_index, next_topics); ([], 0, []) on any
    failure (the caller falls back, loudly)."""
    lang_name = LANGUAGE_NAMES.get(language.lower(), language)
    prompt = (
        f"Field: {field}\nTopic: {topic}\n\n"
        "The scholarly article below is already written. Based ONLY "
        "on what it actually argues and proves, propose SIX narrow, "
        f"specific scholarly titles in {lang_name}.\n"
        "TITLE LAW: each title must name the specific object, method "
        "or result this article is about, so it could fit ONLY this "
        "article. Broad field-label titles are banned (never anything "
        "like 'The role of X in the history of Y', and never a "
        "restatement of the field plus the topic). Also propose THREE "
        "narrow, related topics suitable for a follow-up article in "
        "the same claim area.\n"
        "Reply with JSON only: {\"candidates\": [6 titles], \"best\": "
        "<0-based index of the best candidate>, \"next_topics\": "
        "[3 topics]}\n\n"
        f"ARTICLE ABSTRACT AND INTRODUCTION:\n{part1[:4000]}\n\n"
        f"ARTICLE DISCUSSION AND CONCLUSION:\n{part3[:4000]}\n")
    try:
        raw = call_llm(prompt, api_key, model, temperature=0.3,
                       json_mode=True)
        data = _parse_json_value(raw)
        candidates = [str(c).strip() for c in data.get("candidates", [])
                      if str(c).strip()]
        next_topics = [str(c).strip()
                       for c in data.get("next_topics", [])
                       if str(c).strip()]
        best = int(data.get("best", 0))
        if not (0 <= best < len(candidates)):
            best = 0
        return candidates[:6], best, next_topics[:3]
    except Exception as e:
        print(f"WARNING: title generation failed ({e}); falling back")
        return [], 0, []


def select_title(candidates, best, field, topic):
    """Pick the article title: the LLM's best candidate first, then
    the rest in order — the first one passing the deterministic
    specificity check wins. If none passes, the top candidate is used
    with a loud warning (a finished article is not discarded over its
    title)."""
    if not candidates:
        print("WARNING: no title candidates; using the topic as title")
        return topic
    order = [candidates[best]] + [c for i, c in enumerate(candidates)
                                  if i != best]
    for c in order:
        if title_is_specific(c, field, topic):
            return c
    print("WARNING: no title candidate passed the specificity check; "
          f"using the top candidate: {order[0][:80]}")
    return order[0]


def build_suggestions_text(title, candidates, next_topics):
    """The end-of-production DM text (§15 suggestions-visible law):
    the user SEES the bot's title candidates, the used one marked,
    plus narrow next-topic suggestions."""
    lines = ["🎉 مقالهٔ شما در کانال Discovery منتشر شد.",
             f"عنوان نهایی: «{title}»", "",
             "📌 عنوان‌های پیشنهادی ربات برای این مقاله:"]
    if candidates:
        for c in candidates:
            if c == title:
                lines.append(f"✅ عنوان استفاده‌شده: {c}")
            else:
                lines.append(f"• {c}")
    else:
        lines.append("• (پیشنهاد عنوانی ثبت نشد)")
    lines += ["", "💡 موضوع‌های باریک و مرتبط برای مقالهٔ بعدی:"]
    if next_topics:
        for t in next_topics:
            lines.append(f"• {t}")
    else:
        lines.append("• (پیشنهادی ثبت نشد)")
    return "\n".join(lines)


def send_suggestions_dm(token, chat_id, title, candidates, next_topics):
    """Fail-soft: send the suggestions DM to the requester
    (ARTICLE_CHAT_ID). Never raises, never fails the run. Returns
    True if sent."""
    try:
        chat_id = (chat_id or "").strip()
        if not chat_id or not token:
            print("suggestions DM skipped: no ARTICLE_CHAT_ID/token")
            return False
        text = build_suggestions_text(title, candidates, next_topics)
        for chunk in split_text(text, 3900):
            tg_call("sendMessage", token,
                    {"chat_id": chat_id, "text": chunk})
        print(f"suggestions DM sent to chat {chat_id}")
        return True
    except Exception as e:
        print(f"WARNING: suggestions DM failed (ignored): {e}")
        return False


# --------------------------------------------------------------------------
# Quality funnel report (§16.3) — at the end of EVERY article run,
# success or fail-closed failure, the requester gets the funnel in
# Persian: candidates found -> verified -> with content -> relevant
# -> counted, with the drops by reason, plus the final word count +
# target on success, or the stopping stage on failure. Fail-soft,
# like the suggestions DM; the same text is printed as log lines.
# --------------------------------------------------------------------------

def build_funnel_text(topic, funnel, success, stage=None,
                      word_count=None, word_target=None):
    lines = ["📊 گزارش کیفیت تولید مقاله",
             f"موضوع: {topic}",
             "",
             f"منابع پیدا شده: {funnel.get('candidates', 0)}",
             f"راستی‌آزمایی‌شده: {funnel.get('verified', 0)}",
             f"دارای محتوا (چکیده یا متن کامل): "
             f"{funnel.get('with_content', 0)}",
             f"مرتبط تشخیص داده‌شده: {funnel.get('relevant', 0)}",
             f"منابع نهاییِ شمرده‌شده: {funnel.get('counted', 0)}",
             f"هم‌خوان با فهرست منابع معتبر (رجیستری): "
             f"{funnel.get('registry_matched', 0)}",
             "",
             "حذف‌شده‌ها:",
             f"• نامرتبط: {funnel.get('drop_irrelevant', 0)}",
             f"• بدون محتوا: {funnel.get('drop_no_content', 0)}",
             "• خارج از سنت پژوهشی مجاز (انگلیسی/فرانسوی/آلمانی): "
             f"{funnel.get('drop_tradition', 0)}"]
    if funnel.get("drop_disagreement"):
        lines.append("• اختلاف دو داور ارتباط: "
                     f"{funnel['drop_disagreement']}")
    lines.append("")
    if success:
        lo, hi = word_target or (0, 0)
        lines.append(f"✅ مقاله منتشر شد — تعداد کلمات: {word_count} "
                     f"(هدف: {lo}–{hi})")
    else:
        lines.append(f"❌ تولید متوقف شد — مرحله: {stage or 'نامشخص'}")
    return "\n".join(lines)


def send_funnel_dm(token, chat_id, topic, funnel, success, stage=None,
                   word_count=None, word_target=None):
    """Fail-soft: print the funnel as log lines and DM it to the
    requester (ARTICLE_CHAT_ID). Never raises. Returns True if sent."""
    text = build_funnel_text(topic, funnel, success, stage=stage,
                             word_count=word_count,
                             word_target=word_target)
    for line in text.splitlines():
        print(f"funnel: {line}")
    try:
        chat_id = (chat_id or "").strip()
        if not chat_id or not token:
            print("funnel DM skipped: no ARTICLE_CHAT_ID/token")
            return False
        for chunk in split_text(text, 3900):
            tg_call("sendMessage", token,
                    {"chat_id": chat_id, "text": chunk})
        print(f"funnel DM sent to chat {chat_id}")
        return True
    except Exception as e:
        print(f"WARNING: funnel DM failed (ignored): {e}")
        return False


# Article category/type (§14). Absent env (manual dispatch) defaults to
# intl/isi. TYPE_GUIDE lines are injected into the writing prompts;
# TYPE_LABELS are shown in the channel header.
TYPE_GUIDE = {
    "isc": "Target standard: ISC journal article — Persian body, extended English abstract, Persian references style",
    "isi": "Target standard: ISI (Web of Science) journal article — IMRaD structure, structured abstract",
    "scopus": "Target standard: Scopus-indexed journal article — IMRaD structure, structured abstract, international references style",
    "pubmed": "Target standard: PubMed-indexed biomedical journal article — IMRaD structure, structured abstract, MeSH-style keywords",
    "ieee": "Target standard: IEEE journal article — IEEE structure and IEEE references style, concise technical English",
    "res": "Target standard: Iranian scientific-research journal article (وزارت علوم) — Persian, IMRaD",
    "promo": "Target standard: Iranian scientific-promotional (علمی‌ترویجی) article — Persian, accessible scholarly style",
    "conf": "Target standard: Iranian conference paper — Persian, conference format, shorter",
    "review": "Target standard: Review article (مروری) — thematic synthesis",
    "intlconf": "Target standard: International conference paper — English, conference format",
}

TYPE_LABELS = {
    "isc": "ISC",
    "res": "علمی‌پژوهشی",
    "promo": "علمی‌ترویجی",
    "conf": "همایشی",
    "review": "مروری",
    "isi": "ISI (Web of Science)",
    "scopus": "Scopus",
    "pubmed": "PubMed",
    "ieee": "IEEE",
    "intlconf": "همایشی بین‌المللی",
}


# Target word counts (§15 length standards), by article type.
TYPE_WORD_TARGETS = {
    "res": (5000, 7000),
    "isc": (5000, 7000),
    "isi": (6000, 8000),
    "scopus": (5000, 7000),
    "pubmed": (5000, 7000),
    "ieee": (4000, 6000),
    "review": (8000, 10000),
    "promo": (3000, 4000),
    "conf": (2500, 3500),
    "intlconf": (2500, 3500),
}



def write_article(field, topic, language, sources, api_key, model, guide="",
                  word_target=None):
    lang_name = LANGUAGE_NAMES.get(language.lower(), language)
    cards = source_cards_text(sources)
    if not word_target:
        word_target = (6000, 8000)
    lo, hi = word_target
    mid = (lo + hi) // 2
    hard_rules = (
        "HARD RULES — follow exactly:\n"
        "- Use ONLY the numbered sources below. Never mention any work, author, "
        "number, journal or result that is not in these sources.\n"
        "- Every factual claim must carry its source number like [3] at the end "
        "of the sentence.\n"
        "- Synthesise in your own words; never copy a sentence from an abstract.\n"
        "- If the sources do not answer something, say so plainly instead of "
        "filling the gap.\n"
        "- DEPTH (§15): work with the sources' ACTUAL content — specific data, "
        "formulas, numerical values, named methods and named results exactly "
        "as the source cards give them. Compare the sources head-to-head: "
        "where they agree, where they contradict, and by how much. Include "
        "error, uncertainty and limitation analysis wherever the sources "
        "support it.\n"
        "- NO GENERIC FILLER: any paragraph that could be pasted unchanged "
        "into an article on a different topic is forbidden. If a source card "
        "carries no usable detail on a point, say so in one sentence and "
        "move on — never invent detail.\n"
        f"- TARGET LENGTH: the complete article must reach {lo}–{hi} words.\n"
        f"- Write the ENTIRE output in {lang_name}.\n")
    common = (f"Field: {field}\nTopic: {topic}\n\n{hard_rules}\n"
              + (f"{guide}\n" if guide else "")
              + f"THE ONLY SOURCES YOU MAY USE:\n{cards}\n")

    part1 = call_llm(common +
        f"\nWrite PART 1 of a scholarly article (about {int(mid * 0.18)} "
        "words):\n"
        "1) '## Abstract' — a structured abstract with labelled parts: "
        "Background, Objective, Method, Findings, Conclusion — based only on "
        "the sources. The Findings must state concrete results from the "
        "sources (named methods, values, dates), never generalities.\n"
        "2) '## Keywords' — 5 to 7 keywords on one line.\n"
        "3) '## Introduction' — a funnel: broad context, narrowing to the "
        "specific problem, the specific gap visible in these sources, and "
        "this article's precise aim and claim. Do NOT write a title; the "
        "title is chosen after the article is complete.",
        api_key, model)
    progress_update(55, "✍️ نگارش بخش اول")
    part2 = call_llm(common +
        f"\nWrite PART 2 of the same article: the body (about "
        f"{int(mid * 0.52)} words). Organise the synthesis into 3 to 5 "
        "thematic sections, each with a '## ' heading. In every section: "
        "present the specific evidence from the cards (data, formulas, "
        "numbers, named methods and results), compare the sources with "
        "each other, and analyse errors, uncertainties and disagreements "
        "where the sources allow. Keep every claim cited as [n]. Do NOT "
        "repeat the abstract or introduction.",
        api_key, model)
    progress_update(75, "✍️ نگارش بخش دوم")
    part3 = call_llm(common +
        f"\nWrite PART 3 of the same article (about {int(mid * 0.30)} "
        "words):\n"
        "1) '## Discussion' — what the combined evidence means, how it "
        "compares across the sources, and what the article's claim is "
        "worth.\n"
        "2) '## Conclusion' — the takeaways that the sources actually "
        "support.\n"
        "3) '## Limitations' — honest limits of this article (only "
        "relevant, content-bearing sources were used; abstract/full-text "
        "level; free-access only).\n"
        "Do NOT write a reference list; it is added separately.",
        api_key, model)
    progress_update(90, "✍️ نگارش بخش پایانی")

    # Title law (§15): the title is generated AFTER the body, from the
    # article's actual claim, and must pass the specificity check.
    candidates, best, next_topics = generate_titles(
        field, topic, language, part1, part3, api_key, model)
    title = select_title(candidates, best, field, topic)
    references = "\n".join(format_reference(i, s)
                           for i, s in enumerate(sources, 1))
    article = (f"# {title}\n\n{part1}\n\n{part2}\n\n{part3}\n\n"
               f"## References\n\n{references}\n")
    word_count = len(article.split())
    print(f"word count: {word_count} (target {lo}–{hi})")
    return title, article, {"title_candidates": candidates,
                            "next_topics": next_topics,
                            "word_count": word_count}


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

def _quality_pipeline(field, topic, verified_list, api_key, model):
    """One retrieval wave's quality pipeline (§16): enrich abstracts
    by DOI, attach full texts, then the relevance + content screen.
    Returns (counted, stats) — the screen stats plus this wave's
    verified / with-content counts, for the funnel report (§16.3)."""
    enrich_abstracts(verified_list)
    try:
        attach_fulltexts(verified_list)
    except Exception as e:
        print(f"WARNING: full-text bridge failed (continuing "
              f"without): {e}")
    counted, stats = screen_sources(field, topic, verified_list,
                                    api_key, model)
    stats["verified"] = len(verified_list)
    stats["with_content"] = sum(1 for s in verified_list
                                 if has_content(s))
    return counted, stats


def _merge_funnel(funnel, sstats):
    for k in ("verified", "with_content", "relevant", "counted",
              "drop_irrelevant", "drop_tradition", "drop_disagreement",
              "drop_no_content", "registry_matched"):
        funnel[k] = funnel.get(k, 0) + sstats.get(k, 0)


def _wave2_queries(field, topic, api_key, model):
    """Broader wave-2 query variants (§16.2): the field alone, the
    topic split into keyword halves, and extra English variants via
    the existing translation path. Best-effort; deduped, order kept."""
    out = []

    def add(q):
        if q and q not in out:
            out.append(q)

    add(field)
    tokens = topic.split()
    if len(tokens) >= 2:
        half = (len(tokens) + 1) // 2
        add(" ".join(tokens[:half]))
        add(" ".join(tokens[half:]))
    add(translate_query_to_english(field, api_key, model))
    add(translate_query(f"{field} {topic}", api_key, model, "English"))
    return out


def main():
    field = os.environ.get("FIELD", "").strip()
    topic = os.environ.get("TOPIC", "").strip()
    category = (os.environ.get("ARTICLE_CATEGORY") or "intl").strip().lower()
    if category not in ("ir", "intl"):
        category = "intl"
    article_type = (os.environ.get("ARTICLE_TYPE") or "isi").strip().lower()
    if article_type not in TYPE_GUIDE:
        article_type = "isi"
    language = (os.environ.get("LANGUAGE") or "fa").strip()
    mode = (os.environ.get("MODE") or "review").strip().lower()
    model = (os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash").strip()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()
    provider = llm_provider()
    chain = llm_chain()
    print(f"LLM provider: {provider} (model {llm_model()}); "
          f"brain chain: {chain}")
    get_registry()  # §17: loads + logs the registry counts (fail-soft)

    if not field or not topic:
        raise Fail("FIELD and TOPIC inputs are required.")
    if mode != "review":
        raise Fail(f"Mode '{mode}' is not in the v1 scope (v1 = review/synthesis only).")
    if not any(_brain_key_available(name, api_key) for name in chain):
        raise Fail("No usable LLM brain: chain " + ", ".join(chain)
                   + " — none of its providers has its key available "
                   "(GLM_API_KEY / GEMINI_API_KEY / GH_MODELS_TOKEN).")
    missing = [n for n, v in [("TELEGRAM_BOT_TOKEN", tg_token),
                              ("CHAT_ID", chat_id)] if not v]
    if missing:
        raise Fail("Missing required Actions secret(s): " + ", ".join(missing))

    state = load_state()
    for p in state.get("productions", []):
        if (norm(p.get("category") or "intl") == norm(category)
                and norm(p.get("type") or "isi") == norm(article_type)
                and norm(p.get("field")) == norm(field)
                and norm(p.get("topic")) == norm(topic)
                and norm(p.get("language")) == norm(language)):
            print(f"NOTE: '{topic}' ({field}, {language}) is already in "
                  "state.json — refusing to duplicate. Exiting cleanly.")
            return 0

    # Queries: up to 3 variants of the topic — as given, an Arabic
    # translation, an English translation (translations are best-effort via
    # Gemini; a failed translation never fails the run) — plus the existing
    # field+topic combined query.
    queries = []

    def add_query(q):
        if q and q not in queries:
            queries.append(q)

    add_query(topic)
    add_query(f"{field} {topic}")
    add_query(translate_query(topic, api_key, model, "Arabic"))
    add_query(translate_query_to_english(topic, api_key, model))
    print(f"Queries: {queries}")

    progress_start(os.environ.get("ARTICLE_CHAT_ID", ""), tg_token)
    progress_update(15, "🔎 جست‌وجوی منابع")
    rstate = _new_retrieval_state()
    verified = collect_verified(queries, state=rstate)
    progress_update(35, "📚 راستی‌آزمایی منابع")

    # Quality laws (§15): only genuinely relevant, content-bearing
    # sources count toward the bar — verification alone is not enough.
    funnel = {"candidates": 0, "verified": 0, "with_content": 0,
              "relevant": 0, "counted": 0, "drop_irrelevant": 0,
              "drop_tradition": 0, "drop_disagreement": 0,
              "drop_no_content": 0, "registry_matched": 0}
    counted, sstats = _quality_pipeline(field, topic, verified,
                                        api_key, model)
    _merge_funnel(funnel, sstats)
    progress_update(45, "🎯 گزینش منابع مرتبط")

    if len(counted) < MIN_SOURCES:
        # Tiered wave 2 (§16.2): ONE broader retrieval wave — book
        # sources first — through the SAME verify -> enrich -> bridge
        # -> screen pipeline, sharing wave 1's dedupe/bench machinery,
        # before the run is allowed to fail.
        before_candidates = rstate["total_candidates"]
        w2_queries = _wave2_queries(field, topic, api_key, model)
        print(f"wave 2: only {len(counted)} counted after wave 1 "
              f"(< {MIN_SOURCES}); broader retrieval, queries: "
              f"{w2_queries}")
        book_first = [search_internet_archive, search_annas_archive]
        order2 = book_first + [s for s in SEARCHERS
                               if s not in book_first]
        verified2 = collect_verified(w2_queries, state=rstate,
                                     searcher_order=order2)
        counted2, sstats2 = _quality_pipeline(field, topic, verified2,
                                              api_key, model)
        _merge_funnel(funnel, sstats2)
        verified = verified + verified2
        counted = counted + counted2
        print(f"wave 2: "
              f"+{rstate['total_candidates'] - before_candidates} "
              f"candidates, +{len(verified2)} verified, "
              f"+{len(counted2)} counted")
        progress_update(48, "🎯 موج دوم گزینش منابع")
    funnel["candidates"] = rstate["total_candidates"]

    if len(counted) < MIN_SOURCES:
        send_funnel_dm(tg_token, os.environ.get("ARTICLE_CHAT_ID", ""),
                       topic, funnel, False,
                       stage="گزینش منابع مرتبط و محتوادار")
        raise Fail(
            f"Only {len(counted)} relevant, content-bearing sources for "
            f"'{topic}' out of {len(verified)} verified (minimum "
            f"{MIN_SOURCES}). Refusing to write an article on irrelevant "
            "or title-only material. Try a broader or different topic.")

    _t0 = time.monotonic()
    try:
        title, article, meta = write_article(
            field, topic, language, counted, api_key, model,
            guide=TYPE_GUIDE[article_type],
            word_target=TYPE_WORD_TARGETS.get(article_type))
    except Fail:
        send_funnel_dm(tg_token, os.environ.get("ARTICLE_CHAT_ID", ""),
                       topic, funnel, False, stage="نگارش مقاله")
        raise
    print(f"timing: writing {time.monotonic() - _t0:.1f}s")

    header = (f"📚 Scientific Discovery Agent — new article\n"
              f"Field: {field}\nTopic: {topic}\nLanguage: {language}\n"
              f"Mode: {mode}\nVerified sources: {len(verified)} "
              f"(relevant, content-bearing: {len(counted)})\n"
              f"نوع مقاله: {TYPE_LABELS[article_type]}\n\n{title}")
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
        "category": category, "type": article_type,
        "field": field, "topic": topic, "language": language, "mode": mode,
        "title": title,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": len(counted),
        "message_ids": message_ids,
    })
    save_state(state)
    progress_update(100, "انتشار در کانال")
    # Suggestions-visible law (§15): the requester SEES the bot's
    # title candidates (used one marked) + next-topic suggestions.
    send_suggestions_dm(tg_token, os.environ.get("ARTICLE_CHAT_ID", ""),
                        title, meta.get("title_candidates", []),
                        meta.get("next_topics", []))
    # Funnel report (§16.3): every run ends with the quality funnel
    # in the requester's DM (and in the log).
    send_funnel_dm(tg_token, os.environ.get("ARTICLE_CHAT_ID", ""),
                   topic, funnel, True,
                   word_count=meta.get("word_count"),
                   word_target=TYPE_WORD_TARGETS.get(article_type))
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
