#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline tests for §70 — prior-work sweep, full bibliography,
novelty boundary (Jupiter 2026-11-27, clarified ~21:18: a
UNIVERSAL standing law for EVERY production — any field, topic,
language; no topic-specific special cases).

Covered:
  (a) build_prior_work_set: counted first (flagged in_counted),
      other relevant sources appended flagged prior_work_only,
      dedupe by the §20.5 key, video support never joins;
  (b) assess_novelty: a scripted brain verdict is parsed and
      validated; a failing brain and an invalid verdict both fall
      back to the deterministic UNCONFIRMED assessment (status
      unknown — never a fabricated 'novel');
  (c) format_novelty_block: the 4-part form in English and in
      Persian (unconfirmed -> «تأییدنشده»); insert_novelty_block
      places it before the References, or at the end;
  (d) the §33 bibliography-completeness check: a prior-work
      source missing from the reference list is ONE fixable
      citations issue carrying the exact reference line; a
      complete bibliography is silent; the review_citations role
      surfaces it as a verdict (available), never a block;
  (e) screen_sources exposes relevant_sources (§70): a relevant
      but content-less source is in the relevant set, not in the
      counted — NON-BIRUNI fixtures (plant physiology);
  (f) the source card of a bibliography-only source carries the
      PRIOR-WORK ONLY marker;
  (g) the funnel carries the §70 prior-work line;
  (h) the checkpoint round-trips prior_work_extra and the set is
      rebuilt identically from it;
  (i) main() integration on a NON-BIRUNI topic: novelty 'done'
      fails closed with nothing delivered; 'partial' narrows the
      writer guide and the delivered article carries the block;
      'novel' delivers with the block and records prior_work +
      novelty in state.json;
  (j) the iranjournals.ir portal registry entry: registry-only,
      routes tried recorded; the Keramati journal's own subdomain
      (mct.iranjournals.ir) is among the §37 live-wired journals;
  (k) the three tiers + verification statuses (consultant
      condition 1 + the use-all-sources clarification): a
      relevant source WITH obtained content joins as
      prior_work_content with a full card, never as bib-only;
  (m) the search-evidence line in the block + the comparison
      matrix rendering (conditions 3 + 4); a verdict without a
      matrix falls back, and a 'confirmed' resting on a
      bibliography-only closest is downgraded (in (b6)–(b8));
  (n) the §33 novelty reviewer (condition 5): inert without a
      prior-work set; missing block and unengaged checked source
      are fixable; 'already done' is fatal;
  (o) the deterministic prior-work AUDIT: per-member place +
      type of use on the final text; the unengaged checked
      source is named; the bib-only limitation is recorded.
No network, no real keys.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_article as ba

PASS = [0]
FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        PASS[0] += 1
        print(f"PASS {name}")
    else:
        FAIL[0] += 1
        print(f"FAIL {name} {extra}")


# ----------------------------------------------------------------------
# Fixtures — deliberately NON-BIRUNI (plant physiology), proving the
# §70 stage is universal, plus one Biruni set for the set algebra.
# ----------------------------------------------------------------------

def src(title, doi="", year=2020, abstract="", excerpt="",
        source_type="article", venue="Journal of Tests"):
    return {"title": title, "authors": ["A. Author"],
            "year": year, "venue": venue, "doi": doi,
            "url": "https://example.org/x", "abstract": abstract,
            "fulltext_excerpt": excerpt, "passages": bool(excerpt),
            "fulltext": "ia-passages" if excerpt else "",
            "source_type": source_type, "index": "test"}


FIELD_P = "Plant physiology"
TOPIC_P = "Stomatal regulation in desert plants under drought stress"


def plant_counted():
    return src("Stomatal regulation in desert plants under drought "
               "stress", doi="10.1000/plant1", year=2021,
               abstract="A field study of stomatal conductance in "
                        "desert shrubs during summer drought.",
               excerpt="Stomatal conductance fell by 40 percent "
                       "under drought stress in the studied "
                       "shrubs.")


def plant_bibonly():
    return src("Drought stress and stomatal regulation in desert "
               "plants", doi="10.1000/plant2", year=2019)


# ----------------------------------------------------------------------
# (a) prior-work set construction
# ----------------------------------------------------------------------

