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
  SEARCH_SCOPE (default "full"; "new" restricts the candidate
  search to the §§32–42 additions only — §43),
  GEMINI_MODEL (default "gemini-3.5-flash")
  LLM_PROVIDER (default "gemini"; "github" selects GitHub Models,
  "glm" selects GLM on z.ai),
  GH_MODELS_MODEL (default "openai/gpt-4o"),
  GLM_MODEL (default "glm-4.7-flash", the free z.ai Flash model)
  GLM_MAX_TOKENS (default 8000, the GLM path's output budget)
  GROQ_MODEL (default "openai/gpt-oss-120b"), OPENROUTER_MODEL
  (default "meta-llama/llama-3.3-70b-instruct:free"),
  CLOUDFLARE_MODEL (default "@cf/meta/llama-3.3-70b-fp8-fast"),
  SAMBANOVA_MODEL (default "Meta-Llama-3.3-70B-Instruct"),
  DASHSCOPE_MODEL (default "qwen-plus"), MODELSCOPE_MODEL (default
  "Qwen/Qwen3-32B"), SILICONFLOW_MODEL (default
  "Qwen/Qwen2.5-7B-Instruct"),
  CF_ACCOUNT_ID (the Cloudflare account id — an identifier, not a
  secret; the cloudflare brain is skipped when it is unset)
Secrets (names only, set in the repo's Actions secrets):
  GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID
  GH_MODELS_TOKEN (optional; the GitHub path falls back to GITHUB_TOKEN)
  GLM_API_KEY (the GLM path's key, from z.ai)
  GROQ_API_KEY, OPENROUTER_API_KEY, SAMBANOVA_API_KEY,
  CLOUDFLARE_API_TOKEN, DASHSCOPE_API_KEY, MODELSCOPE_API_KEY,
  SILICONFLOW_API_KEY (optional §22 free-tier brains; each joins the
  chain automatically when its secret exists)
  CORE_API_KEY (optional §42; the CORE aggregator searcher joins
  retrieval automatically when its secret exists)
"""

import email.utils
import hashlib
import html as html_lib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
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
# §20 text-first constants (defined here: default arguments below
# evaluate them at definition time).
MATRIX_MAX_QUERIES = 28
MATRIX_PER_LANG = 6
MATRIX_LANGUAGES = ("en", "ar", "fa", "de", "fr", "ru", "es", "tr",
                    "it", "la", "ur", "uz", "zh", "ja", "ko",
                    "fi", "nl", "pl", "pt", "hr", "sr", "uk", "el")
MIN_FULLTEXT_SOURCES = 3
FULLTEXT_V2_MAX_SOURCES = 14
FULLTEXT_FETCH_MAX_BYTES = 10 * 1024 * 1024
FULLTEXT_GLOBAL_BUDGET_SECONDS = 480
MIN_FETCHED_TEXT_CHARS = 3000

LANGUAGE_NAMES = {
    "fa": "Persian (Farsi)", "en": "English", "ar": "Arabic", "zh": "Chinese",
    "fr": "French", "de": "German", "es": "Spanish", "ru": "Russian",
    "tr": "Turkish", "ur": "Urdu", "it": "Italian", "la": "Latin",
    "uz": "Uzbek", "ja": "Japanese", "ko": "Korean",
    "fi": "Finnish", "nl": "Dutch", "pl": "Polish", "pt": "Portuguese",
    "hr": "Croatian", "sr": "Serbian", "uk": "Ukrainian", "el": "Greek",
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
        # §20 full-text routes: OpenAlex itself names the best
        # open-access location — capture its PDF / OA URLs so the
        # acquisition layer can fetch the text, not just the abstract.
        best_oa = w.get("best_oa_location") or {}
        oa_pdf = (best_oa.get("pdf_url") or "").strip()
        oa_url = ((w.get("open_access") or {}).get("oa_url")
                  or "").strip()
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
            "oa_pdf_url": oa_pdf,
            "fulltext_url": oa_pdf or oa_url,
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
        fulltext_link = ""
        for lnk in bib.get("link", []) or []:
            if lnk.get("url"):
                link = lnk["url"]
                if (lnk.get("type") or "").lower() == "fulltext":
                    fulltext_link = lnk["url"]
                    break
        if fulltext_link:
            link = fulltext_link
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
            "fulltext_url": fulltext_link,
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
        fulltext_url = ""
        for ft in fulltexts:
            u = _openaire_text(ft)
            if u.startswith("http"):
                landing = u
                fulltext_url = u
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
            "fulltext_url": fulltext_url,
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
        # §20 full-text routes: the PMCID unlocks Europe PMC's
        # fullTextXML endpoint (the whole article text), and the record
        # carries an explicit full-text URL list — capture both.
        pmcid = (r.get("pmcid") or "").strip()
        ft_url = ""
        for entry in ((r.get("fullTextUrlList") or {}).get(
                "fullTextUrl") or []):
            u = (entry.get("url") or "").strip()
            if not u:
                continue
            if (entry.get("documentStyle") or "").lower() == "pdf":
                ft_url = u
                break
            ft_url = ft_url or u
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
            "pmcid": pmcid,
            "epmc_id": rid,
            "epmc_source": (r.get("source") or "").strip(),
            "fulltext_url": ft_url,
        })
    return out


def search_hal(query):
    """HAL — the French national open archive (no key, no registration);
    strong for European scholarship and history of science."""
    out = []
    url = ("https://api.archives-ouvertes.fr/search/?wt=json&rows=25&q="
           + urllib.parse.quote(query)
           + "&fl=title_s,authFullName_s,producedDateY_i,doiId_s,uri_s,"
             "fileMain_s")
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
            "fulltext_url": (doc.get("fileMain_s") or "").strip(),
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
# Arab-country journal platforms (§23) — open access, no keys.
# The eight verified national/regional platforms are registered in
# arab_journals_registry.json (§17 wiring: credibility/priority signal
# only, never verification). Four of them also have a live search
# route that was probed from the build sandbox on 2026-11-26 and seen
# answering with real results; only those four are searched here:
#   IMIST/PRSM (Morocco) — JSON API on prsm.imist.ma
#       GET /api/search?criteria_field=all_text&criteria_value=<q>
#           &criteria_operator=OR&page=1&page_size=8
#   KnE Publishing (UAE) — platform search API on kneopen.com
#       POST /wp-admin/admin-ajax.php
#           {action: search_publications_api, currentPage: 1,
#            searchTerm: <q>}  (Solr-style JSON, Crossref DOIs)
#   Dirasat (Jordan) — OJS site-wide search
#       GET dsr.ju.edu.jo/index.php/index/search/search?query=<q>
#   An-Najah (Palestine) — per-journal search (Django/Haystack)
#       GET journals.najah.edu/journal/<slug>/search/?q=<q>
#           for slugs anujr-a, anujr-b, pmpj
# Registry-only (no machine-usable route answered the probe):
#   ASJP (Algeria) and IASJ (Iraq) — servers closed every connection;
#   EKB (Egypt) — HTTP 429 "Access Restricted" on journal subdomains;
#   QScience (Qatar) — JavaScript SPA shell, no searchable content.
# Every platform call is fail-soft: an error, timeout or empty parse
# contributes nothing and never raises. Candidates carry the usual
# shape and flow through the UNCHANGED verification, screening,
# judges and text-first gate; the §18 intake language gate applies
# to them exactly as to every other source.
# --------------------------------------------------------------------------

ARAB_PER_PLATFORM_CAP = 8
ARAB_TOTAL_CAP = 20
_ARAB_TIMEOUT = 10  # seconds, single attempt — a dead platform must
                    # not eat the retrieval wave's source budget
_ARAB_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "ar,en;q=0.8,fr;q=0.6",
}
_NAJAH_JOURNALS = ["anujr-a", "anujr-b", "pmpj"]


def _arab_fetch(url, data=None):
    """One single-shot HTTPS request for the Arab platforms (no
    retries: http_request's 6 attempts x 180 s would stall the wave).
    Returns (status, text); (None, "") on any transport error. A
    truncated chunked body (http.client.IncompleteRead — seen live
    from kneopen.com) is salvaged: the partial bytes usually still
    carry a complete JSON document or result page."""
    import http.client
    try:
        _pace(urllib.parse.urlparse(url).netloc)
        req = urllib.request.Request(url, data=data, headers=_ARAB_HEADERS)
        with urllib.request.urlopen(req, timeout=_ARAB_TIMEOUT) as resp:
            try:
                raw = resp.read(1_500_000)
            except http.client.IncompleteRead as e:
                raw = e.partial or b""
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(300_000).decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception:
        return None, ""


def _arab_json(url, data=None):
    """Fetch + JSON-parse with ONE retry (the KnE endpoint was seen
    truncating its chunked response mid-document; a second attempt
    usually completes). Returns the parsed object or None."""
    for _attempt in range(2):
        status, body = _arab_fetch(url, data=data)
        if status != 200 or not body:
            continue
        try:
            return json.loads(body)
        except ValueError:
            continue
    return None


def _arab_candidate(title, url, index, authors=None, year=None,
                    venue="", doi=None, abstract=""):
    return {"title": (title or "").strip(),
            "authors": [a for a in (authors or []) if a],
            "year": year, "venue": venue or "", "doi": doi,
            "url": url or "", "abstract": abstract or "",
            "arxiv_id": None, "index": index}


def _arab_year(text):
    m = re.search(r"(19|20)\d{2}", str(text or ""))
    return int(m.group(0)) if m else None


def _arab_strip_tags(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def _arab_imist(query):
    """Morocco PRSM/IMIST JSON API (probed live 2026-11-26: HTTP 200,
    {total, results:[{article_title, abstract, authors,
    publication_date, article_url, journal_title}]})."""
    qs = urllib.parse.urlencode(
        [("criteria_field", "all_text"), ("criteria_value", query),
         ("criteria_operator", "OR"), ("page", "1"),
         ("page_size", str(ARAB_PER_PLATFORM_CAP))])
    data = _arab_json("https://prsm.imist.ma/api/search?" + qs)
    if not isinstance(data, dict):
        return []
    out = []
    for r in data.get("results", []) or []:
        if not isinstance(r, dict):
            continue
        authors = [a.strip() for a in
                   str(r.get("authors") or "").split(";") if a.strip()]
        out.append(_arab_candidate(
            r.get("article_title"), r.get("article_url"), "arab:imist",
            authors=authors[:6],
            year=_arab_year(r.get("publication_date")),
            venue=r.get("journal_title") or "PRSM (IMIST)",
            abstract=str(r.get("abstract") or "")[:1500]))
    return out


def _kne_docs_from_body(body):
    """KnE docs from a response body. Full JSON parse first; when the
    body was truncated mid-stream (seen live: the endpoint cuts its
    chunked response, sometimes at 8 KB), salvage every COMPLETE doc
    object inside the "docs" array with a brace-matching scan."""
    if not body:
        return []
    try:
        data = json.loads(body)
        docs = (((data.get("data") or {}).get("results") or {})
                .get("docs")) or []
        if docs:
            return docs
    except (ValueError, AttributeError):
        pass
    anchor = body.find('"docs"')
    if anchor < 0:
        return []
    start = body.find("[", anchor)
    if start < 0:
        return []
    docs, depth, in_str, esc, obj_start = [], 0, False, False, None
    for i in range(start + 1, len(body)):
        ch = body[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    docs.append(json.loads(body[obj_start:i + 1]))
                except ValueError:
                    pass
                obj_start = None
        elif ch == "]" and depth == 0:
            break
    return docs


def _arab_kne(query):
    """UAE KnE Publishing search API (probed live 2026-11-26: POST
    admin-ajax action search_publications_api -> {success, data:
    {results: {numFound, docs: [{title (may carry <mark> tags),
    abstract, authors, publicationYear, journalTitle, doi,
    fullTextUrl}]}}})."""
    payload = urllib.parse.urlencode(
        {"action": "search_publications_api", "currentPage": "1",
         "searchTerm": query}).encode("utf-8")
    docs = []
    for _attempt in range(2):
        status, body = _arab_fetch(
            "https://kneopen.com/wp-admin/admin-ajax.php", data=payload)
        if status != 200 or not body:
            continue
        docs = _kne_docs_from_body(body)
        if docs:
            break
    out = []
    for d in docs[:ARAB_PER_PLATFORM_CAP]:
        if not isinstance(d, dict):
            continue
        year = d.get("publicationYear")
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        out.append(_arab_candidate(
            _arab_strip_tags(d.get("title")),
            d.get("fullTextUrl"), "arab:kne",
            authors=[str(a).strip() for a in (d.get("authors") or [])
                     if str(a).strip()],
            year=year,
            venue=d.get("journalTitle") or "KnE Publishing",
            doi=(str(d.get("doi") or "").strip() or None),
            abstract=_arab_strip_tags(d.get("abstract"))[:1500]))
    return out


def _parse_ojs_search_results(page, index, venue_fallback):
    """OJS 3 search-results markup: one obj_article_summary block per
    hit, title anchor -> /article/view/<id>, authors and publication
    date in the meta divs that follow the anchor."""
    out = []
    for block in re.split(r'<div class="obj_article_summary">', page)[1:]:
        m = re.search(r'<a[^>]+href="([^"]*/article/view/\d+[^"]*)"[^>]*>'
                      r'(.*?)</a>', block, re.S)
        if not m:
            continue
        url, inner = m.group(1), m.group(2)
        inner = re.sub(r'<span class="subtitle">.*?</span>', " ", inner,
                       flags=re.S)
        title = _arab_strip_tags(inner)
        if not title:
            continue
        authors = []
        am = re.search(r'<div class="authors">(.*?)</div>', block, re.S)
        if am:
            authors = [a.strip() for a in re.split(
                r"[,،]", _arab_strip_tags(am.group(1))) if a.strip()]
        pm = re.search(r'<div class="published">(.*?)</div>', block, re.S)
        venue = venue_fallback
        sm = re.search(r'<span class="subtitle">(.*?)</span>',
                       m.group(2), re.S)
        if sm:
            venue = _arab_strip_tags(sm.group(1)) or venue_fallback
        out.append(_arab_candidate(
            title, html_lib.unescape(url), index, authors=authors[:6],
            year=_arab_year(_arab_strip_tags(pm.group(1))) if pm else None,
            venue=venue))
        if len(out) >= ARAB_PER_PLATFORM_CAP:
            break
    return out


def _arab_dirasat(query):
    """Jordan Dirasat OJS site-wide search (probed live 2026-11-26:
    HTTP 200 with real obj_article_summary result blocks)."""
    url = ("https://dsr.ju.edu.jo/index.php/index/search/search?query="
           + urllib.parse.quote(query))
    status, body = _arab_fetch(url)
    if status != 200 or not body:
        return []
    return _parse_ojs_search_results(body, "arab:dirasat",
                                     "Dirasat (University of Jordan)")


def _parse_najah_results(page):
    """An-Najah result cards: journal-article-card blocks whose title
    anchor points at /journal/<slug>/.../article/<id>/; a date chip
    (Online <span>YYYY-MM-DD</span>) and /authors/<id>/ links may
    appear inside the same card."""
    out = []
    for block in re.split(r'<article class="journal-article-card">',
                          page)[1:]:
        m = re.search(r'<h3 class="journal-article-card-title">\s*'
                      r'<a href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        url = urllib.parse.urljoin("https://journals.najah.edu/",
                                   html_lib.unescape(m.group(1)))
        title = _arab_strip_tags(m.group(2))
        if not title:
            continue
        authors = []
        for am in re.finditer(r'<a href="/authors/\d+/"[^>]*>(.*?)</a>',
                              block, re.S):
            name = _arab_strip_tags(am.group(1))
            if name and name not in authors:
                authors.append(name)
        dm = re.search(r'(\d{4})-\d{2}-\d{2}', block)
        out.append(_arab_candidate(
            title, url, "arab:najah", authors=authors[:6],
            year=int(dm.group(1)) if dm else None,
            venue="An-Najah Journals"))
        if len(out) >= 3:
            break
    return out


def _arab_najah(query):
    """Palestine An-Najah per-journal search (probed live 2026-11-26
    on anujr-b: HTTP 200 with real journal-article-card results)."""
    out = []
    for slug in _NAJAH_JOURNALS:
        url = (f"https://journals.najah.edu/journal/{slug}/search/?q="
               + urllib.parse.quote(query))
        try:
            status, body = _arab_fetch(url)
            if status == 200 and body:
                out.extend(_parse_najah_results(body))
        except Exception as e:
            print(f"arab:najah {slug} failed: {e}")
        if len(out) >= ARAB_PER_PLATFORM_CAP:
            break
    return out[:ARAB_PER_PLATFORM_CAP]


_ARAB_PLATFORM_SEARCHERS = [("imist", _arab_imist), ("kne", _arab_kne),
                            ("dirasat", _arab_dirasat),
                            ("najah", _arab_najah)]


def search_arab_platforms(query):
    """§23: search the wired Arab-country journal platforms (IMIST,
    KnE, Dirasat, An-Najah). Per-platform fail-soft, capped at
    ARAB_PER_PLATFORM_CAP per platform and ARAB_TOTAL_CAP overall.
    ASJP, IASJ, EKB and QScience are registry-only (see the §23
    header comment) and are not queried here."""
    out = []
    for name, fn in _ARAB_PLATFORM_SEARCHERS:
        try:
            found = fn(query) or []
        except Exception as e:
            print(f"arab:{name} failed for query {query!r}: {e}")
            continue
        out.extend(found)
        if len(out) >= ARAB_TOTAL_CAP:
            break
    return out[:ARAB_TOTAL_CAP]


# --------------------------------------------------------------------------
# Asian-country journal platforms (§24) — open access, no keys.
# The verified national/regional platforms are registered in
# asian_journals_registry.json (§17 wiring: credibility/priority
# signal only, never verification). Five of them also have a live
# keyword-search route that was probed from the build sandbox on
# 2026-11-26 and seen answering with real results, AND a returned
# item's FULL TEXT was verified reachable (Jupiter's full-text-only
# law, 2026-11-26 — abstracts/metadata alone never count as wired);
# only those five are searched here:
#   GARUDA (Indonesia) — national portal documents search
#       GET garuda.kemdiktisaintek.go.id/documents?q=<q>
#           (title-article result blocks; full text via the detail
#           page's publisher PDF link — verified %PDF 2026-11-26)
#   NepJOL (Nepal), MongoliaJOL (Mongolia), VJOL (Vietnam,
#   vjol.info.vn only) and the Kazakhstan National Academy of
#   Sciences journals — OJS site-wide search
#       GET <host>/index.php/index/search/search?query=<q>
#           (shared _parse_ojs_search_results from §23; full text
#           via the OJS galley download /article/download/<id>/<g>
#           — verified application/pdf, %PDF magic, 2026-11-26)
# Registry-only (no machine-usable keyword route answered the
# probe, or full text could not be verified reachable): BanglaJOL
# (Bangladesh — OJS search answers with real results, but its
# galley PDFs repeatedly timed out / dropped from the probe
# sandbox, so full-text reachability is UNVERIFIED and it stays
# registry-only under the full-text-only law); J-STAGE (Japan —
# JavaScript-built search form; plain GETs
# answered HTTP 500 / an error redirect, no public API/OAI);
# SciOpen and COAJ (China — JavaScript app shells, /search 404 /
# 401); KoreaScience (South Korea — site answers but the search is
# JavaScript-driven, candidate routes 404); CyberLeninka (Russia —
# /api/search rejected plain GET 405 / form POST 400; its OAI-PMH
# endpoint answers but is harvest-only, not keyword search);
# DergiPark (Turkey — web search redirects to a human-verification
# page; OAI-PMH answers but is harvest-only); SID (Iran — Cloudflare
# challenge pages); NOPR (India), MyJurnal (Malaysia) and PhilJOL
# (Philippines — servers closed every connection); PakMediNet
# (Pakistan — HTTP 403 blocked) and SLJOL (Sri Lanka — HTTP 403);
# ThaiJO (Thailand — JavaScript portal, per-journal OJS hosts, no
# single site-wide endpoint answered); Tajikistan NAS (static
# listing sites, no article search); the HEC list (Pakistan) and
# the MSRT portal (Iran) are official lists, not article platforms.
# Transport reuses the §23 single-shot fetch (a dead platform must
# not eat the retrieval wave's budget) and the §23 candidate shape.
# Every platform call is fail-soft; candidates flow through the
# UNCHANGED verification, screening, judges and text-first gate,
# and the §18 intake language gate applies as to every source.
# --------------------------------------------------------------------------

ASIA_PER_PLATFORM_CAP = 8
ASIA_TOTAL_CAP = 24


def _asia_ojs_search(base_url, query, index, venue):
    """One OJS site-wide search (Journals Online family, VJOL,
    Kazakhstan NAS): GET <base>/index.php/index/search/search."""
    url = (base_url + "/index.php/index/search/search?query="
           + urllib.parse.quote(query))
    status, body = _arab_fetch(url)
    if status != 200 or not body:
        return []
    return _parse_ojs_search_results(body, index, venue)


def _parse_garuda_results(page):
    """GARUDA result items: a title-article anchor (title inside an
    <xmp>) followed by author-article anchors and a subtitle-article
    source line carrying the journal, volume and year."""
    out = []
    for block in re.split(r'<a class="title-article"', page)[1:]:
        m = re.match(r'\s+href="([^"]+)"[^>]*>\s*<xmp>(.*?)</xmp>',
                     block, re.S)
        if not m:
            continue
        url = urllib.parse.urljoin(
            "https://garuda.kemdiktisaintek.go.id",
            html_lib.unescape(m.group(1)))
        title = _arab_strip_tags(m.group(2))
        if not title:
            continue
        authors = []
        for am in re.finditer(
                r'<a class="author-article"[^>]*><xmp>(.*?)</xmp>',
                block, re.S):
            name = _arab_strip_tags(am.group(1))
            if name and name not in authors:
                authors.append(name)
        venue = "GARUDA (Indonesia)"
        year = None
        sm = re.search(r'<xmp class="subtitle-article">(.*?)</xmp>',
                       block, re.S)
        if sm:
            line = _arab_strip_tags(sm.group(1))
            if line:
                venue = line
                year = _arab_year(line)
        out.append(_arab_candidate(
            title, url, "asia:garuda", authors=authors[:6],
            year=year, venue=venue))
        if len(out) >= ASIA_PER_PLATFORM_CAP:
            break
    return out


def _asia_garuda(query):
    """Indonesia GARUDA documents search (probed live 2026-11-26:
    HTTP 200 with real title-article result items)."""
    url = ("https://garuda.kemdiktisaintek.go.id/documents?q="
           + urllib.parse.quote(query))
    status, body = _arab_fetch(url)
    if status != 200 or not body:
        return []
    return _parse_garuda_results(body)


_ASIA_OJS_PLATFORMS = [
    ("nepjol", "https://www.nepjol.info", "asia:nepjol",
     "NepJOL (Nepal Journals Online)"),
    ("mongoliajol", "https://www.mongoliajol.info", "asia:mongoliajol",
     "MongoliaJOL (Mongolia Journals Online)"),
    ("vjol", "https://vjol.info.vn", "asia:vjol",
     "VJOL (Vietnam Journals Online)"),
    ("kaznas", "https://journals.nauka-nanrk.kz", "asia:kaznas",
     "National Academy of Sciences of Kazakhstan journals"),
]


def _asia_ojs_platform(name, base_url, index, venue):
    def run(query):
        return _asia_ojs_search(base_url, query, index, venue)
    return run


_ASIA_PLATFORM_SEARCHERS = (
    [("garuda", _asia_garuda)]
    + [(name, _asia_ojs_platform(name, base, index, venue))
       for name, base, index, venue in _ASIA_OJS_PLATFORMS])


def search_asian_platforms(query):
    """§24: search the wired Asian-country journal platforms
    (GARUDA + the OJS platforms NepJOL, MongoliaJOL, VJOL and the
    Kazakhstan NAS journals — each with full text verified
    reachable, see the §24 header comment). Per-platform fail-soft,
    capped at ASIA_PER_PLATFORM_CAP per platform and
    ASIA_TOTAL_CAP overall. The remaining verified platforms are
    registry-only (see the §24 header comment) and are not queried
    here."""
    out = []
    for name, fn in _ASIA_PLATFORM_SEARCHERS:
        try:
            found = fn(query) or []
        except Exception as e:
            print(f"asia:{name} failed for query {query!r}: {e}")
            continue
        out.extend(found)
        if len(out) >= ASIA_TOTAL_CAP:
            break
    return out[:ASIA_TOTAL_CAP]


# --------------------------------------------------------------------------
# Arabic digital libraries + Iraq/Qatar alternate routes (§25) —
# open access, no keys. The libraries are registered in
# arab_libraries_registry.json (§17 wiring: credibility/priority
# signal only, never verification). Jupiter's full-text-only law
# (2026-11-26) governs this section: a route is wired ONLY when the
# probe saw its search answer with real results AND a returned
# item's FULL TEXT was verified reachable — abstracts/metadata
# alone never count. Probed live from the build sandbox on
# 2026-11-26:
#   Shamela (المكتبة الشاملة) — the site's own content search
#       POST shamela.ws/ajax/search {term: <q>} -> HTML fragment of
#       <article class="srch-result"> hits, each linking the exact
#       book page shamela.ws/book/<id>/<page>; a returned book page
#       opened with its full text (the nass body) readable — full
#       text verified. Book candidates (primary-source material).
#       Transport intermittently truncates (IncompleteRead); the
#       fetch salvages partial bodies and retries once. From the
#       build sandbox the CDN also intermittently closes the body
#       stream to Python clients outright (HTTP 200, empty body)
#       for stretches of time, while curl — and urllib in other
#       windows — succeed; the searcher uses a TLS-1.2-capped
#       context (which measurably helped) and fail-soft absorbs
#       a bad window.
#   Iraq alternate route — IASJ itself closed every connection
#       (site, OAI-PMH, alternate host), so Iraqi journal content
#       is reached through the Iraqi Journal of Science
#       (University of Baghdad) OJS site search directly:
#       GET ijs.uobaghdad.edu.iq/index.php/eijs/search/search
#           ?query=<q> (shared _parse_ojs_search_results; a returned
#           article's galley PDF was verified: %PDF-1.7, ~1.16 MB).
#       Its OAI-PMH Identify answers too.
#   Qatar alternate route — QScience (HBKU Press) is a JavaScript
#       SPA with no searchable route and a bot wall (403) from the
#       sandbox, but its journals are Crossref-registered diamond
#       OA (DOI prefix 10.5339): the Crossref works API filtered
#       by the journals' online ISSNs (Qatar Medical Journal
#       2227-0426 — 1,515 works under HBKU Press in the probe;
#       QScience Connect 2223-506X) returns its articles, whose
#       full texts are open at their DOI targets.
# Registry-only (routes exhausted per the multi-route rule —
# site search, public API, OAI-PMH, alternate hosts/mirrors,
# sitemap/feeds and index fallbacks all tried; see DESIGN.md §25):
#   Al-Mustafa (al-mostafa.com) — search answers and returns
#       direct PDF links to primary works, but the file server's
#       HTTPS is broken from the sandbox (empty reply / TLS
#       wrong-signature across curl, urllib, wget, all TLS
#       variants), so full-text delivery is UNVERIFIED;
#   Waqfeya — Next.js client-side app, no machine search route
#       (its book pages link full PDFs on archive.org — the WAQ
#       collection — already covered by the IA searcher);
#   Noor Book and Hindawi — Cloudflare challenge (HTTP 403) on
#       every route from the sandbox;
#   ASJP (Algeria) — every connection closed (site search, OAI on
#       two paths, http/https, www/no-www); EKB (Egypt) — HTTP
#       429 "Access Restricted" on journal subdomains incl. OAI.
# Every call is fail-soft and single-shot (the shared _arab_fetch);
# candidates carry the usual shape and flow through the UNCHANGED
# verification, screening, judges and text-first gate.
# --------------------------------------------------------------------------

LIB_PER_SOURCE_CAP = 8
LIB_TOTAL_CAP = 24
_QSCIENCE_ISSNS = ("2227-0426", "2223-506X")


def _parse_shamela_results(page):
    """Shamela ajax/search fragment: one <article
    class="srch-result"> per hit; .srch-result-title carries
    <a href=".../book/<id>/<page>">book title</a> plus [author]
    and (volume, page); .srch-snippet carries the matched-text
    excerpt. Candidates are BOOK candidates (the book page is the
    full text), shaped like the Internet Archive book candidates."""
    out = []
    for block in re.split(r'<article class="srch-result">',
                          page or "")[1:]:
        m = re.search(r'srch-result-title[^>]*>(.*?)</div>',
                      block, re.S)
        if not m:
            continue
        a = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                      m.group(1), re.S)
        if not a:
            continue
        title = _arab_strip_tags(a.group(2))
        if not title:
            continue
        authors = []
        am = re.search(r"\[([^\]]+)\]",
                       _arab_strip_tags(m.group(1)))
        if am and am.group(1).strip():
            authors = [am.group(1).strip()]
            title = re.sub(r"\s*\[[^\]]*\]\s*$", "", title).strip()
        sm = re.search(r'srch-snippet[^>]*>(.*?)</div>', block, re.S)
        out.append(_arab_candidate(
            title, html_lib.unescape(a.group(1)), "library:shamela",
            authors=authors[:3], venue="Al-Shamela Library (book)",
            abstract=_arab_strip_tags(sm.group(1))[:1500] if sm
            else ""))
        if len(out) >= LIB_PER_SOURCE_CAP:
            break
    return out


def _shamela_fetch(url, data=None):
    """Single-shot fetch for Shamela with a TLS-1.2-capped
    context: the site's CDN intermittently closes the body
    stream to the default Python TLS handshake (probed live
    2026-11-26: default context -> HTTP 200 with an empty body
    or RemoteDisconnected, while a TLS-1.2-capped ALPN context
    returned the full fragment). Returns (status, text);
    (None, "") on transport error; partial bodies are salvaged
    exactly like _arab_fetch."""
    import http.client
    import ssl
    headers = dict(_ARAB_HEADERS)
    headers["X-Requested-With"] = "XMLHttpRequest"
    headers["Referer"] = "https://shamela.ws/search"
    try:
        _pace(urllib.parse.urlparse(url).netloc)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        try:
            ctx.set_alpn_protocols(["http/1.1"])
        except Exception:
            pass
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=_ARAB_TIMEOUT,
                                    context=ctx) as resp:
            try:
                raw = resp.read(1_500_000)
            except http.client.IncompleteRead as e:
                raw = e.partial or b""
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(300_000).decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception:
        return None, ""


def _lib_shamela(query):
    """Shamela content search (probed live 2026-11-26: POST
    /ajax/search {term} -> HTTP 200 fragment with real book-page
    hits; the endpoint intermittently truncates its response, so
    one retry is allowed — _shamela_fetch salvages partial
    bodies)."""
    payload = urllib.parse.urlencode({"term": query}).encode("utf-8")
    for _attempt in range(2):
        status, body = _shamela_fetch("https://shamela.ws/ajax/search",
                                      data=payload)
        if status == 200 and "srch-result" in body:
            return _parse_shamela_results(body)
    return []


def _lib_iraq_ijs(query):
    """Iraq alternate route: Iraqi Journal of Science (University
    of Baghdad) OJS search (probed live 2026-11-26: HTTP 200 with
    20 real obj_article_summary blocks; galley PDF verified)."""
    url = ("https://ijs.uobaghdad.edu.iq/index.php/eijs/search/"
           "search?query=" + urllib.parse.quote(query))
    status, body = _arab_fetch(url)
    if status != 200 or not body:
        return []
    return _parse_ojs_search_results(
        body, "iraq:ijs",
        "Iraqi Journal of Science (University of Baghdad)")


def _parse_qscience_crossref(data):
    out = []
    for it in (data.get("message") or {}).get("items", []) or []:
        if not isinstance(it, dict):
            continue
        titles = it.get("title") or []
        title = str(titles[0] if titles else "").strip()
        if not title:
            continue
        authors = []
        for a in it.get("author") or []:
            if not isinstance(a, dict):
                continue
            name = " ".join(x for x in
                            [str(a.get("given") or ""),
                             str(a.get("family") or "")] if x).strip()
            if name:
                authors.append(name)
        year = None
        for key in ("published", "issued", "created"):
            parts = ((it.get(key) or {}).get("date-parts")
                     or [[None]])[0]
            if parts and parts[0]:
                try:
                    year = int(parts[0])
                except (TypeError, ValueError):
                    year = None
                break
        doi = str(it.get("DOI") or "").strip() or None
        url = str(it.get("URL") or "").strip()
        if not url and doi:
            url = "https://doi.org/" + doi
        venue = str(((it.get("container-title") or [""])[0] or "")
                    ).strip() or "QScience (HBKU Press)"
        out.append(_arab_candidate(
            title, url, "qatar:qscience-crossref",
            authors=authors[:6], year=year, venue=venue, doi=doi))
        if len(out) >= LIB_PER_SOURCE_CAP:
            break
    return out


def _lib_qscience_crossref(query):
    """Qatar alternate route: Crossref works API filtered by the
    online ISSNs of QScience (HBKU Press) journals — Qatar
    Medical Journal 2227-0426 (probe: 1,515 HBKU Press works) and
    QScience Connect 2223-506X. QScience itself is a JS SPA with
    no searchable route; its articles are diamond OA at their
    DOI targets."""
    out = []
    for issn in _QSCIENCE_ISSNS:
        qs = urllib.parse.urlencode(
            [("query", query), ("filter", "issn:" + issn),
             ("rows", str(LIB_PER_SOURCE_CAP)),
             ("select", "DOI,title,author,published,URL,"
                        "container-title")])
        data = _arab_json("https://api.crossref.org/works?" + qs)
        if isinstance(data, dict):
            out.extend(_parse_qscience_crossref(data))
        if len(out) >= LIB_PER_SOURCE_CAP:
            break
    return out[:LIB_PER_SOURCE_CAP]


_LIB_SEARCHERS = [("shamela", _lib_shamela),
                  ("iraq-ijs", _lib_iraq_ijs),
                  ("qscience-crossref", _lib_qscience_crossref)]


def search_arab_libraries(query):
    """§25: search the wired Arabic digital libraries and the
    Iraq/Qatar alternate routes (Shamela books, Iraqi Journal of
    Science OJS, QScience via Crossref ISSN filter — each with
    full text verified reachable, see the §25 header comment).
    Per-source fail-soft, capped at LIB_PER_SOURCE_CAP per source
    and LIB_TOTAL_CAP overall. Al-Mustafa, Waqfeya, Noor Book and
    Hindawi are registry-only (see the §25 header comment) and
    are not queried here."""
    out = []
    for name, fn in _LIB_SEARCHERS:
        try:
            found = fn(query) or []
        except Exception as e:
            print(f"library:{name} failed for query {query!r}: {e}")
            continue
        out.extend(found)
        if len(out) >= LIB_TOTAL_CAP:
            break
    return out[:LIB_TOTAL_CAP]


# --------------------------------------------------------------------------
# European-country journal platforms (§26) — open access, no keys.
# The verified national platforms are registered in
# european_journals_registry.json (§17 wiring: credibility/priority
# signal only, never verification). Jupiter's full-text-only law
# (2026-11-26) governs this section: a platform is wired ONLY when
# the probe saw its keyword search answer with real results AND a
# returned item's FULL TEXT was verified reachable — abstracts or
# metadata alone never count. Probed live from the build sandbox
# on 2026-11-26, multiple routes per platform:
#   Biblioteka Nauki (Poland) — the platform's public JSON API
#       POST bibliotekanauki.pl/api/search {searchCriteria:
#       {generalSearchString, openResources:true},
#       paginationCriteria} -> real documents (totalResults 2,760
#       for "earthquake"); only documents carrying a PDF in
#       fullTextFormats and not accessRestricted are emitted, and
#       a returned item's PDF was verified (GET
#       /articles/64160793.pdf -> application/pdf, %PDF-1.4).
#   Persée (France) — GET persee.fr/search?ta=article&q=<q> ->
#       real doc-result items (31,426 for "tremblement"); full
#       text is page-based OCR HTML: the document page links its
#       text pages at /doc/page/<docid>/<pageid> and a returned
#       item's first page opened with the article's full text
#       readable (the /docAsPDF/ PDF endpoint is altcha-protected
#       and is not used).
#   E-Periodica (Switzerland) — GET e-periodica.ch/search?q=<q>
#       -> real ep-result article items; a returned item's issue
#       OCR full text was verified (GET
#       /download/sbz-002:1931:97/ocr-fulltext -> text/plain,
#       ~2 MB of real OCR text). Some recent items carry a
#       moving-wall embargo; they fail the unchanged gates.
#   tidsskrift.dk (Denmark) — OJS site-wide search (shared §23
#       parser); a returned article's galley PDF was verified
#       (application/pdf, %PDF-1.4).
# Registry-only (routes exhausted per the multi-route rule; see
# DESIGN.md §26): Hrčak (Croatia — search answers HTTP 418
# "Confirm you're not a robot"; OAI-PMH harvest-only); Journal.fi
# (Finland — anti-bot proof-of-work challenge on search; OAI-PMH
# harvest-only); OpenJournals.nl (Netherlands — WordPress portal,
# no platform-wide search, journals on separate hosts);
# OpenEdition (France — anti-bot challenge); SCIndeks (Serbia —
# stateful ASP.NET search, plain GETs report no records; OAI-PMH
# harvest-only); RCAAP (Portugal — anti-bot challenge); Dialnet
# (Spain — search answers with real results but every follow-up
# connection, article page / PDF download / external link, was
# dropped from the sandbox, so full text is UNVERIFIED); EKT
# ePublishing (Greece — platform search HTTP 500, per-journal OJS
# only); SSOAR and BASE (Germany — DSpace app shell on every
# route / anti-bot challenge); DiVA (Sweden — stateful JSF
# search); Idunn (Norway — HTTP 403, partly OA); ETERA (Estonia —
# JS app shell); OUCI (Ukraine — HTTP 403; an index, and Crossref
# is already queried directly); Vernadsky periodicals (Ukraine —
# legacy frameset catalogue); REAL (Hungary — EPrints search 302
# loop; OAI-PMH harvest-only); SciELO Portugal (search error /
# global search 403); RACO (Spain — HTTP 403); DML-CZ (Czechia —
# mathematics-only subject library, no general national portal).
# Belgium, Austria, Ireland, Italy, Romania and Bulgaria have no
# confirmed national OA portal and get no entries (no fabrication).
# Transport reuses the §23 single-shot fetch and candidate shape;
# every platform call is fail-soft; candidates flow through the
# UNCHANGED verification, screening, judges and text-first gate.
# --------------------------------------------------------------------------

EURO_PER_PLATFORM_CAP = 8
EURO_TOTAL_CAP = 24
_BN_KIND_PATH = {"ARTICLE": "articles", "BOOK": "books",
                 "CHAPTER": "chapters"}


def _euro_post_json(url, payload):
    """One single-shot JSON POST for the Biblioteka Nauki API (no
    retries on the http_request ladder: a dead platform must not
    eat the retrieval wave's budget), with ONE retry like
    _arab_json. Returns the parsed object or None."""
    import http.client
    body = json.dumps(payload).encode("utf-8")
    for _attempt in range(2):
        try:
            _pace(urllib.parse.urlparse(url).netloc)
            headers = dict(_ARAB_HEADERS)
            headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=body,
                                         headers=headers)
            with urllib.request.urlopen(req,
                                        timeout=_ARAB_TIMEOUT) as resp:
                try:
                    raw = resp.read(3_000_000)
                except http.client.IncompleteRead as e:
                    raw = e.partial or b""
                if resp.status != 200:
                    continue
                return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
    return None


def _parse_bibliotekanauki(data):
    """Biblioteka Nauki /api/search documents. Full-text-only law:
    only documents whose fullTextFormats carry a PDF and that are
    not access-restricted are emitted; the candidate URL is the
    verified PDF route /<kind>/<id>.pdf."""
    out = []
    for doc in (data or {}).get("documents", []) or []:
        if not isinstance(doc, dict):
            continue
        if doc.get("accessRestricted"):
            continue
        formats = [str(f).upper() for f in
                   (doc.get("fullTextFormats") or [])]
        if "PDF" not in formats:
            continue
        kind = _BN_KIND_PATH.get(
            str(doc.get("publicationType") or "").upper())
        pid = doc.get("publicationId")
        if not kind or not pid:
            continue
        title = _arab_strip_tags(doc.get("mainTitle"))
        if not title:
            continue
        venue = (str(doc.get("journalTitle") or "").strip()
                 or str(doc.get("bookTitle") or "").strip()
                 or "Biblioteka Nauki")
        authors = [str(a).strip() for a in
                   (doc.get("contributors") or []) if str(a).strip()]
        out.append(_arab_candidate(
            title,
            f"https://bibliotekanauki.pl/{kind}/{pid}.pdf",
            "europe:bibliotekanauki", authors=authors[:6],
            year=_arab_year(doc.get("publishedDate")), venue=venue,
            abstract=_arab_strip_tags(doc.get("mainAbstract"))[:1500]))
        if len(out) >= EURO_PER_PLATFORM_CAP:
            break
    return out


def _euro_bibliotekanauki(query):
    """Poland Biblioteka Nauki public JSON API (probed live
    2026-11-26: HTTP 200, real documents, PDF full text verified
    at /articles/<id>.pdf)."""
    payload = {"searchCriteria": {
                   "generalSearchString": query,
                   "openResources": True, "publicationTypes": [],
                   "researchDataLink": False},
               "paginationCriteria": {
                   "pageNumber": 1,
                   "pageSize": EURO_PER_PLATFORM_CAP,
                   "sortingCriteria": {"fieldName": "score",
                                       "direction": "DESC"}}}
    data = _euro_post_json("https://bibliotekanauki.pl/api/search",
                           payload)
    if not isinstance(data, dict):
        return []
    return _parse_bibliotekanauki(data)


def _parse_persee_results(page):
    """Persée search results: one <div class="doc-result"> per hit;
    the title anchor (class "title ...") points at the document
    page /doc/<docid>, contributors sit in .contributors .name
    spans and the bibliographic reference in .documentBibRef."""
    out = []
    for block in re.split(r'<div class="doc-result">',
                          page or "")[1:]:
        m = re.search(r'<a href="(https://www\.persee\.fr/doc/[^"]+)"'
                      r' class="title[^"]*">(.*?)</a>', block, re.S)
        if not m:
            continue
        url = m.group(1).split("?")[0]
        title = _arab_strip_tags(m.group(2))
        if not title:
            continue
        authors = []
        cm = re.search(r'<div class="contributors">(.*?)</div>',
                       block, re.S)
        if cm:
            for nm in re.finditer(r'<span class="name">(.*?)</span>',
                                  cm.group(1), re.S):
                name = _arab_strip_tags(nm.group(1))
                if name and name not in authors:
                    authors.append(name)
        venue = "Persée"
        year = None
        bm = re.search(r'<div class="documentBibRef">(.*?)</div>',
                       block, re.S)
        if bm:
            vm = re.search(r'<a[^>]*>(.*?)</a>', bm.group(1), re.S)
            if vm:
                venue = _arab_strip_tags(vm.group(1)) or venue
            year = _arab_year(_arab_strip_tags(bm.group(1)))
        out.append(_arab_candidate(
            title, url, "europe:persee", authors=authors[:6],
            year=year, venue=venue))
        if len(out) >= EURO_PER_PLATFORM_CAP:
            break
    return out


def _euro_persee(query):
    """France Persée article search (probed live 2026-11-26:
    HTTP 200 with real doc-result items; full text verified as
    page-based OCR HTML via the document page's /doc/page/
    links)."""
    url = ("https://www.persee.fr/search?ta=article&q="
           + urllib.parse.quote(query))
    status, body = _arab_fetch(url)
    if status != 200 or not body:
        return []
    return _parse_persee_results(body)


def _parse_eperiodica_results(page):
    """E-Periodica search results: one ep-results__item per
    article; .ep-result__magazine carries journal + year,
    .ep-result__title the article anchor (/view/<pid>),
    .ep-result__authors the authors and the first
    .ep-result__page__text the matched OCR snippet."""
    out = []
    for block in re.split(r'<div class="ep-results__item ',
                          page or "")[1:]:
        m = re.search(r'<h2 class="ep-result__title">\s*'
                      r'<a href="([^"]+)"[^>]*>(.*?)</a>',
                      block, re.S)
        if not m:
            continue
        url = urllib.parse.urljoin("https://www.e-periodica.ch",
                                   html_lib.unescape(m.group(1)))
        title = _arab_strip_tags(m.group(2))
        if not title:
            continue
        venue = "E-Periodica"
        year = None
        mm = re.search(r'<p class="ep-result__magazine">(.*?)</p>',
                       block, re.S)
        if mm:
            line = _arab_strip_tags(mm.group(1))
            if line:
                venue = line.split("—")[0].strip() or venue
                year = _arab_year(line)
        authors = []
        am = re.search(r'<p class="ep-result__authors">(.*?)</p>',
                       block, re.S)
        if am:
            authors = [a.strip() for a in re.split(
                r"\s/\s", _arab_strip_tags(am.group(1))) if a.strip()]
        abstract = ""
        sm = re.search(r'<p class="ep-result__page__text">(.*?)</p>',
                       block, re.S)
        if sm:
            abstract = _arab_strip_tags(sm.group(1))[:1500]
        out.append(_arab_candidate(
            title, url, "europe:eperiodica", authors=authors[:6],
            year=year, venue=venue, abstract=abstract))
        if len(out) >= EURO_PER_PLATFORM_CAP:
            break
    return out


def _euro_eperiodica(query):
    """Switzerland E-Periodica full-text search (probed live
    2026-11-26: HTTP 200 with real ep-result items; a returned
    item's issue OCR full text verified reachable)."""
    url = ("https://www.e-periodica.ch/search?q="
           + urllib.parse.quote(query))
    status, body = _arab_fetch(url)
    if status != 200 or not body:
        return []
    return _parse_eperiodica_results(body)


def _euro_tidsskrift(query):
    """Denmark tidsskrift.dk OJS site-wide search (probed live
    2026-11-26: HTTP 200 with real obj_article_summary results;
    a returned article's galley PDF verified %PDF)."""
    return _asia_ojs_search("https://tidsskrift.dk", query,
                            "europe:tidsskrift",
                            "tidsskrift.dk (Danish journals)")


_EURO_PLATFORM_SEARCHERS = [("bibliotekanauki", _euro_bibliotekanauki),
                            ("persee", _euro_persee),
                            ("eperiodica", _euro_eperiodica),
                            ("tidsskrift", _euro_tidsskrift)]


def search_european_platforms(query):
    """§26: search the wired European-country journal platforms
    (Biblioteka Nauki, Persée, E-Periodica, tidsskrift.dk — each
    with full text verified reachable, see the §26 header
    comment). Per-platform fail-soft, capped at
    EURO_PER_PLATFORM_CAP per platform and EURO_TOTAL_CAP
    overall. The remaining verified platforms are registry-only
    (see the §26 header comment) and are not queried here."""
    out = []
    for name, fn in _EURO_PLATFORM_SEARCHERS:
        try:
            found = fn(query) or []
        except Exception as e:
            print(f"europe:{name} failed for query {query!r}: {e}")
            continue
        out.extend(found)
        if len(out) >= EURO_TOTAL_CAP:
            break
    return out[:EURO_TOTAL_CAP]


# ---------------------------------------------------------------------------
# §27 — World free digital libraries (Jupiter 2026-11-26: «کتابخانه های
# اینترنتی رایگان دنیا رو آنهایی را که نداریم پیدا کن و اضافه کن»).
#
# Probe-first, same laws as §23–§25: a library is wired LIVE only when a
# live probe proved BOTH that its search answers with real results AND
# that a returned item's FULL TEXT is reachable (plain text / full HTML /
# page text / PDF); no key or registration anywhere; every other probed
# library sits in world_libraries_registry.json as registry-only with the
# evidence recorded in its notes. Full probe table:
# files/world-digital-libraries.md.
#
# LIVE-WIRED (6):
#   * Project Gutenberg — search via the public Gutendex catalogue API
#     (no key); candidates carry the book's text/plain URL on
#     gutenberg.org (the identical files are served by the official PG
#     mirror network; gutenberg.org itself showed a maintenance 503
#     during the probe window).
#   * Wikisource (English edition) — MediaWiki API list=search (no key);
#     the wiki page itself IS the full text.
#   * SciELO Books — the site's own /search/?q= route (no key); book
#     pages link full-text PDFs (%PDF verified in the probe).
#   * MEK (Hungarian Electronic Library) — the site's full-search POST
#     form (no key); item pages link full-text PDFs (%PDF verified).
#   * Faded Page (personal/volunteer, DP Canada) — its catalogue JSON
#     endpoint csearc2.php via POST (no key); candidates carry the
#     book's plain-text file URL (whole-book .txt verified).
#   * The Anarchist Library (volunteer collective) — /search HTML
#     (no key); the text pages are the full text in HTML.
# PERSONAL libraries registry-only: lib.ru (Moshkov — every search
# route 404/500), sacred-texts.com (403), Project Gutenberg Australia
# + Project Runeberg + Global Grey (search is Google site search /
# CSE), Marxists Internet Archive (search page 404, external search),
# Aozora Bunko (no web search route), Standard Ebooks (catalogue
# query ignored server-side; EPUB downloads only). Shadow libraries
# (LibGen / Z-Library / Sci-Hub class) are NOT registered and NOT
# wired: they follow the Anna's Archive precedent — metadata-only
# status, never cited, never a full-text source.
# REGISTRY-ONLY (31 total, evidence in the registry notes): Gallica/BnF,
# HathiTrust, Qatar Digital Library, Cervantes Virtual and BDH (403 from
# the sandbox), Perseus (Hopper 403 + Scaife JS shell), Canadiana (search
# answers but items are IIIF image viewers, no text route verified),
# Library of Congress (unreachable from the sandbox), OAPEN + DOAB (OAI
# harvest-only / JS search), NDL Japan (429 + restricted full text),
# NDLI (unreachable + login wall), Russian Presidential Library (429 +
# reading-room restrictions), Shodhganga (anti-bot wall), ANNO + dLOC
# (JS-shell search), Project Runeberg (search is a Google site:
# redirect), DigitalNZ (API answers without a key but its records are
# metadata-only pointers), and the key-walled: Europeana, DPLA, Trove,
# CORE, Deutsche Digitale Bibliothek, Chinese Text Project.
#
# Candidates reuse _arab_candidate and the §25 fetch discipline
# (single-shot, short timeout, fail-soft per source). Gutendex can be
# slow, so this section's fetches get a bounded 20s timeout.

_WLIB_TIMEOUT = 20


def _wlib_fetch(url, data=None, timeout=_WLIB_TIMEOUT):
    """§27 single-shot fetch, mirroring _arab_fetch (including the
    IncompleteRead salvage) with this section's bounded timeout.
    Returns (status, text); (None, "") on any transport error."""
    import http.client
    try:
        _pace(urllib.parse.urlparse(url).netloc)
        req = urllib.request.Request(url, data=data,
                                     headers=_ARAB_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                raw = resp.read(1_500_000)
            except http.client.IncompleteRead as e:
                raw = e.partial or b""
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(300_000).decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception:
        return None, ""


def _wlib_json(url):
    """Fetch + JSON-parse with ONE retry, like _arab_json. Returns the
    parsed object or None (fail-soft)."""
    for _attempt in range(2):
        status, body = _wlib_fetch(url)
        if status != 200 or not body:
            continue
        try:
            return json.loads(body)
        except ValueError:
            continue
    return None


def _parse_gutendex(data):
    """Gutendex (Project Gutenberg catalogue) JSON -> candidates.

    A book counts only when it carries a plain-text format: the
    candidate URL is the book's whole text file on gutenberg.org.
    """
    out = []
    for book in (data or {}).get("results") or []:
        if not isinstance(book, dict):
            continue
        title = str(book.get("title") or "").strip()
        formats = book.get("formats") or {}
        url = ""
        for key in ("text/plain; charset=utf-8",
                    "text/plain; charset=us-ascii", "text/plain"):
            if formats.get(key):
                url = formats[key]
                break
        if not url:
            for key, value in formats.items():
                if key.startswith("text/plain") and value:
                    url = value
                    break
        if not title or not url:
            continue
        authors = [str(a.get("name") or "").strip()
                   for a in book.get("authors") or []
                   if isinstance(a, dict)]
        subjects = [str(s).strip() for s in book.get("subjects") or []
                    if str(s).strip()]
        out.append(_arab_candidate(
            title, url, "library:gutenberg",
            authors=[a for a in authors if a][:6],
            venue="Project Gutenberg (book)",
            abstract="; ".join(subjects)[:1000]))
        if len(out) >= LIB_PER_SOURCE_CAP:
            break
    return out


def _lib_gutenberg(query):
    data = _wlib_json("https://gutendex.com/books/?" +
                      urllib.parse.urlencode({"search": query}))
    return _parse_gutendex(data) if data else []


def _parse_wikisource_search(data):
    """MediaWiki list=search JSON -> candidates (the page is the text)."""
    out = []
    hits = ((data or {}).get("query") or {}).get("search") or []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        title = str(hit.get("title") or "").strip()
        if not title:
            continue
        url = ("https://en.wikisource.org/wiki/" +
               urllib.parse.quote(title.replace(" ", "_"), safe="/:_-,"))
        out.append(_arab_candidate(
            title, url, "library:wikisource",
            venue="Wikisource (book)",
            abstract=_arab_strip_tags(hit.get("snippet"))[:1000]))
        if len(out) >= LIB_PER_SOURCE_CAP:
            break
    return out


def _lib_wikisource(query):
    url = ("https://en.wikisource.org/w/api.php?" +
           urllib.parse.urlencode({
               "action": "query", "list": "search", "srsearch": query,
               "srnamespace": "0", "srlimit": str(LIB_PER_SOURCE_CAP),
               "format": "json"}))
    data = _wlib_json(url)
    return _parse_wikisource_search(data) if data else []


def _parse_scielo_books(page):
    """SciELO Books /search/ HTML -> candidates (record divs)."""
    out = []
    blocks = re.split(r'<div id="[a-z0-9]+" class="record">', page or "")
    for block in blocks[1:]:
        m = re.search(r'<h3 class="title">\s*<a href="([^"]+)">(.*?)</a>',
                      block, re.S)
        if not m:
            continue
        title = _arab_strip_tags(m.group(2))
        if not title:
            continue
        authors = []
        am = re.search(r"(?:Organizador\(es\)|Autor\(es\)|Editor\(es\))"
                       r":</span>(.*?)</div>", block, re.S)
        if am:
            authors = [a.strip() for a in
                       _arab_strip_tags(am.group(1)).split(";") if a.strip()]
        ym = re.search(r"(?:Ano|Year):</span>\s*(\d{4})", block)
        sm = re.search(r"(?:Sinopse|Synopsis):</span>(.*?)</div>",
                       block, re.S)
        out.append(_arab_candidate(
            title, html_lib.unescape(m.group(1)), "library:scielo-books",
            authors=authors[:6],
            year=int(ym.group(1)) if ym else None,
            venue="SciELO Books (book)",
            abstract=_arab_strip_tags(sm.group(1))[:1500] if sm else ""))
        if len(out) >= LIB_PER_SOURCE_CAP:
            break
    return out


def _lib_scielo_books(query):
    url = ("https://books.scielo.org/search/?" +
           urllib.parse.urlencode({"q": query, "lang": "pt"}))
    status, page = _wlib_fetch(url)
    if status != 200 or not page:
        return []
    return _parse_scielo_books(page)


def _parse_mek_results(page):
    """MEK full-search results HTML -> candidates (itemlink anchors)."""
    out = []
    for block in re.split(r'<a class="itemlink"', page or "")[1:]:
        m = re.match(r'\s+href="([^"]+)"[^>]*>\s*'
                     r'<div class="dcauthor">(.*?)</div>\s*'
                     r'<div class="dctitle">(.*?)</div>', block, re.S)
        if not m:
            continue
        title = _arab_strip_tags(m.group(3))
        if not title:
            continue
        author = _arab_strip_tags(m.group(2))
        ym = re.search(r'class="rkdate">\s*(\d{4})', block)
        out.append(_arab_candidate(
            title, html_lib.unescape(m.group(1)), "library:mek",
            authors=[author] if author else [],
            year=int(ym.group(1)) if ym else None,
            venue="MEK — Hungarian Electronic Library (book)"))
        if len(out) >= LIB_PER_SOURCE_CAP:
            break
    return out


def _lib_mek(query):
    data = urllib.parse.urlencode({
        "dc_creator": "", "dc_title": query, "dc_subject": "",
        "from": "1", "size": str(LIB_PER_SOURCE_CAP + 2),
        "sort": ""}).encode("utf-8")
    status, page = _wlib_fetch("https://mek.oszk.hu/hu/search/elfull/",
                               data=data)
    if status != 200 or not page:
        return []
    return _parse_mek_results(page)


def _parse_fadedpage(data):
    """Faded Page csearc2.php JSON -> candidates.

    A row counts only with a pid: the candidate URL is the book's
    whole-text file (books/<pid>/<pid>.txt, verified live)."""
    out = []
    for row in (data or {}).get("rows") or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        pid = str(row.get("pid") or "").strip()
        if not title or not pid:
            continue
        authors = []
        raw = row.get("authors")
        if isinstance(raw, list):
            names = [str(a.get("realname") or "").strip()
                     for a in raw if isinstance(a, dict)
                     and a.get("type") == "author"]
            if not names:
                names = [str(a.get("realname") or "").strip()
                         for a in raw if isinstance(a, dict)]
            authors = [n for n in names if n]
        elif isinstance(raw, str) and raw.strip().startswith("["):
            names = re.findall(
                r"'type': 'author', 'realname': '([^']+)'", raw)
            if not names:
                names = re.findall(r"'realname': '([^']+)'", raw)
            authors = [n.strip() for n in names if n.strip()]
        year = None
        try:
            year = int(str(row.get("first_publication") or "")[:4])
        except (TypeError, ValueError):
            year = None
        out.append(_arab_candidate(
            title,
            f"https://www.fadedpage.com/books/{pid}/{pid}.txt",
            "library:fadedpage",
            authors=authors[:6], year=year,
            venue="Faded Page (book)",
            abstract=str(row.get("description") or "")[:1500]))
        if len(out) >= LIB_PER_SOURCE_CAP:
            break
    return out


def _lib_fadedpage(query):
    payload = urllib.parse.urlencode({"title": query}).encode("utf-8")
    for _attempt in range(2):
        status, body = _wlib_fetch(
            "https://www.fadedpage.com/csearc2.php", data=payload)
        if status != 200 or not body:
            continue
        try:
            return _parse_fadedpage(json.loads(body))
        except ValueError:
            continue
    return []


def _parse_anarchist_results(page):
    """The Anarchist Library /search HTML -> candidates."""
    out = []
    blocks = re.split(r'<div class="amw-listing-item">', page or "")
    for block in blocks[1:]:
        m = re.search(
            r'<a href="(https://theanarchistlibrary\.org/library/[^"]+)"'
            r' class="list-group-item[^"]*">(.*?)</a>', block, re.S)
        if not m:
            continue
        inner = m.group(2)
        tm = re.search(r"<strong>(.*?)</strong>", inner, re.S)
        title = _arab_strip_tags(tm.group(1)) if tm else ""
        if not title:
            continue
        author = ""
        if tm:
            am = re.match(r"\s*—\s*(.*?)\s*<span",
                          inner[tm.end():], re.S)
            if am:
                author = _arab_strip_tags(am.group(1))
        ym = re.search(r"(\d{4})</small>", inner)
        out.append(_arab_candidate(
            title, m.group(1), "library:anarchist-library",
            authors=[author] if author else [],
            year=int(ym.group(1)) if ym else None,
            venue="The Anarchist Library"))
        if len(out) >= LIB_PER_SOURCE_CAP:
            break
    return out


def _lib_anarchist(query):
    url = ("https://theanarchistlibrary.org/search?" +
           urllib.parse.urlencode({"query": query}))
    status, page = _wlib_fetch(url)
    if status != 200 or not page:
        return []
    return _parse_anarchist_results(page)


_WLIB_SEARCHERS = [
    ("Project Gutenberg", _lib_gutenberg),
    ("Wikisource", _lib_wikisource),
    ("SciELO Books", _lib_scielo_books),
    ("MEK", _lib_mek),
    ("Faded Page", _lib_fadedpage),
    ("The Anarchist Library", _lib_anarchist),
]


def search_digital_libraries(query):
    """§27: the wired world digital libraries, one candidate pool.

    Per-source caps of LIB_PER_SOURCE_CAP and a LIB_TOTAL_CAP ceiling,
    exactly like §25; every source fails soft on its own.
    """
    print(f"[digital-libraries] query={query!r}")
    out = []
    for name, fn in _WLIB_SEARCHERS:
        if len(out) >= LIB_TOTAL_CAP:
            break
        try:
            cands = fn(query) or []
        except Exception as exc:
            print(f"  {name}: failed {exc!r}")
            continue
        print(f"  {name}: {len(cands)} candidates")
        out.extend(cands[:LIB_PER_SOURCE_CAP])
    out = out[:LIB_TOTAL_CAP]
    print(f"[digital-libraries] total {len(out)} candidates")
    return out


# --------------------------------------------------------------------------
# §32 — verified open-text UNIVERSITY LECTURE platforms (surveys §29,
# §30 and §31: files/university-lectures.md, university-lectures-asia.md
# and university-lectures-world.md in the goal workspace).
#
# Jupiter's FULL-TEXT-ONLY law applies exactly as in §§23-27: a
# platform is wired live ONLY when probing proved BOTH (1) a
# search/catalogue route answers with real lecture items for a query,
# AND (2) a returned item's FULL TEXT is reachable (transcript / notes
# PDF, full HTML text, or caption files). Every platform below was
# re-probed from this sandbox on 2026-11-26 (multiple routes each:
# site search, public API, sitemap/feeds, predictable URL patterns,
# alternate hosts); the per-platform evidence and the routes tried for
# the registry-only platforms are recorded in lectures_registry.json.
#
# LIVE-WIRED (12): MIT OCW (sitemap catalogue + resource-page
# transcript PDFs), Open Yale Courses (catalogue + session transcript
# HTML), Stanford SEE (course list + transcript HTML), Harvard CS50
# (week catalogue + notes HTML), TU Delft OCW (WordPress search feed
# + lecture pages), METU OCW (Moodle search + resource PDFs),
# Wikiversity (MediaWiki API;
# LOW-CREDIBILITY community source, flagged in the registry),
# OpenStax (archive-JSON book trees + page JSON; TEXTBOOKS, flagged),
# NYCU OCW (WP search + handout PDFs), Hokkaido OCW (WP search +
# lecture-note PDFs), TÜBA Açık Ders (homepage catalogue + notes /
# transcript PDFs), Bangladesh Open University (directory catalogue +
# module PDFs).
#
# REGISTRY-ONLY (13, in lectures_registry.json with routes tried):
# NPTEL (SPA; archive patterns 404), OpenLearn (Cloudflare 403),
# KOCW (search answers; captions assembled by JS, no file route
# proven), Oxford Podcasts (no query route; per-episode .srt only),
# Collège de France (search returns a landing page, no results),
# PostNauka (API search returns author themes; post detail 403),
# Saylor Academy (Moodle search answers with real courses, but
# the guest course page exposes no unit content — only the
# syllabus and tool pages; unit text not reachable),
# NTU OCW (Cloudflare 403 on all routes), Virtual University of
# Pakistan (handouts 403; hosts unreachable), AIOU Soft Books (book
# PDFs by code pattern but no search/catalogue route), NOUN
# e-Courseware (PDF pattern but no courseware search route), Greek
# Open Courses (stateful JSF portal; eClass chain unproven), Cantabria
# OCW (host closes every connection).
#
# Every wired candidate's URL IS its full text (transcript/notes PDF,
# transcript/notes HTML page, caption-bearing page, or the OpenStax
# page JSON), so the §20 full-text layer fetches it through the
# generic route and passages can be extracted. Lectures enter the
# pipeline as ordinary content-bearing candidates (the §20 hierarchy's
# lecture-transcript tier): caps of LEC_PER_PLATFORM_CAP per platform
# and LEC_TOTAL_CAP per call, per-platform fail-soft, and the
# verification, screening, judges and text-first gate are UNCHANGED.
# Fetches reuse the §27 _wlib_fetch. GRIS inherits via ba.*.
# --------------------------------------------------------------------------

LEC_PER_PLATFORM_CAP = 8
LEC_TOTAL_CAP = 24


def _lec_tokens(query):
    """Lowercase word tokens (len >= 3) of a query, order-preserving."""
    out = []
    for tok in re.findall(r"[^\W_]+", (query or "").lower()):
        if len(tok) >= 3 and tok not in out:
            out.append(tok)
    return out


def _lec_match(text, tokens):
    """How many query tokens appear in the text (word-ish matching)."""
    low = (text or "").lower()
    return sum(1 for tok in tokens if tok in low)


def _lec_anchors(page, href_re):
    """(url, anchor-text) pairs for anchors whose href matches."""
    out = []
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                         page or "", re.S):
        href = html_lib.unescape(m.group(1))
        if re.search(href_re, href):
            out.append((href, _arab_strip_tags(m.group(2))))
    return out


# ------------------------------------------------------------- MIT OCW
def _parse_mit_sitemap_courses(index):
    """MIT OCW sitemap index -> [(course slug, course sitemap URL)]."""
    out = []
    for url in re.findall(r"<loc>([^<]+)</loc>", index or ""):
        m = re.search(r"/courses/([^/]+)/sitemap\.xml$", url)
        if m:
            out.append((m.group(1), url))
    return out


def _lec_mit_ocw(query):
    tokens = _lec_tokens(query)
    if not tokens:
        return []
    status, index = _wlib_fetch("https://ocw.mit.edu/sitemap.xml")
    if status != 200 or not index:
        return []
    matched = [(slug, url) for slug, url in
               _parse_mit_sitemap_courses(index)
               if _lec_match(slug.replace("-", " "), tokens)]
    # Collect the resource pages of the matched courses, then fetch
    # them concurrently (they are large HTML pages; sequential
    # fetching made this platform the wave's long pole).
    jobs = []
    for slug, smap_url in matched[:3]:
        status, smap = _wlib_fetch(smap_url)
        if status != 200 or not smap:
            continue
        resources = [u for u in re.findall(r"<loc>([^<]+)</loc>", smap)
                     if "/resources/" in u]
        resources.sort(key=lambda u: 0 if re.search(
            r"lecture|lec[-_]?\d|session|class", u.lower()) else 1)
        for rurl in resources[:10]:
            jobs.append((slug, rurl))
    jobs = jobs[:10]  # the pre-parallel fetch budget, kept
    pages = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(_wlib_fetch, rurl): rurl
                    for _slug, rurl in jobs}
            for fut in as_completed(futs):
                try:
                    pages[futs[fut]] = fut.result()
                except Exception:
                    pages[futs[fut]] = (0, "")
    out = []
    for slug, rurl in jobs:
        if len(out) >= LEC_PER_PLATFORM_CAP:
            break
        st2, page = pages.get(rurl) or (0, "")
        if st2 != 200 or not page or "transcript" not in \
                page.lower():
            continue
        tm = re.search(r'data-transcriptlink\s*=\s*"([^"]+\.pdf)"',
                       page)
        pick = tm.group(1) if tm else ""
        if not pick:
            pdfs = _lec_anchors(page, r"\.pdf$")
            for href, text in pdfs:
                if text.strip().lower() in ("english",) or \
                        "transcript" in text.lower():
                    pick = href
                    break
            if not pick and pdfs:
                pick = pdfs[0][0]
        if not pick:
            continue
        if pick.startswith("/"):
            pick = "https://ocw.mit.edu" + pick
        rslug = rurl.rstrip("/").rsplit("/", 1)[-1]
        title = (slug.replace("-", " ") + " — " +
                 rslug.replace("-", " "))
        out.append(_arab_candidate(
            title, pick, "lecture:mitocw",
            venue="MIT OpenCourseWare (lecture transcript)"))
    return out


# ------------------------------------------------------------- Open Yale
_YALE_COURSE_RE = r"^/[a-z\-]+/[a-z]+-\d+$"


def _parse_yale_courses(page):
    """OYC /courses catalogue table -> [(course URL, course title)].

    Each course appears twice (course-number cell and title cell,
    relative hrefs); the longest anchor text (the title) wins."""
    best = {}
    for href, text in _lec_anchors(page, _YALE_COURSE_RE):
        if href not in best or len(text) > len(best[href]):
            best[href] = text
    return [("https://oyc.yale.edu" + href, title)
            for href, title in best.items() if title]


def _parse_yale_sessions(page):
    """OYC course page -> [(session URL, session title)]."""
    out, seen = [], set()
    for href, text in _lec_anchors(page, r"^/[a-z\-]+/[a-z]+-\d+/"
                                   r"lecture-\d+$"):
        if href not in seen:
            seen.add(href)
            out.append(("https://oyc.yale.edu" + href, text))
    return out


_YALE_CACHE = None


def _yale_courses():
    """The OYC catalogue, parsed once and cached per process."""
    global _YALE_CACHE
    if _YALE_CACHE is None:
        status, page = _wlib_fetch("https://oyc.yale.edu/courses")
        _YALE_CACHE = _parse_yale_courses(page) \
            if status == 200 and page else []
    return _YALE_CACHE


def _lec_yale(query):
    tokens = _lec_tokens(query)
    if not tokens:
        return []
    matched = [(u, t) for u, t in _yale_courses()
               if _lec_match(t, tokens)]
    out = []
    for curl, ctitle in matched[:2]:
        st2, cpage = _wlib_fetch(curl)
        if st2 != 200 or not cpage:
            continue
        for surl, stitle in _parse_yale_sessions(cpage):
            if len(out) >= LEC_PER_PLATFORM_CAP:
                break
            num = re.search(r"lecture-(\d+)$", surl)
            title = ctitle + " — Lecture " + \
                (num.group(1) if num else "")
            if stitle:
                title += ": " + stitle
            out.append(_arab_candidate(
                title, surl, "lecture:yale",
                venue="Open Yale Courses (lecture transcript)"))
    return out


# ---------------------------------------------------------- Stanford SEE
def _parse_see_courses(page):
    """SEE /Course list -> [(course code, course title)]."""
    out, seen = [], set()
    for href, text in _lec_anchors(page, r"^/Course/[A-Za-z0-9]+$"):
        code = href.rsplit("/", 1)[-1]
        if code not in seen:
            seen.add(code)
            out.append((code, text or code))
    return out


def _parse_see_transcripts(page):
    """SEE course page -> [(transcript HTML URL, title)]."""
    out, seen = [], set()
    for href, text in _lec_anchors(page, r"/transcripts/[^/]+\.html$"):
        if href not in seen:
            seen.add(href)
            out.append((href, text))
    return out


_SEE_CACHE = None


def _see_catalogue():
    """SEE courses with their real titles (from each course page's
    <title>) and transcript links — fetched once, cached per
    process. The /Course list itself shows only course codes."""
    global _SEE_CACHE
    if _SEE_CACHE is not None:
        return _SEE_CACHE
    courses = []
    status, page = _wlib_fetch("https://see.stanford.edu/Course")
    if status == 200 and page:
        codes = [code for code, _text in _parse_see_courses(page)]
        pages = {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(_wlib_fetch,
                              "https://see.stanford.edu/Course/" + code):
                    code for code in codes}
            for fut in as_completed(futs):
                try:
                    pages[futs[fut]] = fut.result()
                except Exception:
                    pages[futs[fut]] = (0, "")
        for code in codes:
            st2, cpage = pages.get(code) or (0, "")
            if st2 != 200 or not cpage:
                continue
            m = re.search(r"<title>(.*?)</title>", cpage, re.S)
            title = _arab_strip_tags(m.group(1)) if m else code
            title = title.split("|")[-1].strip()
            title = re.sub(r"^[A-Z]{2}\d+[A-Z]?\s*-\s*", "", title) \
                or title
            courses.append((code, title,
                            _parse_see_transcripts(cpage)))
    _SEE_CACHE = courses
    return courses


def _lec_stanford_see(query):
    tokens = _lec_tokens(query)
    if not tokens:
        return []
    out = []
    for code, ctitle, transcripts in _see_catalogue():
        if not _lec_match(ctitle + " " + code, tokens):
            continue
        for href, _text in transcripts:
            if len(out) >= LEC_PER_PLATFORM_CAP:
                break
            name = href.rsplit("/", 1)[-1].replace(".html", "")
            out.append(_arab_candidate(
                ctitle + " — " + name,
                "https://see.stanford.edu" + href,
                "lecture:stanford-see",
                venue="Stanford SEE (lecture transcript)"))
    return out


# ------------------------------------------------------------ Harvard CS50
_CS50_TOPICS_CACHE = None


def _parse_cs50_index(page):
    """CS50x index course-nav -> {week: topic} (the nav lists each
    week with its lecture topic: 'Week 0 — Scratch', ...)."""
    topics = {}
    for m in re.finditer(r'href="weeks/(\d+)/"[^>]*>Week \d+</a>\s*'
                         r"([^<]+)", page or ""):
        topics[int(m.group(1))] = _arab_strip_tags(m.group(2))
    return topics


def _cs50_topics():
    """CS50x 2025 week -> lecture topic map (one index fetch, cached
    per process)."""
    global _CS50_TOPICS_CACHE
    if _CS50_TOPICS_CACHE is None:
        status, index = _wlib_fetch("https://cs50.harvard.edu/x/2025/")
        _CS50_TOPICS_CACHE = _parse_cs50_index(index) \
            if status == 200 and index else {}
    return _CS50_TOPICS_CACHE


def _lec_cs50(query):
    tokens = _lec_tokens(query)
    if not tokens:
        return []
    out = []
    for week, topic in sorted(_cs50_topics().items()):
        if _lec_match(topic, tokens):
            out.append(_arab_candidate(
                f"CS50 Lecture {week}: {topic}",
                f"https://cs50.harvard.edu/x/2025/notes/{week}/",
                "lecture:cs50",
                venue="Harvard CS50 (lecture notes)"))
        if len(out) >= LEC_PER_PLATFORM_CAP:
            break
    return out


# ------------------------------------------------------------ TU Delft OCW
def _parse_tudelft_feed(feed):
    """TU Delft WP search RSS -> [(title, item URL)] for course items."""
    out = []
    for block in re.findall(r"<item>(.*?)</item>", feed or "", re.S):
        tm = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                       block, re.S)
        lm = re.search(r"<link>([^<]+)</link>", block)
        if not tm or not lm:
            continue
        url = html_lib.unescape(lm.group(1)).split("#")[0]
        if not re.search(r"/(course-lectures|course-readings|courses)/",
                         url):
            continue
        title = _arab_strip_tags(tm.group(1))
        if title:
            out.append((title, url))
    return out


def _lec_tudelft(query):
    url = ("https://ocw.tudelft.nl/search/" +
           urllib.parse.quote(query, safe="") + "/feed/rss2/")
    status, feed = _wlib_fetch(url)
    if status != 200 or not feed:
        return []
    out = []
    for title, link in _parse_tudelft_feed(feed):
        out.append(_arab_candidate(
            title, link, "lecture:tudelft",
            venue="TU Delft OCW (lecture)"))
        if len(out) >= LEC_PER_PLATFORM_CAP:
            break
    return out


# ---------------------------------------------------------------- METU OCW
def _parse_moodle_courses(page, host):
    """Moodle search results -> [(course URL, course title)]."""
    out, seen = [], set()
    pat = r"^https://" + re.escape(host) + r"/course/view\.php\?id=\d+$"
    for href, text in _lec_anchors(page, pat):
        if href not in seen and text:
            seen.add(href)
            out.append((href, text))
    return out


def _lec_metu(query):
    url = ("https://ocw.metu.edu.tr/course/search.php?" +
           urllib.parse.urlencode({"search": query}))
    status, page = _wlib_fetch(url)
    if status != 200 or not page:
        return []
    out = []
    courses = _parse_moodle_courses(page, "ocw.metu.edu.tr")
    for curl, ctitle in courses[:2]:
        st2, cpage = _wlib_fetch(curl)
        if st2 != 200 or not cpage:
            continue
        resources = _lec_anchors(
            cpage, r"/mod/resource/view\.php\?id=\d+$")
        for rurl, rtitle in resources:
            if len(out) >= LEC_PER_PLATFORM_CAP:
                break
            st3, rpage = _wlib_fetch(rurl)
            if st3 != 200 or not rpage:
                continue
            pm = re.search(r'href="(https://ocw\.metu\.edu\.tr/'
                           r'pluginfile\.php/[^"]+\.pdf)"', rpage)
            if not pm:
                continue
            out.append(_arab_candidate(
                rtitle or ctitle, html_lib.unescape(pm.group(1)),
                "lecture:metu", venue="METU OCW (lecture notes)"))
    return out


# -------------------------------------------------------------- Wikiversity
def _parse_wikiversity_search(data):
    """MediaWiki list=search JSON -> candidates (the page is the text).

    Wikiversity is a LOW-CREDIBILITY community source (flagged in
    lectures_registry.json); it passes the same screening as any
    other candidate and is never trusted on its own."""
    out = []
    hits = ((data or {}).get("query") or {}).get("search") or []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        title = str(hit.get("title") or "").strip()
        if not title:
            continue
        url = ("https://en.wikiversity.org/wiki/" +
               urllib.parse.quote(title.replace(" ", "_"),
                                  safe="/:_-,"))
        out.append(_arab_candidate(
            title, url, "lecture:wikiversity",
            venue="Wikiversity (community course text)",
            abstract=_arab_strip_tags(hit.get("snippet"))[:1000]))
        if len(out) >= LEC_PER_PLATFORM_CAP:
            break
    return out


def _lec_wikiversity(query):
    url = ("https://en.wikiversity.org/w/api.php?" +
           urllib.parse.urlencode({
               "action": "query", "list": "search", "srsearch": query,
               "srnamespace": "0",
               "srlimit": str(LEC_PER_PLATFORM_CAP), "format": "json"}))
    data = _wlib_json(url)
    return _parse_wikiversity_search(data) if data else []


# ----------------------------------------------------------------- OpenStax
_OPENSTAX_CACHE = None


def _openstax_walk(node, out):
    """Collect (page id, clean title, slug) from an archive tree."""
    if not isinstance(node, dict):
        return
    pid = str(node.get("id") or "").split("@")[0]
    title = _arab_strip_tags(str(node.get("title") or ""))
    slug = str(node.get("slug") or "")
    if pid and slug:
        out.append((pid, title, slug))
    for child in node.get("contents") or []:
        _openstax_walk(child, out)


def _openstax_catalogue():
    """{archive, books: [(book id, version, book title,
    [(page id, section title, slug), ...]), ...]} — built once per
    process from OpenStax's own embedded site config (the catalogue
    of all books) plus each book's archive tree JSON."""
    global _OPENSTAX_CACHE
    if _OPENSTAX_CACHE is not None:
        return _OPENSTAX_CACHE
    cache = {"archive": "", "books": []}
    status, page = _wlib_fetch(
        "https://openstax.org/books/astronomy-2e/pages/preface")
    if status == 200 and page:
        m = re.search(r'"archiveUrl":\s*"/apps/archive/([^"]+)"', page)
        archive = m.group(1) if m else ""
        books = re.findall(
            r'"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-'
            r'[0-9a-f]{12})":\s*\{\s*"defaultVersion":\s*"([^"]+)"',
            page)
        if archive and books:
            cache["archive"] = archive

            def _tree(item):
                bid, ver = item
                url = (f"https://openstax.org/apps/archive/{archive}"
                       f"/contents/{bid}@{ver}.json")
                # The archive is a public machine interface; fetch
                # via the full-text layer's capped fetcher (the
                # paced _wlib_fetch would serialise 127 book trees
                # into a minute-plus stall).
                data_bytes, _ctype = _fetch_capped_full(
                    url, cap=1_000_000, timeout=30)
                if not data_bytes:
                    return None
                try:
                    data = json.loads(
                        data_bytes.decode("utf-8", "replace"))
                except ValueError:
                    return None
                nodes = []
                _openstax_walk(data.get("tree") or {}, nodes)
                title = _arab_strip_tags(str(data.get("title") or ""))
                return (bid, ver, title, nodes)

            with ThreadPoolExecutor(max_workers=16) as ex:
                results = list(ex.map(_tree, books))
            cache["books"] = [r for r in results if r]
    _OPENSTAX_CACHE = cache
    return cache


def _lec_openstax(query):
    tokens = _lec_tokens(query)
    if not tokens:
        return []
    cat = _openstax_catalogue()
    if not cat["archive"] or not cat["books"]:
        return []
    out = []
    for bid, ver, btitle, nodes in cat["books"]:
        book_hit = _lec_match(btitle, tokens) > 0
        for pid, title, slug in nodes[1:]:
            if len(out) >= LEC_PER_PLATFORM_CAP:
                break
            if book_hit or _lec_match(title, tokens):
                url = (f"https://openstax.org/apps/archive/"
                       f"{cat['archive']}/contents/{bid}@{ver}:{pid}"
                       f".json")
                out.append(_arab_candidate(
                    (btitle + ": " + title) if title else btitle,
                    url, "lecture:openstax",
                    venue="OpenStax (textbook)"))
        if len(out) >= LEC_PER_PLATFORM_CAP:
            break
    return out


# ----------------------------------------------------------------- NYCU OCW
def _parse_nycu_courses(page):
    """NYCU WP search -> [(course page URL, slug-derived title)]."""
    out, seen = [], set()
    for href, _text in _lec_anchors(page, r"course_page=all-course/"):
        href = href.replace("&#038;", "&")
        if "all-course/" not in href or href in seen:
            continue
        seen.add(href)
        slug = urllib.parse.unquote(
            href.rstrip("/").rsplit("/", 1)[-1])
        out.append((href, slug.replace("-", " ")))
    return out


def _lec_nycu(query):
    url = ("https://ocw.nycu.edu.tw/?" +
           urllib.parse.urlencode({"s": query}))
    status, page = _wlib_fetch(url)
    if status != 200 or not page:
        return []
    out = []
    for curl, ctitle in _parse_nycu_courses(page)[:3]:
        st2, cpage = _wlib_fetch(curl)
        if st2 != 200 or not cpage:
            continue
        pdfs = sorted(set(re.findall(
            r'href="(https://video\.ocw\.nycu\.edu\.tw/[^"]+\.pdf)"',
            cpage)))
        for pdf in pdfs:
            if len(out) >= LEC_PER_PLATFORM_CAP:
                break
            name = pdf.rsplit("/", 1)[-1].replace(".pdf", "")
            out.append(_arab_candidate(
                ctitle + " — " + name.replace("-", " "), pdf,
                "lecture:nycu", venue="NYCU OCW (lecture handouts)"))
    return out


# ------------------------------------------------------------- Hokkaido OCW
def _parse_hokudai_lectures(page):
    """Hokkaido WP search -> [(lecture URL, slug-derived title)]."""
    out, seen = [], set()
    for href, _text in _lec_anchors(
            page, r"^https://ocw\.hokudai\.ac\.jp/lecture/[^/]+$"):
        if href not in seen:
            seen.add(href)
            slug = href.rstrip("/").rsplit("/", 1)[-1]
            out.append((href, slug.replace("-", " ")))
    return out


def _lec_hokudai(query):
    url = ("https://ocw.hokudai.ac.jp/?" +
           urllib.parse.urlencode({"s": query}))
    status, page = _wlib_fetch(url)
    if status != 200 or not page:
        return []
    out = []
    for lurl, ltitle in _parse_hokudai_lectures(page)[:4]:
        st2, lpage = _wlib_fetch(lurl)
        if st2 != 200 or not lpage:
            continue
        pdfs = sorted(set(re.findall(
            r'href="([^"]*wp-content/uploads/[^"]+\.pdf)"', lpage)))
        pdfs.sort(key=lambda u: 0 if re.search(
            r"note|lecture|slide|handout", u.lower()) else 1)
        for pdf in pdfs:
            if len(out) >= LEC_PER_PLATFORM_CAP:
                break
            if "poster" in pdf.lower():
                continue
            name = pdf.rsplit("/", 1)[-1].replace(".pdf", "")
            out.append(_arab_candidate(
                ltitle + " — " + name.replace("-", " "), pdf,
                "lecture:hokudai",
                venue="Hokkaido OCW (lecture notes)"))
    return out


# -------------------------------------------------------------- TÜBA Açık Ders
def _parse_tuba_courses(page):
    """TÜBA homepage catalogue -> [(course URL, course title)].

    Each course card is one big anchor; the title is its <h3>."""
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="(/ders/[a-z0-9\-]+)"[^>]*>'
                         r"(.*?)</a>", page or "", re.S):
        href, inner = m.group(1), m.group(2)
        if href in seen:
            continue
        seen.add(href)
        hm = re.search(r"<h3[^>]*>(.*?)</h3>", inner, re.S)
        title = _arab_strip_tags(hm.group(1)) if hm else ""
        if not title:
            title = href.rsplit("/", 1)[-1].replace("-", " ")
        out.append(("https://acikders.tuba.gov.tr" + href, title))
    return out


_TUBA_CACHE = None


def _tuba_courses():
    """The TÜBA homepage catalogue, parsed once and cached per
    process (the homepage is ~320 KB and static within a run)."""
    global _TUBA_CACHE
    if _TUBA_CACHE is None:
        status, page = _wlib_fetch("https://acikders.tuba.gov.tr/")
        _TUBA_CACHE = _parse_tuba_courses(page) \
            if status == 200 and page else []
    return _TUBA_CACHE


def _lec_tuba(query):
    tokens = _lec_tokens(query)
    if not tokens:
        return []
    matched = [(u, t) for u, t in _tuba_courses()
               if _lec_match(t + " " + u.rsplit("/", 1)[-1]
                             .replace("-", " "), tokens)]
    out = []
    for curl, ctitle in matched[:2]:
        st2, cpage = _wlib_fetch(curl)
        if st2 != 200 or not cpage:
            continue
        pdfs = sorted(set(re.findall(
            r'href="([^"]*ADUploads/[^"]+\.pdf)"', cpage)))
        pdfs.sort(key=lambda u: 0 if "/notes/" in u or
                    "metni" in u.lower() else 1)
        for pdf in pdfs:
            if len(out) >= LEC_PER_PLATFORM_CAP:
                break
            if pdf.startswith("/"):
                pdf = "https://acikders.tuba.gov.tr" + pdf
            name = pdf.rsplit("/", 1)[-1].replace(".pdf", "")
            out.append(_arab_candidate(
                ctitle + " — " + name, pdf, "lecture:tuba",
                venue="TÜBA Açık Ders (lecture notes/transcript)"))
    return out


# ------------------------------------------------- Bangladesh Open University
_BOU_CACHE = None
_BOU_ROOT = "https://ebookbou.edu.bd/Books/Text/"


def _bou_fetch(url):
    """One autoindex listing via the capped fetcher (the paced
    _wlib_fetch would serialise the 24-listing crawl into a
    12-second floor; listings are small machine-generated pages)."""
    data, _ctype = _fetch_capped_full(url, cap=400_000, timeout=30)
    if not data:
        return (0, "")
    return (200, data.decode("utf-8", "replace"))


def _bou_listings():
    """Directory catalogue of ebookbou.edu.bd/Books/Text/: a BFS over
    the Apache-style listings (depth <= 3, bounded fetches), cached
    per process: [(dir URL, [(file name, file URL), ...]), ...]."""
    global _BOU_CACHE
    if _BOU_CACHE is not None:
        return _BOU_CACHE
    found = []
    queue = [(_BOU_ROOT, 0)]
    seen = {_BOU_ROOT}
    fetches = 0
    while queue and fetches < 24:
        # One BFS level at a time; the level's listings are fetched
        # concurrently (sequential listing fetches made this
        # platform the wave's long pole).
        level = []
        while queue and len(level) < 24 - fetches:
            level.append(queue.pop(0))
        pages = {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(_bou_fetch, u): u for u, _d in level}
            for fut in as_completed(futs):
                try:
                    pages[futs[fut]] = fut.result()
                except Exception:
                    pages[futs[fut]] = (0, "")
        fetches += len(level)
        for durl, depth in level:
            status, page = pages.get(durl) or (0, "")
            if status != 200 or not page:
                continue
            pdfs, subdirs = [], []
            for href in re.findall(r'href="([^"]+)"', page):
                if href.startswith("?") or href in ("../", "/"):
                    continue
                full = urllib.parse.urljoin(durl, href)
                if not full.startswith(_BOU_ROOT):
                    continue
                if href.lower().endswith(".pdf"):
                    pdfs.append((urllib.parse.unquote(
                        href.rsplit("/", 1)[-1]), full))
                elif href.endswith("/") and full not in seen and \
                        depth < 3:
                    seen.add(full)
                    subdirs.append(full)
            if pdfs:
                found.append((durl, pdfs))
            queue.extend((u, depth + 1) for u in subdirs)
    _BOU_CACHE = found
    return found


def _lec_bou(query):
    tokens = _lec_tokens(query)
    if not tokens:
        return []
    out = []
    for durl, pdfs in _bou_listings():
        dname = urllib.parse.unquote(
            durl[len(_BOU_ROOT):]).replace("/", " ").replace(
                "_", " ")
        dir_hit = _lec_match(dname, tokens) > 0
        modules = [(n, u) for n, u in pdfs
                   if not n.lower().startswith("index")]
        for name, url in (modules if dir_hit else pdfs):
            if len(out) >= LEC_PER_PLATFORM_CAP:
                break
            title = name.replace(".pdf", "").replace("_", " ")
            if dir_hit or _lec_match(title, tokens):
                out.append(_arab_candidate(
                    title, url, "lecture:bou",
                    venue="Bangladesh Open University (course book)"))
        if len(out) >= LEC_PER_PLATFORM_CAP:
            break
    return out


_LECTURE_SEARCHERS = [
    ("MIT OCW", _lec_mit_ocw),
    ("Open Yale Courses", _lec_yale),
    ("Stanford SEE", _lec_stanford_see),
    ("Harvard CS50", _lec_cs50),
    ("TU Delft OCW", _lec_tudelft),
    ("METU OCW", _lec_metu),
    ("Wikiversity", _lec_wikiversity),
    ("OpenStax", _lec_openstax),
    ("NYCU OCW", _lec_nycu),
    ("Hokkaido OCW", _lec_hokudai),
    ("TÜBA Açık Ders", _lec_tuba),
    ("Bangladesh Open University", _lec_bou),
]


def search_university_lectures(query):
    """§32: the wired open-text university lecture platforms, one
    candidate pool. The platforms run concurrently (they are
    independent hosts; the engine's own wave does the same across
    searchers) and merge in registry order. Per-platform caps of
    LEC_PER_PLATFORM_CAP and a LEC_TOTAL_CAP ceiling, exactly like
    §27; every platform fails soft on its own."""
    print(f"[university-lectures] query={query!r}")
    results = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        fut_map = {ex.submit(fn, query): name
                   for name, fn in _LECTURE_SEARCHERS}
        for fut in as_completed(fut_map):
            name = fut_map[fut]
            try:
                results[name] = fut.result() or []
            except Exception as exc:
                print(f"  {name}: failed {exc!r}")
                results[name] = []
    out = []
    for name, _fn in _LECTURE_SEARCHERS:
        if len(out) >= LEC_TOTAL_CAP:
            break
        cands = results.get(name) or []
        print(f"  {name}: {len(cands)} candidates")
        out.extend(cands[:LEC_PER_PLATFORM_CAP])
    out = out[:LEC_TOTAL_CAP]
    print(f"[university-lectures] total {len(out)} candidates")
    return out


# --------------------------------------------------------------------------
# Ensani — پرتال جامع علوم انسانی (§34) — the Persian humanities
# portal of the Institute for Humanities and Cultural Studies
# (پژوهشگاه علوم انسانی و مطالعات فرهنگی), registered in
# ensani_registry.json (§17 wiring: credibility/priority signal
# only, never verification). The portal is the largest Persian
# humanities article bank (hundreds of journals, hundreds of
# thousands of articles). Probed from the build sandbox on
# 2026-11-26 under the full-text-only law and the multi-route rule:
#   SEARCH — the site's own search form
#       GET ensani.ir/fa/article/search/index?term=<q>&searchType=0
#           (302 -> /fa/article?ArticleSearch[title]=<q>&
#            ArticleSearch[sortBy]=relevance; server-rendered
#            list-view of <article class="article-item"> blocks)
#       ANSWERS with real records, no login (بیرونی -> 410
#       results; تاریخ علم -> 113; تاریخ نجوم -> 7; تاریخ
#       ریاضیات -> 9). The search is portal-wide across ALL of
#       its journals by default — no journal/subject filter is
#       applied — so its history-of-science titles are searched
#       too: the University of Tehran journal «تاریخ علم» was
#       seen in the results (دوره ۱۹, ۱۴۰۰, شماره ۱; پاییز ۱۳۸۲,
#       شماره ۱).
#   FULL TEXT — the article page's own download route
#       GET ensani.ir/fa/article/download/<article_id>
#           (302 -> /file/download/article/*.pdf)
#       probe-verified WITHOUT login or cookies: id 191665 ->
#       170,911 B, %PDF-1.4, 6 pages; id 475321 (a «تاریخ علم»
#       journal article) -> 495,753 B, %PDF-1.7. Candidates
#       therefore carry oa_pdf_url = the download route, and
#       attach_fulltexts_v2 fetches the PDF via its
#       index-fulltext route.
#   Routes also tried: journal-issue listing pages
#   (/fa/article/journal-number/<id>/..., linked from every
#   result — covered by the portal search, not queried
#   separately); the results page's Excel export
#   (/fa/article/excel?model=article&query[...] — exposed by the
#   site, not needed); sitemap.xml -> the site's 404 page;
#   robots.txt -> 404 page; OAI-PMH (/oai?verb=Identify) -> 404;
#   RSS (/fa/article/rss) -> 404; alternate hosts www.ensani.ir
#   and http://ensani.ir both answer 200 (same site); index
#   fallbacks (Crossref/DOAJ/OpenAlex -> ensani PDFs) not
#   needed — the portal's own routes passed both bars.
# The fetch discipline is the §23 single-shot _arab_fetch and
# candidates are shaped by _arab_candidate, exactly like §23.
# --------------------------------------------------------------------------

ENSANI_CAP = 8
_ENSANI_BASE = "https://ensani.ir"
_FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹"


def _ensani_year(text):
    """Year from an ensani journal-issue string: a Persian-digit
    year (۱۳۶۸, the Jalali year the portal prints) is converted
    digit-by-digit; a Latin year passes through unchanged."""
    s = str(text or "")
    m = re.search(r"[۰-۹]{4}|(?:19|20)\d{2}", s)
    if not m:
        return None
    tok = m.group(0)
    if tok[0] in _FA_DIGITS:
        return int("".join(str(_FA_DIGITS.index(c)) for c in tok))
    return int(tok)


def _parse_ensani_results(body):
    """Parse one ensani search-results page: each hit is an
    <article class="article-item"> block whose heading links
    /fa/article/<id>/<slug>, with author links (rel="author") and
    a journal-issue link (class="journal-title")."""
    out = []
    for block in re.findall(
            r'<article class="article-item.*?</article>', body or "",
            re.S):
        m = re.search(r'href="(/fa/article/(\d+)/[^"]*)"[^>]*>'
                      r"(.*?)</a>", block, re.S)
        if not m:
            continue
        title = _arab_strip_tags(m.group(3))
        if not title:
            continue
        art_id = m.group(2)
        authors = [_arab_strip_tags(a) for a in re.findall(
            r'rel="author">(.*?)</a>', block, re.S)]
        jm = re.search(r'journal-title"[^>]*>(.*?)</a>', block, re.S)
        venue = _arab_strip_tags(jm.group(1)) if jm else ""
        cand = _arab_candidate(
            title, _ENSANI_BASE + m.group(1), "persian:ensani",
            authors=authors[:6], year=_ensani_year(venue),
            venue=venue or "Ensani (پرتال جامع علوم انسانی)")
        cand["oa_pdf_url"] = (
            f"{_ENSANI_BASE}/fa/article/download/{art_id}")
        out.append(cand)
    return out


def search_ensani(query):
    """§34: search Ensani (پرتال جامع علوم انسانی) portal-wide —
    every journal the portal carries, including its
    history-of-science titles (مجلهٔ تاریخ علم دانشگاه تهران).
    Fail-soft, capped at ENSANI_CAP."""
    url = (_ENSANI_BASE + "/fa/article/search/index?"
           + urllib.parse.urlencode({"term": query,
                                     "searchType": "0"}))
    status, body = _arab_fetch(url)
    if status != 200 or not body:
        return []
    return _parse_ensani_results(body)[:ENSANI_CAP]


# --------------------------------------------------------------------------
# JIHS — مجلهٔ تاریخ علم (§36) — the Journal for the History of
# Science of the Institute for the History of Science, University
# of Tehran (پژوهشکدهٔ تاریخ علم دانشگاه تهران), registered in
# jihs_registry.json (§17 wiring: credibility/priority signal
# only, never verification). Iran's first and only specialised
# history-of-science journal, علمی-پژوهشی, Persian with English
# abstracts, open access (CC BY 4.0). The journal was already
# reachable indirectly via the §34 Ensani route; this DIRECT
# route removes the Ensani lag so the newest issues arrive as
# soon as the journal publishes them. Probed from the build
# sandbox on 2026-11-26 under the full-text-only law and the
# multi-route rule:
#   SEARCH — the journal site's own search form (homepage form:
#       GET ./ with hidden _action=article and text field
#       keywords)
#       GET jihs.ut.ac.ir/?_action=article&keywords=<q>
#       ANSWERS with real records, no login and no token needed
#       (بیرونی -> 21 results; تاریخ علم -> 167 results,
#       25 per page). Each results block carries the article
#       title, authors, the issue line (دوره/شماره), a DOI
#       (10.22059/jihs...) and — for published articles — a
#       direct PDF link (class="pdf_link").
#   FULL TEXT — that direct PDF link
#       GET jihs.ut.ac.ir/article_<id>_<hash>.pdf
#       probe-verified WITHOUT login or cookies:
#       article_106822_....pdf -> HTTP 200, application/pdf,
#       288,122 B, %PDF-1.5 (the page's own size badge,
#       281.37 K, matches). Candidates therefore carry
#       oa_pdf_url = the article's own PDF file, and
#       attach_fulltexts_v2 fetches the PDF via its
#       index-fulltext route.
#   Online-first articles (مقالات آماده انتشار) have NO PDF yet
#   — neither in the results list nor on their article pages
#   (article_108572.html checked: no PDF link) — so the parser
#   emits only hits that carry a PDF link, per the
#   full-text-only law.
#   Routes also tried: issue/archive listing pages
#   (issue_<id>_<id>.html, e.g. issue_13738_13913.html -> 200 —
#   covered by the site search, not queried separately);
#   sitemap.xml exists (200, ~73 KB; robots.txt is permissive
#   and points to it — neither needed); RSS (/rss.xml -> 404,
#   /rss.php -> 404, /journal/rss -> 404, /?_action=rss -> 403)
#   — no usable feed; alternate hosts www.jihs.ut.ac.ir did not
#   answer from the sandbox and http://jihs.ut.ac.ir returned an
#   empty reply — the canonical https host is the wired one;
#   index fallbacks (Crossref/DOAJ -> the journal's DOIs) not
#   needed — the journal's own routes passed both bars.
# The fetch discipline is the §23 single-shot _arab_fetch and
# candidates are shaped by _arab_candidate, exactly like §34.
# --------------------------------------------------------------------------

JIHS_CAP = 8
_JIHS_BASE = "https://jihs.ut.ac.ir"
_JIHS_VENUE = "تاریخ علم (Journal for the History of Science)"


def _jihs_year(text):
    """Year from a JIHS issue line: the journal prints its Jalali
    years in LATIN digits (دوره 23، شماره 2، اسفند 1404), which
    _ensani_year — Persian digits, or Latin 19xx/20xx — misses;
    fall back to a 13xx/14xx Jalali match."""
    y = _ensani_year(text)
    if y:
        return y
    m = re.search(r"(?:13|14)\d{2}", str(text or ""))
    return int(m.group(0)) if m else None


def _parse_jihs_results(body):
    """Parse one JIHS search-results page: each hit is headed by
    an <h2 class="... list-article-title ..."> whose tag_a anchor
    links article_<id>.html; the text up to the next hit carries
    the issue line (<p class="mb-1">), a DOI paragraph
    (id="ar_doi"), the authors paragraph and — once the article
    is published — a direct PDF link (class="pdf_link"). Hits
    without a PDF link (online-first, no full text anywhere yet)
    are skipped, per the full-text-only law."""
    out = []
    if not body:
        return out
    marks = list(re.finditer(
        r'list-article-title[^>]*>\s*<a class="tag_a" '
        r'href="article_(\d+)\.html">(.*?)</a>', body, re.S))
    for i, m in enumerate(marks):
        seg = body[m.end():marks[i + 1].start()
                   if i + 1 < len(marks) else len(body)]
        pm = re.search(r'href="\./(article_\d+_[0-9a-f]+\.pdf)"', seg)
        if not pm:
            continue
        title = _arab_strip_tags(m.group(2))
        if not title:
            continue
        vm = re.search(r'<p class="mb-1">(.*?)</p>', seg, re.S)
        issue = _arab_strip_tags(vm.group(1)) if vm else ""
        dm = re.search(r'href="https://doi\.org/([^"]+)"', seg)
        am = re.search(r'<p class="mb-1 " dir="rtl" >(.*?)</p>',
                       seg, re.S)
        authors = []
        if am:
            authors = [a.strip() for a in
                       _arab_strip_tags(am.group(1)).split("،")
                       if a.strip()]
        cand = _arab_candidate(
            title, f"{_JIHS_BASE}/article_{m.group(1)}.html",
            "persian:jihs", authors=authors[:6],
            year=_jihs_year(issue),
            venue=(_JIHS_VENUE + (f" — {issue}" if issue else "")),
            doi=(dm.group(1) if dm else None))
        cand["oa_pdf_url"] = f"{_JIHS_BASE}/{pm.group(1)}"
        out.append(cand)
    return out


def search_jihs(query):
    """§36: search مجلهٔ تاریخ علم (Journal for the History of
    Science, University of Tehran) directly on the journal's own
    site — the direct route beside the §34 Ensani route, so the
    newest issues arrive without the portal's lag. Fail-soft,
    capped at JIHS_CAP."""
    url = (_JIHS_BASE + "/?" + urllib.parse.urlencode(
        {"_action": "article", "keywords": query}))
    status, body = _arab_fetch(url)
    if status != 200 or not body:
        return []
    return _parse_jihs_results(body)[:JIHS_CAP]


# --------------------------------------------------------------------------
# Iranian mathematics journals (§37) — the verified Iranian math
# journals registered in iran_math_registry.json (§17 wiring:
# credibility/priority signal only, never verification). Most
# Iranian university journals run the SAME Yektaweb journal
# system as §36's JIHS, so one theme-tolerant parser reads them
# all. Probed from the build sandbox on 2026-11-26 under the
# full-text-only law and the multi-route rule:
#   LIVE-WIRED (11) — the site search answers with real records
#   AND a hit's direct PDF was probe-verified (%PDF- magic, no
#   login): Sahand Communications in Mathematical Analysis
#   (Maragheh; topology -> 25 hits, 25 PDFs), فرهنگ و اندیشه
#   ریاضی (Iranian Mathematical Society; جبر -> 92-result badge,
#   25/25 PDFs), ریاضی و جامعه (Isfahan; جبر -> 46-result badge,
#   25 hits, 24 PDFs — the PDF-less hit is skipped), AUT Journal
#   of Mathematics and Computing (topology -> 25 hits, 17 PDFs),
#   Transactions on Combinatorics (Isfahan, older theme; graph
#   -> 25/25), Algebraic Structures and Their Applications
#   (Yazd; algebra -> 25/25), Iranian Journal of Mathematical
#   Chemistry (Kashan; graph -> 25/25), Categories and General
#   Algebraic Structures with Applications (Shahid Beheshti;
#   category -> 25/25), Journal of Algebra and Related Topics
#   (Guilan; algebra -> 25/25), Journal of Mathematical
#   Modeling (Guilan; differential -> 25/25, topology -> a
#   genuine 0), Analytical and Numerical Solutions for
#   Nonlinear Equations (Damghan, on the gadm host; analysis
#   -> 25/25).
#   REGISTRY-ONLY (8, routes tried in the registry notes):
#   BIMS (Springer — subscription full text), JIMS and the
#   Journal of Hyperstructures (HTTP 403 bot wall from the
#   sandbox), Mahani / JLTA / Communications in Combinatorics
#   and Optimization (hosts did not answer at all), Khayyam
#   Journal of Mathematics (new journal-system install returns
#   zero records; classic site has no search route), IJMSI
#   (article PDFs open and verified, but its only search is a
#   POST form whose POST the sandbox could not complete — no
#   verified machine search route).
# The fetch discipline is the §23 single-shot _arab_fetch and
# candidates are shaped by _arab_candidate, exactly like §36.
# --------------------------------------------------------------------------

IRAN_MATH_PER_JOURNAL_CAP = 8
IRAN_MATH_TOTAL_CAP = 24
_IRAN_MATH_JOURNALS = [
    ("https://scma.maragheh.ac.ir",
     "Sahand Communications in Mathematical Analysis"),
    ("https://mct.iranjournals.ir",
     "فرهنگ و اندیشه ریاضی (Mathematical Culture and Thought)"),
    ("https://math-sci.ui.ac.ir",
     "ریاضی و جامعه (Mathematics and Society)"),
    ("https://ajmc.aut.ac.ir",
     "AUT Journal of Mathematics and Computing"),
    ("https://toc.ui.ac.ir", "Transactions on Combinatorics"),
    ("https://as.yazd.ac.ir",
     "Algebraic Structures and Their Applications"),
    ("https://ijmc.kashanu.ac.ir",
     "Iranian Journal of Mathematical Chemistry"),
    ("https://cgasa.sbu.ac.ir",
     "Categories and General Algebraic Structures "
     "with Applications"),
    ("https://jart.guilan.ac.ir",
     "Journal of Algebra and Related Topics"),
    ("https://jmm.guilan.ac.ir", "Journal of Mathematical Modeling"),
    ("https://gadm.du.ac.ir",
     "Analytical and Numerical Solutions for Nonlinear "
     "Equations"),
]


def _parse_iran_math_results(body, base, venue_name):
    """Parse one Yektaweb search-results page (the §36 JIHS
    shape, theme-tolerant): each hit is headed by an element
    carrying list-article-title whose tag_a anchor links
    article_<id>.html; the text up to the next hit carries the
    issue line (first <p>), a DOI paragraph (id="ar_doi"), the
    authors paragraph and — once the article is published — a
    direct PDF link (class="pdf_link"). Both system themes are
    read (new: h2 + mb-1 paragraphs; old: h5 + margin-bottom-3
    paragraphs). Hits without a PDF link (no full text anywhere
    yet) are skipped, per the full-text-only law."""
    out = []
    if not body:
        return out
    marks = list(re.finditer(
        r'list-article-title[^>]*>\s*<a class="tag_a" '
        r'href="article_(\d+)\.html">(.*?)</a>', body, re.S))
    for i, m in enumerate(marks):
        seg = body[m.end():marks[i + 1].start()
                   if i + 1 < len(marks) else len(body)]
        pm = re.search(r'href="\./(article_\d+_[0-9a-f]+\.pdf)"', seg)
        if not pm:
            continue
        title = _arab_strip_tags(m.group(2))
        if not title:
            continue
        paras = [_arab_strip_tags(p) for p in re.findall(
            r'<p class="[^"]*"[^>]*>(.*?)</p>', seg, re.S)]
        issue = paras[0] if paras else ""
        dm = re.search(r'href="https://doi\.org/([^"]+)"', seg)
        authors = []
        for text in paras[1:]:
            if not text or "doi.org" in text or \
                    text.startswith("10."):
                continue
            authors = [a.strip() for a in
                       re.split(r"[،,;]", text) if a.strip()]
            break
        cand = _arab_candidate(
            title, f"{base}/article_{m.group(1)}.html",
            "persian:iran-math", authors=authors[:6],
            year=_jihs_year(issue),
            venue=(venue_name + (f" — {issue}" if issue else "")),
            doi=(dm.group(1) if dm else None))
        cand["oa_pdf_url"] = f"{base}/{pm.group(1)}"
        out.append(cand)
    return out


def _iran_math_journal(base, venue_name, query):
    """One journal of §37: its own site search, fail-soft,
    capped per journal."""
    url = (base + "/?" + urllib.parse.urlencode(
        {"_action": "article", "keywords": query}))
    status, body = _arab_fetch(url)
    if status != 200 or not body:
        return []
    return _parse_iran_math_results(
        body, base, venue_name)[:IRAN_MATH_PER_JOURNAL_CAP]


def search_iran_math(query):
    """§37: search the live-wired Iranian mathematics journals
    directly on their own sites — the Yektaweb route of §36
    generalised to the eleven journals that passed both bars
    (registry-only journals are not queried; their articles
    arrive via DOAJ/Crossref/OpenAlex). Fail-soft per journal,
    capped at IRAN_MATH_PER_JOURNAL_CAP per journal and
    IRAN_MATH_TOTAL_CAP overall."""
    out = []
    for base, venue_name in _IRAN_MATH_JOURNALS:
        if len(out) >= IRAN_MATH_TOTAL_CAP:
            break
        try:
            out.extend(_iran_math_journal(base, venue_name, query))
        except Exception:
            continue
    return out[:IRAN_MATH_TOTAL_CAP]


# --------------------------------------------------------------------------
# Tajikistan journals (§38) — the verified Tajik journals
# registered in tajik_journals_registry.json (§17 wiring:
# credibility/priority signal only, never verification).
# Tajikistan's own journal sites offer no article-level keyword
# search (multi-route probe from the build sandbox, 2026-11-26):
# the Academy portal journals.anrt.tj is static per-journal
# listings of whole-issue PDFs with no search form;
# vestnik-tnu.com's Joomla finder answers «ничего не найдено»
# even for «математика» (its index does not cover the content);
# uchzap.tj's finder returns ISSUE-archive pages, not articles
# (whole-issue PDF attachments, recent issues via Google Drive);
# tut.tj is a WordPress upload archive; eLibrary carries the
# journals but its full texts need registration (excluded by
# the no-key law); DOAJ carries ZERO Tajik journals
# (API-checked, publisher_country:TJ -> total 0) and the
# journals register no Crossref DOIs (a container-title probe
# matched only the Russian Academy's Doklady).
# The one route that passes both bars is CyberLeninka: eleven
# Tajik journals are carried there with article-level full
# text, and its search API —
#   SEARCH — POST cyberleninka.ru/api/search, JSON body
#       {"mode": "articles", "q": <q>, "size": 8, "from": 0,
#        "journal": <numeric journal id>}
#       ANSWERS with per-journal keyword results (Доклады
#       НАНТ, journal 28997: математика -> 527 hits in the
#       journal; астрономия -> 38, incl. papers of the
#       Academy's Institute of Astrophysics, Sanglok).
#   FULL TEXT — each hit's own PDF
#       GET cyberleninka.ru/article/n/<slug>/pdf
#       probe-verified WITHOUT login: %PDF-1.7. Candidates
#       therefore carry oa_pdf_url = the article's /pdf route,
#       and attach_fulltexts_v2 fetches it directly.
# This supersedes the §24 probe of CyberLeninka, which never
# got a keyword answer (GET /api/search -> 405, form POST ->
# 400/empty, JSON without "mode" -> found:0): the missing
# piece was mode:"articles" (the web app's own transport).
# Transport honesty: from the build sandbox the platform
# intermittently closes the body stream to Python clients
# (HTTP 200, empty body) for stretches of time, while curl —
# and urllib in other windows — succeed (the first live smoke
# returned real candidates through this very path); the
# per-journal fetch retries up to three times and fail-soft
# absorbs a wholly bad window. The engine's production runs
# execute on GitHub's runners, a different egress.
# Registry-only (7, routes tried in the registry notes): the
# Academy's Известия series (физико-математических — classic
# and 2023 series — биологических и медицинских, общественных
# наук) and its Медицинский вестник (portal listings only, not
# on CyberLeninka); Вестник ТНУ (empty finder index);
# Вестник ТУТ (no search route).
# Languages: Tajik (tg) is NOT in MATRIX_LANGUAGES — the gap
# is recorded here, not silently patched. The wired journals
# publish mainly in Russian (ru, in the matrix) and Tajik,
# several with English abstracts, and the platform also
# indexes English titles/abstracts.
# The fetch discipline is the §23 single-shot _arab_fetch
# (up to three attempts per journal, see _tajik_cl_journal)
# and candidates are shaped by _arab_candidate, exactly like
# §23/§37.
# --------------------------------------------------------------------------

TAJIK_PER_JOURNAL_CAP = 8
TAJIK_TOTAL_CAP = 24
_CL_BASE = "https://cyberleninka.ru"
_CL_API = _CL_BASE + "/api/search"
_TAJIK_CL_JOURNALS = [
    (28997, "Доклады Национальной академии наук Таджикистана"),
    (4799, "Вестник Педагогического университета"),
    (36728, "Вестник педагогического университета "
            "(Естественных наук)"),
    (36890, "Историк (Муаррих)"),
    (12079, "Ученые записки Худжандского государственного "
            "университета им. академика Б. Гафурова. "
            "Гуманитарные науки"),
    (32457, "Вестник Таджикского государственного университета "
            "права, бизнеса и политики. Серия общественных наук"),
    (18700, "Вестник Таджикского государственного университета "
            "права, бизнеса и политики. Серия гуманитарных наук"),
    (36727, "Вестник педагогического университета (Серия 2)"),
    (37205, "Финансово-экономический вестник"),
    (27560, "Вестник Авиценны"),
    (29952, "Здравоохранение Таджикистана"),
]


def _parse_tajik_cl_results(data, venue_fallback):
    """Parse one CyberLeninka journal-filtered search response:
    {found, articles: [{name (may carry <b> highlight tags),
    annotation, link (/article/n/<slug>), authors, year,
    journal, journal_link}]}. Hits without an article link or a
    title are skipped (no reachable full text)."""
    out = []
    if not isinstance(data, dict):
        return out
    for art in data.get("articles") or []:
        if not isinstance(art, dict):
            continue
        link = str(art.get("link") or "")
        title = _arab_strip_tags(art.get("name"))
        if not link or not title:
            continue
        url = _CL_BASE + link if link.startswith("/") else link
        year = art.get("year")
        cand = _arab_candidate(
            title, url, "tajik:cyberleninka",
            authors=[str(a) for a in (art.get("authors") or [])][:6],
            year=(year if isinstance(year, int)
                  else _arab_year(year)),
            venue=str(art.get("journal") or venue_fallback),
            abstract=_arab_strip_tags(art.get("annotation"))[:1500])
        cand["oa_pdf_url"] = url + "/pdf"
        out.append(cand)
        if len(out) >= TAJIK_PER_JOURNAL_CAP:
            break
    return out


def _tajik_cl_journal(journal_id, venue_name, query):
    """One Tajik journal on CyberLeninka: the platform's search
    API filtered to the journal, fail-soft, capped per journal.
    Up to three attempts: the platform intermittently closes
    the body stream to Python clients (HTTP 200 with an empty
    body, or a dropped connection) for stretches of time —
    curl, and urllib in other windows, succeed (the same
    transport behaviour §25 recorded for Shamela) — so a later
    attempt in the same call often lands in a better window,
    and fail-soft absorbs a wholly bad one."""
    payload = json.dumps(
        {"mode": "articles", "q": query,
         "size": TAJIK_PER_JOURNAL_CAP, "from": 0,
         "journal": journal_id}).encode("utf-8")
    for _attempt in range(3):
        status, body = _arab_fetch(_CL_API, data=payload)
        if status != 200 or not body:
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue
        return _parse_tajik_cl_results(data, venue_name)
    return []


def search_tajik_journals(query):
    """§38: search the eleven verified Tajik journals carried
    by CyberLeninka, each through the platform's
    journal-filtered search API, with article full text (the
    /pdf route) probe-verified reachable. The registry-only
    Tajik journals (the Academy's Известия series and its
    Медицинский вестник, Вестник ТНУ, Вестник ТУТ) have no
    article-level keyword route anywhere and are not queried
    here. Fail-soft per journal, capped at
    TAJIK_PER_JOURNAL_CAP per journal and TAJIK_TOTAL_CAP
    overall."""
    out = []
    for journal_id, venue_name in _TAJIK_CL_JOURNALS:
        if len(out) >= TAJIK_TOTAL_CAP:
            break
        try:
            out.extend(_tajik_cl_journal(journal_id, venue_name,
                                         query))
        except Exception:
            continue
    return out[:TAJIK_TOTAL_CAP]


# --------------------------------------------------------------------------
# CORE — Connecting Repositories (§42) — the Open University's
# global open-access aggregator (core.ac.uk), registered in
# core_registry.json (§17 wiring: credibility/priority signal
# only, never verification). CORE was already present in the
# pool as a plain index entry (sources_registry_2), a thesis
# archive (thesis_registry) and a §27 digital library —
# registry-only there under the no-key law, after the §27 probe
# of the unkeyed API answered HTTP 429. The §40 survey classed
# it A (wireable after a free email-only registration) and
# Jupiter registered himself on 2026-11-26; the key was emailed
# to him. This section wires the API live. API shape verified
# on 2026-11-26 against the OFFICIAL OpenAPI specification
# (https://api.core.ac.uk/swagger/v3.json — the document behind
# the Swagger UI at https://api.core.ac.uk/docs/v3):
#   SEARCH — POST /v3/search/{entityType} with a JSON
#       SearchParameters body {q, limit, offset} (the documented
#       twin GET /v3/search/{entityType}/ takes the same
#       parameters in the query string) answers a SearchResponse
#       {totalHits, limit, offset, results: [Work]}. A Work
#       carries id, doi, title, authors, abstract,
#       yearPublished, language, journals, publisher, links —
#       and the full-text fields the spec names verbatim:
#       downloadUrl ("The URL from where you can download the
#       full text of the work"), fullText ("plain text
#       extraction of the full text document") and
#       sourceFulltextUrls ("A list of all the URLs where this
#       document can be downloaded from"). The spec prints
#       snake_case names; the live API answers camelCase — the
#       parser accepts both spellings of every field.
#   DISCOVERY — the operation Jupiter pointed at (docs tag
#       "Discovery", operation post_discovery): POST
#       /v3/discover with a DiscoveryParameters body
#       {doi, title, year, eprints_id, plugin_id, referrer}
#       answers a DiscoveryResponse {fullTextLink, source} —
#       the free full-text link for ONE known paper. The tag
#       and both schemas are verified in the OpenAPI document;
#       the path string is the one recorded by the official
#       documentation's write-ups of the Discovery API. It is
#       used here ONLY to resolve a full-text link for a search
#       hit that carries a DOI but no download URL, fail-soft,
#       under a small per-call budget.
#   DOWNLOAD — GET /v3/works/{identifier}/download is the
#       spec's own file route; a hit's own downloadUrl is
#       preferred (it is the provider/CORE PDF directly).
#   AUTH — the spec's security schemes offer the key as an
#       api_key query parameter or as the header
#       "Authorization: Bearer [API_KEY]"; the header is used.
#       The key is read from the CORE_API_KEY environment
#       variable (a repo Actions secret, pasted ONCE by Jupiter
#       — the cross-service paste law). With NO key configured
#       the searcher returns nothing and makes NO request: the
#       unkeyed API is rate-limited to uselessness (§27 probe:
#       HTTP 429) and withholds fullText from public users, so
#       an unkeyed call could not satisfy the full-text law
#       anyway. Exactly the optional-brain-key discipline.
#   FULL TEXT — a hit is emitted ONLY when it carries a
#       downloadUrl or a sourceFulltextUrls entry (or the
#       Discovery call resolves one for its DOI): candidates
#       carry oa_pdf_url = that URL and attach_fulltexts_v2
#       fetches it via its index-fulltext route, exactly like
#       §34/§36. Metadata-only hits are skipped, per the
#       full-text-only law. The query is first biased with the
#       documented _exists_:fullText clause (CORE API v3
#       announcement) so the top of the result list is
#       full-text-bearing; if that returns nothing at all, one
#       plain-query retry runs and the client-side filter still
#       enforces the law.
#   HONEST CAVEAT — no KEYED live smoke has run yet: the key
#       exists only in Jupiter's mailbox (registration
#       2026-11-26). Until he pastes CORE_API_KEY into the repo
#       secrets the searcher stays dormant (fail-soft empty)
#       and this wiring rests on the official spec + the
#       mocked-shape tests in test_core.py.
# The fetch discipline is a single-shot _core_fetch (the §23
# pattern, with the Authorization header) and candidates are
# shaped by _arab_candidate, exactly like §36.
# --------------------------------------------------------------------------

CORE_CAP = 8
CORE_FETCH_LIMIT = 25  # results requested per search; the
                       # full-text filter + CORE_CAP cut them
CORE_DISCOVERY_BUDGET = 4  # discovery resolves per search call
_CORE_BASE = "https://api.core.ac.uk"
_CORE_SEARCH_URL = _CORE_BASE + "/v3/search/works"
_CORE_DISCOVER_URL = _CORE_BASE + "/v3/discover"
_CORE_TIMEOUT = 15  # seconds, single attempt


def _core_api_key():
    """The CORE API key from the environment, or "" — the
    searcher is dormant without it (see the §42 note above)."""
    return (os.environ.get("CORE_API_KEY") or "").strip()


def _core_fetch(url, data=None):
    """One single-shot HTTPS request to the CORE API carrying
    the Bearer key (no retries: http_request's 6 attempts x
    180 s would stall the wave, and a 401/429 here is definitive
    until the key exists). Returns (status, text); (None, "")
    on any transport error. A truncated chunked body is
    salvaged, as in _arab_fetch."""
    import http.client
    headers = {"User-Agent": _ARAB_HEADERS["User-Agent"],
               "Accept": "application/json",
               "Authorization": "Bearer " + _core_api_key()}
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        _pace(urllib.parse.urlparse(url).netloc)
        req = urllib.request.Request(url, data=data,
                                     headers=headers)
        with urllib.request.urlopen(req,
                                    timeout=_CORE_TIMEOUT) as resp:
            try:
                raw = resp.read(2_000_000)
            except http.client.IncompleteRead as e:
                raw = e.partial or b""
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(300_000).decode("utf-8",
                                                  "replace")
        except Exception:
            return e.code, ""
    except Exception:
        return None, ""


def _core_field(item, *names):
    """First non-empty value among the given field spellings
    (the spec prints snake_case, the live API answers
    camelCase)."""
    for n in names:
        v = item.get(n)
        if v not in (None, "", []):
            return v
    return None


def _core_authors(raw):
    """CORE authors: plain strings in the spec's Work schema,
    {name} / {fullName} objects in live answers — accept all."""
    out = []
    for a in raw or []:
        if isinstance(a, str):
            name = a
        elif isinstance(a, dict):
            name = a.get("name") or a.get("fullName") or ""
        else:
            name = ""
        name = str(name).strip()
        if name:
            out.append(name)
    return out[:6]


def _core_work_candidate(work):
    """One CORE Work -> (candidate, has_fulltext), or
    (None, False) when the record is unusable. The candidate's
    oa_pdf_url is set only when the record itself names a
    reachable full text (downloadUrl first, then the first
    sourceFulltextUrls entry); a DOI-only record comes back
    with has_fulltext False so search_core can try the
    Discovery resolution for it."""
    if not isinstance(work, dict):
        return None, False
    title = str(_core_field(work, "title") or "").strip()
    if not title:
        return None, False
    doi = _core_field(work, "doi")
    doi = str(doi).strip() if doi else None
    if doi and doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    if doi and not doi.startswith("10."):
        doi = None
    ft_url = _core_field(work, "downloadUrl", "download_url")
    if not ft_url:
        srcs = _core_field(work, "sourceFulltextUrls",
                           "source_fulltext_urls") or []
        if isinstance(srcs, list):
            for u in srcs:
                if isinstance(u, str) and u.startswith("http"):
                    ft_url = u
                    break
    year = _core_field(work, "yearPublished", "year_published")
    if isinstance(year, str):
        year = _arab_year(year)
    if not isinstance(year, int):
        year = None
    venue = ""
    journals = _core_field(work, "journals") or []
    if isinstance(journals, list) and journals:
        j0 = journals[0]
        venue = (j0.get("title") or "") if isinstance(j0, dict) \
            else str(j0)
    if not venue:
        pub = _core_field(work, "publisher")
        if isinstance(pub, dict):
            venue = pub.get("name") or ""
        elif pub:
            venue = str(pub)
    url = ""
    if doi:
        url = "https://doi.org/" + doi
    else:
        links = _core_field(work, "links") or []
        if isinstance(links, list):
            for lnk in links:
                if isinstance(lnk, str) and lnk.startswith("http"):
                    url = lnk
                    break
                if isinstance(lnk, dict) \
                        and lnk.get("type") == "display" \
                        and str(lnk.get("url") or "").startswith(
                                "http"):
                    url = lnk["url"]
                    break
        if not url and ft_url:
            url = ft_url
    abstract = str(_core_field(work, "abstract") or "")
    cand = _arab_candidate(
        title, url, "core:api",
        authors=_core_authors(_core_field(work, "authors")),
        year=year, venue=str(venue or "").strip(), doi=doi,
        abstract=re.sub(r"\s+", " ", abstract).strip()[:1500])
    lang = _core_field(work, "language")
    if isinstance(lang, dict):
        lang = lang.get("code")
    if isinstance(lang, str) and lang.strip():
        cand["language"] = lang.strip().lower()
    if ft_url:
        cand["oa_pdf_url"] = ft_url
        cand["fulltext_url"] = ft_url
        return cand, True
    return cand, False


def _parse_core_results(data):
    """Parse one CORE works SearchResponse: returns
    (candidates_with_fulltext, doi_only_candidates). Records
    with neither a full-text URL nor a DOI are dropped — no
    route to their full text exists, per the full-text-only
    law."""
    out, pending = [], []
    if not isinstance(data, dict):
        return out, pending
    for work in data.get("results") or []:
        cand, has_ft = _core_work_candidate(work)
        if cand is None:
            continue
        if has_ft:
            out.append(cand)
        elif cand.get("doi"):
            pending.append(cand)
    return out, pending


def _core_discover_fulltext(doi, title, year):
    """§42: the CORE Discovery operation (POST /v3/discover,
    DiscoveryParameters {doi, title, year}) -> the free
    full-text link for one known paper, or "". Fail-soft on
    every failure shape."""
    if not doi and not title:
        return ""
    payload = {}
    if doi:
        payload["doi"] = doi
    if title:
        payload["title"] = title
    if isinstance(year, int):
        payload["year"] = year
    status, body = _core_fetch(
        _CORE_DISCOVER_URL,
        data=json.dumps(payload).encode("utf-8"))
    if status != 200 or not body:
        return ""
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    link = _core_field(data, "fullTextLink", "full_text_link")
    return str(link) if link else ""


def _core_search_once(q):
    """One POST /v3/search/works call -> _parse_core_results
    output, or None on any failure (fail-soft upstream)."""
    payload = json.dumps({"q": q, "limit": CORE_FETCH_LIMIT,
                          "offset": 0}).encode("utf-8")
    status, body = _core_fetch(_CORE_SEARCH_URL, data=payload)
    if status != 200 or not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return _parse_core_results(data)


def search_core(query):
    """§42: search CORE (Connecting Repositories) — the global
    open-access aggregator — through its API v3 works search.
    Dormant (returns [], no request made) while CORE_API_KEY is
    not configured. Fail-soft, capped at CORE_CAP full-text
    candidates per query."""
    if not _core_api_key():
        return []
    query = (query or "").strip()
    if not query:
        return []
    parsed = _core_search_once(
        f"({query}) AND _exists_:fullText")
    if parsed is None:
        return []
    cands, pending = parsed
    if not cands and not pending:
        parsed = _core_search_once(query)
        if parsed is None:
            return []
        cands, pending = parsed
    budget = CORE_DISCOVERY_BUDGET
    for cand in pending:
        if len(cands) >= CORE_CAP or budget <= 0:
            break
        budget -= 1
        link = _core_discover_fulltext(cand.get("doi"),
                                       cand.get("title"),
                                       cand.get("year"))
        if link:
            cand["oa_pdf_url"] = link
            cand["fulltext_url"] = link
            cands.append(cand)
    return cands[:CORE_CAP]


# --------------------------------------------------------------------------
# Source registries (§17) — credibility / whitelist SIGNAL only.
# Sixteen curated registries sit beside this script in the repo:
#   sources_registry.json        (registry 1, registry schema)
#   sources_registry_500_more.json (500 DOAJ journals, compact schema:
#                                   {id, name, url, type, fields,
#                                    languages, country, checked,
#                                    http_status} — no verified/api)
#   sources_registry_2.json      (registry 2, registry schema)
#   thesis_registry.json         (thesis-HOSTING sources, registry
#                                   schema — repositories and ETD
#                                   archives, not individual theses)
#   arab_journals_registry.json  (§23: the eight open-access
#                                   Arab-country journal platforms,
#                                   registry schema — platform entries,
#                                   not individual journals)
#   asian_journals_registry.json (§24: the verified open-access
#                                   Asian-country journal platforms,
#                                   registry schema — platform entries,
#                                   not individual journals)
#   arab_libraries_registry.json (§25: the great Arabic digital
#                                   libraries + the Iraqi Journal of
#                                   Science alternate route, registry
#                                   schema — library/platform entries)
#   european_journals_registry.json (§26: the verified open-access
#                                   European-country journal
#                                   platforms, registry schema —
#                                   platform entries, not journals)
#   world_libraries_registry.json (§27: the world's free digital
#                                   libraries, institutional and
#                                   personal, registry schema —
#                                   library entries, not books)
#   lectures_registry.json       (§32: the verified open-text
#                                   university lecture platforms of
#                                   surveys §29-§31, registry schema —
#                                   platform entries, not lectures)
#   ensani_registry.json         (§34: the Ensani portal — پرتال
#                                   جامع علوم انسانی, the Persian
#                                   humanities article bank,
#                                   registry schema — one platform
#                                   entry, not journals)
#   jihs_registry.json           (§36: مجلهٔ تاریخ علم — the
#                                   Journal for the History of
#                                   Science, University of Tehran,
#                                   wired DIRECTLY on its own site
#                                   beside the §34 Ensani route,
#                                   registry schema — one journal
#                                   entry)
#   iran_math_registry.json      (§37: the Iranian mathematics
#                                   journals — 19 verified entries,
#                                   11 wired DIRECTLY on their own
#                                   Yektaweb sites, 8 registry-only
#                                   with routes tried recorded,
#                                   registry schema — journal
#                                   entries, not articles)
#   tajik_journals_registry.json (§38: the Tajikistan journals —
#                                   18 verified entries, 11 wired
#                                   via the CyberLeninka
#                                   journal-filtered search API,
#                                   7 registry-only with routes
#                                   tried recorded, registry
#                                   schema — journal entries, not
#                                   articles)
#   utihs_registry.json          (§39: the portal of the
#                                   Institute for the History of
#                                   Science, University of Tehran —
#                                   پژوهشکدهٔ تاریخ علم, whose
#                                   journal is wired directly in
#                                   §36; the portal itself is
#                                   REGISTRY-ONLY: the whole host
#                                   sits behind an ArvanCloud
#                                   JavaScript bot wall that no
#                                   keyless machine route passes,
#                                   routes tried recorded in the
#                                   registry notes, registry
#                                   schema — one platform entry)
#   core_registry.json           (§42: CORE — Connecting
#                                   Repositories, the Open
#                                   University's global
#                                   open-access aggregator,
#                                   wired LIVE via its API v3
#                                   works search + Discovery
#                                   full-text resolution, keyed
#                                   by CORE_API_KEY, registry
#                                   schema — one platform entry)
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
                  "thesis_registry.json",
                  "arab_journals_registry.json",
                  "asian_journals_registry.json",
                  "arab_libraries_registry.json",
                  "european_journals_registry.json",
                  "world_libraries_registry.json",
                  "lectures_registry.json",
                  "ensani_registry.json",
                  "jihs_registry.json",
                  "iran_math_registry.json",
                  "tajik_journals_registry.json",
                  "utihs_registry.json",
                  "core_registry.json"]
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


# --------------------------------------------------------------------------
# §41 — the SHELF (قفسه; Jupiter's reuse law, 2026-11-26: «previously
# collected material + new finds — don't redo work»). Until now every
# production re-searched and re-downloaded everything from zero. The
# shelf stores material that was verified ONCE — in past runs or in a
# one-time verified fetch — as extracted text only (<=200 KB per item,
# the same cap as the fulltext storage law; this is the one deliberate
# exception to the no-persistent-files rule, approved by Jupiter),
# under shelves/<name>/ with a manifest.json per shelf. The shelf is
# the BASE layer: search_shelf runs FIRST in wave 1 and the open waves
# top up exactly as before. Shelf candidates carry their stored
# extracted passages directly (no re-download) and then flow through
# the SAME verification, deterministic screen, LLM judges and §20 gate
# as every other candidate — the shelf gives no free pass. Loading is
# fail-soft: a missing/corrupt shelves/ tree logs one line and the run
# continues exactly as before §41.
# --------------------------------------------------------------------------

SHELF_ROOT = os.path.join(_REGISTRY_DIR, "shelves")
SHELF_MAX_CANDIDATES = 8  # per query, like the other searchers' caps
_SHELF_CACHE = None


def load_shelves(base_dir=None):
    """Load every shelf under shelves/: returns a list of
    {name, items: [manifest item, ...]}. Fail-soft per shelf and per
    item: anything unreadable is skipped with one log line. Cached
    after the first load (the shelf does not change mid-run)."""
    global _SHELF_CACHE
    if _SHELF_CACHE is not None and base_dir is None:
        return _SHELF_CACHE
    root = base_dir or SHELF_ROOT
    shelves = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for name in names:
        sdir = os.path.join(root, name)
        mpath = os.path.join(sdir, "manifest.json")
        if not os.path.isdir(sdir) or not os.path.isfile(mpath):
            continue
        try:
            with open(mpath, encoding="utf-8") as f:
                manifest = json.load(f)
            items = manifest.get("items")
            if not isinstance(items, list):
                raise ValueError("manifest items is not a list")
        except Exception as e:
            print(f"shelf: {name} manifest unavailable ({e}) — "
                  "continuing without it")
            continue
        good = []
        for it in items:
            if not isinstance(it, dict):
                continue
            if not it.get("id") or not it.get("work_title") \
                    or not it.get("text_file"):
                continue
            it = dict(it)
            it["_shelf_dir"] = sdir
            good.append(it)
        shelves.append({"name": name, "items": good})
    if base_dir is None:
        _SHELF_CACHE = shelves
    return shelves


_SHELF_STOPWORDS = {
    "the", "for", "and", "with", "from", "that", "this", "its",
    "against", "between", "در", "از", "به", "برای", "که", "را",
    "آن", "این", "في", "من", "إلى", "على", "أن", "ما", "لا",
}


def _shelf_query_terms(query):
    """Search terms from a query string: the whole phrase plus each
    content word of >=3 chars (function words dropped — otherwise a
    word like 'the' floods the passage extraction; Arabic-script
    words match as-is)."""
    q = (query or "").strip()
    terms = []
    if len(q) >= 3:
        terms.append(q)
    for w in re.split(r"[^\w\u0600-\u06FF]+", q):
        w = w.strip().lower()
        if len(w) >= 3 and w not in _SHELF_STOPWORDS and w not in terms:
            terms.append(w)
    return terms


def _shelf_work_text(items):
    """The full stored text of one work: its parts concatenated in
    order. None when any part is unreadable (fail-soft per work)."""
    parts = []
    for it in sorted(items, key=lambda x: x.get("part") or 1):
        path = os.path.join(it["_shelf_dir"], it["text_file"])
        try:
            with open(path, encoding="utf-8") as f:
                parts.append(f.read())
        except OSError:
            return None
    return "\n".join(parts)


def search_shelf(query):
    """§41 shelf searcher: match the query against every shelf's
    manifests (title/author/keywords) and grep the stored texts at
    passage level. Emits ONE candidate per matching WORK, carrying
    passages extracted from the stored text — zero network fetches.
    A work is emitted only when real passages hit (query terms first,
    the work's own manifest keywords as the fallback term set)."""
    terms = _shelf_query_terms(query)
    if not terms:
        return []
    out = []
    for shelf in load_shelves():
        works = {}
        for it in shelf["items"]:
            works.setdefault(it.get("work_id") or it["id"],
                             []).append(it)
        scored = []
        for work_id, items in works.items():
            head = items[0]
            meta = " ".join([
                head.get("work_title") or "",
                head.get("author") or "",
                " ".join(head.get("keywords") or []),
            ]).lower()
            meta_hit = any(t.lower() in meta for t in terms)
            text = _shelf_work_text(items)
            if text is None:
                continue
            excerpt, hits = extract_passages(text, terms)
            if not excerpt and meta_hit:
                excerpt, hits = extract_passages(
                    text, head.get("keywords") or [])
            if not excerpt:
                continue
            scored.append((hits, head, excerpt))
        scored.sort(key=lambda x: -x[0])
        for hits, head, excerpt in scored[:SHELF_MAX_CANDIDATES]:
            kind = (head.get("kind") or "").strip()
            cand = {
                "title": head.get("work_title") or "",
                "authors": [head["author"]] if head.get("author")
                           else [],
                "year": head.get("year"),
                "venue": head.get("source_platform") or "Shelf",
                "doi": head.get("doi"),
                "url": head.get("source_url") or "",
                "abstract": excerpt[:1500],
                "arxiv_id": None,
                "index": f"shelf:{shelf['name']}",
                "language": head.get("language"),
                "fulltext": True,
                "fulltext_excerpt": excerpt[:FULLTEXT_EXCERPT_CHARS],
                "fulltext_route": f"shelf:{shelf['name']}",
                "passages": True,
                "passage_hits": hits,
                "shelf_item": True,
                "shelf": shelf["name"],
                "shelf_work": head.get("work_id") or head["id"],
            }
            if kind == "primary book":
                cand["source_type"] = "primary"
                cand["primary_relation"] = "author"
            out.append(cand)
    return out


SEARCHERS = [search_shelf, search_openalex, search_crossref, search_openaire,
             search_hal, search_zbmath, search_europe_pmc,
             search_internet_archive, search_google_books,
             search_annas_archive, search_doaj, search_arxiv,
             search_semantic_scholar, search_arab_platforms,
             search_asian_platforms, search_arab_libraries,
             search_european_platforms, search_digital_libraries,
             search_university_lectures, search_ensani, search_jihs,
             search_iran_math, search_tajik_journals, search_core]

# English-first retrieval (§18): wave 1 queries the abstract-rich
# indexes ONLY — Crossref is held back because, queried with a
# non-English topic, it floods the candidate pool with keyword noise
# (Persian/Afghan matches) that crowds out the genuine EN/FR/DE
# scholarship before the relevance screen ever sees it. Crossref runs
# as a top-up only when wave 1 yields too few verified candidates.
WAVE1_SEARCHERS = [search_shelf, search_openalex, search_europe_pmc, search_hal,
                   search_openaire, search_semantic_scholar, search_doaj,
                   search_zbmath, search_internet_archive,
                   search_google_books, search_arxiv,
                   search_annas_archive, search_arab_platforms,
                   search_asian_platforms, search_arab_libraries,
                   search_european_platforms, search_digital_libraries,
                   search_university_lectures, search_ensani, search_jihs,
                   search_iran_math, search_tajik_journals, search_core]
CROSSREF_SEARCHERS = [search_crossref]


# --------------------------------------------------------------------------
# Search scope (§43, Jupiter 2026-11-26: «یعنی قفسه بعلاوه موارد
# جدید» — for the article, examine ONLY the newly added items).
# The SEARCH_SCOPE environment variable selects the candidate
# pool for a run:
#   "full" (default; also any unrecognised value) — today's
#       behaviour, byte-for-byte: every searcher in SEARCHERS /
#       WAVE1_SEARCHERS, the Crossref top-up, wave 2 and the
#       §19 primary lane all run as before.
#   "new" — the candidate search runs ONLY the searchers added
#       in the newest build wave (§§32–42), i.e. the shelf plus
#       the new items:
#         search_shelf               (§41 — the collected shelf;
#                                     carries the primary books
#                                     WITH passages, so the
#                                     text-first gate's primary
#                                     leg stays satisfiable)
#         search_university_lectures (§32)
#         search_ensani              (§34)
#         search_jihs                (§36)
#         search_iran_math           (§37)
#         search_tajik_journals      (§38)
#         search_core                (§42)
#       (§39 utihs and §40 are registry-only — they have no
#       searcher to include; §33 is the review layer, not a
#       searcher.) Every older searcher is skipped: the filter
#       sits in collect_verified, the single choke point every
#       retrieval wave flows through, so wave 1, the wave-2
#       broader wave and any other collect_verified call are
#       scoped alike; the Crossref top-up is skipped outright
#       and the §19 primary lane (IA + Google Books catalogue
#       searches — pre-§32 searchers) stands down, its role
#       taken by the shelf. EVERYTHING downstream is unchanged:
#       the same screening, the same judges, the same fail-closed
#       text-first gate (MIN_SOURCES counted, MIN_FULLTEXT_SOURCES
#       with passages, >= 1 primary with passages), §20.10 topic
#       adaptation, the §33 review layer and delivery. The scope
#       is VISIBLE: a line in the funnel (§16.3), a line in the
#       §20.7 report, and a note at the head of the article
#       itself (search_scope_note), all only in "new" mode.
# --------------------------------------------------------------------------

NEW_ONLY_SEARCHERS = [search_shelf, search_university_lectures,
                      search_ensani, search_jihs, search_iran_math,
                      search_tajik_journals, search_core]
_NEW_ONLY_NAMES = {s.__name__ for s in NEW_ONLY_SEARCHERS}


def search_scope():
    """The active search scope (§43): "new" only when the
    SEARCH_SCOPE environment variable says so; "full" otherwise
    (unset, "full", or anything unrecognised — fail-safe to the
    established behaviour)."""
    return "new" if (os.environ.get("SEARCH_SCOPE") or "") \
        .strip().lower() == "new" else "full"


def scoped_searchers(order):
    """The searcher order for one retrieval wave under the
    active scope (§43): unchanged in "full" mode; in "new" mode
    only the §§32–42 searchers survive, in the caller's order."""
    order = list(order)
    if search_scope() != "new":
        return order
    return [s for s in order if s.__name__ in _NEW_ONLY_NAMES]


def _scope_lines():
    """§43 visibility lines (Persian) for the funnel / §20.7
    report. Empty in "full" mode."""
    if search_scope() != "new":
        return []
    return ["",
            "دامنهٔ جستجو: فقط منابع تازه‌افزودهٔ §§32–42 "
            "(قفسهٔ منابع گردآوری‌شده، CORE، سخنرانی‌های دانشگاهی، "
            "ensani، jihs، مجلات ریاضی ایران، مجلات تاجیکستان)"]


def search_scope_note(language):
    """The in-article §43 note (in the article's language),
    naming the restricted search scope of this production."""
    if (language or "").strip().lower().startswith("fa"):
        return ("> یادداشت: دامنهٔ جستجو در این تولید فقط منابع "
                "تازه‌افزودهٔ §§32–42 بوده است (قفسهٔ منابع "
                "گردآوری‌شده، CORE، سخنرانی‌های دانشگاهی، ensani، "
                "jihs، مجلات ریاضی ایران و مجلات تاجیکستان)؛ همهٔ "
                "دروازه‌های کیفیت بدون تغییر اعمال شده‌اند.")
    return ("> Note: this production searched ONLY the newly "
            "added sources (§§32–42: the collected-material "
            "shelf, CORE, university lectures, ensani, jihs, "
            "Iranian mathematics journals and Tajik journals); "
            "all quality gates applied unchanged.")


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
    "search_arab_platforms": "Arab platforms",
    "search_asian_platforms": "Asian platforms",
    "search_arab_libraries": "Arab libraries",
    "search_european_platforms": "European platforms",
    "search_digital_libraries": "Digital libraries",
    "search_university_lectures": "University lectures",
    "search_shelf": "Shelf (§41)",
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


# --------------------------------------------------------------------------
# Language gate at intake (§18): Persian/Arabic candidates are dropped
# BEFORE verification — the SOURCE LAW admits credible scholarship in
# English, French or German only, and the Persian/Afghan keyword noise
# was consuming the whole candidate cap + verification budget. The one
# exception is a primary text in a scholarly edition / translation of
# the EN/FR/DE tradition (e.g. a critical edition of Biruni's own
# works). The gate never admits anything by itself: survivors still
# face the unchanged verification + relevance + content laws.
# --------------------------------------------------------------------------

_GATE_LANGUAGES = {"fa", "fas", "per", "ar", "ara", "ur", "urd"}
_EDITION_MARKERS = ("critical edition", "scholarly edition",
                    "édition critique", "kritische ausgabe",
                    "translated by", "translated from",
                    "english translation", "french translation",
                    "german translation", "translation of",
                    "edited by", "ed. and tr", "éd. critique")


def _arabic_script_ratio(text):
    letters = [ch for ch in (text or "") if ch.isalpha()]
    if not letters:
        return 0.0
    arab = sum(1 for ch in letters if "\u0600" <= ch <= "\u06FF")
    return arab / len(letters)


def _is_scholarly_edition(candidate):
    title = (candidate.get("title") or "").lower()
    return any(marker in title for marker in _EDITION_MARKERS)


def language_gate_drop(candidate):
    """True when the §18 intake language gate drops this candidate:
    its metadata language is Persian/Arabic/Urdu, or its title is
    Arabic-script dominant — unless it is a primary text in a
    scholarly edition/translation."""
    # §41: shelf items are a closed, human-verified set whose full
    # text is already in hand — the primary text itself, admissible
    # under the same principle as the primary lane's own-text
    # exemption. The gate's target (unverified fa/ar keyword noise
    # flooding intake) is unaffected: nothing enters the shelf
    # without one-time verification and real extracted text.
    if candidate.get("shelf_item"):
        return False
    lang = (candidate.get("language") or "").strip().lower()
    if lang in _GATE_LANGUAGES:
        return not _is_scholarly_edition(candidate)
    if _arabic_script_ratio(candidate.get("title")) >= 0.5:
        return not _is_scholarly_edition(candidate)
    return False


def _new_retrieval_state():
    """Shared retrieval machinery state for collect_verified(): the
    dedupe set, the per-source time budgets / bench set, the verify
    cache and the cumulative candidate count. Passing the SAME state
    to a second call (wave 2, §16.2) makes that call respect wave 1's
    bench decisions and never re-fetch a wave-1 candidate."""
    return {"seen": set(), "source_time": {}, "benched": set(),
            "cache": None, "total_candidates": 0,
            "lang_gate_dropped": 0}


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
    order = scoped_searchers(searcher_order if searcher_order
                             else SEARCHERS)
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
                if language_gate_drop(c):
                    state["lang_gate_dropped"] = \
                        state.get("lang_gate_dropped", 0) + 1
                    continue
                # §20.5 cross-language dedupe: the same work found
                # through several languages counts ONCE — by DOI,
                # else by normalised title (+year when known; the
                # year-less alias also blocks the same title arriving
                # once with and once without a year).
                key = _dedupe_key(c)
                alias = "t:" + _norm_title_key(c["title"]) + "|"
                if key in seen or alias in seen:
                    continue
                seen.add(key)
                seen.add(alias)
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
    print(f"language gate (§18): dropped "
          f"{state.get('lang_gate_dropped', 0)} Persian/Arabic "
          "candidates at intake so far (scholarly editions excepted)")
    print(_counts_line("Candidates by source:", found_by, kept_by))
    print(f"Candidates collected: {total_candidates}")
    print(f"Verified sources: {len(verified)}")
    print("Verified by source: " + ("; ".join(
        f"{name}={n}" for name, n in sorted(verified_by.items())) or "(none)"))
    print(f"timing: retrieval {retrieval_t:.1f}s")
    print(f"timing: verification {verification_t:.1f}s")
    return verified


def retrieve_wave1(queries, rstate):
    """Wave 1 (§18): the abstract-rich indexes with the English-first
    queries, Crossref EXCLUDED. Crossref runs afterwards as a top-up
    ONLY when wave 1 produced fewer than EARLY_STOP_VERIFIED verified
    candidates. Both calls share `rstate`, so the top-up respects
    wave 1's dedupe set, bench decisions and language-gate count."""
    verified = collect_verified(queries, state=rstate,
                                searcher_order=WAVE1_SEARCHERS)
    if search_scope() == "new":
        print("search scope (§43): NEW-ONLY — wave 1 ran only the "
              "§§32–42 searchers (shelf, lectures, ensani, jihs, "
              "iran-math, tajik, CORE); Crossref top-up skipped")
        return verified
    if len(verified) < EARLY_STOP_VERIFIED:
        print(f"wave 1 (§18): {len(verified)} verified from the "
              f"abstract-rich indexes (< {EARLY_STOP_VERIFIED}) — "
              "Crossref top-up now runs with the same queries")
        verified = verified + collect_verified(
            queries, state=rstate, searcher_order=CROSSREF_SEARCHERS)
    else:
        print(f"wave 1 (§18): {len(verified)} verified — Crossref "
              "top-up not needed")
    return verified


# --------------------------------------------------------------------------
# §19 — primary sources: source map, reserved-lane retrieval, in-book
# search. Root fix (Jupiter, 2026-11-22: «باید از ریشه درست کنی»): the
# pipeline was journal-article shaped — topical keyword search, one
# shared verified pool filled by the journal indexes first, books with
# no abstract failing the content gate, and a full-text bridge that
# only ever read a book's opening pages. A primary source (the topic
# author's own work, the founding paper/document) therefore never
# reached the writer. This stage is GENERAL (any field/topic/author):
# a brain-built source map names the topic's foundational works, they
# are searched BY TITLE/AUTHOR in library catalogues, verified in a
# reserved lane that does not compete for the journal pool, and their
# content is extracted by searching INSIDE the book text for the
# topic's keywords — not by reading the head. Fail-soft throughout:
# any failure leaves the §18 pipeline exactly as it was.
# --------------------------------------------------------------------------

PRIMARY_MAX_VERIFIED = 3
IA_FULLTEXT_MAX_BYTES = 8 * 1024 * 1024
PASSAGE_WINDOW_CHARS = 6000
PASSAGE_MAX_CLUSTERS = 3
PASSAGE_CONTEXT_BEFORE = 1000


def build_source_map(field, topic, api_key, model):
    """ONE brain-chain call mapping this topic's primary sources:
    {primary_works: [{author, title, language, kind, relation}],
    keywords, name_variants}. §20 v2: the map names BOTH the author's
    own foundational works (relation='author', <=4) AND other books
    that discuss the topic (relation='about', <=4) — books as a class
    sit in the primary lane (Jupiter, 2026-11-23). Every mapped title
    is then verified against the Internet Archive catalogue before it
    is trusted; unconfirmed titles are dropped (logged). Fail-soft:
    ANY failure returns an empty map and the run continues exactly
    as §18."""
    empty = {"primary_works": [], "keywords": [], "name_variants": []}
    prompt = (
        "You are mapping the PRIMARY sources for a scholarly article, "
        "the way a researcher plans library work before searching.\n"
        f"Field: {field}\nTopic: {topic}\n\n"
        "Reply with JSON only, like:\n"
        '{"primary_works": [{"author": "...", "title": "...", '
        '"language": "...", "kind": "book", "title_ar": "..."}], '
        '"other_books": [{"author": "...", "title": "...", '
        '"language": "...", "kind": "book"}], '
        '"keywords": ["..."], "name_variants": ["..."]}\n\n'
        "Rules:\n"
        "- primary_works: up to 4 FOUNDATIONAL primary sources a "
        "researcher consults FIRST for this topic — when the topic "
        "concerns a historical author or school, that author's own "
        "work(s) on the subject; for a scientific topic, the original "
        "paper/document that founded the result; a classic primary "
        "text in its standard scholarly edition/translation counts. "
        "Give each title in its best-known scholarly form (the "
        "original-language title, its standard transliteration, or "
        "its standard translated title) — it will be searched BY "
        "TITLE AND AUTHOR in library catalogues (Internet Archive, "
        "Google Books), so it must be the real catalogue title, and "
        "kind is one of book / paper / document. When a work's title "
        "also exists in Arabic script, give that exact Arabic-script "
        "title as \"title_ar\" (omit the key otherwise).\n"
        "- other_books: up to 4 OTHER real books that DISCUSS this "
        "topic — scholarly monographs, commentaries, specialised "
        "textbooks or standard handbooks in which this exact subject "
        "is treated at length (not the author's own works; those go "
        "in primary_works). Same catalogue-title rules.\n"
        "- Only list works you are confident really exist. If the "
        "topic has no identifiable primary works, return an empty "
        "list — never invent a title.\n"
        "- keywords: 6–12 words or short phrases (English; add "
        "original-language terms, including Arabic-script ones, "
        "where the primary literature uses them) that mark THIS "
        "topic's passages inside a full book text — specific terms, "
        "place names, method names, not broad field words.\n"
        "- name_variants: the Latin-script name variants of the "
        "main author(s) (e.g. al-Biruni / Biruni / Abu Rayhan).")
    try:
        raw = call_llm(prompt, api_key, model, temperature=0.2,
                       json_mode=True)
        data = _parse_json_value(raw)
        if not isinstance(data, dict):
            print("WARNING: source map (§19) reply was not an object; "
                  "continuing without primary sources")
            return empty

        def _parse_works(items, relation):
            parsed = []
            for w in items or []:
                if not isinstance(w, dict):
                    continue
                title = str(w.get("title") or "").strip()
                if not title:
                    continue
                kind = str(w.get("kind") or "book").strip().lower()
                if kind not in ("book", "paper", "document"):
                    kind = "book"
                parsed.append({
                    "author": str(w.get("author") or "").strip(),
                    "title": title[:200],
                    "language": str(w.get("language") or "").strip(),
                    "kind": kind,
                    "relation": relation,
                    "title_ar": str(w.get("title_ar") or "").strip()})
                if len(parsed) >= 4:
                    break
            return parsed

        works = _parse_works(data.get("primary_works"), "author")
        works += _parse_works(data.get("other_books"), "about")

        def _str_list(key, cap, maxlen=120):
            out = []
            for v in data.get(key) or []:
                v = str(v).strip()
                if v and v not in out:
                    out.append(v[:maxlen])
                if len(out) >= cap:
                    break
            return out

        # §20.2 catalogue verification: trust no mapped title until
        # the catalogue itself confirms a matching work exists.
        confirmed = []
        for w in works:
            ok, matched = verify_work_in_catalogue(w)
            if ok:
                if matched and matched != w["title"]:
                    print(f"source map (§20): catalogue confirms "
                          f"{w['title']!r} as {matched!r}")
                    # §20.4: the REAL catalogue title becomes the
                    # work's identity for search + citation; the
                    # brain-mapped title is kept for traceability.
                    w["mapped_title"] = w["title"]
                    w["title"] = matched
                confirmed.append(w)
            else:
                print(f"source map (§20): DROPPED unconfirmed title "
                      f"{w['title']!r} (not confirmed by any "
                      "catalogue: IA / OpenLibrary / Google Books)")
        smap = {"primary_works": confirmed,
                "keywords": _str_list("keywords", 12),
                "name_variants": _str_list("name_variants", 6)}
        print(f"source map (§20): {len(confirmed)} catalogue-verified "
              f"work(s) of {len(works)} mapped, "
              f"{len(smap['keywords'])} keyword(s), "
              f"name variants {smap['name_variants']}")
        for w in confirmed:
            print(f"  primary work [{w['relation']}]: {w['author']} — "
                  f"{w['title']} ({w['kind']})")
        return smap
    except Exception as e:
        print(f"WARNING: source map (§19) failed ({e}); continuing "
              "without primary sources")
        return empty


def _lucene_clean(text):
    """Strip Lucene field-query syntax characters from a title/author
    so it can sit safely inside title:(...) / creator:(...)."""
    return re.sub(r"[^\w\s.'’-]+", " ", text or "",
                  flags=re.UNICODE).strip()


def search_ia_primary(work):
    """Internet Archive advancedsearch BY TITLE/CREATOR FIELDS for one
    primary work (§19) — never a topical keyword search. §20: every
    title VARIANT (brain form, diacritic-stripped plain form,
    Arabic-script form) gets its own title:(...) clause, because the
    catalogue holds the same work under different scripts and
    transliterations. Candidates are tagged source_type='primary'."""
    author = _lucene_clean(work.get("author"))
    parts = []
    for variant in _title_variants(work):
        cleaned = _lucene_clean(variant)
        if cleaned:
            parts.append(f"title:({cleaned})")
    if author:
        parts.append(f"creator:({author})")
    if not parts:
        return []
    q = "(" + " OR ".join(parts) + ") AND mediatype:texts"
    url = ("https://archive.org/advancedsearch.php?q="
           + urllib.parse.quote(q)
           + "&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=creator"
             "&fl%5B%5D=date&fl%5B%5D=language"
           + "&rows=10&page=1&output=json")
    data = http_json(url)
    out = []
    for doc in (data.get("response") or {}).get("docs", []) or []:
        ident = str(doc.get("identifier") or "").strip()
        if not ident:
            continue
        creator = doc.get("creator") or []
        if isinstance(creator, str):
            creator = [creator]
        date = str(doc.get("date") or "")
        cand = {
            "title": str(doc.get("title") or "").strip(),
            "authors": [str(a).strip() for a in creator
                        if str(a).strip()],
            "year": int(date[:4]) if date[:4].isdigit() else None,
            "venue": "Internet Archive",
            "doi": None,
            "url": f"https://archive.org/details/{ident}",
            "abstract": "",
            "arxiv_id": None,
            "index": "Internet Archive",
            "source_type": "primary",
        }
        lang = str(doc.get("language") or "").strip()
        if lang:
            cand["language"] = lang
        out.append(cand)
    return out


def search_google_books_primary(work):
    """Google Books with intitle:/inauthor: field operators for one
    primary work (§19). Candidates are tagged source_type='primary'."""
    title = (work.get("title") or "").replace('"', " ").strip()
    author = (work.get("author") or "").replace('"', " ").strip()
    bits = []
    if title:
        bits.append(f'intitle:"{title}"')
    if author:
        bits.append(f'inauthor:"{author}"')
    if not bits:
        return []
    url = ("https://www.googleapis.com/books/v1/volumes?maxResults=10&q="
           + urllib.parse.quote(" ".join(bits)))
    data = http_json(url)
    if not (data.get("items")):
        plain = _strip_diacritics(title)
        if plain and plain != title:
            bits[0] = f'intitle:"{plain}"'
            url = ("https://www.googleapis.com/books/v1/volumes"
                   "?maxResults=10&q="
                   + urllib.parse.quote(" ".join(bits)))
            data = http_json(url)
    out = []
    for item in data.get("items", []) or []:
        info = item.get("volumeInfo") or {}
        published = str(info.get("publishedDate") or "")
        cand = {
            "title": (info.get("title") or "").strip(),
            "authors": [a for a in info.get("authors", []) if a],
            "year": int(published[:4]) if published[:4].isdigit()
                    else None,
            "venue": info.get("publisher") or "Google Books",
            "doi": None,
            "url": info.get("infoLink") or item.get("selfLink") or "",
            "abstract": (info.get("description") or "")[:1500],
            "arxiv_id": None,
            "index": "Google Books",
            "source_type": "primary",
        }
        lang = str(info.get("language") or "").strip()
        if lang:
            cand["language"] = lang
        out.append(cand)
    return out


def _primary_title_score(candidate_title, work_title):
    """Token-overlap of a catalogue hit's title against the requested
    work's title (0..1) — how strongly the hit IS the requested work."""
    want = _screen_tokens(work_title)
    if not want:
        return 0.0
    got = _screen_tokens(candidate_title)
    return len(want & got) / len(want)


def _primary_work_score(candidate_title, work):
    """§20 fix (run 45107999596): the score of a catalogue hit against
    a mapped work is the MAX over ALL title variants — the catalogue
    holds the same work under Latin and Arabic-script titles, and
    scoring an Arabic-titled copy against the Latin form alone gave
    0, so the lane threw away the very book the catalogue check had
    just confirmed."""
    return max((_primary_title_score(candidate_title, v)
                for v in _title_variants(work)), default=0.0)


def _primary_author_match(candidate, work):
    author = _lucene_clean(work.get("author")).lower()
    if not author:
        return False
    surname = author.split()[-1]
    if len(surname) < 3:
        return False
    return any(surname in (a or "").lower()
               for a in candidate.get("authors") or [])


def retrieve_primary_sources(smap, rstate, extra_keywords=None,
                             max_new=None):
    """The reserved primary lane (§19): for each work in the source
    map, field-search the library catalogues, verify the best matches
    in their OWN lane (the existing verification law — IA item page /
    publisher page) and return up to PRIMARY_MAX_VERIFIED verified
    primary sources. They never consume the journal pool's
    MAX_VERIFIED slots. Fail-soft per work and per source. §20: the
    map may hold up to 8 works (author's own + books about the topic)
    and the §20 matrix core keywords join the in-text search set;
    max_new caps how many NEW primaries this call may add (the wave-2
    re-run must respect the same global cap)."""
    if max_new is None:
        max_new = PRIMARY_MAX_VERIFIED
    if search_scope() == "new":
        # §43: the primary lane's catalogue searches (IA + Google
        # Books) are pre-§32 searchers — in new-only mode the lane
        # stands down; the §41 shelf carries the primary books
        # (with passages) through the main pool instead.
        print("primary sources (§19): lane skipped — search "
              "scope (§43) is new-only; primaries come from the "
              "§41 shelf")
        return []
    works = (smap or {}).get("primary_works") or []
    if not works or max_new <= 0:
        if works:
            print("primary sources (§19): primary cap already "
                  "reached — lane skipped")
        else:
            print("primary sources (§19): no primary works in the "
                  "source map — stage skipped")
        return []
    if rstate.get("cache") is None:
        rstate["cache"] = load_verify_cache()
    cache = rstate["cache"]
    seen = rstate["seen"]
    keywords = (list(smap.get("keywords") or [])
                + list(smap.get("name_variants") or []))
    for kw in extra_keywords or []:
        if kw and kw not in keywords:
            keywords.append(kw)
    verified_primaries = []
    for work in works[:8]:
        if len(verified_primaries) >= max_new:
            break
        cands = []
        for fn in (search_ia_primary, search_google_books_primary):
            try:
                cands.extend(fn(work))
            except Exception as e:
                print(f"WARNING: primary search {fn.__name__} failed "
                      f"for {work.get('title', '')[:60]!r}: {e}")
        ranked = sorted(
            cands,
            key=lambda c: -_primary_work_score(c.get("title"), work))
        picked = []
        for c in ranked:
            if not c.get("title"):
                continue
            score = _primary_work_score(c["title"], work)
            if score < 0.34 and not _primary_author_match(c, work):
                continue  # a catalogue neighbour, not this work
            key = _dedupe_key(c)
            alias = "t:" + _norm_title_key(c["title"]) + "|"
            if key in seen or alias in seen:
                continue
            # Intake language gate (§18) applies — EXCEPT that a
            # strong title match to the requested work IS the primary
            # text itself, admissible in a scholarly edition /
            # translation under the SOURCE LAW.
            if language_gate_drop(c) and score < 0.6:
                rstate["lang_gate_dropped"] = \
                    rstate.get("lang_gate_dropped", 0) + 1
                continue
            seen.add(key)
            seen.add(alias)
            c["source_type"] = "primary"
            c["primary_work"] = work.get("title")
            # §20.4/§20.7 traceability: how the mapped work was
            # catalogue-confirmed and under what name the brain had
            # mapped it (the candidate's own title is the REAL
            # catalogue title of the found copy).
            c["primary_relation"] = work.get("relation")
            if work.get("mapped_title"):
                c["mapped_title"] = work["mapped_title"]
            if work.get("catalogue_via"):
                c["catalogue_via"] = work["catalogue_via"]
            if work.get("catalogue_title"):
                c["catalogue_title"] = work["catalogue_title"]
            if work.get("catalogue_id"):
                c["catalogue_id"] = work["catalogue_id"]
            c["search_keywords"] = keywords
            picked.append(c)
            rstate["total_candidates"] = \
                rstate.get("total_candidates", 0) + 1
            if len(picked) >= 2:
                break
        if not picked:
            continue
        with ThreadPoolExecutor(max_workers=VERIFY_WORKERS) as ex:
            outcomes = list(ex.map(
                lambda c: _verify_one(c, cache), picked))
        for c, (ok, _via) in zip(picked, outcomes):
            if not ok:
                continue
            verified_primaries.append(c)
            print(f"  verified primary [{len(verified_primaries)}] "
                  f"{c['title'][:80]}")
            if len(verified_primaries) >= max_new:
                break
    save_verify_cache(cache)
    print(f"primary sources (§19): {len(verified_primaries)} verified "
          "in the primary lane (reserved slots — the journal pool is "
          "untouched)")
    return verified_primaries


def extract_passages(text, keywords, window=PASSAGE_WINDOW_CHARS,
                     max_clusters=PASSAGE_MAX_CLUSTERS):
    """In-book search (§19): find the topic keywords inside a book's
    full text (case-insensitive; Arabic-script keywords match as-is),
    cluster the hits, and return (excerpt, hit_count) — passage
    windows around the densest clusters, merged and capped at
    FULLTEXT_EXCERPT_CHARS. (None, 0) when nothing hits. The excerpt
    comes from the HIT REGIONS, never from the file head."""
    if not text or not keywords:
        return None, 0
    low = text.lower()
    hits = []
    for kw in keywords:
        needle = (kw or "").strip().lower()
        if len(needle) < 3:
            continue
        start = 0
        while True:
            i = low.find(needle, start)
            if i < 0:
                break
            hits.append(i)
            start = i + len(needle)
    if not hits:
        return None, 0
    hits.sort()
    clusters = [[hits[0]]]
    for h in hits[1:]:
        if h - clusters[-1][0] <= window:
            clusters[-1].append(h)
        else:
            clusters.append([h])
    clusters.sort(key=len, reverse=True)
    spans = []
    for cur in clusters[:max_clusters]:
        a = max(0, cur[0] - PASSAGE_CONTEXT_BEFORE)
        b = min(len(text), max(cur[0] + window, cur[-1] + 400))
        spans.append((a, b))
    spans.sort()
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    parts = [text[a:b].strip() for a, b in merged]
    excerpt = "\n\n[...]\n\n".join(p for p in parts if p)
    if len(excerpt) > FULLTEXT_EXCERPT_CHARS:
        excerpt = excerpt[:FULLTEXT_EXCERPT_CHARS]
    return (excerpt or None), len(hits)


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


def _ia_djvu_text(ia_id, cap=IA_TEXT_MAX_BYTES):
    """The item's ready-extracted full text, or None (a miss — e.g.
    lending-restricted items — is never an error). The real file name
    comes from the metadata API; it is not always <id>_djvu.txt.
    `cap` bounds the fetch (the §19 in-book search passes a larger cap
    so keyword hits deep in the book are reachable)."""
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
    data = _fetch_capped(url, cap, timeout=60)
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
        # §19 in-book search for PRIMARY sources: fetch the IA text
        # in FULL (larger cap) and extract the passages around the
        # source-map keyword hits — the relevant chapter, not the
        # book's opening pages. Only the extracted passages are
        # cached (storage law). No hits -> the head excerpt below is
        # the fallback, exactly as before.
        keywords = s.get("search_keywords") or []
        if s.get("source_type") == "primary" and keywords:
            pid = ia_id or _ia_find_by_title(s["title"])
            if pid:
                full = _ia_djvu_text(pid, cap=IA_FULLTEXT_MAX_BYTES)
                if full:
                    excerpt, hits = extract_passages(full, keywords)
                    if excerpt:
                        _write_cache(md5 or pid, excerpt)
                        s["fulltext_excerpt"] = \
                            excerpt[:FULLTEXT_EXCERPT_CHARS]
                        s["passages"] = True
                        s["passage_hits"] = hits
                        s["fulltext_route"] = "ia-passages"
                        print(f"fulltext route: ia-passages ({hits} "
                              "keyword hits, passages extracted)")
                        return True, False
                    print("fulltext: primary book fetched but no "
                          "keyword hits — head excerpt fallback")
                    _write_cache(md5 or pid, full)
                    s["fulltext_excerpt"] = \
                        full[:FULLTEXT_EXCERPT_CHARS]
                    s["fulltext_route"] = "ia-text-head"
                    print("fulltext route: ia-text (head fallback)")
                    return True, False
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
            s["fulltext_route"] = route
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
    # §19: primary sources are served FIRST by the bridge (their
    # passages are the run's grounding); the rest keep their order.
    ordered = ([s for s in verified
                if s.get("source_type") == "primary"]
               + [s for s in verified
                  if s.get("source_type") != "primary"])
    for s in ordered:
        md5 = s.get("md5") or None
        ia_id = (_ia_id_from_url(s.get("url", ""))
                 if s.get("index") == "Internet Archive" else None)
        if not md5 and not ia_id and s.get("source_type") != "primary":
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


# ---------------------------------------------------------------------------
# §20.3 — full-text acquisition layer for ALL source types.
# attach_fulltexts() above serves BOOKS (IA _djvu.txt / LibGen). This
# layer generalises it: for every screened candidate, try in order
#   (i)   Europe PMC fullTextXML (when a PMCID is on the record);
#   (ii)  Unpaywall's best open-access location for the DOI;
#   (iii) OpenAlex's best OA location / oa_url;
#   (iv)  the explicit full-text link an index carried (DOAJ /
#         OpenAIRE / HAL records);
#   (v)   a generic fetcher over the record's own URL (PDF via the
#         existing pdftotext path, HTML via stdlib tag-stripping).
# Books still go through the §19 bridge first (in-book passage search
# on the full IA text). Every fetched text is searched INSIDE with the
# topic keywords (extract_passages); only the extracted passages are
# cached — source files are never stored (storage law). Fail-soft per
# route and per record; a global soft budget bounds the whole phase.
# ---------------------------------------------------------------------------

def _html_to_text(raw):
    """HTML/XML bytes or str -> plain text (stdlib only): drop script
    and style blocks, strip tags, unescape entities, collapse space."""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = raw or ""
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ",
                  text)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _fetch_capped_full(url, cap=FULLTEXT_FETCH_MAX_BYTES, timeout=60):
    """Download a URL once (no retry storm) and report the content
    type: (data_bytes, content_type) or (None, '')."""
    req = urllib.request.Request(url)
    req.add_header("User-Agent",
                   "discovery-agent/1.0 (research; "
                   "mailto:research@example.org)")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            return resp.read(cap + 1)[:cap], ctype
    except Exception:
        return None, ""


def _text_from_download(data, ctype, url):
    """(text, kind) from downloaded bytes: PDF via pdftotext (the
    book path's extractor), HTML/XML via tag-stripping, else decoded
    plain text. None when nothing usable comes out."""
    if not data:
        return None
    # PDF by magic bytes, or by content type when the body is not
    # actually HTML (some .pdf URLs serve an HTML landing page).
    is_pdf = data[:5] == b"%PDF-" or (
        "pdf" in ctype and data.lstrip()[:1] != b"<")
    if is_pdf:
        fd, path = tempfile.mkstemp(suffix=".pdf")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            return _extract_pdf(path, "v2-fetch") or None
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    return _html_to_text(data) or None


def _europepmc_fulltext(s):
    """Route (i): Europe PMC fullTextXML for the record's PMCID —
    the complete article text as structured XML (free, no key)."""
    pmcid = (s.get("pmcid") or "").strip()
    if not pmcid:
        return None
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/"
           + urllib.parse.quote(pmcid) + "/fullTextXML")
    data, _ctype = _fetch_capped_full(url)
    if not data:
        return None
    return _html_to_text(data) or None


