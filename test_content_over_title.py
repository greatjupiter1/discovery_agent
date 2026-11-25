"""Mocked offline checks for §20.4 (CONTENT-OVER-TITLE law) and
§20.7 (detailed source report).

§20.4: (i) a mapped title garbled/different from the catalogue title
is confirmed when a REAL IA copy with a fetchable djvu exists, and
the work adopts the copy's REAL title (mapped title kept); (ii) no
real copy findable anywhere -> still dropped unconfirmed; (iii) an
Arabic book carrying extracted passages with ZERO title/abstract
topic overlap reaches the LLM judges via the shared screen path;
(iii-b) the GRIS-path shape — a primary whose fetch produced only a
HEAD excerpt (passages flag never set, the GRIS live-run failure) —
also reaches the judges now; (iv) a title-only source with zero
overlap and no content is still deterministic-dropped; (v) GRIS
parity: gris_sample drives the SAME shared screen (ba._quality_pipeline
-> ba.screen_sources), exercised here through ba._quality_pipeline
exactly as gris_sample.stage_retrieval calls it.

§20.7: the itemised report builder renders a counted primary with
catalogue-via + keyword hits + mapped title, lists a disagreement
near-miss, and renders an honest empty report for no counted sources.

No network: http_json, the Google Books searcher, the djvu fetcher
and the LLM screen are fakes.
"""
import sys

import build_article as ba
import gris_sample as gs

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


TOPIC = ("اندازه‌گیری شعاع زمین به روش ابوریحان بیرونی در نندنه: "
         "بازسازی محاسبهٔ او در تحدید نهایات الأماکن و سنجش خطای آن "
         "در برابر مقدار امروزی")
FIELD = "نجوم و ریاضیات در آثار ابوریحان بیرونی"

# ---------------------------------------------------------------------------
# §20.4 (i)/(ii): existence-based catalogue confirmation
# ---------------------------------------------------------------------------
# The brain garbled the mapped title; the catalogue holds the work
# under a completely different title form, by the same author, and
# the IA item's djvu text FETCHES. §20.3's overlap thresholds would
# reject this (shared tokens: only 'qanun'); §20.4 must confirm it.
GARBLED_WORK = {"author": "Abu Rayhan al-Biruni",
                "title": "Biruni's Lost Treatise on Qanun Secrets XYZ"}
REAL_COPY_DOC = {
    "identifier": "al-qanun-al-masudi-biruni-muhammad-ibn-ahmad",
    "title": "Al Qanūn Al Masʿūdī القانون المسعودي "
             "Bīrūnī, Muḥammad Ibn Aḥmad بيروني، محمد بن أحمد",
}
STATE = {"ia_docs": [], "ol_docs": [], "djvu": None}


def fake_http_json(url, **kw):
    if "archive.org" in url:
        return {"response": {"docs": STATE["ia_docs"]}}
    if "openlibrary.org" in url:
        return {"docs": STATE["ol_docs"]}
    return {}


ba.http_json = fake_http_json
ba.search_google_books_primary = lambda work: []
ba._ia_djvu_text = lambda ia_id, cap=0: STATE["djvu"]

STATE["ia_docs"] = [REAL_COPY_DOC]
STATE["ol_docs"] = []
STATE["djvu"] = "x" * 500  # fetchable full text
work = dict(GARBLED_WORK)
ok, matched = ba.verify_work_in_catalogue(work)
check("(i-a) garbled mapped title + real retrievable IA copy -> "
      "confirmed", ok)
check("(i-b) confirmed under the copy's REAL catalogue title",
      matched == REAL_COPY_DOC["title"])
check("(i-c) catalogue_via records the retrievable-copy route",
      work.get("catalogue_via") == "archive.org (retrievable copy)")
check("(i-d) catalogue_id records the IA identifier",
      work.get("catalogue_id") == REAL_COPY_DOC["identifier"])

# build_source_map adoption: the work takes the real title, the
# mapped title is preserved.
w2 = {"title": GARBLED_WORK["title"]}
matched2 = REAL_COPY_DOC["title"]
if matched2 != w2["title"]:
    w2["mapped_title"] = w2["title"]
    w2["title"] = matched2
check("(i-e) source-map adoption: real title in, mapped_title kept",
      w2["title"] == REAL_COPY_DOC["title"]
      and w2["mapped_title"] == GARBLED_WORK["title"])

# (ii) no real copy anywhere: IA answers with an unrelated doc, the
# djvu probe finds nothing fetchable, OL empty, GB empty -> dropped.
STATE["ia_docs"] = [{"identifier": "other", "title": "A Cookbook of "
                      "Andalusia", "creator": ["Someone Else"]}]
STATE["djvu"] = None
ok, _ = ba.verify_work_in_catalogue(
    {"author": "Nobody",
     "title": "The Nonexistent Treatise on Flying Carpets of Ghazna"})
check("(ii) no real copy/record anywhere -> still dropped "
      "unconfirmed (no-fabrication law intact)", not ok)

# (ii-b) same-author record exists but its djvu does NOT fetch and
# OpenLibrary has nothing -> the retrievable-copy route must not
# confirm on a dead copy when nothing else corroborates strongly.
STATE["ia_docs"] = [REAL_COPY_DOC]
STATE["djvu"] = None
ok, matched = ba.verify_work_in_catalogue(dict(GARBLED_WORK))
check("(ii-b) copy record present but djvu NOT fetchable -> "
      "existence-via-copy does not fire (falls through, unconfirmed)",
      not ok)

# ---------------------------------------------------------------------------
# §20.4 (iii)/(iv): content-based screening in the shared screen path
# ---------------------------------------------------------------------------
ba._llm_relevance_screen = lambda f, t, b, ak, m, provider=None: {
    i: (True, "mock keep", "primary") for i in range(1, len(b) + 1)}