def test_prior_work_set():
    counted = [plant_counted(),
               src("Guard cell signalling in arid-zone species",
                   doi="10.1000/plant3", year=2018,
                   abstract="x" * 60, excerpt="Guard cells...")]
    dupe = dict(counted[0])  # same DOI as counted[0]
    bib = plant_bibonly()
    video = src("A lecture on stomata", source_type="video")
    pw = ba.build_prior_work_set(counted, counted + [dupe, bib,
                                                     video])
    check("(a1) set = counted + non-duplicate relevant only",
          len(pw) == 3, str(len(pw)))
    check("(a2) counted flagged in_counted, not prior_work_only",
          pw[0].get("in_counted") is True
          and not pw[0].get("prior_work_only"))
    check("(a3) extra flagged prior_work_only / not in_counted",
          pw[2].get("prior_work_only") is True
          and pw[2].get("in_counted") is False)
    check("(a4) bib-only helper returns exactly the extras",
          ba.prior_work_bib_only(pw) == [pw[2]])
    check("(a5) empty inputs -> empty set",
          ba.build_prior_work_set([], []) == [])


# ----------------------------------------------------------------------
# (b) novelty assessment
# ----------------------------------------------------------------------

def test_assess_novelty():
    pw = ba.build_prior_work_set([plant_counted()], [plant_bibonly()])
    keep = ba.call_llm
    try:
        ba.call_llm = lambda *a, **k: json.dumps({
            "claim": "First interval model of stomatal response",
            "closest": "Stomatal regulation in desert plants "
                       "under drought stress",
            "boundary": "the interval model itself",
            "status": "partial", "credibility": "confirmed",
            "matrix": [{"source": "Stomatal regulation in desert "
                                  "plants under drought stress",
                        "data": "same field data",
                        "method": "point estimates, not intervals",
                        "output": "a single conductance value",
                        "scope": "the whole response curve",
                        "difference": "the interval model itself"}]})
        got = ba.assess_novelty(FIELD_P, TOPIC_P, "en", pw, "k", "m")
        check("(b1) brain verdict parsed (partial/confirmed, "
              "matrix kept)",
              got["status"] == "partial"
              and got["credibility"] == "confirmed"
              and got["boundary"] == "the interval model itself"
              and len(got.get("matrix") or []) == 1,
              str(got))

        def boom(*a, **k):
            raise ba.Fail("all brains down (scripted)")
        ba.call_llm = boom
        got = ba.assess_novelty(FIELD_P, TOPIC_P, "en", pw, "k", "m")
        check("(b2) brain failure -> UNCONFIRMED fallback",
              got["status"] == "unknown"
              and got["credibility"] == "unconfirmed", str(got))
        check("(b3) fallback closest = first counted title",
              got["closest"] == pw[0]["title"], str(got))

        ba.call_llm = lambda *a, **k: json.dumps({
            "claim": "c", "closest": "x", "boundary": "b",
            "status": "sideways", "credibility": "confirmed"})
        got = ba.assess_novelty(FIELD_P, TOPIC_P, "en", pw, "k", "m")
        check("(b4) invalid status -> fallback, never invented",
              got["status"] == "unknown", str(got))
        got = ba.assess_novelty(FIELD_P, TOPIC_P, "en", [], "k", "m")
        check("(b5) empty prior-work set -> fallback",
              got["status"] == "unknown"
              and got["credibility"] == "unconfirmed", str(got))

        row = {"source": "s", "data": "d", "method": "m",
               "output": "o", "scope": "sc", "difference": "df"}
        ba.call_llm = lambda *a, **k: json.dumps({
            "claim": "c", "closest": "Stomatal regulation in "
                       "desert plants under drought stress",
            "boundary": "b", "status": "novel",
            "credibility": "confirmed"})
        got = ba.assess_novelty(FIELD_P, TOPIC_P, "en", pw, "k", "m")
        check("(b6) verdict WITHOUT a comparison matrix is "
              "unusable -> UNCONFIRMED fallback (condition 4)",
              got["status"] == "unknown"
              and got["credibility"] == "unconfirmed", str(got))
        ba.call_llm = lambda *a, **k: json.dumps({
            "claim": "c",
            "closest": "Drought stress and stomatal regulation "
                       "in desert plants",
            "boundary": "b", "status": "novel",
            "credibility": "confirmed", "matrix": [row]})
        got = ba.assess_novelty(FIELD_P, TOPIC_P, "en", pw, "k", "m")
        check("(b7) 'confirmed' resting on a BIBLIOGRAPHY-ONLY "
              "closest is downgraded to unconfirmed (condition 3)",
              got["status"] == "novel"
              and got["credibility"] == "unconfirmed", str(got))
        ba.call_llm = lambda *a, **k: json.dumps({
            "claim": "c",
            "closest": "Stomatal regulation in desert plants "
                       "under drought stress",
            "boundary": "b", "status": "partial",
            "credibility": "confirmed", "matrix": [row],
            "relations": {"2": "indirectly relevant",
                          "1": "indirectly relevant"}})
        got = ba.assess_novelty(FIELD_P, TOPIC_P, "en", pw, "k", "m")
        check("(b8) relevance gradation applies to the "
              "bibliography-only member only, never to a "
              "content-checked one (condition 1)",
              pw[1].get("prior_work_status") == "indirectly relevant"
              and pw[0].get("prior_work_status")
              == "full-text checked", str(pw))
    finally:
        ba.call_llm = keep


