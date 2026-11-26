"""Mocked offline checks for the §19 primary-source root redesign.
No network: catalogues, verification and the brain are all fakes."""
import sys
import urllib.parse

import build_article as ba

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


def cand(title, abstract="", doi=None, index="OpenAlex", **extra):
    c = {"title": title, "authors": ["A. Scholar"], "year": 2010,
         "venue": "Journal", "doi": doi, "url": "https://example.org/x",
         "abstract": abstract, "arxiv_id": None, "index": index}
    c.update(extra)
    return c


# Silence cache writes / loads and verification globally.
ba.save_verify_cache = lambda cache: None
ba.load_verify_cache = lambda: {}
ba._verify_one = lambda c, cache: (True, "test")

WORK = {"author": "Muhammad ibn Ahmad al-Biruni",
        "title": "Kitab Tahdid Nihayat al-Amakin",
        "language": "ar", "kind": "book"}
IA_DOC = {"identifier": "Kitab_Tahdid_Nihayat",
          "title": "Kitab Tahdid Nihayat Al-amakin",
          "creator": ["Muhammad ibn Ahmad al-Biruni"],
          "date": "1967", "language": "eng"}

captured_urls = []


def fake_http_json(url, **kw):
    captured_urls.append(url)
    if "archive.org" in url:
        return {"response": {"docs": [IA_DOC]}}
    if "googleapis" in url:
        return {"items": []}
    return {}


ba.http_json = fake_http_json

# ---- (a) source map generated, consumed, fail-soft -----------------------
ba.call_llm = lambda *a, **k: (
    '{"primary_works": [{"author": "Muhammad ibn Ahmad al-Biruni", '
    '"title": "Kitab Tahdid Nihayat al-Amakin", "language": "ar", '
    '"kind": "book"}], "keywords": ["Ghazna", "qibla", "azimuth"], '
    '"name_variants": ["al-Biruni", "Biruni"]}')
smap = ba.build_source_map("History of astronomy", "سمت قبله غزنه",
                           "key", "model")
check("(a1) source map returns the primary work",
      len(smap["primary_works"]) == 1
      and smap["primary_works"][0]["title"].startswith("Kitab Tahdid"))
check("(a2) source map returns keywords + name variants",
      "Ghazna" in smap["keywords"] and "al-Biruni" in smap["name_variants"])

captured_urls.clear()
rst = ba._new_retrieval_state()
prims = ba.retrieve_primary_sources(smap, rst)
check("(a3) source map is consumed: the mapped work was retrieved",
      len(prims) == 1 and prims[0]["source_type"] == "primary")


def boom(*a, **k):
    raise RuntimeError("brain down")


ba.call_llm = boom
empty_map = ba.build_source_map("F", "T", "k", "m")
check("(a4) brain failure -> empty map, no crash",
      empty_map == {"primary_works": [], "keywords": [],
                    "name_variants": []})
check("(a5) empty map -> primary stage skipped, no crash",
      ba.retrieve_primary_sources(empty_map, ba._new_retrieval_state())
      == [])

# ---- (b) primary search uses title/creator fields ------------------------
captured_urls.clear()
ba.search_ia_primary(WORK)
ba.search_google_books_primary(WORK)
ia_url = urllib.parse.unquote(
    next(u for u in captured_urls if "archive.org" in u))
gb_url = urllib.parse.unquote(
    next(u for u in captured_urls if "googleapis" in u))
check("(b1) IA search is a title:(...) field query",
      "title:(Kitab Tahdid Nihayat al-Amakin)" in ia_url)
check("(b2) IA search is also a creator:(...) field query",
      "creator:(Muhammad ibn Ahmad al-Biruni)" in ia_url)
check("(b3) Google Books search uses intitle:/inauthor:",
      'intitle:"Kitab Tahdid Nihayat al-Amakin"' in gb_url
      and 'inauthor:"Muhammad ibn Ahmad al-Biruni"' in gb_url)
check("(b4) no topical keyword query is used",
      "qibla direction" not in ia_url and "qibla direction" not in gb_url)

# ---- (c) in-book search: passages from the hit region --------------------
HEAD = "HEADMARKER preface and introduction. " + \
    ("lorem ipsum dolor sit amet " * 12000)  # ~324k chars, no keywords