ba._LAST_BRAIN["name"] = "glm"
ba.llm_chain = lambda: ["glm"]

ARABIC_PASSAGES = ("قال البيروني في القانون المسعودي في معرفة "
                   "ارتفاع الجبل وانحطاط الأفق وشعاع الأرض ") * 6

rec_passages = {"title": "القانون المسعودي", "abstract": "",
                "source_type": "primary",
                "fulltext_excerpt": ARABIC_PASSAGES,
                "passages": True, "passage_hits": 6,
                "fulltext_route": "ia-passages", "fulltext": True,
                "index": "Internet Archive",
                "url": "https://archive.org/details/x"}
counted, stats = ba.screen_sources(FIELD, TOPIC, [dict(rec_passages)],
                                   "k", "m")
check("(iii) Arabic book with passages, zero title/abstract overlap "
      "-> reaches LLM judges and counts",
      len(counted) == 1 and stats["drop_irrelevant"] == 0)

# (iii-b) the GRIS live-run shape: full text fetched but keyword
# matching inside the book found NO hits -> head excerpt only, the
# `passages` flag was never set. §20.1 missed this; §20.4 must not.
rec_head = {"title": "القانون المسعودي", "abstract": "",
            "source_type": "primary",
            "fulltext_excerpt": ARABIC_PASSAGES,
            "fulltext_route": "ia-text-head", "fulltext": True,
            "index": "Internet Archive",
            "url": "https://archive.org/details/y"}
counted, stats = ba.screen_sources(FIELD, TOPIC, [dict(rec_head)],
                                   "k", "m")
check("(iii-b) GRIS shape: primary with HEAD excerpt only (no "
      "passages flag) -> reaches LLM judges, not deterministic-"
      "dropped", len(counted) == 1 and stats["drop_irrelevant"] == 0)

# (iii-c) any source type: a non-primary article carrying a fetched
# excerpt with zero overlap also reaches the judges.
rec_article = {"title": "القانون المسعودي", "abstract": "",
               "source_type": "article",
               "fulltext_excerpt": ARABIC_PASSAGES,
               "fulltext_route": "generic", "fulltext": True,
               "index": "OpenAlex", "url": "https://example.org/a"}
counted, stats = ba.screen_sources(FIELD, TOPIC, [dict(rec_article)],
                                   "k", "m")
check("(iii-c) non-primary source carrying an excerpt -> reaches "
      "LLM judges (§20.4 is type-agnostic)",
      len(counted) == 1 and stats["drop_irrelevant"] == 0)

# (iv) title-only, zero overlap, no content -> still dropped.
rec_bare = {"title": "القانون المسعودي", "abstract": "",
            "source_type": "primary", "index": "Internet Archive",
            "url": "https://archive.org/details/z"}
counted, stats = ba.screen_sources(FIELD, TOPIC, [dict(rec_bare)],
                                   "k", "m")
check("(iv) title-only source, zero overlap, no content -> still "
      "deterministic-dropped",
      len(counted) == 0 and stats["drop_irrelevant"] == 1)

# ---------------------------------------------------------------------------
# §20.4 (v): GRIS parity — gris_sample drives the SAME shared pipeline
# ---------------------------------------------------------------------------
check("(v-a) gris_sample uses ba._quality_pipeline (shared path)",
      gs.ba is ba and hasattr(gs.ba, "_quality_pipeline"))
ba.enrich_abstracts = lambda lst: None
ba.attach_fulltexts_v2 = lambda lst, kws: {"attempted": 0,
                                           "fetched": 0,
                                           "by_route": {}}
counted, sstats = ba._quality_pipeline(
    FIELD, TOPIC, [dict(rec_head)], "k", "m", keywords=[])
check("(v-b) GRIS entry point (ba._quality_pipeline, as called by "
      "gris_sample.stage_retrieval): head-excerpt primary counts",
      len(counted) == 1 and sstats["drop_irrelevant"] == 0)

# ---------------------------------------------------------------------------
# §20.7: detailed source report builder
# ---------------------------------------------------------------------------
primary = {"title": REAL_COPY_DOC["title"],
           "mapped_title": "Al-Qanun al-Masudi fi al-Hay'a",
           "source_type": "primary", "primary_relation": "author",
           "fulltext_route": "ia-passages", "passage_hits": 120,
           "catalogue_via": "archive.org (retrievable copy)"}
article = {"title": "Biruni's Measurement of the Earth",
           "source_type": "article", "index": "OpenAlex",
           "fulltext_route": "unpaywall", "passage_hits": 102}
report = ba.build_detailed_source_report(
    TOPIC, [primary, article],
    disagreement_titles=["The Science of Al-Biruni"])
check("(r1) report lists the counted primary under its REAL title",
      REAL_COPY_DOC["title"] in report)
check("(r2) report shows the mapped title alongside",
      "Al-Qanun al-Masudi fi al-Hay'a" in report)
check("(r3) report shows catalogue-via for the primary",
      "archive.org (retrievable copy)" in report)
check("(r4) report shows keyword-hit counts",
      "120" in report and "102" in report)
check("(r5) report shows full-text routes",
      "ia-passages" in report and "unpaywall" in report)
check("(r6) report lists the disagreement near-miss by title",
      "The Science of Al-Biruni" in report
      and "اختلاف دو داور" in report)
empty = ba.build_detailed_source_report(TOPIC, [], [])
check("(r7) empty counted list -> honest empty report",
      "هیچ‌کدام" in empty and "گزارش ریز منابع" in empty)

failed = [n for n, ok in PASS if not ok]
print(f"\n{len(PASS) - len(failed)}/{len(PASS)} checks passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