# ----------------------------------------------------------------------
# (c) the novelty block in the article
# ----------------------------------------------------------------------

def test_novelty_block():
    nov = {"claim": "C", "closest": "K", "boundary": "B",
           "status": "partial", "credibility": "unconfirmed"}
    block = ba.format_novelty_block(nov, "en")
    check("(c1) English block carries all four parts",
          "## Novelty boundary (§70)" in block
          and "- Claim: C" in block
          and "- Closest direct prior work: K" in block
          and "- Exact novelty boundary: B" in block
          and "unconfirmed" in block, block)
    block_fa = ba.format_novelty_block(nov, "fa")
    check("(c2) Persian block localised, unconfirmed = تأییدنشده",
          "مرز نوآوری" in block_fa and "تأییدنشده" in block_fa
          and "ادعا" in block_fa, block_fa)
    art = "# T\n\n## Body\n\ntext\n\n## References\n\n[1] X.\n"
    out = ba.insert_novelty_block(art, block)
    check("(c3) block inserted before the References",
          out.index("Novelty boundary") < out.index("## References"),
          out)
    out2 = ba.insert_novelty_block("# T\n\n## Body\n\ntext\n", block)
    check("(c4) no References heading -> block appended at end",
          out2.rstrip().endswith("unconfirmed"), out2[-120:])


# ----------------------------------------------------------------------
# (d) §33 bibliography completeness
# ----------------------------------------------------------------------

def _pw_sources():
    counted = [plant_counted()]
    bib = plant_bibonly()
    pw = ba.build_prior_work_set(counted, [bib])
    sources = counted + [bib]  # write_sources order in main()
    return pw, sources


def _article_with_refs(numbers, sources):
    refs = "\n".join(ba.format_reference(n, sources[n - 1])
                     for n in numbers)
    body = " ".join(f"claim [{n}]." for n in numbers)
    return (f"# T\n\n## Body\n\n{body}\n\n## Prior Work\n\n"
            f"{body}\n\n## References\n\n{refs}\n")


def test_bibliography_completeness():
    pw, sources = _pw_sources()
    full = _article_with_refs([1, 2], sources)
    check("(d1) complete bibliography -> no completeness issues",
          ba._bibliography_completeness_issues(full, sources, pw)
          == [])
    missing = _article_with_refs([1], sources)
    issues = ba._bibliography_completeness_issues(missing, sources,
                                                  pw)
    check("(d2) missing prior-work entry -> exactly one issue",
          len(issues) == 1, str(issues))
    if issues:
        i = issues[0]
        check("(d3) the issue is fixable, citations role, writing "
              "stage", i["severity"] == "fixable"
              and i["role"] == "citations"
              and i["fix_stage"] == "citations", str(i))
        check("(d4) the issue carries the exact reference line",
              ba.format_reference(2, sources[1]) in i["problem"],
              i["problem"])
    check("(d5) no prior-work set -> silent",
          ba._bibliography_completeness_issues(missing, sources,
                                               []) == [])
    check("(d6) no reference list at all -> silent (base check "
          "owns that fatal)",
          ba._bibliography_completeness_issues(
              "# T\n\n## Body\n\ntext\n", sources, pw) == [])


def test_review_citations_surfaces_completeness():
    pw, sources = _pw_sources()
    counted = [sources[0]]
    ctx = {"field": FIELD_P, "topic": TOPIC_P, "language": "en",
           "article_type": "isi", "title": "T", "sources": sources,
           "prior_work": pw, "word_target": (100, 300),
           "api_key": "k", "model": "m"}
    keep = ba.call_llm
    try:
        ba.call_llm = lambda *a, **k: json.dumps({"issues": []})
        verdict = ba.review_citations(
            _article_with_refs([1], sources), counted, ctx)
        hits = [i for i in verdict["issues"]
                if "§70" in i["problem"]]
        check("(d7) citations verdict is AVAILABLE (not a block) "
              "and carries the §70 fixable issue",
              verdict["available"] and len(hits) == 1
              and hits[0]["severity"] == "fixable", str(verdict))
    finally:
        ba.call_llm = keep


# ----------------------------------------------------------------------
# (e) screen_sources exposes the relevant set (non-Biruni)
# ----------------------------------------------------------------------