def _unpaywall_best_url(doi):
    """Route (ii): Unpaywall's best open-access location for a DOI
    (free; the request carries only a contact email, no key)."""
    if not doi:
        return ""
    url = ("https://api.unpaywall.org/v2/"
           + urllib.parse.quote(doi, safe="")
           + "?email=" + urllib.parse.quote(UNPAYWALL_EMAIL))
    try:
        data = http_json(url)
    except Exception:
        return ""
    best = data.get("best_oa_location") or {}
    url = best.get("url_for_pdf") or best.get("url") or ""
    if url:
        return url
    for loc in data.get("oa_locations") or []:
        url = loc.get("url_for_pdf") or loc.get("url") or ""
        if url:
            return url
    return ""


def _generic_fetch_text(url):
    """Route (v): download a URL (<=10 MB) and extract its text."""
    if not url or not url.startswith("http"):
        return None
    data, ctype = _fetch_capped_full(url)
    return _text_from_download(data, ctype, url)


def _attach_fulltext_v2_one(s, keywords):
    """One candidate through the §20 layer. Sets fulltext_excerpt,
    fulltext=True, fulltext_route, and passages=True when the excerpt
    was located by keyword search inside the fetched text. Returns
    True when a usable full text was obtained."""
    if s.get("fulltext_excerpt"):
        s["fulltext"] = True
        s.setdefault("fulltext_route", "existing")
        return True
    kws = list(keywords or []) or list(s.get("search_keywords") or [])
    if kws and not s.get("search_keywords"):
        s["search_keywords"] = kws
    # Books first: the §19 bridge (IA in-book passage search on the
    # full text, LibGen fallback) remains the book route.
    md5 = s.get("md5") or None
    ia_id = (_ia_id_from_url(s.get("url", ""))
             if s.get("index") == "Internet Archive" else None)
    book_like = bool(md5 or ia_id or s.get("source_type") == "primary")
    if book_like:
        got, _cached = _attach_fulltext_one(s)
        if got:
            s["fulltext"] = True
            return True
    routes = []
    if s.get("pmcid"):
        routes.append(("europepmc-xml", lambda: _europepmc_fulltext(s)))
    if s.get("doi"):
        routes.append(("unpaywall", None))  # resolved lazily below
    oa_url = (s.get("oa_pdf_url") or s.get("fulltext_url") or "").strip()
    if oa_url:
        routes.append(("index-fulltext",
                       lambda u=oa_url: _generic_fetch_text(u)))
    if s.get("url"):
        routes.append(("generic",
                       lambda u=s["url"]: _generic_fetch_text(u)))
    for label, fetch in routes:
        try:
            if label == "unpaywall":
                target = _unpaywall_best_url(s.get("doi"))
                if not target:
                    continue
                text = _generic_fetch_text(target)
            else:
                text = fetch()
        except Exception as e:
            print(f"fulltext v2: route {label} errored for "
                  f"{s['title'][:50]!r}: {e}")
            continue
        if not text or len(text) < MIN_FETCHED_TEXT_CHARS:
            continue
        excerpt, hits = extract_passages(text, kws)
        if excerpt:
            _write_cache(s.get("doi") or normalise_title(s["title"]),
                         excerpt)
            s["fulltext_excerpt"] = excerpt[:FULLTEXT_EXCERPT_CHARS]
            s["fulltext"] = True
            s["passages"] = True
            s["passage_hits"] = hits
            s["fulltext_route"] = label
            print(f"fulltext v2: {label} passages for "
                  f"{s['title'][:60]!r} ({hits} keyword hits)")
            return True
        if label in ("europepmc-xml", "unpaywall", "index-fulltext"):
            # A structured full text with no keyword hits: keep the
            # head as grounding (the book bridge does the same); the
            # relevance screen still judges it. Generic landing-page
            # HTML never gets this fallback.
            _write_cache(s.get("doi") or normalise_title(s["title"]),
                         text)
            s["fulltext_excerpt"] = text[:FULLTEXT_EXCERPT_CHARS]
            s["fulltext"] = True
            s["fulltext_route"] = label + "-head"
            print(f"fulltext v2: {label} full text (no keyword hits, "
                  f"head kept) for {s['title'][:60]!r}")
            return True
    return False


