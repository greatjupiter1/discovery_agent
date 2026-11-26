"""Mocked offline checks for the §18 English-first retrieval fix.
No network: searchers, verification and the brain are all fakes."""
import sys

import build_article as ba

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


def cand(title, abstract="", doi=None, index="OpenAlex", language=None):
    c = {"title": title, "authors": ["A. Scholar"], "year": 2010,
         "venue": "Journal", "doi": doi, "url": "https://example.org/x",
         "abstract": abstract, "arxiv_id": None, "index": index}
    if language:
        c["language"] = language
    return c


EN_ABSTRACT = ("This paper reconstructs al-Biruni's spherical "
               "trigonometry for the qibla of Ghazna and compares his "
               "azimuth with the modern value in detail.")
PERSIAN_NOISE = cand("بررسی وضعیت اقتصادی ولایت هرات و برنامه‌های دولت",
                     index="Crossref")
EN_BIRUNI = cand("Al-Biruni's determination of the qibla of Ghazna",
                 abstract=EN_ABSTRACT, doi="10.1000/test1")

# Silence cache writes / loads during collect_verified.
ba.save_verify_cache = lambda cache: None
ba.load_verify_cache = lambda: {}
ba._verify_one = lambda c, cache: (True, "test")

# ---- (a) English queries are generated and used in wave 1 ---------------
ba.call_llm = lambda *a, **k: (
    '{"queries": ["al-Biruni qibla Ghazna spherical trigonometry", '
    '"Biruni Tahdid nihayat al-amakin azimuth"], '
    '"french": "al-Biruni qibla Ghazna", "german": "al-Biruni Qibla Ghazna"}')
qs = ba.english_first_queries("History of astronomy", "سمت قبله غزنه بیرونی",
                              "key", "model")
check("(a1) expansion returns English queries",
      len(qs) >= 3 and qs[0].startswith("al-Biruni qibla"))
check("(a2) FR/DE variants included",
      any("al-Biruni qibla Ghazna" == q for q in qs))

seen_queries = []


def fake_openalex(query):
    seen_queries.append(query)
    return [EN_BIRUNI]


def fake_crossref(query):
    seen_queries.append(("CROSSREF", query))
    return [cand("Crossref-only item", abstract=EN_ABSTRACT,
                 index="Crossref")]


ba.WAVE1_SEARCHERS = [fake_openalex]
ba.CROSSREF_SEARCHERS = [fake_crossref]
st = ba._new_retrieval_state()
verified = ba.retrieve_wave1(qs, st)
check("(a3) wave 1 ran the English queries",
      qs[0] in seen_queries)
check("(d) genuine English abstract-bearing Biruni candidate verified",
      any(v["title"] == EN_BIRUNI["title"] for v in verified))

# ---- (b) Crossref only as top-up when wave 1 < 15 verified --------------
check("(b1) Crossref top-up ran because wave 1 had <15 verified",
      any(isinstance(q, tuple) and q[0] == "CROSSREF" for q in seen_queries))

seen_queries.clear()


def rich_openalex(query):
    return [cand(f"Biruni study {query} {i}", abstract=EN_ABSTRACT,
                 doi=f"10.2000/{abs(hash((query, i))) % 99999}")
            for i in range(20)]


ba.WAVE1_SEARCHERS = [rich_openalex]
st2 = ba._new_retrieval_state()
v2 = ba.retrieve_wave1(qs[:1], st2)
check("(b2) Crossref skipped when wave 1 reaches the early-stop bar",
      not any(isinstance(q, tuple) for q in seen_queries) and len(v2) >= 15)

# ---- (c) Persian/Afghan noise dropped at intake --------------------------
check("(c1) gate drops Arabic-script title",
      ba.language_gate_drop(PERSIAN_NOISE) is True)
check("(c2) gate drops metadata language=fa",
      ba.language_gate_drop(cand("Some English Title", language="fa"))
      is True)
check("(c3) scholarly-edition exception survives the gate",
      ba.language_gate_drop(cand(
          "Tahdid nihayat al-amakin: a critical edition",
          language="fa")) is False)
check("(d2) gate keeps the English Biruni candidate",
      ba.language_gate_drop(EN_BIRUNI) is False)


def noisy_openalex(query):
    return [PERSIAN_NOISE, EN_BIRUNI]


ba.WAVE1_SEARCHERS = [noisy_openalex]
ba.CROSSREF_SEARCHERS = [lambda query: []]
st3 = ba._new_retrieval_state()
v3 = ba.retrieve_wave1(["al-Biruni qibla"], st3)
check("(c4) noise dropped inside collect flow, Biruni kept",
      len(v3) == 1 and v3[0]["title"] == EN_BIRUNI["title"]
      and st3["lang_gate_dropped"] == 1)

# ---- (e) brain failure falls back without crashing -----------------------
def boom(*a, **k):
    raise RuntimeError("brain down")


ba.call_llm = boom
check("(e) expansion failure returns [] (legacy fallback)",
      ba.english_first_queries("F", "T", "k", "m") == [])

# ---- (f) fail-closed bar + registry unchanged ----------------------------
check("(f1) MIN_SOURCES bar still 5", ba.MIN_SOURCES == 5)
reg = ba.get_registry()
check("(f2) registry still loads (2109 unique URLs)",
      reg[2]["unique_urls"] == 2109)
ft = ba.build_funnel_text("t", {"candidates": 1, "verified": 1,
                                "with_content": 0, "relevant": 0,
                                "counted": 0, "registry_matched": 0,
                                "drop_language_gate": 7,
                                "drop_irrelevant": 0,
                                "drop_no_content": 0,
                                "drop_tradition": 0}, False, stage="s")
check("(f3) funnel text carries the language-gate count",
      "دروازهٔ زبان" in ft and "7" in ft)

failed = [n for n, ok in PASS if not ok]
print(f"\n{len(PASS) - len(failed)}/{len(PASS)} checks passed")
sys.exit(1 if failed else 0)