def test_screen_relevant_sources():
    a = plant_counted()
    b = plant_bibonly()  # relevant title, NO content at all
    counted, stats = ba.screen_sources(FIELD_P, TOPIC_P, [a, b],
                                       "k", "m")
    check("(e1) both sources judged relevant",
          stats["relevant"] == 2, str(stats))
    check("(e2) only the content-bearing one counted",
          len(counted) == 1 and stats["counted"] == 1
          and stats["drop_no_content"] == 1, str(stats))
    check("(e3) relevant_sources carries BOTH (the §70 pool)",
          len(stats.get("relevant_sources") or []) == 2,
          str(stats.get("relevant_sources")))


# ----------------------------------------------------------------------
# (f) source card marker + (g) funnel line
# ----------------------------------------------------------------------

def test_card_and_funnel():
    bib = plant_bibonly()
    bib["prior_work_only"] = True
    card = ba._source_card_text(3, bib)
    check("(f1) bib-only card carries the PRIOR-WORK ONLY marker",
          "PRIOR-WORK ONLY (§70)" in card
          and "NEVER attribute" in card, card)
    plain = ba._source_card_text(1, plant_counted())
    check("(f2) counted card has no such marker",
          "PRIOR-WORK ONLY" not in plain)
    funnel = {"candidates": 9, "verified": 7, "with_content": 6,
              "relevant": 5, "counted": 5, "registry_matched": 0,
              "primary_counted": 1, "fulltext_counted": 4,
              "prior_work": 7, "prior_work_bib_only": 2}
    text = ba.build_funnel_text(TOPIC_P, funnel, True,
                                word_count=10, word_target=(5, 15))
    check("(g1) funnel carries the §70 prior-work line",
          "پیشینهٔ پژوهش (§70)" in text and "7" in text, text)
    funnel2 = dict(funnel)
    del funnel2["prior_work"]
    text2 = ba.build_funnel_text(TOPIC_P, funnel2, True,
                                 word_count=10, word_target=(5, 15))
    check("(g2) funnel without §70 numbers is unchanged",
          "§70" not in text2)


# ----------------------------------------------------------------------
# (h) checkpoint round-trip of the bib-only prior-work members
# ----------------------------------------------------------------------

def test_checkpoint_prior_work():
    counted = [plant_counted()]
    bib = plant_bibonly()
    keep = ba.RUNS_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            ba.RUNS_DIR = td
            ba.save_stage_checkpoint(
                FIELD_P, TOPIC_P, "en", "intl", "isi", "review",
                ["q"], {}, {"core_keywords": [], "name_variants": []},
                ["stomatal"], counted,
                {"candidates": 2, "verified": 2, "counted": 1},
                [], 2, prior_work_extra=[bib])
            payload = ba.load_stage_checkpoint(
                FIELD_P, TOPIC_P, "en", "intl", "isi", "review")
            check("(h1) checkpoint carries prior_work_extra",
                  payload is not None
                  and len(payload.get("prior_work_extra") or [])
                  == 1
                  and payload["prior_work_extra"][0]["title"]
                  == bib["title"], str(payload and
                                       payload.get("prior_work_extra")))
            rebuilt = ba.build_prior_work_set(
                payload["counted"],
                payload["counted"] + payload["prior_work_extra"])
            check("(h2) set rebuilt from checkpoint flags the "
                  "extra prior_work_only",
                  len(rebuilt) == 2
                  and rebuilt[1].get("prior_work_only") is True,
                  str(rebuilt))
    finally:
        ba.RUNS_DIR = keep


# ----------------------------------------------------------------------
# (i) main() integration — NON-BIRUNI topic, three novelty outcomes
# ----------------------------------------------------------------------

def _plant_counted_five():
    out = []
    for i in range(5):
        s = src(f"Stomatal study {i} on drought stress in desert "
                f"plants", doi=f"10.1000/p{i}", year=2015 + i,
                abstract="a" * 60,
                excerpt="Stomatal conductance data for shrubs.",
                source_type="primary" if i == 0 else "article")
        out.append(s)
    return out


