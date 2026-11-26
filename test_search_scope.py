#!/usr/bin/env python3
"""Offline tests for §43: the SEARCH_SCOPE environment variable.
"full" (default) is today's behaviour, unchanged; "new" restricts
the candidate search to ONLY the searchers added in the newest
build wave (§§32–42) — the §41 shelf plus the new items:
search_shelf, search_university_lectures (§32), search_ensani
(§34), search_jihs (§36), search_iran_math (§37),
search_tajik_journals (§38), search_core (§42). No network:
every searcher is replaced by a recording stub (a tripwire —
the assertions read the call log, so a forbidden searcher
cannot slip through silently), and the verify-cache I/O is
stubbed out. Downstream must be untouched: the text-first gate
constants and verdicts are asserted identical.
"""
import importlib.util
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


NEW_NAMES = ["search_shelf", "search_university_lectures",
             "search_ensani", "search_jihs", "search_iran_math",
             "search_tajik_journals", "search_core"]


class scoped_env:
    """Set SEARCH_SCOPE for a block; restore afterwards."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.old = os.environ.get("SEARCH_SCOPE")
        if self.value is None:
            os.environ.pop("SEARCH_SCOPE", None)
        else:
            os.environ["SEARCH_SCOPE"] = self.value

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("SEARCH_SCOPE", None)
        else:
            os.environ["SEARCH_SCOPE"] = self.old


# --- 1. scope parsing: fail-safe to "full" --------------------
for value, want in ((None, "full"), ("full", "full"),
                    ("new", "new"), ("NEW", "new"),
                    (" new ", "new"), ("garbage", "full"),
                    ("", "full")):
    with scoped_env(value):
        check(f"SEARCH_SCOPE={value!r} -> {want!r} (both scripts)",
              ba.search_scope() == want
              and pt.search_scope() == want)

# --- 2. the new-only set: exactly the §§32–42 searchers -------
check("NEW_ONLY_SEARCHERS is exactly the 7 newest searchers",
      ba.NEW_ONLY_SEARCHERS == [ba.search_shelf,
                                ba.search_university_lectures,
                                ba.search_ensani, ba.search_jihs,
                                ba.search_iran_math,
                                ba.search_tajik_journals,
                                ba.search_core]
      and [s.__name__ for s in ba.NEW_ONLY_SEARCHERS] == NEW_NAMES)
check("every new-only searcher is in SEARCHERS and WAVE1",
      all(s in ba.SEARCHERS and s in ba.WAVE1_SEARCHERS
          for s in ba.NEW_ONLY_SEARCHERS))
check("no pre-§32 searcher is in the new-only set",
      all(s not in ba.NEW_ONLY_SEARCHERS for s in
          (ba.search_openalex, ba.search_crossref,
           ba.search_internet_archive, ba.search_annas_archive,
           ba.search_hal, ba.search_doaj)))

# --- 3. scoped_searchers: full = identity, new = filter -------
with scoped_env(None):
    check("full scope: SEARCHERS order returned unchanged",
          ba.scoped_searchers(ba.SEARCHERS) == ba.SEARCHERS
          and ba.scoped_searchers(ba.WAVE1_SEARCHERS)
          == ba.WAVE1_SEARCHERS)
with scoped_env("new"):
    check("new scope: SEARCHERS filters to the 7, order kept",
          ba.scoped_searchers(ba.SEARCHERS)
          == ba.NEW_ONLY_SEARCHERS)
    check("new scope: WAVE1 filters to the same 7",
          ba.scoped_searchers(ba.WAVE1_SEARCHERS)
          == ba.NEW_ONLY_SEARCHERS)
    check("new scope: Crossref-only order filters to empty",
          ba.scoped_searchers(ba.CROSSREF_SEARCHERS) == [])

# --- 4. tripwire: which searchers actually RUN ----------------
def make_stub(fn, log):
    def stub(query):
        log.append(fn.__name__)
        return []
    stub.__name__ = fn.__name__
    return stub


def tripwire_wave1(scope):
    """Run retrieve_wave1 with every searcher stubbed; return
    the recorded call log."""
    log = []
    saved = (ba.SEARCHERS, ba.WAVE1_SEARCHERS, ba.CROSSREF_SEARCHERS,
             ba.load_verify_cache, ba.save_verify_cache)
    try:
        ba.SEARCHERS = [make_stub(s, log) for s in saved[0]]
        ba.WAVE1_SEARCHERS = [make_stub(s, log) for s in saved[1]]
        ba.CROSSREF_SEARCHERS = [make_stub(s, log) for s in saved[2]]
        ba.load_verify_cache = lambda: {}
        ba.save_verify_cache = lambda cache: None
        state = ba._new_retrieval_state()
        state["cache"] = {}
        with scoped_env(scope):
            ba.retrieve_wave1(["scope probe query"], state)
    finally:
        (ba.SEARCHERS, ba.WAVE1_SEARCHERS, ba.CROSSREF_SEARCHERS,
         ba.load_verify_cache, ba.save_verify_cache) = saved
    return log


log_new = tripwire_wave1("new")
check("new mode wave 1: ONLY the 7 new searchers ran",
      sorted(set(log_new)) == sorted(NEW_NAMES)
      and len(log_new) == len(NEW_NAMES))
check("new mode wave 1: Crossref top-up never ran",
      "search_crossref" not in log_new)

log_full = tripwire_wave1(None)
check("full mode wave 1: every WAVE1 searcher ran + Crossref "
      "top-up", set(log_full)
      == {s.__name__ for s in ba.WAVE1_SEARCHERS}
      | {"search_crossref"})


def tripwire_wave2(scope):
    """The wave-2 order (book sources first + SEARCHERS) through
    collect_verified — the §43 filter sits at that choke point."""
    log = []
    saved = (ba.load_verify_cache, ba.save_verify_cache)
    try:
        ba.load_verify_cache = lambda: {}
        ba.save_verify_cache = lambda cache: None
        book_first = [make_stub(ba.search_internet_archive, log),
                      make_stub(ba.search_annas_archive, log)]
        order2 = book_first + [make_stub(s, log)
                               for s in ba.SEARCHERS]
        state = ba._new_retrieval_state()
        state["cache"] = {}
        with scoped_env(scope):
            ba.collect_verified(["scope probe query"], state=state,
                                searcher_order=order2)
    finally:
        ba.load_verify_cache, ba.save_verify_cache = saved
    return log


log_w2 = tripwire_wave2("new")
check("new mode wave 2: IA/Anna's skipped, only the 7 ran",
      sorted(set(log_w2)) == sorted(NEW_NAMES))

# --- 5. tripwire: the §19 primary lane stands down ------------
def tripwire_primary(scope):
    log = []
    saved = (ba.search_ia_primary, ba.search_google_books_primary)
    try:
        def ia_stub(work):
            log.append("search_ia_primary")
            return []

        def gb_stub(work):
            log.append("search_google_books_primary")
            return []

        ba.search_ia_primary = ia_stub
        ba.search_google_books_primary = gb_stub
        state = ba._new_retrieval_state()
        state["cache"] = {}
        smap = {"primary_works": [{"title": "Tahdid al-Amakin"}],
                "keywords": [], "name_variants": []}
        with scoped_env(scope):
            out = ba.retrieve_primary_sources(smap, state)
    finally:
        ba.search_ia_primary, ba.search_google_books_primary = saved
    return log, out


log_p, out_p = tripwire_primary("new")
check("new mode: primary lane skipped, catalogues never called",
      log_p == [] and out_p == [])
log_p, out_p = tripwire_primary(None)
check("full mode: primary lane active (catalogues called)",
      "search_ia_primary" in log_p
      and "search_google_books_primary" in log_p)

# --- 6. gate constants + verdicts unchanged -------------------
check("gate constants unchanged (5 counted / 3 full-text)",
      ba.MIN_SOURCES == 5 and ba.MIN_FULLTEXT_SOURCES == 3)


def _counted(n, passages, primaries):
    out = []
    for i in range(n):
        s = {"source_type": "article", "fulltext": "x" * 4000}
        if i < passages:
            s["passages"] = ["p"]
        out.append(s)
    for i in range(primaries):
        out[i]["source_type"] = "primary"
    return out


check("gate: 5 counted, 3 passages, 1 primary -> ok",
      ba.text_first_gate(_counted(5, 3, 1))["ok"] is True)
check("gate: 4 counted -> fails closed",
      ba.text_first_gate(_counted(4, 3, 1))["ok"] is False)
check("gate: 5 counted, 2 passages -> fails closed",
      ba.text_first_gate(_counted(5, 2, 1))["ok"] is False)
check("gate: 5 counted, 3 passages, no primary -> fails closed",
      ba.text_first_gate(_counted(5, 3, 0))["ok"] is False)

# --- 7. visibility: funnel, §20.7 report, article note --------
FUNNEL = {"candidates": 9, "verified": 7, "with_content": 6,
          "relevant": 5, "counted": 5, "registry_matched": 2,
          "primary_counted": 1, "fulltext_counted": 3}
with scoped_env("new"):
    check("new mode: funnel carries the scope line",
          "تازه‌افزوده" in ba.build_funnel_text(
              "موضوع آزمایشی", FUNNEL, True, word_count=100,
              word_target=(90, 110)))
    check("new mode: §20.7 report carries the scope line",
          "تازه‌افزوده" in ba.build_detailed_source_report(
              "موضوع آزمایشی", []))
    check("new mode: article note (fa) names the scope",
          "دامنهٔ جستجو" in ba.search_scope_note("fa"))
    check("new mode: article note (en) names the scope",
          "newly added sources" in ba.search_scope_note("en"))
with scoped_env(None):
    check("full mode: funnel + report carry NO scope line",
          "تازه‌افزوده" not in ba.build_funnel_text(
              "موضوع آزمایشی", FUNNEL, True, word_count=100,
              word_target=(90, 110))
          and "تازه‌افزوده" not in ba.build_detailed_source_report(
              "موضوع آزمایشی", [])
          and ba._scope_lines() == [])

# --- 8. propose_topics: grounding honours the scope -----------
def tripwire_grounding(scope):
    log = []
    saved = (pt.search_openalex, pt.search_crossref,
             pt.search_openaire, pt.search_europe_pmc)
    try:
        def mk(fn):
            def stub(q):
                log.append(fn.__name__)
                return []
            return stub

        pt.search_openalex = mk(saved[0])
        pt.search_crossref = mk(saved[1])
        pt.search_openaire = mk(saved[2])
        pt.search_europe_pmc = mk(saved[3])
        with scoped_env(scope):
            pt.grounding_digest("Mathematics", "geometry theorems",
                                "", "test-model")
    finally:
        (pt.search_openalex, pt.search_crossref,
         pt.search_openaire, pt.search_europe_pmc) = saved
    return log


check("propose new mode: external grounding searchers stand "
      "down (shelf only)", tripwire_grounding("new") == [])
check("propose full mode: external grounding searchers run",
      len(tripwire_grounding(None)) > 0)

# --- 9. workflow wiring ----------------------------------------
yml = open(os.path.join(HERE, ".github", "workflows",
                        "discovery.yml")).read()
check("discovery.yml: search_scope input + SEARCH_SCOPE env in "
      "both jobs", "search_scope:" in yml
      and yml.count("SEARCH_SCOPE: ${{ inputs.search_scope }}")
      == 2)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