def attach_fulltexts_v2(candidates, keywords, deadline=None):
    """§20.3 — run the acquisition layer over the verified candidates
    (primaries first, then the rest), in parallel, bounded by
    FULLTEXT_V2_MAX_SOURCES attempts and a global soft budget: once
    the deadline passes, remaining candidates are skipped and logged.
    Returns {attempted, fetched, by_route}."""
    ordered = ([s for s in candidates
                if s.get("source_type") == "primary"]
               + [s for s in candidates
                  if s.get("source_type") != "primary"])
    selected = ordered[:FULLTEXT_V2_MAX_SOURCES]
    stats = {"attempted": 0, "fetched": 0, "by_route": {}}
    if not selected:
        return stats
    if deadline is None:
        deadline = time.monotonic() + FULLTEXT_GLOBAL_BUDGET_SECONDS
    t0 = time.monotonic()

    def _job(s):
        if time.monotonic() > deadline:
            print(f"fulltext v2: global budget reached — skipping "
                  f"{s['title'][:50]!r}")
            return s, False, True
        return s, _attach_fulltext_v2_one(s, keywords), False

    with ThreadPoolExecutor(max_workers=FULLTEXT_WORKERS) as ex:
        outcomes = list(ex.map(_job, selected))
    skipped = 0
    for s, got, was_skipped in outcomes:
        if was_skipped:
            skipped += 1
            continue
        stats["attempted"] += 1
        if got:
            stats["fetched"] += 1
            route = s.get("fulltext_route") or "?"
            stats["by_route"][route] = stats["by_route"].get(route, 0) + 1
    print(f"fulltext v2 (§20): fetched {stats['fetched']}/"
          f"{stats['attempted']} attempted "
          f"({skipped} skipped past the global budget); routes: "
          + (", ".join(f"{k}={v}" for k, v in
                       sorted(stats["by_route"].items())) or "none"))
    print(f"timing: full-text layer v2 {time.monotonic() - t0:.1f}s")
    return stats