def _run_main(novelty_json):
    counted = _plant_counted_five()
    bib = plant_bibonly()
    funnel = {"candidates": 9, "verified": 7, "with_content": 6,
              "relevant": 6, "counted": 5, "registry_matched": 0,
              "primary_counted": 1, "fulltext_counted": 4,
              "drop_language_gate": 0, "drop_irrelevant": 1,
              "drop_no_content": 1, "drop_tradition": 0,
              "drop_disagreement": 0}
    captured = {"funnels": [], "saved": {}, "write": {}}
    sent = []

    def fake_fresh(field, topic, language, category, article_type,
                   mode, api_key, model, tg_token):
        return {"counted": counted, "funnel": dict(funnel),
                "disagreement_titles": [], "queries": ["q"],
                "ft_keywords": ["stomatal"],
                "source_map": {},
                "relevant_sources": counted + [bib],
                "verified_count": 7,
                "gate": {"ok": True, "counted": 5,
                         "fulltext_passages": 4,
                         "primary_passages": 1}}

    def fake_write(field, topic, language, sources, api_key, model,
                   guide="", word_target=None):
        captured["write"] = {"sources": list(sources),
                             "guide": guide}
        refs = "\n".join(ba.format_reference(i, s)
                         for i, s in enumerate(sources, 1))
        article = ("# A Narrow Plant Title\n\n## Abstract\n\n"
                   "Findings from the sources [1].\n\n"
                   "## Prior Work\n\nDiscussed [1] and [6].\n\n"
                   "## Body\n\nMore [1].\n\n## References\n\n"
                   + refs + "\n")
        return ("A Narrow Plant Title", article,
                {"title_candidates": [], "next_topics": [],
                 "word_count": 120})

    def fake_review(article, counted_, ctx):
        return {"ok": True, "verdict": "pass", "rounds": 0,
                "issues_found": 0, "issues_fixed": 0, "roles": {},
                "blocked_roles": [], "issue_records": [],
                "article": article}

    names = ("_fresh_screening", "write_article", "run_review_layer",
             "send_funnel_dm", "send_detailed_source_report_dm",
             "send_review_report_dm", "send_suggestions_dm",
             "fetch_youtube_support", "load_state", "save_state",
             "consume_stage_checkpoint", "progress_start",
             "progress_update", "progress_fail", "call_llm",
             "tg_call", "tg_send_document")
    keep = {n: getattr(ba, n) for n in names}
    env_keep = {k: os.environ.get(k) for k in (
        "FIELD", "TOPIC", "LANGUAGE", "MODE", "ARTICLE_CATEGORY",
        "ARTICLE_TYPE", "GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN",
        "CHAT_ID", "ARTICLE_CHAT_ID")}
    try:
        ba._fresh_screening = fake_fresh
        ba.write_article = fake_write
        ba.run_review_layer = fake_review
        ba.send_funnel_dm = lambda t, c, topic, f, ok, **k: (
            captured["funnels"].append((f, ok, k.get("stage"))),
            True)[1]
        ba.send_detailed_source_report_dm = lambda *a, **k: True
        ba.send_review_report_dm = lambda *a, **k: True
        ba.send_suggestions_dm = lambda *a, **k: None
        ba.fetch_youtube_support = lambda q, k: []
        ba.load_state = lambda: {}
        ba.save_state = lambda s: captured["saved"].update(s)
        ba.consume_stage_checkpoint = lambda *a, **k: None
        ba.progress_start = lambda *a, **k: None
        ba.progress_update = lambda *a, **k: None
        ba.progress_fail = lambda *a, **k: None
        ba.call_llm = lambda *a, **k: novelty_json
        ba.tg_call = lambda method, token, payload: (
            sent.append(payload), {"message_id": 1})[1]
        ba.tg_send_document = lambda *a, **k: {"message_id": 9}
        os.environ.update({
            "FIELD": FIELD_P, "TOPIC": TOPIC_P, "LANGUAGE": "en",
            "MODE": "review", "ARTICLE_CATEGORY": "intl",
            "ARTICLE_TYPE": "isi", "GEMINI_API_KEY": "k",
            "TELEGRAM_BOT_TOKEN": "t", "CHAT_ID": "-1001",
            "ARTICLE_CHAT_ID": "42"})
        try:
            ba.main()
            rc = 0
        except ba.Fail:
            rc = -1
    finally:
        for n, v in keep.items():
            setattr(ba, n, v)
        for k, v in env_keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    delivered = "\n".join(p.get("text", "") for p in sent)
    return rc, captured, delivered


