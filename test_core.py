#!/usr/bin/env python3
"""Offline tests for §42: CORE (core.ac.uk) wired live into the
Discovery engine via its API v3 — the works search
(POST /v3/search/works) for full-text-bearing candidates and
the Discovery operation (POST /v3/discover) resolving a free
full-text link for DOI-only hits. No network: _core_fetch is
monkeypatched with fixtures shaped on the OFFICIAL OpenAPI
spec (https://api.core.ac.uk/swagger/v3.json, verified
2026-11-26) — the spec prints snake_case field names, the live
API answers camelCase, so both spellings are exercised. The
API key is a dummy test string in the environment; the real
key exists only in Jupiter's mailbox / repo secrets.
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(HERE, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ba = _load("build_article", "build_article.py")
pt = _load("propose_topics", "propose_topics.py")

passed = failed = 0


def check(label, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok   -", label)
    else:
        failed += 1
        print("FAIL -", label)


# --- fixtures: SearchResponse shapes from the official spec --
# camelCase (the live API's spelling): one hit with downloadUrl,
# one with only sourceFulltextUrls, one DOI-only (discovery
# fodder), one metadata-only (must be dropped), one title-less.
CAMEL = {
    "totalHits": 5, "limit": 25, "offset": 0,
    "results": [
        {"id": "COREID1", "doi": "10.1234/earth.radius.2020",
         "title": "Biruni's Measurement of the Earth",
         "authors": [{"name": "A. Researcher"},
                     {"name": "B. Scholar"}],
         "abstract": "A study of the dip-of-the-horizon method.",
         "downloadUrl": "https://core.ac.uk/download/pdf/111.pdf",
         "sourceFulltextUrls": [],
         "yearPublished": 2020,
         "language": {"code": "en", "name": "English"},
         "journals": [{"title": "Journal of Earth Studies"}],
         "publisher": "Example University",
         "links": [{"type": "display",
                    "url": "https://example.org/article/1"}]},
        {"id": "COREID2", "doi": None,
         "title": "Medieval Astronomy in Persian Sources",
         "authors": [{"name": "C. Author"}],
         "abstract": "",
         "downloadUrl": None,
         "sourceFulltextUrls": ["https://repo.example.ac.ir/f.pdf",
                                "https://mirror.example.org/f.pdf"],
         "yearPublished": 2019,
         "language": {"code": "fa", "name": "Persian"},
         "journals": [], "publisher": "Repo Publisher",
         "links": []},
        {"id": "COREID3", "doi": "10.5678/doi.only.hit",
         "title": "A DOI-Only Record Needing Discovery",
         "authors": [{"name": "D. Writer"}],
         "abstract": "No download URL in the search hit.",
         "downloadUrl": None, "sourceFulltextUrls": [],
         "yearPublished": 2021,
         "language": {"code": "en", "name": "English"},
         "journals": [{"title": "Annals of Testing"}],
         "publisher": "", "links": []},
        {"id": "COREID4", "doi": None,
         "title": "Metadata Only, No Full Text Anywhere",
         "authors": [], "abstract": "Must be dropped.",
         "downloadUrl": None, "sourceFulltextUrls": [],
         "yearPublished": 2018, "language": None,
         "journals": [], "publisher": "", "links": []},
        {"id": "COREID5", "doi": "10.9999/no.title",
         "title": "",
         "authors": [], "abstract": "",
         "downloadUrl": "https://core.ac.uk/download/pdf/x.pdf",
         "sourceFulltextUrls": [], "yearPublished": 2022,
         "language": None, "journals": [], "publisher": "",
         "links": []},
    ],
}

# snake_case (the spec's printed spelling), authors as strings.
SNAKE = {
    "total_hits": 1, "limit": 25, "offset": 0,
    "results": [
        {"id": "COREID9", "doi": "10.4321/snake.case",
         "title": "Snake Case Spec Record",
         "authors": ["E. Spec", "F. Author"],
         "abstract": "Spec-spelling fixture.",
         "download_url": "https://core.ac.uk/download/pdf/9.pdf",
         "source_fulltext_urls": [],
         "year_published": 2017,
         "language": {"code": "de", "name": "German"},
         "journals": [{"title": "Zeitschrift für Tests"}],
         "publisher": "", "links": []},
    ],
}

DISCOVERY_OK = {"fullTextLink": "https://free.example.org/full.pdf",
                "source": "repository"}


class FetchLog:
    """Routes _core_fetch by URL; records every call."""

    def __init__(self, search_body, discover_body=DISCOVERY_OK,
                 search_status=200):
        self.search_body = search_body
        self.discover_body = discover_body
        self.search_status = search_status
        self.calls = []

    def __call__(self, url, data=None):
        self.calls.append((url, data))
        if url.endswith("/v3/discover"):
            return 200, json.dumps(self.discover_body)
        return self.search_status, json.dumps(self.search_body)

    def searches(self):
        return [c for c in self.calls if "/search/" in c[0]]

    def discovers(self):
        return [c for c in self.calls if c[0].endswith("/discover")]


def with_key(fn):
    old = os.environ.get("CORE_API_KEY")
    os.environ["CORE_API_KEY"] = "test-key-not-real"
    try:
        return fn()
    finally:
        if old is None:
            os.environ.pop("CORE_API_KEY", None)
        else:
            os.environ["CORE_API_KEY"] = old


# --- 1. dormant without a key: no request, no candidates -----
os.environ.pop("CORE_API_KEY", None)
log = FetchLog(CAMEL)
ba._core_fetch = log
check("no CORE_API_KEY -> [] and zero HTTP calls",
      ba.search_core("Biruni earth radius") == []
      and log.calls == [])

# --- 2. camelCase parse: full-text preference + drops --------
def t_camel():
    log = FetchLog(CAMEL)
    ba._core_fetch = log
    out = ba.search_core("Biruni earth radius")
    check("camelCase: 3 candidates (2 direct + 1 discovered)",
          len(out) == 3)
    check("camelCase: downloadUrl preferred as oa_pdf_url",
          out[0]["oa_pdf_url"]
          == "https://core.ac.uk/download/pdf/111.pdf"
          and out[0]["fulltext_url"] == out[0]["oa_pdf_url"])
    check("camelCase: title/authors/year/venue/doi carried",
          out[0]["title"] == "Biruni's Measurement of the Earth"
          and out[0]["authors"] == ["A. Researcher", "B. Scholar"]
          and out[0]["year"] == 2020
          and out[0]["venue"] == "Journal of Earth Studies"
          and out[0]["doi"] == "10.1234/earth.radius.2020")
    check("camelCase: landing url is the DOI url",
          out[0]["url"] == "https://doi.org/10.1234/earth.radius.2020")
    check("camelCase: index tag + language carried",
          out[0]["index"] == "core:api"
          and out[0]["language"] == "en")
    check("camelCase: sourceFulltextUrls first entry used",
          out[1]["oa_pdf_url"] == "https://repo.example.ac.ir/f.pdf"
          and out[1]["language"] == "fa")
    check("camelCase: DOI-only hit resolved via Discovery",
          out[2]["oa_pdf_url"] == "https://free.example.org/full.pdf"
          and out[2]["doi"] == "10.5678/doi.only.hit")
    check("camelCase: exactly one discovery call, with the DOI",
          len(log.discovers()) == 1
          and json.loads(log.discovers()[0][1])["doi"]
          == "10.5678/doi.only.hit")
    check("camelCase: plain query sent (no _exists_ clause — "
          "the live API rejects it with HTTP 500)",
          json.loads(log.searches()[0][1])["q"]
          == "Biruni earth radius"
          and len(log.searches()) == 1)
    titles = [c["title"] for c in out]
    check("camelCase: metadata-only + title-less hits dropped",
          "Metadata Only, No Full Text Anywhere" not in titles
          and len(titles) == 3)


with_key(t_camel)

# --- 3. snake_case (spec spelling) parses identically --------
def t_snake():
    log = FetchLog(SNAKE)
    ba._core_fetch = log
    out = ba.search_core("spec record")
    check("snake_case: 1 candidate with download_url honoured",
          len(out) == 1
          and out[0]["oa_pdf_url"]
          == "https://core.ac.uk/download/pdf/9.pdf")
    check("snake_case: string authors + year + language",
          out[0]["authors"] == ["E. Spec", "F. Author"]
          and out[0]["year"] == 2017
          and out[0]["language"] == "de")


with_key(t_snake)

# --- 4. discovery failure leaves the DOI-only hit out --------
def t_discover_fail():
    log = FetchLog(CAMEL, discover_body={"fullTextLink": None})
    ba._core_fetch = log
    out = ba.search_core("Biruni earth radius")
    check("discovery without a link -> hit stays out (2 left)",
          len(out) == 2
          and all(c.get("oa_pdf_url") for c in out))


with_key(t_discover_fail)

# --- 5. discovery budget: at most 4 resolves per call --------
def t_budget():
    many = {"totalHits": 9, "results": [
        {"doi": f"10.1000/doi.only.{i}", "title": f"DOI Only {i}",
         "authors": [], "abstract": "", "downloadUrl": None,
         "sourceFulltextUrls": [], "yearPublished": 2020,
         "language": None, "journals": [], "publisher": "",
         "links": []} for i in range(9)]}
    log = FetchLog(many)
    ba._core_fetch = log
    out = ba.search_core("doi only")
    check("discovery budget caps resolves at 4",
          len(log.discovers()) == ba.CORE_DISCOVERY_BUDGET == 4
          and len(out) == 4)


with_key(t_budget)

# --- 6. cap: at most CORE_CAP candidates per query -----------
def t_cap():
    many = {"totalHits": 12, "results": [
        {"doi": f"10.2000/full.text.{i}", "title": f"Full Text {i}",
         "authors": [], "abstract": "",
         "downloadUrl": f"https://core.ac.uk/download/pdf/{i}.pdf",
         "sourceFulltextUrls": [], "yearPublished": 2020,
         "language": None, "journals": [], "publisher": "",
         "links": []} for i in range(12)]}
    log = FetchLog(many)
    ba._core_fetch = log
    out = ba.search_core("full text")
    check("cap: 12 full-text hits -> CORE_CAP (8) candidates",
          len(out) == 8 == ba.CORE_CAP)


with_key(t_cap)

# --- 7. fail-soft: HTTP errors, garbage, transport failure ---
def t_failsoft():
    ba._core_fetch = lambda url, data=None: (500, "server error")
    check("HTTP 500 -> []", ba.search_core("x") == [])
    ba._core_fetch = lambda url, data=None: (200, "not json {")
    check("garbage JSON -> []", ba.search_core("x") == [])
    ba._core_fetch = lambda url, data=None: (None, "")
    check("transport failure -> []", ba.search_core("x") == [])
    ba._core_fetch = lambda url, data=None: (401, "unauthorised")
    check("HTTP 401 (bad key) -> []", ba.search_core("x") == [])
    check("empty query -> []", ba.search_core("   ") == [])


with_key(t_failsoft)

# --- 8. one plain search call per query (regression: the ---
# --- old _exists_-biased first call failed terminally) -------
def t_retry():
    state = {"n": 0}

    def fetch(url, data=None):
        if url.endswith("/discover"):
            return 200, json.dumps(DISCOVERY_OK)
        state["n"] += 1
        return 200, json.dumps({"totalHits": 0, "results": []})

    ba._core_fetch = fetch
    out = ba.search_core("spec record")
    check("empty plain search -> [] after exactly ONE search "
          "call (no biased first call, no phantom retry)",
          state["n"] == 1 and out == [])
    # and a failed (non-200) search likewise ends the call —
    # visibly, via the WARNING _core_search_once prints.
    ba._core_fetch = lambda url, data=None: (500, "boom")
    check("HTTP 500 on the plain search -> []",
          ba.search_core("spec record") == [])


with_key(t_retry)

# --- 9. parser unit behaviour --------------------------------
cands, pending = ba._parse_core_results(CAMEL)
check("parser: 2 direct + 1 pending from the camelCase fixture",
      len(cands) == 2 and len(pending) == 1
      and pending[0]["doi"] == "10.5678/doi.only.hit")
check("parser: non-dict payload -> empty",
      ba._parse_core_results([1, 2]) == ([], []))
check("discover: empty identifiers -> '' without a call",
      ba._core_discover_fulltext(None, None, None) == "")

# --- 10. registry: CORE entry + pool counts, both scripts ----
reg = json.load(open(os.path.join(HERE, "core_registry.json")))
check("core_registry.json: one platform entry for core.ac.uk",
      len(reg) == 1 and reg[0]["url"] == "https://core.ac.uk"
      and reg[0]["type"] == "platform" and reg[0]["verified"] is True)
check("core_registry.json: api field documents search + discovery",
      "/v3/search/works" in reg[0]["api"]
      and "/v3/discover" in reg[0]["api"]
      and "CORE_API_KEY" in reg[0]["api"])
for tag, m in (("build_article", ba), ("propose_topics", pt)):
    check(f"{tag}: core_registry.json in REGISTRY_FILES",
          "core_registry.json" in m.REGISTRY_FILES)
    dom, _urls, st = m.load_source_registry()
    check(f"{tag}: pool is 21 files / 2188 loaded / 2135 unique",
          st["files_ok"] == 21 and st["loaded"] == 2188
          and st["unique_urls"] == 2135)
    check(f"{tag}: core.ac.uk present in the domain index",
          "core.ac.uk" in dom)

# --- 11. registration in the retrieval waves ------------------
check("search_core registered in SEARCHERS + WAVE1",
      ba.search_core in ba.SEARCHERS
      and ba.search_core in ba.WAVE1_SEARCHERS)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