HIT = ("The azimuth of Ghazna and the qibla direction are computed "
       "here. Ghazna qibla Ghazna qibla. " * 40)
FULL = HEAD + HIT + ("lorem ipsum dolor sit amet " * 2000)
assert FULL.find("Ghazna") > 300000

excerpt, hits = ba.extract_passages(FULL, ["Ghazna", "qibla"])
check("(c1) extract_passages finds the deep hits", hits > 50)
check("(c2) excerpt comes from the hit region, not the head",
      excerpt is not None and "Ghazna" in excerpt
      and "HEADMARKER" not in excerpt)

cache_writes = []
ba._read_cache = lambda key: None
ba._write_cache = lambda key, text: cache_writes.append((key, text))
ba._ia_djvu_text = lambda ia_id, cap=None: FULL
primary_book = cand(
    "Kitab Tahdid Nihayat Al-amakin", abstract="",
    index="Internet Archive",
    url="https://archive.org/details/Kitab_Tahdid_Nihayat",
    source_type="primary", search_keywords=["Ghazna", "qibla"])
got, _cached = ba._attach_fulltext_one(primary_book)
ex2 = primary_book.get("fulltext_excerpt") or ""
check("(c3) primary book with no abstract gets a full-text excerpt",
      got and len(ex2) > 500)
check("(c4) excerpt is the hit-region passages, not the head",
      "Ghazna" in ex2 and "HEADMARKER" not in ex2)
check("(c5) primary book now counts as content-bearing",
      ba.has_content(primary_book))
check("(c6) cache stores ONLY the passages (storage law)",
      cache_writes and len(cache_writes[0][1]) < len(FULL)
      and "Ghazna" in cache_writes[0][1])

# ---- (d) primary lane does not consume the journal pool ------------------
EN_ABSTRACT = ("This paper reconstructs al-Biruni's spherical "
               "trigonometry for the qibla of Ghazna and compares his "
               "azimuth with the modern value in detail.")


def rich_searcher(query):
    return [cand(f"Biruni study {query} {i}", abstract=EN_ABSTRACT,
                 doi=f"10.3000/{abs(hash((query, i))) % 99999}")
            for i in range(20)]


ba.WAVE1_SEARCHERS = [rich_searcher]
ba.CROSSREF_SEARCHERS = [lambda query: []]
rst2 = ba._new_retrieval_state()
journal = ba.retrieve_wave1(["al-Biruni qibla"], rst2)
check("(d1) journal pool fills to MAX_VERIFIED=20",
      len(journal) == ba.MAX_VERIFIED == 20)
prims2 = ba.retrieve_primary_sources(smap, rst2)
check("(d2) primaries are verified ADDITIONALLY (reserved lane)",
      len(prims2) >= 1 and len(journal) == 20
      and all(p not in journal for p in prims2))

# ---- (e) judge: by-type criteria + second-judge chain fallback -----------
prompts = []


def judge_llm(prompt, api_key, model, temperature=0.4, json_mode=False,
              provider=None):
    prompts.append((provider, prompt))
    if provider is None:
        ba._LAST_BRAIN["name"] = "glm"
        return ('[{"i": 1, "keep": true, "reason": "genuinely about '
                'the method"}, {"i": 2, "keep": true, "reason": '
                '"primary text on the topic"}]')
    if provider == "gemini":
        raise RuntimeError("HTTP 503: model overloaded")
    return ('[{"i": 1, "keep": true, "reason": "second judge agrees"}]')


ba.call_llm = judge_llm
secondary = cand("Al-Biruni's qibla method applied with modern tools",
                 abstract=EN_ABSTRACT, doi="10.4000/x")
primary_c = cand("Kitab Tahdid Nihayat Al-amakin", abstract="",
                 index="Internet Archive", source_type="primary",
                 fulltext_excerpt=("Ghazna qibla azimuth computation "
                                   "by al-Biruni. " * 20))
ba._llm_relevance_screen("History of astronomy", "سمت قبله غزنه",
                         [secondary, primary_c], "k", "m")
first_prompt = prompts[0][1]
check("(e1) judge prompt carries the PRIMARY type line",
      "PRIMARY SOURCE" in first_prompt
      and "Type: Secondary scholarship" in first_prompt)
check("(e2) judge prompt states the author's-own-treatment rule",
      "relevant by definition" in first_prompt)