# ---------------------------------------------------------------------------
# §20.4 — YouTube lecture transcripts: SUPPORT ONLY. A transcript can
# help the writer explain, but a video NEVER counts toward the
# publication gate and is cited only as support; every claim taken
# from it must still rest on a counted book/article source. Fail-soft:
# no yt-dlp, no network, no subtitles -> the run simply has no videos.
# ---------------------------------------------------------------------------

_ACADEMIC_CHANNEL_MARKERS = (
    "university", "université", "universitaet", "institute",
    "institution", "society", "academy", "college", "museum",
    "library", "lecture", "lectures", "conference", "seminar",
    "school", "faculty", "observatory", "mit ", "oxford", "cambridge",
    "stanford", "harvard", "yale", "princeton", "open course",
    "opencourseware", "nptel", "coursera", "khan academy")


def _vtt_to_text(vtt):
    lines = []
    for line in (vtt or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        if "-->" in line or line.isdigit():
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return " ".join(lines)


def fetch_youtube_support(queries_en, keywords, max_videos=3):
    """Search yt-dlp (when installed) for lectures on the English
    matrix queries, keep clearly academic channels, fetch English
    subtitles, and return passage-bearing support records tagged
    source_type='video'. [] on any failure — silently, one log line."""
    if os.environ.get("DISCOVERY_NO_YOUTUBE"):
        print("youtube support (§20): disabled by environment")
        return []
    exe = shutil.which("yt-dlp")
    if not exe:
        print("youtube support (§20): yt-dlp not installed — skipped")
        return []
    found = []
    seen_ids = set()
    for q in (queries_en or [])[:2]:
        try:
            proc = subprocess.run(
                [exe, "-j", "--no-warnings", "--skip-download",
                 f"ytsearch5:{q}"],
                capture_output=True, text=True, timeout=120)
        except Exception as e:
            print(f"youtube support (§20): search failed ({e}) — "
                  "skipped")
            return found
        for line in (proc.stdout or "").splitlines():
            try:
                v = json.loads(line)
            except Exception:
                continue
            vid = v.get("id") or ""
            channel = (v.get("channel") or v.get("uploader") or "")
            title = v.get("title") or ""
            if not vid or vid in seen_ids:
                continue
            seen_ids.add(vid)
            hay = (channel + " " + title).lower()
            if not any(m in hay for m in _ACADEMIC_CHANNEL_MARKERS):
                continue
            found.append({"id": vid, "title": title,
                          "channel": channel,
                          "url": f"https://www.youtube.com/watch?v={vid}"})
            if len(found) >= max_videos * 2:
                break
        if len(found) >= max_videos * 2:
            break
    out = []
    for v in found:
        if len(out) >= max_videos:
            break
        text = ""
        with tempfile.TemporaryDirectory() as td:
            try:
                subprocess.run(
                    [exe, "--skip-download", "--write-subs",
                     "--write-auto-subs", "--sub-langs", "en.*",
                     "--sub-format", "vtt", "--no-warnings",
                     "-o", os.path.join(td, "%(id)s.%(ext)s"),
                     v["url"]],
                    capture_output=True, text=True, timeout=180)
            except Exception:
                continue
            for name in os.listdir(td):
                if name.endswith(".vtt"):
                    try:
                        with open(os.path.join(td, name),
                                  encoding="utf-8",
                                  errors="replace") as f:
                            text = _vtt_to_text(f.read())
                    except OSError:
                        pass
                    break
        if not text or len(text) < MIN_FETCHED_TEXT_CHARS:
            continue
        excerpt, hits = extract_passages(text, keywords)
        if not excerpt:
            continue
        out.append({
            "title": v["title"], "authors": [v["channel"]],
            "year": None, "venue": "YouTube lecture (support only)",
            "doi": None, "url": v["url"], "abstract": "",
            "arxiv_id": None, "index": "YouTube",
            "source_type": "video", "fulltext": True,
            "passages": True, "passage_hits": hits,
            "fulltext_route": "youtube-transcript",
            "fulltext_excerpt": excerpt[:FULLTEXT_EXCERPT_CHARS]})
        print(f"youtube support (§20): transcript passages from "
              f"{v['title'][:60]!r} ({hits} keyword hits)")
    print(f"youtube support (§20): {len(out)} supporting "
          "transcript(s) — never counted toward the gate")
    return out


def text_first_gate(counted):
    """§20.6 — the TEXT-FIRST publication gate. Publication requires
    ALL of: >= MIN_SOURCES relevant sources (the §19 judging), >=
    MIN_FULLTEXT_SOURCES of the counted carrying extracted full-text
    PASSAGES, and >= 1 primary (the author's own book, or a book
    discussing the topic) with passages extracted. Video transcripts
    never count. Returns the verdict + the numbers for the funnel."""
    counted_only = [s for s in counted
                    if s.get("source_type") != "video"]
    fulltext_passages = sum(1 for s in counted_only
                            if s.get("passages"))
    primary_passages = sum(
        1 for s in counted_only
        if s.get("source_type") == "primary" and s.get("passages"))
    fulltext_any = sum(1 for s in counted_only if s.get("fulltext"))
    ok = (len(counted_only) >= MIN_SOURCES
          and fulltext_passages >= MIN_FULLTEXT_SOURCES
          and primary_passages >= 1)
    return {"ok": ok, "counted": len(counted_only),
            "fulltext_passages": fulltext_passages,
            "fulltext_any": fulltext_any,
            "primary_passages": primary_passages}


# --------------------------------------------------------------------------
# Topic adaptation (§20.10, Jupiter 2026-11-26: «در چنین مواردی موضوع
# رو طوری تغییر بده که بتونیم مقاله رو تولید کنیم») — when a requested
# topic fails the text-first gate because genuinely relevant counted
# sources are too few for THAT exact framing, the engine adapts the
# topic instead of simply stopping: a brain proposes narrow adapted
# topics grounded ONLY in the evidence already in hand (counted
# sources + their passages, near-miss titles, the source map), each
# candidate is re-screened through the SAME fresh pipeline and must
# pass the SAME gate (thresholds never lowered), at most
# MAX_TOPIC_ADAPTATIONS attempts. The change is visible in the log,
# the funnel (§16.3), the §20.7 report, the article itself and
# state.json. If no candidate passes — or candidate generation
# fails — the run fails closed exactly as before, with the
# adaptations tried listed in the final report.
# --------------------------------------------------------------------------

MAX_TOPIC_ADAPTATIONS = 2


def propose_topic_adaptations(field, original_topic, language,
                              counted, disagreement_titles, source_map,
                              api_key, model):
    """Brain-proposed adapted topics (§20.10), grounded ONLY in
    evidence already in hand. Returns up to 3 candidates
    [{'topic': ..., 'reason': ...}] ranked by expected source
    support. Fail-soft: ANY brain/parse failure returns [] and the
    caller fails closed exactly as before."""
    counted = [s for s in (counted or [])
               if s.get("source_type") != "video"]
    evidence = []
    for i, s in enumerate(counted, 1):
        excerpt = (s.get("fulltext_excerpt")
                   or s.get("abstract") or "").strip()
        evidence.append(
            f"{i}. {s.get('title') or '(untitled)'}"
            f" [type={s.get('source_type') or 'article'}]"
            + (f"\n   passage/abstract excerpt: {excerpt[:600]}"
               if excerpt else ""))
    near = [t for t in (disagreement_titles or []) if t]
    try:
        smap_text = json.dumps(source_map or {},
                               ensure_ascii=False)[:3000]
    except Exception:
        smap_text = str(source_map or "")[:3000]
    prompt = (
        "The research topic below FAILED a strict text-first "
        "quality gate: too few genuinely relevant, content-bearing "
        "sources support that EXACT framing. Propose adapted topics "
        "that the sources ALREADY FOUND can actually support.\n"
        f"Field: {field}\n"
        f"Original topic: {original_topic}\n"
        f"Article output language code: {language} — write every "
        "adapted topic string in the same language/style as the "
        "original topic.\n\n"
        "COUNTED SOURCES ALREADY IN HAND (the ONLY evidence you may "
        "ground a candidate in):\n"
        + ("\n".join(evidence) if evidence else "(none)")
        + "\n\nNEAR-MISS TITLES (kept by one judge, dropped on "
        "disagreement):\n"
        + ("\n".join(f"- {t}" for t in near) if near else "(none)")
        + f"\n\nSOURCE MAP:\n{smap_text}\n\n"
        "Rules for every candidate:\n"
        "- SAME field and SAME core subject/author as the original "
        "topic — only the framing/angle/scope may shift (broaden, "
        "narrow, or re-angle around the content the counted sources "
        "actually support).\n"
        "- NARROW and SPECIFIC title (title law): no broad "
        "field-label titles.\n"
        "- Each candidate carries an explicit one-line reason "
        "naming which counted sources support it.\n"
        "- Up to 3 candidates, ranked by expected source support, "
        "best first.\n"
        "Reply with ONLY a JSON array like: "
        '[{"topic": "...", "reason": "..."}]')
    try:
        raw = call_llm(prompt, api_key, model, temperature=0.2)
    except Exception as e:
        print(f"topic adaptation (§20.10): candidate generation "
              f"failed ({e}) — no candidates")
        return []
    candidates = []
    try:
        text = (raw or "").strip()
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            raise ValueError("no JSON array in brain reply")
        data = json.loads(text[start:end + 1])
        if isinstance(data, dict):
            data = data.get("candidates") or data.get("topics") or []
        for item in data or []:
            if isinstance(item, str):
                topic_text, reason = item.strip(), ""
            elif isinstance(item, dict):
                topic_text = str(item.get("topic")
                                 or item.get("title") or "").strip()
                reason = str(item.get("reason") or "").strip()
            else:
                continue
            if (topic_text and norm(topic_text) != norm(original_topic)
                    and all(norm(c["topic"]) != norm(topic_text)
                            for c in candidates)):
                candidates.append({"topic": topic_text,
                                   "reason": reason})
            if len(candidates) >= 3:
                break
    except Exception as e:
        print(f"topic adaptation (§20.10): candidate parse failed "
              f"({e}) — no candidates")
        return []
    print(f"topic adaptation (§20.10): {len(candidates)} "
          f"candidate(s) proposed for {original_topic!r}")
    return candidates


def attempt_topic_adaptation(field, original_topic, language,
                             category, article_type, mode, api_key,
                             model, tg_token, counted,
                             disagreement_titles, source_map):
    """§20.10 selection + re-screening. Each candidate (at most
    MAX_TOPIC_ADAPTATIONS attempts) goes through the SAME
    _fresh_screening pipeline and the SAME text_first_gate — no
    threshold is ever lowered, and the original topic's counted set
    is never reused as an adapted topic's. Returns
    (adaptation_info, winning_fresh_or_None)."""
    info = {"original_topic": original_topic, "adapted_topic": None,
            "reason": "", "attempts": []}
    candidates = propose_topic_adaptations(
        field, original_topic, language, counted,
        disagreement_titles, source_map, api_key, model)
    for cand in candidates[:MAX_TOPIC_ADAPTATIONS]:
        adapted, reason = cand["topic"], cand.get("reason", "")
        print(f"topic adaptation (§20.10): trying adapted topic "
              f"{adapted!r} (reason: {reason})")
        fresh = _fresh_screening(field, adapted, language, category,
                                 article_type, mode, api_key, model,
                                 tg_token)
        gate = fresh["gate"]
        attempt = {"topic": adapted, "reason": reason,
                   "counted": gate["counted"],
                   "fulltext_passages": gate["fulltext_passages"],
                   "primary_passages": gate["primary_passages"],
                   "ok": bool(gate["ok"])}
        info["attempts"].append(attempt)
        print(f"topic adaptation (§20.10): adapted funnel for "
              f"{adapted!r}: counted={gate['counted']}, "
              f"full-text passages={gate['fulltext_passages']}, "
              f"primary passages={gate['primary_passages']}, "
              f"gate ok={gate['ok']}")
        if gate["ok"]:
            info["adapted_topic"] = adapted
            info["reason"] = reason
            print(f"topic adapted: {original_topic} -> {adapted} "
                  f"(reason: {reason})")
            return info, fresh
    if info["attempts"]:
        print("topic adaptation (§20.10): no candidate passed the "
              "gate — failing closed as before")
    return info, None


def topic_adaptation_note(original_topic, adapted_topic, reason,
                          language):
    """The in-article §20.10 note (in the article's language)."""
    if (language or "").strip().lower().startswith("fa"):
        return ("> یادداشت: موضوع این مقاله تغییر یافته است. "
                f"موضوع اولیه «{original_topic}» بود؛ چون برای آن "
                "قاب‌بندی دقیق، منابع مرتبط و متن‌دار کافی پیدا نشد، "
                f"موضوع به «{adapted_topic}» تغییر یافت تا مقاله بر "
                "پایهٔ منابعی که واقعاً در دست است نوشته شود."
                + (f" دلیل: {reason}" if reason else ""))
    return ("> Note: this article's topic was adapted. The original "
            f"topic was \"{original_topic}\"; because too few "
            "genuinely relevant, full-text sources supported that "
            f"exact framing, the topic was adapted to "
            f"\"{adapted_topic}\" so the article rests on the "
            "sources actually in hand."
            + (f" Reason: {reason}" if reason else ""))


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

# §22: seven free-tier OpenAI-compatible backup brains. Each is used
# only when its credential exists in the environment (see the chain
# append rule in llm_chain()); with no keys set, behaviour is exactly
# the legacy glm/gemini chain.
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_DEFAULT_MODEL = "openai/gpt-oss-120b"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
CLOUDFLARE_DEFAULT_MODEL = "@cf/meta/llama-3.3-70b-fp8-fast"
SAMBANOVA_URL = "https://api.sambanova.ai/v1/chat/completions"
SAMBANOVA_DEFAULT_MODEL = "Meta-Llama-3.3-70B-Instruct"
DASHSCOPE_URL = ("https://dashscope-intl.aliyuncs.com"
                 "/compatible-mode/v1/chat/completions")
DASHSCOPE_DEFAULT_MODEL = "qwen-plus"
MODELSCOPE_URL = "https://api-inference.modelscope.cn/v1/chat/completions"
MODELSCOPE_DEFAULT_MODEL = "Qwen/Qwen3-32B"
SILICONFLOW_URL = "https://api.siliconflow.cn/v1/chat/completions"
SILICONFLOW_DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"
EXTRA_BRAINS_MAX_TOKENS = 8000

# Display labels for the §22 brains (log / error messages).
_EXTRA_BRAIN_LABELS = {"groq": "Groq", "openrouter": "OpenRouter",
                       "cloudflare": "Cloudflare Workers AI",
                       "sambanova": "SambaNova",
                       "dashscope": "DashScope",
                       "modelscope": "ModelScope",
                       "siliconflow": "SiliconFlow"}


def _cloudflare_url():
    """The Workers AI OpenAI-compatible endpoint for the configured
    account; "" when CF_ACCOUNT_ID is unset (the cloudflare brain is
    then unavailable, exactly like a missing key)."""
    account = (os.environ.get("CF_ACCOUNT_ID") or "").strip()
    if not account:
        return ""
    return ("https://api.cloudflare.com/client/v4/accounts/"
            f"{account}/ai/v1/chat/completions")


def _brain_endpoint(name):
    """(url, api_key) for a §22 OpenAI-compatible brain, resolved
    from the environment at call time; either element is "" when the
    brain is not configured. ("" , "") for any other name."""
    if name == "groq":
        return (GROQ_URL,
                (os.environ.get("GROQ_API_KEY") or "").strip())
    if name == "openrouter":
        return (OPENROUTER_URL,
                (os.environ.get("OPENROUTER_API_KEY") or "").strip())
    if name == "cloudflare":
        return (_cloudflare_url(),
                (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip())
    if name == "sambanova":
        return (SAMBANOVA_URL,
                (os.environ.get("SAMBANOVA_API_KEY") or "").strip())
    if name == "dashscope":
        return (DASHSCOPE_URL,
                (os.environ.get("DASHSCOPE_API_KEY") or "").strip())
    if name == "modelscope":
        return (MODELSCOPE_URL,
                (os.environ.get("MODELSCOPE_API_KEY") or "").strip())
    if name == "siliconflow":
        return (SILICONFLOW_URL,
                (os.environ.get("SILICONFLOW_API_KEY") or "").strip())
    return ("", "")


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
    if name == "groq":
        return (os.environ.get("GROQ_MODEL")
                or GROQ_DEFAULT_MODEL).strip()
    if name == "openrouter":
        return (os.environ.get("OPENROUTER_MODEL")
                or OPENROUTER_DEFAULT_MODEL).strip()
    if name == "cloudflare":
        return (os.environ.get("CLOUDFLARE_MODEL")
                or CLOUDFLARE_DEFAULT_MODEL).strip()
    if name == "sambanova":
        return (os.environ.get("SAMBANOVA_MODEL")
                or SAMBANOVA_DEFAULT_MODEL).strip()
    if name == "dashscope":
        return (os.environ.get("DASHSCOPE_MODEL")
                or DASHSCOPE_DEFAULT_MODEL).strip()
    if name == "modelscope":
        return (os.environ.get("MODELSCOPE_MODEL")
                or MODELSCOPE_DEFAULT_MODEL).strip()
    if name == "siliconflow":
        return (os.environ.get("SILICONFLOW_MODEL")
                or SILICONFLOW_DEFAULT_MODEL).strip()
    return (os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash").strip()


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


def _extra_brain_once(name, prompt, temperature=0.4, json_mode=False):
    """One single attempt on a §22 OpenAI-compatible brain (Groq,
    OpenRouter, Cloudflare Workers AI, SambaNova, DashScope,
    ModelScope or SiliconFlow — no retries;
    call_llm's brain chain owns retrying and failover). Auth is an
    "Authorization: Bearer <key>" header with the brain's own env
    key. Unlike GLM, no "thinking" field is sent: these endpoints
    are plain OpenAI-compatible chat completions and the field is a
    z.ai extension. A truncated reply (finish_reason "length")
    raises Fail like any other failed attempt: truncated text never
    silently enters an article."""
    label = _EXTRA_BRAIN_LABELS.get(name, name)
    url, api_key = _brain_endpoint(name)
    if not url:
        raise Fail(f"{label} brain has no endpoint available: "
                   "set the CF_ACCOUNT_ID repo variable.")
    if not api_key:
        raise Fail(f"{label} brain has no API key available.")
    body = json.dumps({
        "model": _brain_model(name),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": EXTRA_BRAINS_MAX_TOKENS,
        **({"response_format": {"type": "json_object"}} if json_mode else {}),
    }).encode("utf-8")
    status, raw = http_request(url, method="POST", data=body,
                               headers={
                                   "Content-Type": "application/json",
                                   "Authorization": f"Bearer {api_key}"})
    if status != 200:
        raise Fail(f"{label} HTTP {status}: {raw[:300]!r}")
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise Fail(f"{label} returned a non-JSON body "
                   f"(HTTP {status}): {raw[:300]!r}")
    try:
        choice = data["choices"][0]
        text = (choice["message"]["content"] or "").strip()
        finish = choice.get("finish_reason")
    except Exception:
        raise Fail(f"{label} returned an unexpected shape: {raw[:300]!r}")
    if finish == "length":
        raise Fail(f"{label} reply truncated at "
                   f"{EXTRA_BRAINS_MAX_TOKENS} tokens "
                   "(finish_reason=length)")
    if not text:
        raise Fail(f"{label} returned empty text")
    return text

# ----------------------------------------------------------------------
# Brain chain: every logical LLM call walks an ordered list of brains
# (env LLM_CHAIN, comma-separated; default: LLM_PROVIDER, then glm,
# then gemini — the retired "github" service is never in the default
# chain). Each brain gets CHAIN_ATTEMPTS attempts with a
# CHAIN_BACKOFF_SECONDS x attempt pause between them; a brain whose
# key is missing is skipped. Only when every brain is skipped or
# exhausted does the call fail — fail-closed, as before.
#
# §22 append rule: any of the seven free-tier brains (EXTRA_BRAINS)
# whose credential is present in the environment is APPENDED to the
# chain — after whatever LLM_CHAIN / the default produced, in
# EXTRA_BRAINS order, without duplicating a name already listed —
# so a new brain joins the run the moment its secret exists, with
# no further configuration. With no new keys set, the chain is
# exactly the legacy chain.
# ----------------------------------------------------------------------

CHAIN_ATTEMPTS = 3
CHAIN_BACKOFF_SECONDS = 15
KNOWN_BRAINS = ("glm", "gemini", "github",
               "groq", "openrouter", "cloudflare", "sambanova",
               "dashscope", "modelscope", "siliconflow")
EXTRA_BRAINS = ("groq", "openrouter", "cloudflare", "sambanova",
                "dashscope", "modelscope", "siliconflow")


def llm_chain():
    """The ordered brain chain for this run. LLM_CHAIN (e.g.
    "glm,gemini") wins when set; otherwise the chain is LLM_PROVIDER
    (when set) followed by glm and gemini, deduplicated, order
    preserved. "github" is excluded from the DEFAULT chain (GitHub
    Models is retired); it is honoured only when named explicitly in
    LLM_CHAIN. Finally the §22 append rule adds every EXTRA_BRAINS
    brain whose credential is present, in EXTRA_BRAINS order."""
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
    for name in EXTRA_BRAINS:
        if name not in chain and _brain_key_available(name, ""):
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
    if name in EXTRA_BRAINS:
        url, key = _brain_endpoint(name)
        return bool(url and key)
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
    if name in EXTRA_BRAINS:
        return _extra_brain_once(name, prompt,
                                 temperature=temperature,
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


def english_first_queries(field, topic, api_key, model):
    """English-first query expansion (§18): ONE brain-chain call turns
    the (often Persian) field+topic into 3–5 precise ENGLISH scholarly
    search queries — Latin-script name variants (al-Biruni / Biruni /
    Abu Rayhan), the narrow topic's technical terms, the named work —
    plus, for a historical scholarly figure, one French and one German
    query. These queries drive wave 1, so the abstract-rich indexes
    search the EN/FR/DE literature directly instead of matching
    Persian keywords. Best-effort: ANY failure returns [] and the
    caller falls back to the legacy query behaviour — never fatal."""
    prompt = (
        "You build search queries for scholarly literature indexes "
        "(OpenAlex, HAL, Europe PMC, Semantic Scholar).\n"
        f"Field: {field}\nTopic: {topic}\n\n"
        "Write 3 to 5 PRECISE search queries IN ENGLISH that would "
        "find the genuine scholarly literature on this exact narrow "
        "topic. Rules:\n"
        "- Use Latin-script name variants for historical figures "
        "(e.g. al-Biruni, Biruni, Abu Rayhan) and the standard "
        "English titles/transliterations of their works.\n"
        "- Include the specific technical terms of the topic "
        "(methods, instruments, tables, places) — not broad field "
        "words alone.\n"
        "- Each query: 3–9 words, keywords only, no quotes, no "
        "Boolean operators.\n"
        "If the topic concerns a historical scholarly figure, ALSO "
        "give one French and one German query (same precision); "
        "otherwise set them to null.\n"
        "Reply with JSON only, like: "
        '{"queries": ["...", "..."], "french": "...", '
        '"german": "..."}')
    try:
        raw = call_llm(prompt, api_key, model, temperature=0.2,
                       json_mode=True)
        data = _parse_json_value(raw)
        items = []
        if isinstance(data, dict):
            items = list(data.get("queries") or [])
            for key in ("french", "german", "fr", "de"):
                extra = data.get(key)
                if isinstance(extra, str) and extra.strip():
                    items.append(extra)
                elif isinstance(extra, list):
                    items.extend(extra)
        elif isinstance(data, list):
            items = list(data)
        queries = []
        for q in items:
            q = str(q).strip().strip('"')[:200]
            if q and q not in queries:
                queries.append(q)
        queries = queries[:7]
        if queries:
            print(f"English-first queries (§18): {queries}")
        else:
            print("WARNING: English-first expansion returned no "
                  "queries; falling back to legacy queries")
        return queries
    except Exception as e:
        print(f"WARNING: English-first query expansion failed ({e}); "
              "falling back to legacy queries")
        return []


# ---------------------------------------------------------------------------
# §20 TEXT-FIRST upgrade (Jupiter's order, 2026-11-23): the article is
# written from FULL TEXT, not abstracts. Four parts:
#   1. a MULTILINGUAL QUERY MATRIX — the topic is expanded into several
#      phrasings per relevant language around fixed CORE KEYWORDS, and
#      each language's queries run in that language's open sources;
#   2. SOURCE MAP v2 — the brain names the author's own books AND other
#      books discussing the topic, and every title is verified against
#      the Internet Archive catalogue before it is trusted;
#   3. a FULL-TEXT ACQUISITION LAYER for ALL source types (Europe PMC
#      XML, Unpaywall, OpenAlex OA locations, index full-text links, a
#      generic PDF/HTML fetcher; books keep the §19 IA/LibGen routes),
#      with in-text passage extraction — only passages are stored;
#   4. a TEXT-FIRST GATE — publication needs >=5 relevant sources, >=3
#      of them carrying extracted full-text passages, and >=1 primary
#      book with passages extracted. YouTube lecture transcripts are
#      support only and never count toward the gate.
# ---------------------------------------------------------------------------

UNPAYWALL_EMAIL = CROSSREF_MAILTO


def _strip_diacritics(text):
    """Latin/Arabic diacritics removed (NFKD + combining-mark drop), so
    'Qānūn' and 'Qanun' compare equal; Arabic-script letters survive."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed
                   if unicodedata.category(ch) != "Mn")


def _norm_title_key(title):
    """Cross-language dedupe key component: lowercase, diacritics and
    punctuation stripped, whitespace collapsed (§20.5). Modifier
    letters (ʿ ʾ) and apostrophes are dropped outright so that
    'Masʿūdī' and 'Masudi' normalise identically."""
    t = _strip_diacritics((title or "").lower())
    t = re.sub(r"[\u02bc\u02be\u02bf\u2018\u2019']", "", t)
    t = re.sub(r"[^a-z0-9\u0600-\u06FF]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _dedupe_key(candidate):
    """One work counts once (§20.5): by DOI when present, else by
    normalised title + year."""
    doi = (candidate.get("doi") or "").strip().lower()
    if doi:
        return "doi:" + doi
    return "t:" + _norm_title_key(candidate.get("title")) + \
        "|" + str(candidate.get("year") or "")


def multilingual_query_matrix(field, topic, api_key=None, model=None):
    """§20.1 — one brain-chain call builds the search matrix: CORE
    KEYWORDS (author/work/technical terms + synonyms and historical
    forms), NAME VARIANTS, and per-language query sets — several
    phrasings per language, all anchored on the core keywords. English
    always; the brain adapts the other languages to the field (for
    historical / Islamic-science topics: ar, fa, de, fr, ru, es, tr).
    Hard caps: <=28 queries total, <=6 per language, priority-ordered
    (English first). ANY failure returns an empty matrix and the
    caller falls back to the §18 English-first behaviour."""
    empty = {"core_keywords": [], "name_variants": [], "languages": [],
             "queries": []}
    prompt = (
        "You build a MULTILINGUAL SEARCH MATRIX for scholarly "
        "literature and open full-text sources.\n"
        f"Field: {field}\nTopic: {topic}\n\n"
        "Return JSON only, with exactly these keys:\n"
        '- "core_keywords": 6-12 CORE terms that anchor relevance: '
        "the author's name, the named work(s), the central technical "
        "terms, plus synonyms and historical forms of those terms "
        "(e.g. for the size of the Earth: 'Earth radius', 'Earth "
        "diameter', 'size of the Earth', Arabic مساحة الأرض).\n"
        '- "name_variants": every script/spelling form of the '
        "author's name (e.g. Biruni, al-Biruni, Alberuni, البيروني, "
        "ابوریحان).\n"
        '- "queries": an object mapping a language code to a list of '
        "2-5 DIFFERENT phrasings in that language, every phrasing "
        "built around the core keywords (question form, term form, "
        "form naming the work, form naming the place/method). "
        "Always include \"en\". Add the languages in which serious "
        "scholarship on this topic is written — for historical or "
        "Islamic-science topics include ar, fa, de, fr, ru, es, tr; "
        "for other fields choose the languages that actually carry "
        "the literature. Allowed codes: en ar fa de fr ru es tr it "
        "la ur uz zh ja ko fi nl pl pt hr sr uk el.\n"
        "Each query: 3-10 words, keywords only, no quotes, no "
        "Boolean operators, written in that language's own script.")
    try:
        raw = call_llm(prompt, api_key, model, temperature=0.2,
                       json_mode=True)
        data = _parse_json_value(raw)
        if not isinstance(data, dict):
            print("WARNING: query matrix reply was not a JSON object; "
                  "falling back to §18 English-first queries")
            return empty
        core = []
        for kw in data.get("core_keywords") or []:
            kw = str(kw).strip()
            if kw and kw not in core:
                core.append(kw[:80])
        variants = []
        for v in data.get("name_variants") or []:
            v = str(v).strip()
            if v and v not in variants:
                variants.append(v[:80])
        by_lang = data.get("queries") or {}
        if not isinstance(by_lang, dict):
            print("WARNING: query matrix carried no per-language "
                  "queries; falling back to §18 English-first queries")
            return empty
        ordered_langs = [l for l in MATRIX_LANGUAGES if l in by_lang]
        ordered_langs += [l for l in by_lang
                          if l in MATRIX_LANGUAGES
                          and l not in ordered_langs]
        queries, languages = [], []
        for lang in ordered_langs:
            items = by_lang.get(lang) or []
            if isinstance(items, str):
                items = [items]
            kept = 0
            for q in items:
                q = str(q).strip().strip('"')[:200]
                if q and q not in queries and kept < MATRIX_PER_LANG:
                    queries.append(q)
                    kept += 1
            if kept:
                languages.append(lang)
            if len(queries) >= MATRIX_MAX_QUERIES:
                break
        queries = queries[:MATRIX_MAX_QUERIES]
        if not queries:
            print("WARNING: query matrix produced no usable queries; "
                  "falling back to §18 English-first queries")
            return empty
        print(f"Query matrix (§20): languages={languages}, "
              f"{len(queries)} queries, core keywords={core[:8]}, "
              f"name variants={variants[:6]}")
        return {"core_keywords": core[:12],
                "name_variants": variants[:8],
                "languages": languages, "queries": queries}
    except Exception as e:
        print(f"WARNING: query matrix failed ({e}); falling back to "
              "§18 English-first queries")
        return empty


def _title_variants(work):
    """Searchable title forms for a mapped work (§20.2): the brain's
    form, the diacritic-stripped plain form, and the Arabic-script
    form when the brain supplied one — deduplicated, order kept."""
    variants = []
    for t in (work.get("title"), _strip_diacritics(work.get("title")),
              work.get("title_ar")):
        t = (t or "").strip()
        if t and t not in variants:
            variants.append(t)
    return variants


def _ia_title_docs(title, rows=10):
    """Internet Archive advancedsearch restricted to the title field —
    the catalogue check for mapped works (§20.2)."""
    q = "title:(" + _lucene_clean(title) + ")"
    url = ("https://archive.org/advancedsearch.php?q="
           + urllib.parse.quote(q)
           + f"&fl[]=identifier,title,creator&rows={rows}"
             "&output=json")
    data = http_json(url)
    return (data.get("response") or {}).get("docs", []) or []


# ---------------------------------------------------------------------------
# §20.3 (2026-11-25, run 45660280064 lesson): catalogue confirmation must
# NOT depend on Google Books. That run dropped both mapped titles of
# al-Biruni's Qanun — Google Books returned HTTP 429 (daily quota) all
# run, and the old IA check searched title:(<the full long title>),
# whose terms are ANDed by Lucene, so the real IA item (held under a
# shorter mixed-script title) never matched. Confirmation now succeeds
# when ANY independent no-key catalogue confirms the work:
#   1. Internet Archive — several query forms (exact title field per
#      variant, distinctive-keyword title search, creator + keywords),
#      matched on normalised text (see _catalogue_norm);
#   2. OpenLibrary — title+author search, same matching rule;
#   3. Google Books — OPTIONAL corroboration only: its failure is
#      logged fail-soft and can never block or be required.
# The law is unchanged: a title confirmed by NO catalogue is still
# dropped as catalogue-unconfirmed, and when NO catalogue answers at
# all the work is kept unverified (fail-soft), exactly as before.
# ---------------------------------------------------------------------------

# Arabic letter unifications for catalogue matching: the same word is
# catalogued with Arabic-script and Persian-script letters, teh
# marbuta vs heh, alef forms, etc. Both sides are folded to one form.
_CATALOGUE_ARABIC_FOLD = str.maketrans({
    "ي": "ی",  # Arabic yeh -> Farsi yeh
    "ك": "ک",  # Arabic kaf -> Farsi kaf
    "ة": "ه",  # teh marbuta -> heh
    "ؤ": "و",  # waw with hamza -> waw
    "ئ": "ی",  # yeh with hamza -> yeh
    "ى": "ی",  # alef maksura -> yeh
    "أ": "ا",  # alef with hamza above -> alef
    "إ": "ا",  # alef with hamza below -> alef
    "آ": "ا",  # alef with madda -> alef
    "ـ": "",        # tatweel dropped
})

# (Self-contained: this block sits before _SCREEN_STOPWORDS in the
# file, so the generic words are repeated here.)
_CATALOGUE_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "over", "under",
    "between", "among", "that", "this", "these", "those", "have",
    "has", "are", "was", "were", "will", "would", "can", "could",
    "its", "der", "die", "das", "und", "den", "des", "les", "une",
    "est", "sur", "aux", "dans", "par", "qui", "que",
    "al", "el", "il", "ibn", "ben", "bin", "abu", "von", "van",
    "kitab", "book",
}


def _catalogue_norm(text):
    """Normal form for catalogue matching: diacritics stripped (so
    'Qānūn' = 'Qanun'), modifier letters (ʿ ʾ) dropped, Arabic letter
    variants folded (ي/ی, ك/ک, ة/ه, alef forms), lowercased."""
    t = _strip_diacritics(text or "")
    t = re.sub(r"[ʿʾʻʼ'’]", "", t)
    t = t.translate(_CATALOGUE_ARABIC_FOLD)
    return t.lower()


def _catalogue_tokens(text):
    """Distinctive tokens of a title/author string in catalogue
    normal form: word tokens of length >= 3, stopwords removed."""
    return {t for t in re.findall(r"[^\W_]+", _catalogue_norm(text))
            if len(t) >= 3 and t not in _CATALOGUE_STOPWORDS}


def _tokens_match(a, b):
    """Two normalised tokens match when equal, or when one is a
    prefix of the other and both are at least 4 chars (covers
    transliteration wobble like masudi / masudic, qanun / qanuni)."""
    if a == b:
        return True
    if min(len(a), len(b)) >= 4:
        return a.startswith(b) or b.startswith(a)
    return False


def _matched_tokens(want, got):
    """The subset of `want` tokens that have a match in `got`."""
    return {t for t in want if any(_tokens_match(t, g) for g in got)}


def _catalogue_match(work, variant, doc_title, doc_creators,
                     doc_identifier=""):
    """(confirmed, score) — the §20.3 matching rule. Both sides are
    compared in catalogue normal form. Let A = the variant's
    distinctive title tokens, B = the doc title's tokens, shared =
    A-tokens matched in B, overlap = |shared| / min(|A|, |B|).
    The author is corroborated when any distinctive author token
    matches the doc's creators, title or identifier (IA records often
    carry no creator field and embed the author in the title itself).
    Confirm when:
      (1) overlap >= 0.6 AND (author corroborated OR >= 2 shared
          tokens) — a strong title match; or
      (2) author corroborated AND >= 2 shared tokens AND the shared
          tokens are at least half of A — the catalogue's short title
          against the map's long title (the Qanun case: the mapped
          'Al-Qanun al-Masudi fi al-Hay'a wa al-Nujum' vs the IA
          item's 'Al Qanun Al Masudi …').
    (Run 45107999596 lesson, kept: a 1-token title like 'India'
    matched on that token alone, without author corroboration, is
    NOT evidence — 'Theatre in India' confirms nothing.)"""
    a = _catalogue_tokens(variant)
    b = _catalogue_tokens(doc_title)
    if not a or not b:
        return False, 0.0
    shared = _matched_tokens(a, b)
    overlap = len(shared) / min(len(a), len(b))
    author_text = " ".join(doc_creators or []) + " " + \
        (doc_title or "") + " " + (doc_identifier or "")
    author_ok = bool(_matched_tokens(
        _catalogue_tokens(work.get("author")),
        _catalogue_tokens(author_text)))
    if overlap >= 0.6 and (author_ok or len(shared) >= 2):
        return True, overlap
    if author_ok and len(shared) >= 2 and len(shared) * 2 >= len(a):
        return True, overlap
    return False, overlap


def _ia_catalogue_docs(work):
    """(docs, responded): Internet Archive catalogue docs for a mapped
    work from SEVERAL query forms (§20.3), deduplicated by
    identifier: the exact title field per title variant, a
    distinctive-keyword title search per variant (Lucene ANDs the
    terms of a full-title query, so a longer mapped title never
    matched the catalogue's shorter one), and a creator + keywords
    search. IA confirmation alone is sufficient — no other catalogue
    is consulted when IA confirms."""
    queries = []
    variants = _title_variants(work)
    for variant in variants:
        cleaned = _lucene_clean(variant)
        if cleaned:
            queries.append("title:(" + cleaned + ")")
    for variant in variants:
        kws = [t for t in re.findall(r"[^\W_]+",
                                     _lucene_clean(variant))
               if len(t) >= 3
               and t.lower() not in _CATALOGUE_STOPWORDS]
        if len(kws) >= 2:
            queries.append("title:(" + " OR ".join(kws[:6]) + ")")
    author = _lucene_clean(work.get("author"))
    if author:
        surname = author.split()[-1]
        kws = []
        for variant in variants:
            for t in re.findall(r"[^\W_]+", _lucene_clean(variant)):
                if len(t) >= 3 and \
                        t.lower() not in _CATALOGUE_STOPWORDS \
                        and t not in kws:
                    kws.append(t)
        if kws:
            queries.append("creator:(" + surname + ") AND title:("
                           + " OR ".join(kws[:6]) + ")")
    docs = {}
    responded = False
    for q in queries:
        url = ("https://archive.org/advancedsearch.php?q="
               + urllib.parse.quote(q)
               + "&fl[]=identifier,title,creator&rows=10"
                 "&output=json")
        try:
            data = http_json(url)
            responded = True
        except Exception as e:
            print(f"NOTE: IA catalogue query unreachable ({e})")
            continue
        for doc in (data.get("response") or {}).get("docs", []) or []:
            ident = str(doc.get("identifier") or "")
            docs[ident or str(len(docs))] = doc
    return list(docs.values()), responded


def _has_arabic_script(text):
    """True when the string carries Arabic-script characters
    (Arabic + Persian blocks)."""
    return any("؀" <= ch <= "ۿ" for ch in (text or ""))  # U+0600..U+06FF


def _ia_arabic_docs(work):
    """(docs, responded) — §20.8: Internet Archive docs from GENERAL
    (non-fielded) queries over the Arabic-script title variants.
    Live-verified 2026-11-26 against the Qanun item
    (al-qanun-al-masudi-biruni-muhammad-ibn-ahmad, creator field
    EMPTY, a multi-work collection): the Latin title-field forms
    return numFound=0 and the Arabic title-FIELD query
    title:(القانون المسعودي) returns 13 docs but the real item
    ranks below the engine's rows=10 window, while the general
    query القانون المسعودي بيروني returns numFound=4 with the item
    inside, and the bare Arabic title returns numFound=18 — so the
    general query is run with rows=50 (the whole small result set)
    and deduplicated by identifier. IA's fielded index simply does
    not surface this class of item (creator-less, mixed-script,
    collection items) the way its general index does."""
    queries = []
    for variant in _title_variants(work):
        if not _has_arabic_script(variant):
            continue
        cleaned = _lucene_clean(variant)
        if cleaned and cleaned not in queries:
            queries.append(cleaned)
    docs = {}
    responded = False
    for q in queries:
        url = ("https://archive.org/advancedsearch.php?q="
               + urllib.parse.quote(q)
               + "&fl[]=identifier,title,creator&rows=50"
                 "&output=json")
        try:
            data = http_json(url)
            responded = True
        except Exception as e:
            print(f"NOTE: IA Arabic catalogue query unreachable ({e})")
            continue
        for doc in (data.get("response") or {}).get("docs", []) or []:
            ident = str(doc.get("identifier") or "")
            docs[ident or str(len(docs))] = doc
    return list(docs.values()), responded


def _ia_slug_candidates(work):
    """§20.8: IA identifier-slug candidates derived from the Latin
    title variants — lowercase, diacritics stripped, non-alphanumeric
    runs folded to single hyphens ('Al-Qanun al-Mas'udi' ->
    'al-qanun-al-masudi'). IA identifiers ARE title slugs, usually
    with the author's surname appended, so the candidates are: the
    full slug, its 4/3/2-token prefixes, and the 4-token prefix and
    full slug with the author surname appended. Prefix queries
    (identifier:<slug>*) then find the real item even when no title
    search surfaces it."""
    surname = ""
    author = _strip_diacritics(work.get("author") or "")
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", author) if p]
    if parts:
        surname = parts[-1].lower()
    slugs = []
    for variant in _title_variants(work):
        if _has_arabic_script(variant):
            continue
        folded = _strip_diacritics(variant).lower()
        folded = re.sub(r"['’ʿʾʻʼ]", "", folded)  # Mas'udi -> masudi
        toks = [t for t in re.split(r"[^a-z0-9]+", folded) if t]
        if not toks:
            continue
        full = "-".join(toks)
        cands = [full]
        for n in (4, 3, 2):
            if len(toks) > n:
                cands.append("-".join(toks[:n]))
        base = "-".join(toks[:4]) if len(toks) >= 2 else full
        if surname and len(surname) >= 3:
            cands.append(base + "-" + surname)
            if full != base:
                cands.append(full + "-" + surname)
        for c in cands:
            if len(c) >= 5 and c not in slugs:
                slugs.append(c)
    return slugs[:8]


def _ia_identifier_docs(work):
    """(docs, responded) — §20.8: Internet Archive docs from
    IDENTIFIER-pattern prefix queries (identifier:<slug>*), used
    when the title searches returned nothing usable. Live-verified
    2026-11-26: identifier:al-qanun-al-masudi* returns numFound=2
    including the real Qanun item; identifier:al-qanun* returns 6
    including it. A hit here is only a CANDIDATE — the caller
    applies the same title-matching rules to the doc's real title
    (and identifier) before anything is confirmed."""
    docs = {}
    responded = False
    for slug in _ia_slug_candidates(work):
        q = "identifier:" + slug + "*"
        url = ("https://archive.org/advancedsearch.php?q="
               + urllib.parse.quote(q)
               + "&fl[]=identifier,title,creator&rows=10"
                 "&output=json")
        try:
            data = http_json(url)
            responded = True
        except Exception as e:
            print(f"NOTE: IA identifier query unreachable ({e})")
            continue
        for doc in (data.get("response") or {}).get("docs", []) or []:
            ident = str(doc.get("identifier") or "")
            docs[ident or str(len(docs))] = doc
    return list(docs.values()), responded


def _openlibrary_docs(work):
    """(docs, responded): OpenLibrary search.json docs for a mapped
    work — a second independent no-key catalogue (§20.3). Each doc is
    normalised to {'title': ..., 'creator': [...]}."""
    title = (work.get("title") or "").strip()
    author = (work.get("author") or "").strip()
    if not title:
        return [], False
    url = ("https://openlibrary.org/search.json?title="
           + urllib.parse.quote(title)
           + "&author=" + urllib.parse.quote(author)
           + "&limit=10&fields=title,author_name")
    try:
        data = http_json(url)
    except Exception as e:
        print(f"NOTE: OpenLibrary catalogue check unreachable for "
              f"{title!r} ({e})")
        return [], False
    out = []
    for doc in data.get("docs", []) or []:
        out.append({"title": doc.get("title") or "",
                    "creator": doc.get("author_name") or []})
    return out, True


EXISTENCE_FETCH_CAP = 128 * 1024  # §20.4: bounded djvu fetchability probe
EXISTENCE_PROBE_MAX = 3           # §20.4: at most this many IA items probed


# §20.8: tokens too generic to carry an existence confirmation.
# The §20.4 rule (author corroborated + >= 1 shared distinctive
# token) proved too loose live (GRIS retest run 45685919581): the
# brain's garbled Tahdid title 'Tahdid al-Amanat li-Tahqiq
# al-Aqalim' was existence-confirmed against al-Biruni's INDIA —
# the only shared token was 'tahqiq', a generic genre word (a
# "tahqiq" is any critical edition / investigation) — and two more
# wrong books were confirmed the same way ('The History of the
# Exact Sciences in Islam' -> 'Studies in tudor history' via
# 'history'; 'The Measurement of the Earth in the Time of
# al-Biruni' -> 'in time of earthquake' via 'time'). A token in
# this set therefore never counts toward the §20.8 threshold:
# genre/form words (tahqiq, kitab, book, treatise, works,
# introduction, studies), broad field words (history, science,
# sciences) and other near-universal title words (time, earth,
# world, life, art, commentary...). 'kitab'/'book' are also in
# _CATALOGUE_STOPWORDS; they are repeated here so this set is
# self-contained.
_CATALOGUE_WEAK_TOKENS = {
    "tahqiq", "tahqiqat", "history", "science", "sciences",
    "scientific", "book", "books", "kitab", "kutub", "introduction",
    "studies", "study", "time", "earth", "world", "life", "art",
    "arts", "treatise", "treatises", "works", "work", "commentary",
    "notes", "essays", "essay", "letters", "manual", "guide",
    "principles", "theory", "practice", "general", "complete",
    "collected", "selected", "volume", "volumes", "part", "parts",
    "first", "second", "third", "new", "old", "great", "short",
}


def _distinctive_shared_tokens(work, doc_title, doc_identifier=""):
    """§20.8: the DISTINCTIVE title tokens shared between ANY title
    variant of the mapped work and the catalogue doc — its real
    title OR its identifier (IA identifiers are title slugs:
    'al-qanun-al-masudi-biruni-...' carries qanun + masudi).
    A shared token counts only when it is not a weak/generic token
    (see _CATALOGUE_WEAK_TOKENS) and not one of the author's own
    name tokens — author corroboration is a separate requirement
    and must not double-count as title evidence (otherwise any
    same-author book sharing one generic word would 'confirm')."""
    got = _catalogue_tokens(doc_title)
    if doc_identifier:
        got = got | _catalogue_tokens(
            str(doc_identifier).replace("-", " ").replace("_", " "))
    if not got:
        return set()
    author_tokens = _catalogue_tokens(work.get("author"))
    shared = set()
    for variant in _title_variants(work):
        shared |= _matched_tokens(_catalogue_tokens(variant), got)
    return {t for t in shared
            if t not in _CATALOGUE_WEAK_TOKENS
            and t not in author_tokens}


def _catalogue_existence_match(work, doc_title, doc_creators,
                               doc_identifier=""):
    """§20.4 existence rule (CONTENT-OVER-TITLE law), TIGHTENED by
    §20.8: a catalogue record counts as evidence that the mapped
    work EXISTS when the AUTHOR is corroborated (any distinctive
    author token matches the doc's creators, title or identifier)
    AND at least TWO DISTINCTIVE title tokens are shared between
    any title variant and the doc's real title or identifier
    (see _distinctive_shared_tokens — weak/generic tokens like
    'tahqiq', 'history', 'time' never count, and neither do the
    author's own name tokens). The title's exact form stays
    irrelevant — catalogues hold the same work under different
    titles, transliterations and scripts, and Jupiter's law is
    that the title must not decide a book's fate. The Qanun passes
    via qanun + masudi (Latin title/identifier) and القانون +
    المسعودي (Arabic title); the garbled-Tahdid-vs-India case
    fails: its only shared token is the weak 'tahqiq'. The
    1-token-alone guard is preserved and strengthened: without
    author corroboration nothing confirms ('India' vs 'Theatre in
    India')."""
    author_text = " ".join(doc_creators or []) + " " + \
        (doc_title or "") + " " + (doc_identifier or "")
    author_ok = bool(_matched_tokens(
        _catalogue_tokens(work.get("author")),
        _catalogue_tokens(author_text)))
    if not author_ok:
        return False
    if not _catalogue_tokens(doc_title) and not doc_identifier:
        return False
    return len(_distinctive_shared_tokens(
        work, doc_title, doc_identifier)) >= 2


def _ia_retrievable_copy(work, ia_docs):
    """§20.4: find a REAL, RETRIEVABLE IA copy of the mapped work —
    an item that plausibly is this work (the §20.3 matcher OR the
    existence rule above) whose djvu full text actually fetches
    (bounded probe). Returns the doc, or None. A retrievable copy is
    the strongest existence proof: the pipeline will read the book's
    CONTENT, and content — not the title — decides relevance later."""
    candidates = []
    for doc in ia_docs:
        creators = doc.get("creator") or []
        if isinstance(creators, str):
            creators = [creators]
        ident = str(doc.get("identifier") or "")
        if not ident:
            continue
        strong = any(
            _catalogue_match(work, variant, doc.get("title") or "",
                             creators, ident)[0]
            for variant in _title_variants(work))
        if strong or _catalogue_existence_match(
                work, doc.get("title") or "", creators, ident):
            candidates.append(doc)
    for doc in candidates[:EXISTENCE_PROBE_MAX]:
        ident = str(doc.get("identifier") or "")
        try:
            text = _ia_djvu_text(ident, cap=EXISTENCE_FETCH_CAP)
        except Exception as e:
            print(f"NOTE: IA retrievability probe failed for "
                  f"{ident} ({e})")
            continue
        if text:
            return doc
    return None


def verify_work_in_catalogue(work):
    """§20.3 — a mapped title is trusted only if an independent
    catalogue actually holds a work matching one of the title
    variants. Internet Archive and OpenLibrary are each sufficient on
    their own; Google Books is optional corroboration whose failure
    (HTTP 429 quota, timeout, anything) is logged fail-soft and NEVER
    blocks a confirmation from IA or OpenLibrary. Returns
    (confirmed, matched_title) and logs which catalogue confirmed.
    A catalogue TRANSPORT failure everywhere keeps the work
    (fail-soft, logged): an unreachable catalogue must not silently
    delete a real book, but a catalogue that answers with no match
    drops the title — the §19 lesson was a brain inventing or
    misnaming the key work.

    §20.4 (CONTENT-OVER-TITLE law, Jupiter 2026-11-25): confirmation
    is EXISTENCE-based. A work is confirmed when a real copy is found
    and retrievable (an IA item whose djvu text fetches), or when a
    catalogue record exists for the author+work under ANY title form
    (author corroborated + >= 1 shared distinctive title token). The
    §20.3 title-overlap thresholds can no longer drop a work for
    which such a copy/record exists. On confirmation the confirming
    catalogue, the copy's REAL catalogue title and (for IA) its
    identifier are recorded on the work dict (catalogue_via /
    catalogue_title / catalogue_id) so the real title becomes the
    citation identity downstream. A work with NO real copy or record
    anywhere is still dropped — invented works still die.

    §20.8 (2026-11-26, the Qanun lesson): the IA item for
    al-Biruni's Qanun EXISTS (identifier
    al-qanun-al-masudi-biruni-muhammad-ibn-ahmad, creator field
    empty, a multi-work collection) yet every Latin title-field
    query returned numFound=0, the Arabic title-field query ranked
    it below the rows window, and OpenLibrary/Google Books could
    not confirm it either — so both mapped titles were dropped
    while the book sat in the archive. Two more IA routes now run
    before a work can be dropped: a GENERAL search over the
    Arabic-script title variants, and an IDENTIFIER-pattern
    prefix search from slugs derived from the Latin variants —
    both live-verified to return the Qanun item; their hits are
    candidates judged by the same matching rules. And the §20.4
    existence rule is tightened: it now needs >= 2 DISTINCTIVE
    shared title tokens (weak tokens like 'tahqiq'/'history' and
    the author's own name tokens never count), after the GRIS
    retest existence-confirmed three WRONG books on one generic
    token each."""
    title = (work.get("title") or "").strip()
    author = (work.get("author") or "").strip()
    variants = _title_variants(work)
    responded = False

    def _confirm_from_ia_docs(docs, via):
        """§20.3 title-match over one pool of IA docs; on a match,
        record the confirming route + the doc's REAL title and
        identifier on the work and return the real title."""
        for doc in docs:
            doc_title = doc.get("title") or ""
            creators = doc.get("creator") or []
            if isinstance(creators, str):
                creators = [creators]
            for variant in variants:
                ok, _score = _catalogue_match(
                    work, variant, doc_title, creators,
                    str(doc.get("identifier") or ""))
                if ok:
                    work["catalogue_via"] = via
                    work["catalogue_title"] = doc_title
                    work["catalogue_id"] = \
                        str(doc.get("identifier") or "")
                    print(f"catalogue-confirmed via: {via} — "
                          f"{title!r} matched {doc_title!r}")
                    return doc_title
        return ""

    def _confirm_from_copy(copy_doc, via):
        doc_title = copy_doc.get("title") or ""
        work["catalogue_via"] = via
        work["catalogue_title"] = doc_title
        work["catalogue_id"] = str(copy_doc.get("identifier") or "")
        print(f"catalogue-confirmed via: {via} — {title!r} exists "
              f"as {doc_title!r} [{work['catalogue_id']}] with a "
              "fetchable full text (§20.4 existence rule, §20.8 "
              "tightened)")
        return doc_title

    # 1. Internet Archive (multi-form search; sufficient alone).
    try:
        ia_docs, ia_responded = _ia_catalogue_docs(work)
        responded = responded or ia_responded
        hit = _confirm_from_ia_docs(ia_docs, "archive.org")
        if hit:
            return True, hit
        # 1a. §20.8: general (non-fielded) search over the
        # Arabic-script title variants — IA's fielded index does
        # not surface creator-less mixed-script collection items
        # (the Qanun case) but its general index does.
        ar_docs, ar_responded = _ia_arabic_docs(work)
        responded = responded or ar_responded
        hit = _confirm_from_ia_docs(
            ar_docs, "archive.org (arabic title)")
        if hit:
            return True, hit
        # 1b. §20.4 existence: a real RETRIEVABLE copy confirms the
        # work even when no title form matches the §20.3 thresholds —
        # the copy's content will be judged, not its title.
        copy_doc = _ia_retrievable_copy(work, ia_docs + ar_docs)
        if copy_doc is not None:
            return True, _confirm_from_copy(
                copy_doc, "archive.org (retrievable copy)")
        # 1c. §20.8: identifier-pattern search — only when the
        # title routes above found nothing usable. The hits are
        # candidates: the same §20.3 matching (and, for the copy
        # probe, the tightened §20.8 existence rule) decides.
        id_docs, id_responded = _ia_identifier_docs(work)
        responded = responded or id_responded
        hit = _confirm_from_ia_docs(
            id_docs, "archive.org (identifier)")
        if hit:
            return True, hit
        copy_doc = _ia_retrievable_copy(work, id_docs)
        if copy_doc is not None:
            return True, _confirm_from_copy(
                copy_doc, "archive.org (identifier)")
    except Exception as e:
        print(f"NOTE: IA catalogue check failed for {title!r} ({e})")

    # 2. OpenLibrary (sufficient alone).
    ol_docs, ol_responded = _openlibrary_docs(work)
    responded = responded or ol_responded
    for doc in ol_docs:
        for variant in variants:
            ok, _score = _catalogue_match(
                work, variant, doc.get("title") or "",
                doc.get("creator") or [])
            if ok:
                work["catalogue_via"] = "openlibrary.org"
                work["catalogue_title"] = doc.get("title")
                print(f"catalogue-confirmed via: openlibrary.org — "
                      f"{title!r} matched {doc.get('title')!r}")
                return True, doc.get("title")
    # 2b. §20.4 existence: an OpenLibrary record for the author+work
    # under ANY title form confirms existence.
    for doc in ol_docs:
        if _catalogue_existence_match(work, doc.get("title") or "",
                                      doc.get("creator") or []):
            work["catalogue_via"] = \
                "openlibrary.org (any-title record)"
            work["catalogue_title"] = doc.get("title")
            print(f"catalogue-confirmed via: openlibrary.org "
                  f"(any-title record) — {title!r} exists as "
                  f"{doc.get('title')!r} (§20.4 existence rule)")
            return True, doc.get("title")

    # 3. Google Books — optional corroboration ONLY. Any failure is
    # fail-soft and never required, never blocking.
    try:
        hits = search_google_books_primary(
            {"title": title, "author": author})
        responded = True
        for h in hits:
            for variant in variants:
                ok, _score = _catalogue_match(
                    work, variant, h.get("title") or "",
                    h.get("authors") or [])
                if ok:
                    work["catalogue_via"] = "google books"
                    work["catalogue_title"] = h.get("title")
                    print(f"catalogue-confirmed via: google books — "
                          f"{title!r} matched {h.get('title')!r}")
                    return True, h.get("title")
    except Exception as e:
        print(f"NOTE: Google Books catalogue check unreachable for "
              f"{title!r} ({e}); continuing without it (fail-soft)")

    if responded:
        return False, ""
    print(f"NOTE: no catalogue answered for {title!r}; keeping the "
          "mapped work unverified (fail-soft)")
    return True, ""


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
        if s.get("source_type") == "primary":
            card += ("\n    PRIMARY SOURCE (the author's own work / "
                     "a foundational document for this topic; the "
                     "excerpt below is from the work itself)")
        if s.get("source_type") == "video":
            card += ("\n    SUPPORTING LECTURE TRANSCRIPT (YouTube) — "
                     "cite ONLY as support for an explanation; never "
                     "as the sole evidence for a claim, and never as "
                     "a primary source.")
        if s.get("registry_match"):
            card += (f"\n    Registry match (curated open-access "
                     f"registry, credibility signal): "
                     f"{s['registry_match']}")
        if s.get("fulltext_excerpt"):
            if s.get("passages"):
                card += (f"\n    Full-text PASSAGES extracted from "
                         f"inside this source (located by the "
                         f"topic's keywords — the writing input for "
                         f"[{i}]):\n"
                         + s["fulltext_excerpt"][:FULLTEXT_CARD_CHARS])
            else:
                card += (f"\n    Full-text excerpt from this verified "
                         f"source itself (grounding for [{i}]):\n"
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
        if s.get("source_type") == "primary":
            type_line = ("PRIMARY SOURCE — the author's own work / a "
                         "foundational document for this topic")
        else:
            type_line = "Secondary scholarship"
        # §20.4: the extracted passages / full-text excerpt are the
        # evidence for EVERY source type — they outrank the abstract,
        # and the title is never the evidence.
        content = (s.get("fulltext_excerpt") or "").strip()
        content_label = ("Extracted passages from inside the source"
                         if content else "Abstract")
        if not content:
            content = (s.get("abstract") or "").strip()
        lines.append(
            f"[{j}] Title: {s.get('title', '')}\n"
            f"    Type: {type_line}\n"
            f"    Authors: {', '.join((s.get('authors') or [])[:4])} | "
            f"Year: {s.get('year') or 'n.d.'} | "
            f"Venue: {s.get('venue') or 'n/a'} | "
            f"Index: {s.get('index') or '?'}\n"
            f"    {content_label}: "
            f"{content[:1200] or '(no abstract available)'}")
    prompt = (
        "You are screening candidate sources for a scholarly article.\n"
        f"Field: {field}\nTopic: {topic}\n\n"
        "Judge relevance by the CONTENT shown for each candidate — "
        "the extracted passages taken from INSIDE the source — NOT "
        "by its title (§20.4 content-over-title law): a title that "
        "does not mention the topic does NOT make a source "
        "irrelevant when its passages address the topic, and a "
        "title that matches the topic does NOT save a source whose "
        "content does not address it.\n\n"
        "Judge EACH candidate by its TYPE (marked on its line):\n"
        "- PRIMARY SOURCE (the topic author's own work, or a "
        "foundational document, in a scholarly edition or "
        "translation): keep=true when its extracted passages "
        "genuinely address the topic. When the topic is about that "
        "author's work or method, the author's own treatment IS "
        "relevant by definition.\n"
        "- SECONDARY SCHOLARSHIP: keep=true when it is genuinely "
        "ABOUT the topic's subject or method — a general treatment "
        "of the same method, or a modern application of it, still "
        "counts. A source does NOT have to perform the article's "
        "own exact claim, reconstruction or error analysis itself.\n"
        "For BOTH types, BOTH of these must also hold:\n"
        "1) The match is genuine, not a keyword coincidence (a "
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
    the first — agrees. If every different brain errors, the first
    judge's brain is the logged availability fallback (§20.2); a
    keep counts as unjudged only when all chain brains errored.
    Every verdict is logged, and the fail-closed bar is applied to
    the result."""
    stats = {"input": len(sources), "relevant": 0, "counted": 0,
             "drop_irrelevant": 0, "drop_tradition": 0,
             "drop_disagreement": 0, "drop_no_content": 0,
             "registry_matched": 0, "primary_counted": 0,
             # §20.7: titles the first judge kept that died as judge
             # disagreement — the itemised report's near-miss list.
             "disagreement_titles": []}
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
            if s.get("passages") or \
                    (s.get("fulltext_excerpt") or "").strip():
                # §20.4 CONTENT-OVER-TITLE law (generalises §20.1):
                # the deterministic title/abstract-overlap drop must
                # NEVER drop a source that carries extracted content
                # — passages OR a fetched full-text excerpt (even a
                # head excerpt) — of ANY source type. The content IS
                # the relevance evidence; the LLM screen judges it.
                # (§20.1 keyed only on `passages` for primaries; the
                # GRIS live run showed a primary whose fetch produced
                # a head excerpt — keyword matching inside the book
                # found no hits, so `passages` was never set — was
                # still deterministic-dropped despite its full text
                # having been fetched. That gap is now closed for
                # every source type and every orchestration, since
                # both call this shared function.)
                kind = ("extracted passages" if s.get("passages")
                        else "a fetched full-text excerpt")
                s["relevance_reason"] = (
                    f"source carrying {kind} — sent to LLM screen "
                    "despite zero topic-term overlap (§20.4: content, "
                    "not title, is judged)")
                borderline.append(s)
                print(f"relevance: borderline {title[:70]} — "
                      f"{s['relevance_reason']}")
            else:
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
            others2 = [name for name in llm_chain()
                       if name != first_brain
                       and name in KNOWN_BRAINS
                       and _brain_key_available(name, api_key)]
            # Fail-soft along the chain (§19) + availability
            # fallback (§20.2): a pinned brain that errors (e.g.
            # HTTP 429/503) no longer kills the first judge's
            # keeps — the NEXT available chain brain judges
            # instead. If EVERY different brain errors, the first
            # judge's own brain is tried LAST as an availability
            # fallback; its use is logged loudly, never silent.
            # Only when every brain in the chain has genuinely
            # been tried and errored does a keep count as
            # unjudged, and the existing fail-closed disagreement
            # handling applies.
            candidates2 = list(others2)
            if (first_brain in KNOWN_BRAINS
                    and _brain_key_available(first_brain, api_key)):
                candidates2.append(first_brain)
            if not candidates2:
                print("second judge: unavailable (no keyed brain "
                      "in the chain) — first-judge verdicts "
                      "stand")
            else:
                for cand_brain in candidates2:
                    if cand_brain == first_brain:
                        print(f"second judge via fallback brain: "
                              f"{cand_brain} (same provider as the "
                              f"first judge — availability fallback: "
                              f"every other chain brain failed)")
                    else:
                        print(f"second judge: {cand_brain} reviews "
                              f"{len(first_keeps)} borderline keep(s) "
                              f"of first judge {first_brain}")
                    raw2 = _llm_relevance_screen(
                        field, topic, first_keeps, api_key, model,
                        provider=cand_brain)
                    if raw2:
                        second_brain = cand_brain
                        for k, s2 in enumerate(first_keeps, 1):
                            if k in raw2:
                                second[id(s2)] = raw2[k]
                        break
                    print(f"second judge: {cand_brain} returned no "
                          "verdict (provider failure) — trying the "
                          "next brain in the chain")
                if second_brain is None:
                    second_brain = candidates2[0]
                    print("second judge: ALL chain brains failed — "
                          "keeps count as unjudged (fail-closed)")
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
                    stats["disagreement_titles"].append(title)
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
    stats["primary_counted"] = sum(
        1 for s in counted if s.get("source_type") == "primary")
    stats["fulltext_counted"] = sum(1 for s in counted
                                    if s.get("fulltext"))
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

def _adaptation_lines(adaptation):
    """§20.10 visibility lines (Persian) for the funnel / §20.7
    report. Empty when no adaptation was attempted."""
    if not adaptation or not adaptation.get("attempts"):
        return []
    lines = ["",
             f"موضوع اولیه: {adaptation.get('original_topic', '')}"]
    if adaptation.get("adapted_topic"):
        lines.append(f"موضوع تغییر‌یافته: "
                     f"{adaptation['adapted_topic']}")
        lines.append(f"دلیل تغییر موضوع: "
                     f"{adaptation.get('reason', '')}")
    else:
        lines.append("موضوع تغییر‌یافته: هیچ‌کدام پذیرفته نشد")
    lines.append("تلاش‌های تغییر موضوع (§20.10):")
    for a in adaptation["attempts"]:
        lines.append(f"• {a.get('topic', '')} — شمارش‌شده: "
                     f"{a.get('counted', 0)}")
    return lines


def build_funnel_text(topic, funnel, success, stage=None,
                      word_count=None, word_target=None,
                      adaptation=None, review=None):
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
             f"منابع اصلی (متن مؤلف/سند بنیادین): "
             f"{funnel.get('primary_counted', 0)}",
             f"متن کامل گرفته‌شده: "
             f"{funnel.get('fulltext_counted', 0)}",
             "",
             "حذف‌شده‌ها:",
             "• دروازهٔ زبان در ورودی (فارسی/عربی): "
             f"{funnel.get('drop_language_gate', 0)}",
             f"• نامرتبط: {funnel.get('drop_irrelevant', 0)}",
             f"• بدون محتوا: {funnel.get('drop_no_content', 0)}",
             "• خارج از سنت پژوهشی مجاز (انگلیسی/فرانسوی/آلمانی): "
             f"{funnel.get('drop_tradition', 0)}"]
    if funnel.get("drop_disagreement"):
        lines.append("• اختلاف دو داور ارتباط: "
                     f"{funnel['drop_disagreement']}")
    lines += _scope_lines()
    lines += _adaptation_lines(adaptation)
    lines += _review_lines(review)
    lines.append("")
    if success:
        lo, hi = word_target or (0, 0)
        lines.append(f"✅ مقاله منتشر شد — تعداد کلمات: {word_count} "
                     f"(هدف: {lo}–{hi})")
    else:
        lines.append(f"❌ تولید متوقف شد — مرحله: {stage or 'نامشخص'}")
    return "\n".join(lines)


def send_funnel_dm(token, chat_id, topic, funnel, success, stage=None,
                   word_count=None, word_target=None, adaptation=None,
                   review=None):
    """Fail-soft: print the funnel as log lines and DM it to the
    requester (ARTICLE_CHAT_ID). Never raises. Returns True if sent."""
    text = build_funnel_text(topic, funnel, success, stage=stage,
                             word_count=word_count,
                             word_target=word_target,
                             adaptation=adaptation, review=review)
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


# --------------------------------------------------------------------------
# Detailed source report (§20.7, Jupiter 2026-11-25: «بعد از این ریز
# منابع و محتوای مرتبط رو پیدا کردی بنویس ببینم وضع چطوره») — after
# sources and relevant content are found, the owner gets an ITEMISED
# report, not just funnel counts: every counted source with its real
# title, type/relation, how its full text was obtained, its in-text
# keyword-hit count and (for primaries) which catalogue confirmed it;
# then the near-misses — first-judge keeps that died as judge
# disagreement. Strictly a read-only reporting layer: it changes no
# screening or confirmation behaviour. Sent at the text-first gate
# point, whether the gate passes OR fails; fail-soft like the funnel.
# --------------------------------------------------------------------------

def _source_type_label_fa(s):
    """Persian type/relation label for the §20.7 report."""
    if s.get("source_type") == "primary":
        if s.get("primary_relation") == "about":
            return "منبع اصلی — کتاب دربارهٔ موضوع"
        return "منبع اصلی — اثر مؤلف/سند بنیادین"
    if s.get("source_type") == "video":
        return "ویدئو (پشتیبان — شمرده نمی‌شود)"
    idx = (s.get("index") or "").strip()
    return f"مقاله/منبع نمایه‌ای ({idx})" if idx else "مقاله/منبع نمایه‌ای"


def build_detailed_source_report(topic, counted,
                                 disagreement_titles=None,
                                 adaptation=None):
    """The §20.7 itemised report text (Persian). Pure builder —
    mocked-testable, no I/O."""
    lines = ["📚 گزارش ریز منابع و محتوای مرتبط (§20.7)",
             f"موضوع: {topic}", ""]
    lines += _adaptation_lines(adaptation)
    if _adaptation_lines(adaptation):
        lines.append("")
    lines += _scope_lines()
    if _scope_lines():
        lines.append("")
    counted = list(counted or [])
    if counted:
        lines.append(f"✅ منابع شمرده‌شده ({len(counted)}):")
        for i, s in enumerate(counted, 1):
            title = (s.get("title") or "(بدون عنوان)").strip()
            lines.append(f"{i}. {title}")
            mapped = (s.get("mapped_title") or "").strip()
            if mapped and mapped != title:
                lines.append(f"   • عنوانِ نقشهٔ اولیه: {mapped}")
            lines.append(f"   • نوع: {_source_type_label_fa(s)}")
            route = (s.get("fulltext_route") or "").strip()
            if route:
                lines.append(f"   • مسیر گرفتن متن کامل: {route}")
            elif (s.get("fulltext_excerpt") or "").strip():
                lines.append("   • مسیر گرفتن متن کامل: گزیدهٔ متن "
                             "(مسیر ثبت نشده)")
            else:
                lines.append("   • متن کامل: گرفته نشد (چکیده مبنا "
                             "بوده است)")
            if s.get("passage_hits"):
                lines.append(f"   • یافته‌های کلیدواژه در متن: "
                             f"{s['passage_hits']}")
            if s.get("source_type") == "primary":
                via = (s.get("catalogue_via") or "").strip()
                if via:
                    lines.append(f"   • تأیید کاتالوگ: {via}")
                else:
                    lines.append("   • تأیید کاتالوگ: در رکورد ثبت "
                                 "نشده")
    else:
        lines.append("✅ منابع شمرده‌شده: هیچ‌کدام — هیچ منبعی از "
                     "دروازهٔ متن‌محور نگذشت.")
    near = [t for t in (disagreement_titles or []) if t]
    lines.append("")
    if near:
        lines.append(f"⚠️ نزدیک‌به‌قبول‌ها — داور اول نگه داشت، با "
                     f"اختلاف دو داور حذف شد ({len(near)}):")
        for t in near:
            lines.append(f"• {t}")
    else:
        lines.append("⚠️ نزدیک‌به‌قبول‌ها (اختلاف دو داور): هیچ‌کدام")
    return "\n".join(lines)


def send_detailed_source_report_dm(token, chat_id, topic, counted,
                                   disagreement_titles=None,
                                   adaptation=None):
    """Fail-soft: print the §20.7 report as log lines and DM it to
    the requester (ARTICLE_CHAT_ID) as its own message(s), split
    with the same 3900-char chunking as the funnel. Never raises,
    never fails the run. Returns True if sent."""
    text = build_detailed_source_report(topic, counted,
                                        disagreement_titles,
                                        adaptation=adaptation)
    for line in text.splitlines():
        print(f"source-report: {line}")
    try:
        chat_id = (chat_id or "").strip()
        if not chat_id or not token:
            print("source-report DM skipped: no ARTICLE_CHAT_ID/token")
            return False
        for chunk in split_text(text, 3900):
            tg_call("sendMessage", token,
                    {"chat_id": chat_id, "text": chunk})
        print(f"source-report DM sent to chat {chat_id}")
        return True
    except Exception as e:
        print(f"WARNING: source-report DM failed (ignored): {e}")
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
        "- CLAIM MAPPING (§20): every main claim must rest on an extracted "
        "full-text PASSAGE of a cited source (the cards mark passage-bearing "
        "sources). A claim supported only by an abstract must be hedged as "
        "such ('as reported in the abstract of [n]') or omitted.\n"
        "- NUMERIC DERIVATION (§20): any numeric or formula-based claim must "
        "be RE-DERIVED step by step in the article text from the source's "
        "stated inputs — show the substitution and the arithmetic — and the "
        "derived value compared with the value the source itself reports.\n"
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
# Multi-robot review layer (§33, Jupiter 2026-11-26: «بساز کلا» — build
# the 6 missing roles of the 14-role roster) — AFTER a complete draft
# exists and BEFORE channel delivery, five component reviewers check
# the article in parallel and a content supervisor checks the
# coherence of the whole:
#   1. citations  — code first (every listed reference IS one of the
#      numbered sources it claims to be; every in-text [n] resolves),
#      then an LLM pass on whether the cited source's passages
#      actually support the claim at the citation.
#   2. numbers    — code first (every numeric claim must appear in,
#      or be arithmetically derivable from, the counted sources'
#      passages — the §20 numeric-derivation law), then an LLM pass
#      on units/context sanity.
#   3. claims     — an LLM pass grounded ONLY in the counted sources'
#      passages (modelled on GRIS's check_claims, for Discovery's
#      free-form article): unsupported central claims are fatal,
#      peripheral ones fixable (remove or label as hypothesis).
#   4. structure  — code (required sections per TYPE_GUIDE, word
#      count in the type's target range, the TITLE LAW) + an LLM
#      pass on section order/logic.
#   5. language   — code (output-language script sanity) + an LLM
#      fluency/terminology pass.
#   6. supervisor — its own coherence LLM pass over the whole article
#      (the same number/term/citation must mean the same thing in
#      the abstract, the body and the conclusion; abstract claims
#      must be a subset of body claims) + DETERMINISTIC aggregation
#      of all six verdicts.
# RED LINES: the publish decision is deterministic code over the
# verdicts — no LLM vote publishes an article; reviewers and the
# supervisor NEVER write article content, they only flag; repairs go
# through the writer path, at most REVIEW_MAX_REPAIR_ROUNDS rounds,
# each round re-running ONLY the failed roles + the supervisor; a
# repair may never invent a source, number or quote; a reviewer whose
# brain calls all fail gives NO verdict and BLOCKS publication (never
# a silent pass) — its deterministic code checks still run and still
# count. The text-first gate, the screening judges and every
# threshold are untouched: this layer only ever sees a draft that
# already passed them. GRIS does not inherit the layer (§33 design
# note): it keeps its deterministic claim checker.
# --------------------------------------------------------------------------

REVIEW_MAX_REPAIR_ROUNDS = 2
REVIEW_ROLES = ("citations", "numbers", "claims", "structure",
                "language")
REVIEW_ROLE_LABELS_FA = {
    "citations": "منابع و ارجاع‌ها",
    "numbers": "اعداد و محاسبات",
    "claims": "ادعا و شاهد",
    "structure": "ساختار و قالب",
    "language": "زبان و ترجمه",
    "supervisor": "ناظر — پیوستگی کل",
}
_REVIEW_GROUNDING_PER_SOURCE = 2200
_REVIEW_GROUNDING_TOTAL = 30000
_REVIEW_ARTICLE_CAP = 24000
_REVIEW_CLAIMS_BATCH = 30


def _review_issue(role, where, problem, severity, fix_stage):
    if severity not in ("fatal", "fixable"):
        severity = "fixable"
    return {"role": role, "where": str(where or "")[:200],
            "problem": str(problem or "")[:400],
            "severity": severity,
            "fix_stage": fix_stage or role}


def _review_verdict(role, issues, available=True):
    return {"role": role, "available": bool(available),
            "ok": bool(available) and not issues,
            "issues": list(issues or [])}


def _review_issue_key(issue):
    return (issue.get("role", "") + "|" + issue.get("where", "")
            + "|" + issue.get("problem", "")).strip().lower()


def _review_split_refs(article):
    """Split the article into (body, references text) at the
    References heading (English or Persian)."""
    lines = (article or "").splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("## References") or s.startswith("## منابع"):
            return "\n".join(lines[:i]), "\n".join(lines[i:])
    return article or "", ""


def _review_grounding_text(counted):
    """Numbered grounding cards for the review LLM passes: ONLY the
    counted sources (video support never grounds a claim), abstract
    + extracted passages, capped per source and in total."""
    out = []
    total = 0
    for i, s in enumerate(counted, 1):
        parts = [f"[{i}] {s.get('title', '')}"]
        abstract = (s.get("abstract") or "").strip()
        if abstract:
            parts.append("Abstract: " + abstract[:800])
        excerpt = (s.get("fulltext_excerpt") or "").strip()
        if excerpt:
            label = "Passages" if s.get("passages") else "Excerpt"
            parts.append(f"{label}: "
                         + excerpt[:_REVIEW_GROUNDING_PER_SOURCE])
        card = "\n".join(parts)
        if out and total + len(card) > _REVIEW_GROUNDING_TOTAL:
            break
        out.append(card)
        total += len(card)
    return "\n\n".join(out)


def _review_article_capped(article, cap=_REVIEW_ARTICLE_CAP):
    a = article or ""
    if len(a) <= cap:
        return a
    half = cap // 2
    return (a[:half] + "\n[... middle elided for review ...]\n"
            + a[-half:])


def _review_llm_issues(role, prompt, api_key, model, fix_stage,
                       allow_fatal=True):
    """One reviewer LLM pass. Returns (issues, available): any brain
    failure or unparseable reply means NO verdict (available=False)
    — the supervisor then blocks publication rather than passing
    the article silently. Items without a stated problem are not
    issues and are dropped."""
    try:
        raw = call_llm(prompt, api_key, model, temperature=0.1,
                       json_mode=True)
        data = _parse_json_value(raw)
        items = data.get("issues") if isinstance(data, dict) else data
        issues = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            problem = str(it.get("problem") or "").strip()
            if not problem:
                continue
            sev = str(it.get("severity") or "fixable").lower()
            if sev not in ("fatal", "fixable") or not allow_fatal:
                sev = "fixable"
            issues.append(_review_issue(
                role, it.get("where", ""), problem, sev, fix_stage))
        return issues, True
    except Exception as e:
        print(f"WARNING: review role {role}: LLM pass failed ({e}) "
              "— no verdict from this role")
        return [], False


# --- Role 1: citations -----------------------------------------------------

def _citations_code_issues(article, sources):
    """Deterministic citation checks (§33 role 1, code first): the
    reference list must be exactly the numbered sources it claims
    to list, and every in-text [n] must resolve to a listed,
    matching source. A listed reference matching NO source, or a
    citation pointing outside the source list, is fatal — that is
    an invented or corrupted reference, the core fabrication the
    whole engine exists to prevent."""
    issues = []
    body, refs = _review_split_refs(article)
    listed = {}
    for line in refs.splitlines():
        m = re.match(r"\s*\[(\d+)\]\s*(.*)", line)
        if m:
            listed[int(m.group(1))] = line
    if not listed:
        issues.append(_review_issue(
            "citations", "References",
            "the article has no parseable numbered reference list",
            "fatal", "citations"))
        return issues
    for n, line in sorted(listed.items()):
        if n < 1 or n > len(sources):
            issues.append(_review_issue(
                "citations", f"reference [{n}]",
                "listed reference has no corresponding source — "
                "invented reference", "fatal", "citations"))
            continue
        s = sources[n - 1]
        title_key = normalise_title(s.get("title", ""))
        line_key = normalise_title(line)
        doi = (s.get("doi") or "").strip().lower()
        if title_key and title_key not in line_key \
                and not (doi and doi in line.lower()):
            issues.append(_review_issue(
                "citations", f"reference [{n}]",
                "listed reference does not match source "
                f"[{n}] ('{(s.get('title') or '')[:80]}') — wrong "
                "or invented reference", "fatal", "citations"))
    cited = {int(x) for x in re.findall(r"\[(\d+)\]", body)}
    for n in sorted(cited):
        if n < 1 or n > len(sources):
            issues.append(_review_issue(
                "citations", f"in-text citation [{n}]",
                "the text cites a source number that does not "
                "exist", "fatal", "citations"))
        elif n not in listed:
            issues.append(_review_issue(
                "citations", f"in-text citation [{n}]",
                "cited in the text but missing from the reference "
                "list", "fixable", "citations"))
    for n in sorted(listed):
        if n not in cited and 1 <= n <= len(sources):
            issues.append(_review_issue(
                "citations", f"reference [{n}]",
                "listed in the references but never cited in the "
                "text", "fixable", "citations"))
    return issues


def review_citations(article, counted, context):
    sources = context.get("sources") or counted
    issues = _citations_code_issues(article, sources)
    body, _ = _review_split_refs(article)
    prompt = (
        "You are the CITATIONS REVIEWER in a scholarly article "
        "review panel. You never rewrite text; you only flag "
        "problems.\n"
        f"Field: {context.get('field', '')}\n"
        f"Topic: {context.get('topic', '')}\n\n"
        "For each in-text citation [n] in the ARTICLE, judge from "
        "the GROUNDING (the only sources that exist) whether the "
        "cited source's abstract/passages actually support the "
        "claim made in the sentence carrying [n]. Flag every "
        "citation whose source does not support its claim: "
        "severity \"fatal\" when the unsupported claim is central "
        "to the article, \"fixable\" otherwise. Do not flag style, "
        "grammar or structure. If everything is supported, return "
        "an empty list.\n"
        'Reply with JSON only: {"issues": [{"where": "[n] + a '
        'short quote of the claim", "problem": "...", '
        '"severity": "fatal|fixable"}]}\n\n'
        "GROUNDING (the counted sources, numbered as cited):\n"
        + _review_grounding_text(counted)
        + "\n\nARTICLE:\n" + _review_article_capped(body))
    llm_issues, available = _review_llm_issues(
        "citations", prompt, context["api_key"], context["model"],
        "citations")
    return _review_verdict("citations", issues + llm_issues,
                           available)


# --- Role 2: numbers -------------------------------------------------------

_REVIEW_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789")
_REVIEW_NUMBER_RE = re.compile(
    r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+")
_REVIEW_CENTRAL_SECTIONS = ("abstract", "چکیده", "conclusion",
                            "نتیجه")


def _review_number_tokens(text):
    """(value, decimals) for every numeric token in text — Latin,
    Persian and Arabic-Indic digits, thousands separators removed.
    `decimals` is the precision the value is stated at, so a source
    value rounded to that precision counts as the same number."""
    t = (text or "").translate(_REVIEW_DIGITS)
    out = []
    for m in _REVIEW_NUMBER_RE.finditer(t):
        tok = m.group(0).replace(",", "")
        try:
            value = float(tok)
        except ValueError:
            continue
        decimals = len(tok.split(".")[1]) if "." in tok else 0
        out.append((value, decimals))
    return out


def _review_article_numbers(article):
    """The article BODY's numeric claims: the reference list, URLs
    and citation markers are excluded, and heading/list numbering
    is not a claim. Returns [(value, decimals, section)] so the
    centrality of an unsupported number can be judged."""
    body, _ = _review_split_refs(article)
    body = re.sub(r"https?://\S+", " ", body)
    body = re.sub(r"\[\d+\]", " ", body)
    out = []
    section = ""
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("#"):
            section = s.lstrip("# ").strip().lower()
            s = re.sub(r"^\d+[\.\)]\s*", "", s.lstrip("# "))
        elif re.match(r"^\d+[\.\)]\s+", s):
            s = re.sub(r"^\d+[\.\)]\s+", "", s)
        for value, decimals in _review_number_tokens(s):
            out.append((value, decimals, section))
    return out


def _review_source_number_pool(counted):
    pool = set()
    for s in counted:
        for text in (s.get("title"), s.get("abstract"),
                     s.get("fulltext_excerpt")):
            for value, _ in _review_number_tokens(text or ""):
                pool.add(value)
        year = s.get("year")
        if isinstance(year, (int, float)):
            pool.add(float(year))
        elif year:
            for value, _ in _review_number_tokens(str(year)):
                pool.add(value)
    return pool


def _review_number_supported(value, decimals, pool):
    def matches(x):
        return abs(round(x, decimals) - value) < 1e-9 \
            or abs(x - value) < 1e-9

    if any(matches(y) for y in pool):
        return True
    # Bounded arithmetic derivation from pairs of source numbers
    # (sums, differences, products, ratios, percentages): the §20
    # numeric-derivation law allows a number the article re-derives
    # from the sources' stated inputs.
    vals = sorted(pool)[:250]
    for i, a in enumerate(vals):
        for b in vals[i:]:
            cands = (a + b, a - b, b - a, a * b,
                     (a / b if b else None),
                     (b / a if a else None),
                     (a / b * 100 if b else None),
                     ((a - b) / b * 100 if b else None))
            for cand in cands:
                if cand is not None and matches(cand):
                    return True
    return False


def _numbers_code_issues(article, counted):
    issues = []
    pool = _review_source_number_pool(counted)
    seen = set()
    for value, decimals, section in _review_article_numbers(article):
        if (value, decimals) in seen:
            continue
        seen.add((value, decimals))
        if _review_number_supported(value, decimals, pool):
            continue
        central = any(k in section
                      for k in _REVIEW_CENTRAL_SECTIONS)
        issues.append(_review_issue(
            "numbers",
            f"number {value:g} (section: {section or 'body'})",
            "numeric claim appears in no counted source passage "
            "and is not derivable from their numbers",
            "fatal" if central else "fixable", "numbers"))
    return issues


def review_numbers(article, counted, context):
    issues = _numbers_code_issues(article, counted)
    body, _ = _review_split_refs(article)
    numeric_lines = []
    for line in body.splitlines():
        s = line.strip()
        if s and any(ch.isdigit()
                     for ch in s.translate(_REVIEW_DIGITS)):
            numeric_lines.append(s[:300])
    prompt = (
        "You are the NUMBERS REVIEWER in a scholarly article "
        "review panel. You never rewrite text; you only flag "
        "problems.\n"
        f"Field: {context.get('field', '')}\n"
        f"Topic: {context.get('topic', '')}\n\n"
        "Below are the article's lines that carry numbers, and the "
        "GROUNDING (the counted sources' abstracts/passages — the "
        "only numbers that exist). Flag ONLY unit/context problems "
        "the deterministic check cannot see: a value quoted with "
        "the wrong unit, a percentage stated against the wrong "
        "base, the same quantity given two different values in "
        "different lines, a value that is impossible for its "
        "stated quantity. Severity \"fatal\" for a wrong central "
        "result, \"fixable\" otherwise. If there is no such "
        "problem, return an empty list.\n"
        'Reply with JSON only: {"issues": [{"where": "short '
        'quote", "problem": "...", "severity": '
        '"fatal|fixable"}]}\n\n'
        "NUMERIC LINES:\n" + "\n".join(numeric_lines[:60])
        + "\n\nGROUNDING:\n" + _review_grounding_text(counted))
    llm_issues, available = _review_llm_issues(
        "numbers", prompt, context["api_key"], context["model"],
        "numbers")
    return _review_verdict("numbers", issues + llm_issues, available)


# --- Role 3: claims --------------------------------------------------------

_REVIEW_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?؟۔])\s+")


def _review_sentences(article):
    """Body prose sentences: headings, the keywords line and the
    reference list are structural, not claims (modelled on GRIS's
    _checkable_lines)."""
    body, _ = _review_split_refs(article)
    out = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.lower().startswith("keywords") \
                or s.startswith("کلیدواژه"):
            continue
        for sent in _REVIEW_SENTENCE_SPLIT_RE.split(line):
            sent = sent.strip()
            if sent:
                out.append(sent)
    return out


def _review_is_factual(sentence):
    """The GRIS factual-claim heuristic: a sentence is a factual
    claim when it carries a number/date, a citation marker, or is a
    full declarative sentence (>= 12 words). Questions and
    explicitly-labelled hypotheses are not factual claims."""
    s = (sentence or "").strip()
    if not s or s.endswith("?") or s.endswith("؟"):
        return False
    if s.lower().startswith("hypothesis") \
            or s.startswith("پیشنهاد/فرضیه"):
        return False
    if re.search(r"\d", s.translate(_REVIEW_DIGITS)):
        return True
    if re.search(r"\[\d+\]", s):
        return True
    return len(s.split()) >= 12


def review_claims(article, counted, context):
    sentences = [s for s in _review_sentences(article)
                 if _review_is_factual(s)]
    if not sentences:
        return _review_verdict("claims", [], True)
    grounding = _review_grounding_text(counted)
    issues = []
    for start in range(0, len(sentences), _REVIEW_CLAIMS_BATCH):
        batch = sentences[start:start + _REVIEW_CLAIMS_BATCH]
        listing = "\n".join(f"SENTENCE {j}: {s[:400]}"
                            for j, s in enumerate(batch, 1))
        prompt = (
            "You are the CLAIMS REVIEWER in a scholarly article "
            "review panel. You never rewrite text; you only judge "
            "support.\n"
            f"Field: {context.get('field', '')}\n"
            f"Topic: {context.get('topic', '')}\n\n"
            "For EACH numbered sentence, decide whether the "
            "GROUNDING (extracted passages/abstracts of the "
            "counted sources — the only evidence that exists) "
            "supports the sentence's factual claim. A claim is "
            "supported when the grounding states it or directly "
            "implies it; a citation marker alone is NOT support. "
            "Also judge centrality: central=true when the article "
            "would lose its main point without this claim. You "
            "MUST return one verdict per sentence — an unjudged "
            "sentence blocks publication.\n"
            'Reply with JSON only: {"verdicts": [{"i": 1, '
            '"supported": true, "central": false}]}\n\n'
            "GROUNDING:\n" + grounding + "\n\n" + listing)
        try:
            raw = call_llm(prompt, context["api_key"],
                           context["model"], temperature=0.1,
                           json_mode=True)
            data = _parse_json_value(raw)
            verdicts = data.get("verdicts") \
                if isinstance(data, dict) else data
            judged = {}
            for v in verdicts or []:
                if isinstance(v, dict) and v.get("i") is not None:
                    judged[int(v["i"])] = v
            if len(judged) < len(batch):
                print("WARNING: review role claims: brain judged "
                      f"{len(judged)}/{len(batch)} sentences — no "
                      "complete verdict from this role")
                return _review_verdict("claims", issues,
                                       available=False)
            for j, sent in enumerate(batch, 1):
                v = judged[j]
                if not v.get("supported"):
                    central = bool(v.get("central"))
                    issues.append(_review_issue(
                        "claims", sent[:120],
                        "factual claim not supported by any "
                        "counted source passage",
                        "fatal" if central else "fixable",
                        "claims"))
        except Exception as e:
            print(f"WARNING: review role claims: LLM pass failed "
                  f"({e}) — no verdict from this role")
            return _review_verdict("claims", issues, available=False)
    return _review_verdict("claims", issues, True)


# --- Role 4: structure -----------------------------------------------------

_REVIEW_SECTION_ALIASES = {
    "abstract": ("abstract", "چکیده"),
    "keywords": ("keywords", "کلیدواژه", "واژه‌های کلیدی"),
    "introduction": ("introduction", "مقدمه"),
    "discussion": ("discussion", "بحث"),
    "conclusion": ("conclusion", "نتیجه‌گیری", "نتیجه گیری"),
    "references": ("references", "منابع"),
}
_REVIEW_ABSTRACT_LABELS = ("background", "objective", "method",
                           "findings", "conclusion")


def _review_headings(article):
    heads = []
    for line in (article or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            heads.append(s.lstrip("#").strip())
    return heads


def _structure_code_issues(article, context):
    issues = []
    atype = context.get("article_type", "isi")
    guide = TYPE_GUIDE.get(atype, "")
    heads = _review_headings(article)
    head_low = [h.lower() for h in heads]

    def has_section(key):
        return any(alias in h for alias
                   in _REVIEW_SECTION_ALIASES[key]
                   for h in head_low)

    # Title law (§15): a title line must exist and be specific.
    title = (context.get("title") or "").strip()
    if not title and heads:
        title = heads[0]
    if not title:
        issues.append(_review_issue(
            "structure", "title", "the article has no title",
            "fixable", "structure"))
    elif not title_is_specific(title, context.get("field", ""),
                               context.get("topic", "")):
        issues.append(_review_issue(
            "structure", "title",
            "title is a broad field/topic label, not a specific "
            "claim (TITLE LAW)", "fixable", "structure"))
    # Required sections for every type.
    for key in ("abstract", "keywords", "introduction",
                "discussion", "conclusion", "references"):
        if not has_section(key):
            issues.append(_review_issue(
                "structure", f"section: {key}",
                f"required section '{key}' is missing",
                "fatal" if key == "references" else "fixable",
                "structure"))
    # Structured abstract labels, when the type's guide asks for
    # one (isi/scopus/pubmed per TYPE_GUIDE).
    if "structured abstract" in guide:
        body_low = (article or "").lower()
        m = re.search(r"abstract", body_low)
        block = body_low[m.start():m.start() + 2500] if m else ""
        present = sum(1 for lab in _REVIEW_ABSTRACT_LABELS
                      if lab in block)
        if present < 4:
            issues.append(_review_issue(
                "structure", "section: abstract",
                "structured abstract lacks its labelled parts "
                "(Background/Objective/Method/Findings/Conclusion)",
                "fixable", "structure"))
    # IMRaD types: the body between Introduction and Discussion
    # must carry at least two thematic sections.
    if "IMRaD" in guide:
        try:
            i0 = next(i for i, h in enumerate(head_low)
                      if any(a in h for a
                             in _REVIEW_SECTION_ALIASES[
                                 "introduction"]))
            i1 = next(i for i, h in enumerate(head_low)
                      if any(a in h for a
                             in _REVIEW_SECTION_ALIASES[
                                 "discussion"]))
            body_heads = i1 - i0 - 1
        except StopIteration:
            body_heads = 0
        if body_heads < 2:
            issues.append(_review_issue(
                "structure", "section: body",
                "IMRaD body has fewer than two thematic sections "
                "between Introduction and Discussion",
                "fixable", "structure"))
    # Word count within the type's target range (§15).
    target = context.get("word_target") \
        or TYPE_WORD_TARGETS.get(atype)
    if target:
        lo, hi = target
        wc = len((article or "").split())
        if wc < lo or wc > hi:
            issues.append(_review_issue(
                "structure", "word count",
                f"article is {wc} words; the target for this type "
                f"is {lo}–{hi}", "fixable", "structure"))
    return issues


def review_structure(article, counted, context):
    issues = _structure_code_issues(article, context)
    heads = _review_headings(article)
    prompt = (
        "You are the STRUCTURE REVIEWER in a scholarly article "
        "review panel. You never rewrite text; you only flag "
        "problems.\n"
        f"Article type standard: "
        f"{TYPE_GUIDE.get(context.get('article_type', ''), '')}\n\n"
        "The article's section outline is below. Flag ONLY "
        "order/logic problems: a section that belongs elsewhere, "
        "results discussed before they are presented, a "
        "conclusion that introduces a new topic, a discussion "
        "that repeats the introduction instead of interpreting "
        "the body. Do not flag missing sections (code checks "
        "those) or style.\n"
        'Reply with JSON only: {"issues": [{"where": "section '
        'name", "problem": "...", "severity": "fixable"}]}\n\n'
        "OUTLINE:\n" + "\n".join(f"- {h}" for h in heads)
        + "\n\nOPENING OF THE ARTICLE:\n"
        + (article or "")[:2500])
    llm_issues, available = _review_llm_issues(
        "structure", prompt, context["api_key"], context["model"],
        "structure", allow_fatal=False)
    return _review_verdict("structure", issues + llm_issues,
                           available)


# --- Role 5: language ------------------------------------------------------

def _language_code_issues(article, context):
    """Output-language script sanity: a Persian/Arabic article is
    mostly Arabic-script; an English one is not. A draft in the
    wrong language entirely is fatal — no repair round fixes a
    whole-article language swap honestly."""
    issues = []
    language = (context.get("language") or "").lower()
    body, _ = _review_split_refs(article)
    letters = [ch for ch in body if ch.isalpha()]
    if not letters:
        return issues
    arab = sum(1 for ch in letters if "\u0600" <= ch <= "\u06FF")
    ratio = arab / len(letters)
    if language in ("fa", "ar") and ratio < 0.35:
        issues.append(_review_issue(
            "language", "whole article",
            f"article should be in {language} but only "
            f"{ratio:.0%} of its letters are Arabic-script — "
            "wrong output language", "fatal", "language"))
    elif language == "en" and ratio > 0.10:
        issues.append(_review_issue(
            "language", "whole article",
            "article should be in English but "
            f"{ratio:.0%} of its letters are Arabic-script — "
            "wrong output language", "fatal", "language"))
    return issues


def review_language(article, counted, context):
    issues = _language_code_issues(article, context)
    lang_name = LANGUAGE_NAMES.get(
        (context.get("language") or "").lower(),
        context.get("language", ""))
    extra = ""
    if (context.get("language") or "").lower() == "fa":
        extra = (" The article is Persian: also flag Arabic "
                 "loanwords used inconsistently, Latin technical "
                 "terms left untranslated where a standard Persian "
                 "term exists and is used elsewhere, and any "
                 "right-to-left ordering breakage around numbers "
                 "or Latin terms.")
    prompt = (
        "You are the LANGUAGE REVIEWER in a scholarly article "
        "review panel. You never rewrite text; you only flag "
        "problems.\n"
        f"The article is written in {lang_name}.\n\n"
        "Flag ONLY: grammar or fluency errors that impede a "
        "scholarly reader, the same concept named by two "
        "different terms in different sections (terminology "
        "inconsistency), and sentences whose meaning is unclear."
        + extra + " Do not flag style preferences.\n"
        'Reply with JSON only: {"issues": [{"where": "short '
        'quote", "problem": "...", "severity": "fixable"}]}\n\n'
        "ARTICLE:\n" + _review_article_capped(article))
    llm_issues, available = _review_llm_issues(
        "language", prompt, context["api_key"], context["model"],
        "language", allow_fatal=False)
    return _review_verdict("language", issues + llm_issues,
                           available)


# --- Role 6: the supervisor ------------------------------------------------

def _review_coherence(article, counted, context):
    """The supervisor's own pass (§33 role 6): coherence of the
    WHOLE — the same number, term or citation must mean the same
    thing in the abstract, the body and the conclusion, and every
    abstract claim must be a claim the body actually makes."""
    prompt = (
        "You are the SUPERVISOR of a scholarly article review "
        "panel, checking the coherence of the WHOLE article. You "
        "never rewrite text; you only flag problems.\n"
        f"Field: {context.get('field', '')}\n"
        f"Topic: {context.get('topic', '')}\n\n"
        "Flag ONLY cross-section incoherence:\n"
        "- the same quantity stated with different values in the "
        "abstract, the body and the conclusion;\n"
        "- the same term used with two different meanings, or a "
        "key term renamed mid-article;\n"
        "- a claim made in the abstract or conclusion that the "
        "body never makes or supports;\n"
        "- a citation [n] attached to different claims in "
        "different sections in a way that contradicts itself.\n"
        "Severity \"fatal\" when the abstract and the conclusion "
        "contradict each other on the article's main result, "
        "\"fixable\" otherwise. If the article is coherent, "
        "return an empty list.\n"
        'Reply with JSON only: {"issues": [{"where": "sections '
        'involved", "problem": "...", "severity": '
        '"fatal|fixable"}]}\n\n'
        "ARTICLE:\n" + _review_article_capped(article))
    issues, available = _review_llm_issues(
        "supervisor", prompt, context["api_key"], context["model"],
        "writing")
    return _review_verdict("supervisor", issues, available)


_REVIEWERS = {"citations": review_citations,
              "numbers": review_numbers,
              "claims": review_claims,
              "structure": review_structure,
              "language": review_language}


def _run_reviewer(role, article, counted, context):
    try:
        return _REVIEWERS[role](article, counted, context)
    except Exception as e:
        print(f"WARNING: review role {role} crashed ({e}) — no "
              "verdict from this role")
        return _review_verdict(role, [], available=False)


def _repair_article(article, issues, counted, context):
    """Writer-path targeted repair (§33): the WRITER receives ONLY
    the flagged issues and returns the full corrected article.
    Reviewers never touch the text. Returns the repaired article,
    or None when the repair failed or its output is unusable (too
    short, references or title lost) — the round is then lost and
    the layer fails closed. A repair may never invent a source, a
    number or a quote: an unfixable claim must be removed or
    explicitly labelled as a hypothesis."""
    listing = "\n".join(
        f"- [{i['role']}/{i['severity']}] at {i['where']}: "
        f"{i['problem']}" for i in issues)
    prompt = (
        "You are the WRITER of the scholarly article below, "
        "repairing the exact problems the review panel flagged.\n"
        "RULES:\n"
        "- Fix ONLY the listed problems; keep every other "
        "sentence, heading and the reference list exactly as "
        "they are.\n"
        "- NEVER invent a source, a number, a quote or a "
        "reference. The GROUNDING lists the only sources and "
        "numbers that exist.\n"
        "- A claim that cannot be supported from the grounding "
        "must be REMOVED, or kept only when explicitly labelled "
        "as a hypothesis.\n"
        "- Keep the '# ' title line and every section heading.\n"
        "- Reply with the COMPLETE corrected article only — no "
        "commentary, no code fences.\n\n"
        "FLAGGED PROBLEMS:\n" + listing + "\n\n"
        "GROUNDING (the counted sources):\n"
        + _review_grounding_text(counted)
        + "\n\nARTICLE:\n" + article)
    try:
        out = call_llm(prompt, context["api_key"], context["model"],
                       temperature=0.2)
    except Exception as e:
        print(f"WARNING: review repair round failed ({e})")
        return None
    out = (out or "").strip()
    out = re.sub(r"^```(?:markdown|md)?\s*", "", out)
    out = re.sub(r"\s*```$", "", out).strip()
    if len(out) < 0.6 * len(article):
        print("WARNING: review repair output unusable (too short "
              "— truncated?); round lost")
        return None
    if not out.startswith("#"):
        print("WARNING: review repair output unusable (title line "
              "lost); round lost")
        return None
    _, refs = _review_split_refs(out)
    if not refs:
        print("WARNING: review repair output unusable (reference "
              "list lost); round lost")
        return None
    return out


def run_review_layer(article, counted, context):
    """The §33 supervisor. Runs the five component reviewers in
    parallel, then its own coherence pass; aggregates the six
    verdicts with DETERMINISTIC code: a role with no verdict
    BLOCKS publication; any fatal issue FAILS at once; fixable
    issues go through targeted writer repairs (max
    REVIEW_MAX_REPAIR_ROUNDS rounds, re-running only the failed
    roles + the supervisor); fixable issues surviving the last
    round FAIL. Returns the result dict consumed by main(), the
    funnel (§16.3) and the §33 review report."""
    current = article
    rounds = 0
    found = {}
    order = []
    last = {}
    roles_filter = None
    while True:
        roles_now = [r for r in REVIEW_ROLES
                     if roles_filter is None or r in roles_filter]
        verdicts = {}
        with ThreadPoolExecutor(
                max_workers=max(1, len(roles_now))) as ex:
            futs = {ex.submit(_run_reviewer, r, current, counted,
                              context): r for r in roles_now}
            for fut in as_completed(futs):
                verdicts[futs[fut]] = fut.result()
        last.update(verdicts)
        last["supervisor"] = _review_coherence(current, counted,
                                               context)
        all_issues = [i for v in last.values()
                      for i in v["issues"]]
        for i in all_issues:
            k = _review_issue_key(i)
            if k not in found:
                found[k] = i
                order.append(k)
        remaining = {_review_issue_key(i) for i in all_issues}
        fixed_keys = [k for k in order if k not in remaining]
        base = {"rounds": rounds,
                "issues_found": len(order),
                "issues_fixed": len(fixed_keys),
                "roles": {r: {"ok": v["ok"],
                              "available": v["available"],
                              "issues": v["issues"]}
                          for r, v in last.items()},
                "issue_records": [
                    dict(found[k], fixed=k in fixed_keys)
                    for k in order],
                "article": current,
                "blocked_roles": []}
        unavailable = [r for r, v in last.items()
                       if not v["available"]]
        if unavailable:
            print("review layer (§33): BLOCKED — no verdict from: "
                  + ", ".join(unavailable))
            return dict(base, ok=False, verdict="blocked",
                        blocked_roles=unavailable)
        fatals = [i for i in all_issues
                  if i["severity"] == "fatal"]
        if fatals:
            print(f"review layer (§33): FAIL — {len(fatals)} "
                  "fatal issue(s)")
            return dict(base, ok=False, verdict="fail")
        if not all_issues:
            print(f"review layer (§33): PASS after {rounds} repair "
                  f"round(s); {len(order)} issue(s) found, "
                  f"{len(fixed_keys)} fixed")
            return dict(base, ok=True, verdict="pass")
        if rounds >= REVIEW_MAX_REPAIR_ROUNDS:
            print("review layer (§33): FAIL — fixable issues "
                  "remain after the maximum repair rounds")
            return dict(base, ok=False, verdict="fail")
        rounds += 1
        print(f"review layer (§33): repair round {rounds} for "
              f"{len(all_issues)} fixable issue(s)")
        repaired = _repair_article(current, all_issues, counted,
                                   context)
        if repaired is None:
            return dict(base, ok=False, verdict="fail",
                        rounds=rounds, repair_failed=True)
        current = repaired
        roles_filter = {i["role"] for i in all_issues
                        if i["role"] in REVIEW_ROLES}


# --- §33 reporting: funnel lines + the itemised review report --------------

def _review_lines(review):
    """§33 lines for the quality funnel (Persian). Empty when the
    run carries no review result."""
    if not review:
        return []
    lines = ["", "بررسی چندرباتهٔ پس از نگارش (§33):"]
    roles = review.get("roles") or {}
    for role in REVIEW_ROLES + ("supervisor",):
        v = roles.get(role)
        label = REVIEW_ROLE_LABELS_FA[role]
        if v is None:
            lines.append(f"• {label}: —")
        elif not v.get("available", True):
            lines.append(f"• {label}: در دسترس نبود (مسدودکننده)")
        elif v.get("issues"):
            lines.append(f"• {label}: {len(v['issues'])} ایراد")
        else:
            lines.append(f"• {label}: قبول")
    lines.append(
        f"ایرادهای یافته‌شده: {review.get('issues_found', 0)}"
        f" — اصلاح‌شده: {review.get('issues_fixed', 0)}"
        f" — دورهای اصلاح: {review.get('rounds', 0)}")
    verdict = review.get("verdict")
    if verdict == "pass":
        lines.append("نتیجهٔ بررسی چندرباته: قبول ✅")
    elif verdict == "blocked":
        names = "، ".join(
            REVIEW_ROLE_LABELS_FA.get(r, r)
            for r in review.get("blocked_roles") or [])
        lines.append("نتیجهٔ بررسی چندرباته: مسدود — داور در "
                     f"دسترس نبود: {names}")
    else:
        lines.append("نتیجهٔ بررسی چندرباته: رد ❌")
    return lines


def build_review_report(topic, review):
    """The §33 itemised review report (Persian), modelled on the
    §20.7 source report: per role, the verdict and every issue
    with its severity and whether a repair round fixed it. Pure
    builder — no I/O."""
    lines = ["🔎 گزارش بررسی چندرباتهٔ مقاله (§33)",
             f"موضوع: {topic}", ""]
    verdict = (review or {}).get("verdict")
    head = {"pass": "قبول ✅", "blocked": "مسدود ⛔",
            "fail": "رد ❌"}.get(verdict, str(verdict))
    lines.append(
        f"نتیجه: {head} — دورهای اصلاح: "
        f"{review.get('rounds', 0)} — ایراد یافته‌شده: "
        f"{review.get('issues_found', 0)} — اصلاح‌شده: "
        f"{review.get('issues_fixed', 0)}")
    if review.get("blocked_roles"):
        names = "، ".join(
            REVIEW_ROLE_LABELS_FA.get(r, r)
            for r in review["blocked_roles"])
        lines.append(f"داورانِ بدون verdict (مسدودکننده): {names}")
    if review.get("repair_failed"):
        lines.append("⚠️ یک دور اصلاح خروجی قابل استفاده نداد و "
                     "از دست رفت.")
    records = review.get("issue_records") or []
    roles = review.get("roles") or {}
    for role in REVIEW_ROLES + ("supervisor",):
        v = roles.get(role) or {}
        label = REVIEW_ROLE_LABELS_FA[role]
        lines.append("")
        if not v.get("available", True):
            lines.append(f"⛔ {label}: در دسترس نبود — verdict "
                         "ندارد")
            continue
        recs = [r for r in records if r.get("role") == role]
        if not recs:
            lines.append(f"✅ {label}: بدون ایراد")
            continue
        lines.append(f"⚠️ {label}: {len(recs)} ایراد")
        for r in recs:
            sev = "کشنده" if r.get("severity") == "fatal" \
                else "قابل اصلاح"
            state = "اصلاح شد" if r.get("fixed") else "باقی ماند"
            lines.append(f"• [{sev}] {r.get('where', '')} — "
                         f"{r.get('problem', '')} ({state})")
    return "\n".join(lines)


def send_review_report_dm(token, chat_id, topic, review):
    """Fail-soft: print the §33 review report as log lines and DM
    it to the requester (ARTICLE_CHAT_ID), split with the same
    3900-char chunking as the funnel. Never raises, never fails
    the run. Returns True if sent."""
    text = build_review_report(topic, review)
    for line in text.splitlines():
        print(f"review-report: {line}")
    try:
        chat_id = (chat_id or "").strip()
        if not chat_id or not token:
            print("review-report DM skipped: no ARTICLE_CHAT_ID/"
                  "token")
            return False
        for chunk in split_text(text, 3900):
            tg_call("sendMessage", token,
                    {"chat_id": chat_id, "text": chunk})
        print(f"review-report DM sent to chat {chat_id}")
        return True
    except Exception as e:
        print(f"WARNING: review-report DM failed (ignored): {e}")
        return False


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
# Stage checkpointing + resume (§20.6)
#
# Jupiter's law (2026-11-25): «مشکل هر جا ظاهر شده ربات بیاد از همون
# جا ادامه بده نه از قبل دوباره صفر» — wherever a problem appears,
# the bot continues FROM THAT POINT, never from zero. When the
# screening/gate stage completes, the run's counted sources + funnel
# are persisted to runs/<signature>/checkpoint.json (the workflow
# commits it back on success AND on failure). A later dispatch with
# the RESUME input set (any non-empty value — the repair agent
# passes the failed stage's label) and matching inputs loads the
# checkpoint and goes straight to the gate check + writing: no
# matrix, no source map, no retrieval, no screening. A missing,
# corrupt or stale checkpoint NEVER crashes a run — it simply runs
# fresh (fail-soft). A successful production marks its checkpoint
# consumed, so a later dispatch never silently reuses stale sources.
# --------------------------------------------------------------------------

RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "runs")
CHECKPOINT_MAX_BYTES = 2_000_000
# The fields of a counted source record that the gate, the writer
# and the §20.7 report read — the checkpoint stores exactly these.
CHECKPOINT_SOURCE_KEYS = (
    "title", "authors", "year", "venue", "doi", "url", "abstract",
    "source_type", "primary_relation", "index", "registry_match",
    "fulltext", "fulltext_route", "fulltext_excerpt", "passages",
    "passage_hits", "mapped_title", "catalogue_via",
    "catalogue_title", "catalogue_id")
# Writer-prompt caps (source_cards_text): the checkpoint never
# stores more of a text than the writer itself would be shown.
CHECKPOINT_ABSTRACT_CHARS = 1500
CHECKPOINT_EXCERPT_LADDER = (FULLTEXT_CARD_CHARS, 6000, 3000, 1500,
                             800, 400)


def run_signature(field, topic, language, category, article_type,
                  mode):
    """Stable identity of one production request: the first 16 hex
    chars of sha256 over the normalised inputs."""
    raw = "|".join(norm(x) for x in
                   (field, topic, language, category, article_type,
                    mode))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def checkpoint_path(signature):
    return os.path.join(RUNS_DIR, signature, "checkpoint.json")


def _checkpoint_source(source, excerpt_cap):
    """A storage copy of one counted source record: only the fields
    the gate/writer/report read, texts capped at the writer's own
    caps. The live record is never mutated."""
    out = {}
    for key in CHECKPOINT_SOURCE_KEYS:
        if key in source:
            out[key] = source[key]
    if out.get("abstract"):
        out["abstract"] = out["abstract"][:CHECKPOINT_ABSTRACT_CHARS]
    if out.get("fulltext_excerpt"):
        out["fulltext_excerpt"] = out["fulltext_excerpt"][:excerpt_cap]
    return out


def save_stage_checkpoint(field, topic, language, category,
                          article_type, mode, queries, smap, matrix,
                          ft_keywords, counted, funnel,
                          disagreement_titles, verified_count):
    """Persist the post-screening checkpoint; returns the signature.
    Raises on I/O or serialisation errors — the CALLER wraps this
    fail-soft (a checkpoint problem must never fail the run)."""
    signature = run_signature(field, topic, language, category,
                              article_type, mode)
    payload = {
        "version": 1,
        "signature": signature,
        "status": "available",
        "stage_reached": "screened",
        "created_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "inputs": {"field": field, "topic": topic,
                   "language": language, "category": category,
                   "article_type": article_type, "mode": mode},
        "queries": list(queries or []),
        "source_map": smap or {},
        "matrix": {"core_keywords": list(
                       (matrix or {}).get("core_keywords") or []),
                   "name_variants": list(
                       (matrix or {}).get("name_variants") or [])},
        "ft_keywords": list(ft_keywords or []),
        "funnel": dict(funnel or {}),
        "disagreement_titles": list(disagreement_titles or []),
        "verified_count": int(verified_count or 0),
    }
    # Size guard: excerpts are stored at the writer's own cap; only
    # if the serialised checkpoint still exceeds the budget are the
    # excerpts trimmed further (and the trim is logged).
    text = None
    for cap in CHECKPOINT_EXCERPT_LADDER:
        payload["counted"] = [_checkpoint_source(s, cap)
                              for s in (counted or [])]
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        if len(text.encode("utf-8")) <= CHECKPOINT_MAX_BYTES:
            if cap < FULLTEXT_CARD_CHARS:
                print(f"checkpoint: excerpts trimmed to {cap} chars "
                      f"to fit the {CHECKPOINT_MAX_BYTES}-byte "
                      "budget")
            break
    path = checkpoint_path(signature)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    print(f"checkpoint saved: {path} (signature {signature}, "
          f"{len(payload['counted'])} counted sources, "
          f"{len(text.encode('utf-8'))} bytes)")
    return signature


def load_stage_checkpoint(field, topic, language, category,
                          article_type, mode):
    """Load the checkpoint for THIS dispatch's inputs, or None.
    Fail-soft by design: a missing file, corrupt JSON, a consumed
    checkpoint or a signature/inputs mismatch all log plainly and
    return None (the caller then runs fresh). Never raises."""
    signature = run_signature(field, topic, language, category,
                              article_type, mode)
    path = checkpoint_path(signature)
    if not os.path.exists(path):
        print(f"resume: no checkpoint at {path} — running fresh")
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        print(f"resume: checkpoint at {path} is unreadable ({e}) — "
              "running fresh")
        return None
    want = {"field": field, "topic": topic, "language": language,
            "category": category, "article_type": article_type,
            "mode": mode}
    got = payload.get("inputs") or {}
    if (payload.get("signature") != signature
            or payload.get("status") != "available"
            or payload.get("stage_reached") != "screened"
            or not isinstance(payload.get("counted"), list)
            or not isinstance(payload.get("funnel"), dict)
            or any(norm(got.get(k)) != norm(v)
                   for k, v in want.items())):
        print(f"resume: checkpoint at {path} does not match this "
              "dispatch (or is already consumed) — running fresh")
        return None
    print(f"resume: checkpoint {signature} loaded "
          f"(created {payload.get('created_at')})")
    return payload


def consume_stage_checkpoint(field, topic, language, category,
                             article_type, mode):
    """Mark this request's checkpoint consumed after a successful
    production, so no later dispatch silently reuses its sources.
    Fail-soft: never raises."""
    try:
        signature = run_signature(field, topic, language, category,
                                  article_type, mode)
        path = checkpoint_path(signature)
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        payload["status"] = "consumed"
        payload["consumed_at"] = datetime.now(
            timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        print(f"checkpoint consumed: {signature}")
    except Exception as e:
        print(f"WARNING: checkpoint consume failed (ignored): {e}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def _quality_pipeline(field, topic, verified_list, api_key, model,
                      keywords=None):
    """One retrieval wave's quality pipeline (§16): enrich abstracts
    by DOI, attach full texts, then the relevance + content screen.
    §20: the full-text stage is the acquisition layer (all source
    types, passage extraction), with the book bridge inside it.
    Returns (counted, stats) — the screen stats plus this wave's
    verified / with-content counts, for the funnel report (§16.3)."""
    enrich_abstracts(verified_list)
    try:
        attach_fulltexts_v2(verified_list, keywords or [])
    except Exception as e:
        print(f"WARNING: full-text layer v2 failed (continuing "
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
              "drop_no_content", "registry_matched", "primary_counted",
              "fulltext_counted"):
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


def _fresh_screening(field, topic, language, category, article_type,
                     mode, api_key, model, tg_token):
    """The fresh production pipeline up to the text-first gate:
    multilingual query matrix -> source map -> wave 1 + the primary
    lane -> the quality pipeline -> wave 2 when the gate is unmet.
    Persists the §20.6 stage checkpoint at the end (fail-soft) and
    returns everything main() needs afterwards: the counted
    sources, the funnel, the disagreement titles, the queries, the
    full-text keywords, the verified count and the gate verdict."""

    # Queries (§20 multilingual matrix; §18 fallback): the topic is
    # expanded into several phrasings PER RELEVANT LANGUAGE around
    # fixed core keywords, and each language's queries run in that
    # language's open sources. If the matrix fails, fall back to the
    # §18 English-first expansion, then to the legacy behaviour.
    matrix = multilingual_query_matrix(field, topic, api_key, model)
    queries = list(matrix["queries"])
    if not queries:
        queries = english_first_queries(field, topic, api_key, model)
    if not queries:
        def add_query(q):
            if q and q not in queries:
                queries.append(q)

        add_query(topic)
        add_query(f"{field} {topic}")
        add_query(translate_query(topic, api_key, model, "Arabic"))
        add_query(translate_query_to_english(topic, api_key, model))
    print(f"Queries: {queries}")

    # Source map (§19/§20 v2): one brain call names the author's own
    # primary works AND other books discussing the topic, plus
    # in-text keywords; titles are catalogue-verified inside
    # build_source_map. Fail-soft — an empty map simply means the
    # primary stage below is skipped.
    smap = build_source_map(field, topic, api_key, model)
    # The full-text layer's in-text search set (§20): source-map
    # keywords + name variants + the matrix's core keywords.
    ft_keywords = []
    for kw in (list(smap.get("keywords") or [])
               + list(smap.get("name_variants") or [])
               + list(matrix["core_keywords"])
               + list(matrix["name_variants"])):
        if kw and kw not in ft_keywords:
            ft_keywords.append(kw)

    progress_start(os.environ.get("ARTICLE_CHAT_ID", ""), tg_token)
    progress_update(15, "🔎 جست‌وجوی منابع")
    rstate = _new_retrieval_state()
    verified = retrieve_wave1(queries, rstate)
    # Primary lane (§19): the topic's own foundational works, field-
    # searched and verified in reserved slots alongside wave 1; they
    # join the same enrich -> bridge -> screen pipeline below.
    primaries = retrieve_primary_sources(
        smap, rstate, extra_keywords=matrix["core_keywords"])
    if primaries:
        verified = verified + primaries
    progress_update(35, "📚 راستی‌آزمایی منابع")

    # Quality laws (§15): only genuinely relevant, content-bearing
    # sources count toward the bar — verification alone is not enough.
    funnel = {"candidates": 0, "verified": 0, "with_content": 0,
              "relevant": 0, "counted": 0, "drop_irrelevant": 0,
              "drop_tradition": 0, "drop_disagreement": 0,
              "drop_no_content": 0, "registry_matched": 0,
              "primary_counted": 0, "fulltext_counted": 0,
              "drop_language_gate": 0}
    counted, sstats = _quality_pipeline(field, topic, verified,
                                        api_key, model,
                                        keywords=ft_keywords)
    _merge_funnel(funnel, sstats)
    disagreement_titles = list(sstats.get("disagreement_titles")
                               or [])
    progress_update(45, "🎯 گزینش منابع مرتبط")

    gate = text_first_gate(counted)
    if not gate["ok"]:
        # Tiered wave 2 (§16.2): ONE broader retrieval wave — book
        # sources first — through the SAME verify -> enrich -> bridge
        # -> screen pipeline, sharing wave 1's dedupe/bench machinery,
        # before the run is allowed to fail. §20: wave 2 also runs
        # when wave 1 passed the old count bar but not the TEXT-FIRST
        # gate (too few passage-bearing sources / no primary text).
        before_candidates = rstate["total_candidates"]
        w2_queries = list(queries) + [q for q in _wave2_queries(
            field, topic, api_key, model) if q not in queries]
        print(f"wave 2: text-first gate not met after wave 1 "
              f"(counted={gate['counted']}, full-text passages="
              f"{gate['fulltext_passages']}, primary passages="
              f"{gate['primary_passages']}); broader retrieval, "
              f"queries: {w2_queries}")
        book_first = [search_internet_archive, search_annas_archive]
        order2 = book_first + [s for s in SEARCHERS
                               if s not in book_first]
        verified2 = collect_verified(w2_queries, state=rstate,
                                     searcher_order=order2)
        primaries2 = retrieve_primary_sources(
            smap, rstate, extra_keywords=matrix["core_keywords"],
            max_new=PRIMARY_MAX_VERIFIED - len(primaries))
        if primaries2:
            verified2 = verified2 + primaries2
        counted2, sstats2 = _quality_pipeline(field, topic, verified2,
                                              api_key, model,
                                              keywords=ft_keywords)
        _merge_funnel(funnel, sstats2)
        disagreement_titles += list(
            sstats2.get("disagreement_titles") or [])
        verified = verified + verified2
        counted = counted + counted2
        print(f"wave 2: "
              f"+{rstate['total_candidates'] - before_candidates} "
              f"candidates, +{len(verified2)} verified, "
              f"+{len(counted2)} counted")
        progress_update(48, "🎯 موج دوم گزینش منابع")
        gate = text_first_gate(counted)
    funnel["candidates"] = rstate["total_candidates"]
    funnel["drop_language_gate"] = rstate.get("lang_gate_dropped", 0)

    # §20.6: persist the stage checkpoint (counted sources + funnel)
    # so a later RESUME dispatch continues from this point instead
    # of starting over. Fail-soft — a checkpoint problem never
    # fails the run.
    try:
        save_stage_checkpoint(field, topic, language, category,
                              article_type, mode, queries, smap,
                              matrix, ft_keywords, counted, funnel,
                              disagreement_titles, len(verified))
    except Exception as exc:
        print(f"WARNING: stage checkpoint save failed (ignored): "
              f"{exc}")
    return {"counted": counted, "funnel": funnel,
            "disagreement_titles": disagreement_titles,
            "queries": queries, "ft_keywords": ft_keywords,
            "source_map": smap,
            "verified_count": len(verified), "gate": gate}


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
    model = (os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash").strip()
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

    # §20.6 resume: a dispatch with RESUME set (any non-empty
    # value; the repair agent passes the failed stage's label) and
    # a matching stage checkpoint skips the whole retrieval /
    # screening pipeline and continues from the gate check with the
    # checkpointed counted sources. No valid checkpoint -> fresh.
    original_topic = topic
    adaptation = None
    resume_req = os.environ.get("RESUME", "").strip()
    ckpt = None
    if resume_req:
        ckpt = load_stage_checkpoint(field, topic, language, category,
                                     article_type, mode)
    if ckpt:
        counted = ckpt["counted"]
        funnel = ckpt["funnel"]
        disagreement_titles = list(ckpt.get("disagreement_titles")
                                   or [])
        queries = list(ckpt.get("queries") or [])
        ft_keywords = list(ckpt.get("ft_keywords") or [])
        source_map = ckpt.get("source_map") or {}
        verified_count = ckpt.get("verified_count",
                                  funnel.get("verified", 0))
        gate = text_first_gate(counted)
        progress_start(os.environ.get("ARTICLE_CHAT_ID", ""),
                       tg_token)
        progress_update(55, "✍️ نگارش مقاله")
        print(f"resumed from checkpoint: stage screened, "
              f"{len(counted)} counted sources")
    else:
        fresh = _fresh_screening(field, topic, language, category,
                                 article_type, mode, api_key, model,
                                 tg_token)
        counted = fresh["counted"]
        funnel = fresh["funnel"]
        disagreement_titles = fresh["disagreement_titles"]
        queries = fresh["queries"]
        ft_keywords = fresh["ft_keywords"]
        source_map = fresh.get("source_map") or {}
        verified_count = fresh["verified_count"]
        gate = fresh["gate"]

    if not gate["ok"]:
        # §20.10 topic adaptation: before failing closed, propose
        # adapted topics grounded in the evidence in hand and
        # re-screen each through the SAME pipeline + gate (max
        # MAX_TOPIC_ADAPTATIONS attempts, thresholds unchanged).
        adaptation, adapted_fresh = attempt_topic_adaptation(
            field, original_topic, language, category, article_type,
            mode, api_key, model, tg_token, counted,
            disagreement_titles, source_map)
        if adapted_fresh is not None:
            counted = adapted_fresh["counted"]
            funnel = adapted_fresh["funnel"]
            disagreement_titles = adapted_fresh[
                "disagreement_titles"]
            queries = adapted_fresh["queries"]
            ft_keywords = adapted_fresh["ft_keywords"]
            verified_count = adapted_fresh["verified_count"]
            gate = adapted_fresh["gate"]
            topic = adaptation["adapted_topic"]
            # §20.6 checkpoint choice (§20.10): the adapted run's
            # _fresh_screening already saved a checkpoint keyed by
            # the ADAPTED topic's signature; the ORIGINAL topic's
            # checkpoint is marked consumed below (at the success
            # consume step, alongside the adapted one) so a later
            # original-topic dispatch can never wrongly resume the
            # failed-gate screening as if it belonged to a passing
            # production.

    if not gate["ok"]:
        if gate["counted"] < MIN_SOURCES:
            stage = "گزینش منابع مرتبط و محتوادار"
            reason = (f"Only {gate['counted']} relevant, "
                      f"content-bearing sources for '{topic}' out of "
                      f"{verified_count} verified (minimum "
                      f"{MIN_SOURCES}). Refusing to write an article "
                      "on irrelevant or title-only material.")
        elif gate["fulltext_passages"] < MIN_FULLTEXT_SOURCES:
            stage = "دروازهٔ متن‌محور: متن کامل کافی نیست"
            reason = (f"Text-first gate (§20): {gate['counted']} "
                      f"relevant sources but only "
                      f"{gate['fulltext_passages']} carry extracted "
                      f"full-text passages (minimum "
                      f"{MIN_FULLTEXT_SOURCES}). Refusing to write an "
                      "article from abstracts alone.")
        else:
            stage = "دروازهٔ متن‌محور: منبع اصلیِ متن‌دار نیست"
            reason = (f"Text-first gate (§20): no primary source "
                      f"(the author's own book, or a book discussing "
                      f"the topic) with passages extracted from its "
                      f"full text. Refusing to publish without the "
                      "primary text.")
        # §20.7: the itemised source report goes out at the gate
        # point whether the gate passes or fails — here it failed.
        send_detailed_source_report_dm(
            tg_token, os.environ.get("ARTICLE_CHAT_ID", ""), topic,
            counted, disagreement_titles, adaptation=adaptation)
        send_funnel_dm(tg_token, os.environ.get("ARTICLE_CHAT_ID", ""),
                       topic, funnel, False, stage=stage,
                       adaptation=adaptation)
        raise Fail(reason + " Try a broader or different topic.")

    # Writer input (§20.6): passage-bearing sources first; YouTube
    # lecture transcripts join as SUPPORT only (never counted).
    counted.sort(key=lambda s: 0 if s.get("passages")
                 else (1 if s.get("fulltext") else 2))
    en_queries = [q for q in queries]
    videos = []
    try:
        videos = fetch_youtube_support(en_queries, ft_keywords)
    except Exception as e:
        print(f"youtube support (§20): failed ({e}) — continuing "
              "without")
    write_sources = counted + videos

    _t0 = time.monotonic()
    try:
        title, article, meta = write_article(
            field, topic, language, write_sources, api_key, model,
            guide=TYPE_GUIDE[article_type],
            word_target=TYPE_WORD_TARGETS.get(article_type))
    except Fail:
        send_funnel_dm(tg_token, os.environ.get("ARTICLE_CHAT_ID", ""),
                       topic, funnel, False, stage="نگارش مقاله",
                       adaptation=adaptation)
        raise
    print(f"timing: writing {time.monotonic() - _t0:.1f}s")

    # §20.10: an adapted production carries the adaptation note
    # near the top of the article itself, naming both topics.
    if adaptation and adaptation.get("adapted_topic"):
        note = topic_adaptation_note(original_topic,
                                     adaptation["adapted_topic"],
                                     adaptation.get("reason", ""),
                                     language)
        if article.startswith("# "):
            first_nl = article.find("\n")
            article = (article[:first_nl + 1] + "\n" + note + "\n"
                       + article[first_nl + 1:])
        else:
            article = note + "\n\n" + article

    # §43: a new-only production carries the scope note near the
    # top of the article itself, naming the restricted pool.
    if search_scope() == "new":
        note = search_scope_note(language)
        if article.startswith("# "):
            first_nl = article.find("\n")
            article = (article[:first_nl + 1] + "\n" + note + "\n"
                       + article[first_nl + 1:])
        else:
            article = note + "\n\n" + article

    # §33: the multi-robot review layer — five component reviewers
    # + the coherence supervisor, AFTER the complete draft and
    # BEFORE channel delivery. The publish decision is
    # deterministic code over the verdicts; reviewers never write;
    # a reviewer with no verdict BLOCKS the article. On failure the
    # itemised review report + the funnel go to the requester DM
    # (fail-soft) and the run fails closed.
    print("--- §33 review layer: starting ---")
    review = run_review_layer(
        article, counted,
        {"field": field, "topic": topic, "language": language,
         "article_type": article_type, "title": title,
         "sources": write_sources,
         "word_target": TYPE_WORD_TARGETS.get(article_type),
         "api_key": api_key, "model": model})
    if not review["ok"]:
        print(f"review layer (§33): STOP — verdict "
              f"{review['verdict']}; nothing is published")
        send_review_report_dm(
            tg_token, os.environ.get("ARTICLE_CHAT_ID", ""), topic,
            review)
        send_funnel_dm(tg_token, os.environ.get("ARTICLE_CHAT_ID", ""),
                       topic, funnel, False,
                       stage="بررسی چندرباتهٔ پس از نگارش (§33)",
                       adaptation=adaptation, review=review)
        raise Fail("review layer blocked publication "
                   f"({review['verdict']}): "
                   f"{review['issues_found']} issue(s) found, "
                   f"{review['issues_fixed']} fixed, "
                   f"{review['rounds']} repair round(s)")
    article = review["article"]
    print("review layer (§33): PASS — article cleared for delivery")

    header = (f"📚 Scientific Discovery Agent — new article\n"
              f"Field: {field}\nTopic: {topic}\nLanguage: {language}\n"
              f"Mode: {mode}\nVerified sources: {verified_count} "
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
        "original_topic": original_topic,
        "adapted_topic": (adaptation or {}).get("adapted_topic"),
        "title": title,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": len(counted),
        "message_ids": message_ids,
        "review": {"rounds": review.get("rounds", 0),
                   "issues_found": review.get("issues_found", 0),
                   "issues_fixed": review.get("issues_fixed", 0),
                   "verdict": review.get("verdict")},
    })
    save_state(state)
    # §20.6: the production succeeded — consume the stage
    # checkpoint so a later dispatch never reuses stale sources.
    consume_stage_checkpoint(field, topic, language, category,
                             article_type, mode)
    # §20.10 checkpoint choice: on an adapted success ALSO consume
    # the ORIGINAL topic's checkpoint (see the adaptation block
    # above) — it holds the failed-gate screening and must never be
    # resumed by a later original-topic dispatch.
    if adaptation and adaptation.get("adapted_topic"):
        consume_stage_checkpoint(field, original_topic, language,
                                 category, article_type, mode)
    progress_update(100, "انتشار در کانال")
    # Suggestions-visible law (§15): the requester SEES the bot's
    # title candidates (used one marked) + next-topic suggestions.
    send_suggestions_dm(tg_token, os.environ.get("ARTICLE_CHAT_ID", ""),
                        title, meta.get("title_candidates", []),
                        meta.get("next_topics", []))
    # §20.7: the gate passed — the itemised source report goes to
    # the requester alongside the funnel.
    send_detailed_source_report_dm(
        tg_token, os.environ.get("ARTICLE_CHAT_ID", ""), topic,
        counted, disagreement_titles, adaptation=adaptation)
    # Funnel report (§16.3): every run ends with the quality funnel
    # in the requester's DM (and in the log).
    send_funnel_dm(tg_token, os.environ.get("ARTICLE_CHAT_ID", ""),
                   topic, funnel, True,
                   word_count=meta.get("word_count"),
                   word_target=TYPE_WORD_TARGETS.get(article_type),
                   adaptation=adaptation, review=review)
    # §33: the itemised review report goes to the requester DM
    # alongside the funnel (fail-soft).
    send_review_report_dm(tg_token,
                          os.environ.get("ARTICLE_CHAT_ID", ""),
                          topic, review)
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