def test_main_novelty_outcomes():
    novel = json.dumps({
        "claim": "An interval model of stomatal response",
        "closest": "Stomatal regulation in desert plants under "
                   "drought stress",
        "boundary": "the interval model itself",
        "status": "novel", "credibility": "confirmed",
        "matrix": [{"source": "Stomatal regulation in desert "
                              "plants under drought stress",
                    "data": "d", "method": "m", "output": "o",
                    "scope": "s", "difference": "df"}]})
    rc, cap, delivered = _run_main(novel)
    check("(i1) NON-BIRUNI novel claim: production succeeds",
          rc == 0, f"rc={rc}")
    check("(i2) writers received counted + the bib-only source",
          len(cap["write"].get("sources") or []) == 6
          and cap["write"]["sources"][-1].get("prior_work_only")
          is True, str(cap["write"].get("sources")))
    check("(i3) delivered article carries the novelty block",
          "## Novelty boundary (§70)" in delivered, delivered[:300])
    prods = cap["saved"].get("productions") or []
    check("(i4) state.json records prior_work + novelty",
          len(prods) == 1 and prods[0].get("prior_work") == 6
          and prods[0].get("novelty", {}).get("status") == "novel",
          str(prods))

    partial = json.dumps({
        "claim": "An interval model of stomatal response",
        "closest": "Stomatal regulation in desert plants under "
                   "drought stress",
        "boundary": "only the interval model is new",
        "status": "partial", "credibility": "confirmed",
        "matrix": [{"source": "Stomatal regulation in desert "
                              "plants under drought stress",
                    "data": "d", "method": "m", "output": "o",
                    "scope": "s", "difference": "df"}]})
    rc, cap, delivered = _run_main(partial)
    check("(i5) partial: production succeeds, guide NARROWED",
          rc == 0 and "NARROWED" in cap["write"].get("guide", "")
          and "only the interval model is new"
          in cap["write"].get("guide", ""), str(cap["write"]))

    done = json.dumps({
        "claim": "An interval model of stomatal response",
        "closest": "Stomatal regulation in desert plants under "
                   "drought stress",
        "boundary": "nothing remains",
        "status": "done", "credibility": "confirmed",
        "matrix": [{"source": "Stomatal regulation in desert "
                              "plants under drought stress",
                    "data": "d", "method": "m", "output": "o",
                    "scope": "s", "difference": "df"}]})
    rc, cap, delivered = _run_main(done)
    check("(i6) done: main() FAILS CLOSED, nothing delivered",
          rc == -1 and delivered == "", f"rc={rc}")
    check("(i7) done: the funnel report went out as a failure "
          "with the §70 stage",
          any((not ok) and stage and "§70" in stage
              for _, ok, stage in cap["funnels"]),
          str(cap["funnels"]))
    check("(i8) done: no production recorded",
          "productions" not in cap["saved"], str(cap["saved"]))


# ----------------------------------------------------------------------
# (k) three tiers + verification statuses (conditions 1 + the
#     use-all-sources goal clarification)
# ----------------------------------------------------------------------

def test_tiers_and_statuses():
    counted = [src("Abstract-only counted study",
                   doi="10.1000/abs1", year=2020,
                   abstract="a" * 60)]  # content via abstract only
    pool = src("Relevant pool study with obtained text",
               doi="10.1000/pool1", year=2022,
               abstract="b" * 60,
               excerpt="Extracted passage from the pool study.")
    bib = plant_bibonly()
    pw = ba.build_prior_work_set(counted, [pool, bib])
    check("(k1) three tiers: counted / content pool / bib-only",
          len(pw) == 3 and pw[0].get("in_counted") is True
          and pw[1].get("prior_work_content") is True
          and pw[2].get("prior_work_only") is True, str(pw))
    check("(k2) content-pool helper returns exactly the pool",
          ba.prior_work_content_pool(pw) == [pw[1]])
    check("(k3) statuses: excerpt-bearing = full-text checked; "
          "abstract-only counted = record verified, full text "
          "NOT checked; bib-only likewise",
          pw[1].get("prior_work_status") == "full-text checked"
          and pw[0].get("prior_work_status")
          == ("bibliographic record verified but full text NOT "
              "checked")
          and pw[2].get("prior_work_status")
          == ("bibliographic record verified but full text NOT "
              "checked"), str([s.get("prior_work_status")
                               for s in pw]))
    card = ba._source_card_text(2, pw[1])
    check("(k4) content-pool card carries the PRIOR-WORK CONTENT "
          "marker and its excerpt",
          "PRIOR-WORK CONTENT (§70)" in card
          and "MUST engage" in card
          and "Extracted passage from the pool study." in card,
          card)
    funnel = {"candidates": 9, "verified": 7, "with_content": 6,
              "relevant": 5, "counted": 5, "registry_matched": 0,
              "primary_counted": 1, "fulltext_counted": 4,
              "prior_work": 3, "prior_work_bib_only": 1,
              "prior_work_content": 1,
              "prior_work_audit": {"full_text_checked": 2,
                                   "engaged": 2, "not_engaged": 0,
                                   "bibliography_only": 1}}
    text = ba.build_funnel_text(TOPIC_P, funnel, True,
                                word_count=10, word_target=(5, 15))
    check("(k5) funnel carries the content-pool + audit lines",
          "استخر محتوای پیشینه (§70)" in text
          and "حسابرسی پیشینه (§70)" in text, text)


# ----------------------------------------------------------------------
# (m) search-evidence line + comparison matrix rendering
#     (conditions 3 + 4)
# ----------------------------------------------------------------------