check("(e3) judge prompt states 'need not perform the exact claim'",
      "does NOT have to perform the article's" in first_prompt
      and "exact claim" in first_prompt)

prompts.clear()
ba.llm_chain = lambda: ["glm", "gemini", "github"]
ba._brain_key_available = lambda name, api_key: True
topic_fa = "تعیین سمت قبلهٔ غزنه به روش ابوریحان بیرونی"
counted, stats = ba.screen_sources("History of astronomy", topic_fa,
                                   [dict(secondary)], "k", "m")
check("(e4) second judge fell back past the 503 brain (github used)",
      any(p[0] == "github" for p in prompts))
check("(e5) the first judge's keep SURVIVED via the fallback verdict",
      len(counted) == 1 and stats["drop_disagreement"] == 0)

# ---- (e6/e7) §20.1: a primary WITH extracted passages is never
# deterministically dropped — it goes to the LLM screen instead.
# Arabic title + empty abstract + Arabic excerpt share NO token with
# the Persian topic, so without the fix this hits the zero-overlap
# drop branch (same script family 'arab' on both sides).
topic_qibla = "تعیین سمت قبلهٔ غزنه به روش ابوریحان بیرونی"
qanun_excerpt = ("معرفة جهة مكة من بلاد خراسان بالحساب "
                 "والجداول الفلكية. " * 20)
primary_with = cand("القانون المسعودي", abstract="",
                    index="Internet Archive", source_type="primary",
                    passages=True, fulltext_excerpt=qanun_excerpt)
prompts.clear()
counted_p, stats_p = ba.screen_sources("History of astronomy", topic_qibla,
                                       [dict(primary_with)], "k", "m")
check("(e6) primary WITH passages is NOT deterministically dropped",
      len(counted_p) == 1 and stats_p["drop_irrelevant"] == 0)
check("(e6b) primary WITH passages reached the LLM screen",
      len(prompts) > 0)
primary_without = cand("القانون المسعودي", abstract="",
                       index="Internet Archive", source_type="primary",
                       passages=False, fulltext_excerpt=qanun_excerpt)
prompts.clear()
counted_n, stats_n = ba.screen_sources("History of astronomy", topic_qibla,
                                       [dict(primary_without)], "k", "m")
# §20.4 SUPERSEDES the old §20.1 reading of this case (updated
# 2026-11-25): this record carries a fetched full-text excerpt (the
# head-excerpt shape — in-book keyword search found no hits, so the
# `passages` flag was never set; exactly the GRIS live-run failure).
# Content, not the passages flag, is now the criterion: it reaches
# the LLM screen like any content-bearing source.
check("(e7) §20.4: primary with a fetched excerpt but NO passages "
      "flag now reaches the LLM screen (not deterministic-dropped)",
      len(counted_n) == 1 and stats_n["drop_irrelevant"] == 0
      and len(prompts) > 0)
primary_bare = cand("القانون المسعودي", abstract="",
                    index="Internet Archive", source_type="primary",
                    passages=False)
prompts.clear()
counted_b, stats_b = ba.screen_sources("History of astronomy", topic_qibla,
                                       [dict(primary_bare)], "k", "m")
check("(e7b) primary with NO content at all (title-only) is still "
      "deterministically dropped",
      len(counted_b) == 0 and stats_b["drop_irrelevant"] == 1
      and len(prompts) == 0)

# ---- (f) standing laws + funnel ------------------------------------------
check("(f1) MIN_SOURCES bar still 5", ba.MIN_SOURCES == 5)
reg = ba.get_registry()
check("(f2) registry still loads (2045 unique URLs)",
      reg[2]["unique_urls"] == 2045)
ft = ba.build_funnel_text("t", {"candidates": 9, "verified": 8,
                                "with_content": 5, "relevant": 5,
                                "counted": 5, "registry_matched": 1,
                                "primary_counted": 2,
                                "drop_language_gate": 0,
                                "drop_irrelevant": 0,
                                "drop_no_content": 0,
                                "drop_tradition": 0}, True,
                          word_count=6000, word_target=(6000, 8000))
check("(f3) funnel text carries the primary-sources line",
      "منابع اصلی (متن مؤلف/سند بنیادین): 2" in ft)

failed = [n for n, ok in PASS if not ok]
print(f"\n{len(PASS) - len(failed)}/{len(PASS)} checks passed")
sys.exit(1 if failed else 0)