def test_block_evidence_and_matrix():
    nov = {"claim": "C", "closest": "K", "boundary": "B",
           "status": "novel", "credibility": "unconfirmed",
           "matrix": [{"source": "K", "data": "same",
                       "method": "different", "output": "none",
                       "scope": "part", "difference": "the model"}]}
    ev = {"verified": 41, "relevant": 12, "prior_work": 9,
          "full_text_checked": 6, "bib_only": 3}
    block = ba.format_novelty_block(nov, "en", evidence=ev)
    check("(m1) block carries the search-evidence line",
          "Search evidence (§70)" in block and "41" in block
          and "does not prove none" in block, block)
    block_fa = ba.format_novelty_block(nov, "fa", evidence=ev)
    check("(m2) Persian block carries the evidence line",
          "شواهد جست‌وجو (§70)" in block_fa, block_fa)
    mx = ba.format_comparison_matrix(nov, "en")
    check("(m3) matrix renders heading, columns and the row",
          "## Prior-work comparison (§70)" in mx
          and "Same data?" in mx and "What remains new" in mx
          and "| K | same | different | none | part | the model |"
          in mx, mx)
    check("(m4) no matrix rows -> no matrix section (the absence "
          "is visible via the unconfirmed block)",
          ba.format_comparison_matrix(
              {"matrix": []}, "en") == "")


# ----------------------------------------------------------------------
# (n) the §33 novelty reviewer (condition 5 + the audit rule)
# ----------------------------------------------------------------------

def test_review_novelty():
    pw, sources = _pw_sources()
    counted = [sources[0]]
    base_ctx = {"field": FIELD_P, "topic": TOPIC_P,
                "language": "en", "article_type": "isi",
                "title": "T", "sources": sources,
                "word_target": (100, 300), "api_key": "k",
                "model": "m"}
    keep = ba.call_llm
    try:
        def boom(*a, **k):
            raise AssertionError("no brain call expected")
        ba.call_llm = boom
        v = ba.review_novelty("# T\n\n## Body\n\ntext [1].\n",
                              counted, dict(base_ctx))
        check("(n1) no prior-work set -> available, issue-free, "
              "NO brain call (never a no-verdict block)",
              v["available"] and v["ok"] and v["issues"] == [],
              str(v))
        ctx = dict(base_ctx, prior_work=pw,
                   novelty={"claim": "C", "closest": "K",
                            "boundary": "B", "status": "novel",
                            "credibility": "unconfirmed"})
        ba.call_llm = lambda *a, **k: json.dumps({"issues": []})
        art = _article_with_refs([1, 2], sources)
        v = ba.review_novelty(art, counted, ctx)
        hits = [i for i in v["issues"]
                if "novelty-boundary statement is missing"
                in i["problem"]]
        check("(n2) missing novelty block -> ONE fixable novelty "
              "issue carrying the assessed statement",
              len(hits) == 1 and hits[0]["severity"] == "fixable"
              and hits[0]["role"] == "novelty", str(v["issues"]))
        art2 = ("# T\n\n## Novelty boundary (§70)\n\n- Claim: C\n"
                "\n## Body\n\nno citations here\n\n## References\n\n"
                + "\n".join(ba.format_reference(i, s)
                            for i, s in enumerate(sources, 1))
                + "\n")
        v = ba.review_novelty(art2, counted, ctx)
        hits = [i for i in v["issues"] if "AUDIT (§70)" in
                i["problem"]]
        check("(n3) full-text-checked source engaged nowhere -> "
              "fixable AUDIT issue",
              len(hits) == 1 and hits[0]["severity"] == "fixable",
              str(v["issues"]))
        art3 = art2.replace("no citations here",
                            "the data of [1] are used")
        ba.call_llm = lambda *a, **k: json.dumps({"issues": [
            {"where": "claim",
             "problem": "already done: the closest work does it",
             "severity": "fatal"}]})
        v = ba.review_novelty(art3, counted, ctx)
        check("(n4) reviewer CAN reject: 'already done' arrives "
              "as a FATAL novelty issue",
              len(v["issues"]) == 1
              and v["issues"][0]["severity"] == "fatal"
              and v["issues"][0]["problem"].startswith(
                  "already done"), str(v["issues"]))
        ba.call_llm = lambda *a, **k: json.dumps({"issues": []})
        v = ba.review_novelty(art3, counted, ctx)
        check("(n5) engaged source + block present + clean LLM "
              "verdict -> no issues",
              v["available"] and v["issues"] == [], str(v))
    finally:
        ba.call_llm = keep


# ----------------------------------------------------------------------
# (o) the deterministic prior-work audit (consultant addition)
# ----------------------------------------------------------------------

def test_prior_work_audit():
    a = src("Alpha stomatal data study", doi="10.1000/a1",
            abstract="a" * 60, excerpt="Alpha passages.")
    b = src("Beta stomatal method study", doi="10.1000/b1",
            abstract="b" * 60, excerpt="Beta passages.")
    c = src("Gamma closest prior work", doi="10.1000/c1",
            abstract="c" * 60, excerpt="Gamma passages.")
    d = plant_bibonly()
    e = src("Epsilon unengaged study", doi="10.1000/e1",
            abstract="e" * 60, excerpt="Epsilon passages.")
    pw = ba.build_prior_work_set([a, c, e], [b, d])
    sources = [a, b, c, d, e]  # write_sources order (numbers 1-5)
    # rebuild the set in write order for the audit call
    pw = [a, b, c, d, e]
    refs = "\n".join(ba.format_reference(i, s)
                     for i, s in enumerate(sources, 1))
    article = (
        "# T\n\n## Prior Work\n\nAlpha [1] supplies the data; "
        "Beta [2] is positioned against.\n\n## Body\n\nThe method "
        "of [1] is reused.\n\n## Novelty boundary (§70)\n\n"
        "- Claim: X\n- Closest direct prior work: Gamma closest "
        "prior work\n\n## References\n\n" + refs + "\n")
    novelty = {"closest": "Gamma closest prior work"}
    audit = ba.build_prior_work_audit(article, sources, pw,
                                      novelty)
    by_title = {en["title"]: en for en in audit["entries"]}
    ea = by_title["Alpha stomatal data study"]
    check("(o1) audit: Alpha used in prior-work section + body "
          "(critique + data/method/result)",
          ea["places"] == ["prior-work section", "body"]
          and ea["use_types"] == ["critique", "data/method/result"]
          and ea["status"] == "full-text checked", str(ea))
    eb = by_title["Beta stomatal method study"]
    check("(o2) audit: Beta (content pool) engaged as critique",
          eb["places"] == ["prior-work section"]
          and eb["use_types"] == ["critique"], str(eb))
    ec = by_title["Gamma closest prior work"]
    check("(o3) audit: the closest work's use type is "
          "novelty-boundary",
          ec["places"] == ["novelty block"]
          and ec["use_types"] == ["novelty-boundary"], str(ec))
    ed = by_title[d["title"]]
    check("(o4) audit: bibliography-only entry carries its "
          "limitation, never a content use",
          ed["status"].startswith("bibliographic record")
          and ed["limitation"] == "full text not obtained"
          and ed["places"] == [], str(ed))
    check("(o5) audit counts: 4 checked, 3 engaged, Epsilon "
          "named not-engaged",
          audit["full_text_checked"] == 4
          and audit["engaged"] == 3
          and audit["not_engaged"] == ["Epsilon unengaged study"]
          and audit["bibliography_only"] == 1
          and audit["content_pool"] == 1, str(audit))


# ----------------------------------------------------------------------
# (j) iranjournals.ir portal: registry-only, routes recorded
# ----------------------------------------------------------------------

def test_iranjournals_registry():
    base = os.path.dirname(os.path.abspath(__file__))
    raw = json.loads(open(os.path.join(
        base, "asian_journals_registry.json"),
        encoding="utf-8").read())
    by_url = {e["url"]: e for e in raw}
    portal = by_url.get("https://iranjournals.ir")
    check("(j1) iranjournals.ir portal is registered",
          portal is not None)
    if portal:
        check("(j2) portal is registry-only (no api route)",
              portal["api"] == "" and portal["type"] == "platform",
              str(portal.get("api")))
        check("(j3) routes tried + reason recorded in notes",
              "REGISTRY-ONLY" in portal["notes"]
              and "Sinaweb" in portal["notes"]
              and "journals.msrt.ir" in portal["notes"],
              portal["notes"][:120])
    bases = {b for b, _ in ba._IRAN_MATH_JOURNALS}
    check("(j4) the Keramati journal (mct.iranjournals.ir) is "
          "live-wired in §37",
          "https://mct.iranjournals.ir" in bases, str(bases))


def main():
    test_prior_work_set()
    test_assess_novelty()
    test_novelty_block()
    test_bibliography_completeness()
    test_review_citations_surfaces_completeness()
    test_screen_relevant_sources()
    test_card_and_funnel()
    test_checkpoint_prior_work()
    test_main_novelty_outcomes()
    test_tiers_and_statuses()
    test_block_evidence_and_matrix()
    test_review_novelty()
    test_prior_work_audit()
    test_iranjournals_registry()
    print(f"\n{PASS[0]}/{PASS[0] + FAIL[0]} checks passed")
    return 1 if FAIL[0] else 0


if __name__ == "__main__":
    sys.exit(main())
