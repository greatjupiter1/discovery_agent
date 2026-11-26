#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline tests for the §33 multi-robot review layer (Jupiter
2026-11-26: «بساز کلا» — build the 6 missing roles): five component
reviewers (citations, numbers, claims, structure, language) + the
content supervisor, running AFTER a complete draft and BEFORE
channel delivery.

Fully offline: ba.call_llm is replaced by a scripted fake brain;
ba.tg_call is captured. Covered:
  (a) the deterministic citation check catches an invented
      reference and an out-of-range in-text citation;
  (b) the deterministic numbers check catches a number that appears
      in no counted source passage, and accepts a number that is
      arithmetically derivable from passage numbers;
  (c) supervisor aggregation: a fatal issue fails at once (no
      repair round);
  (d) a fixable issue goes through the writer repair loop and the
      article then passes;
  (e) fixable issues that survive REVIEW_MAX_REPAIR_ROUNDS fail
      closed;
  (f) a reviewer whose brain calls all fail gives NO verdict and
      BLOCKS publication;
  (g) a clean article passes with 0 repair rounds;
  (h) an unsupported CENTRAL claim (claims reviewer) is fatal;
  (i) the language script check catches a wrong-language draft;
  (j) the funnel gains the §33 lines and the itemised review
      report builds;
  (k) main() integration: a review-blocked draft is NOT delivered
      and the run fails closed.
No network, no real keys.
"""

import json
import os
import sys

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
# Fixtures: two counted sources with passages, and articles built on
# them. The source numbers are 6335.7 (km) and 34 (arc-minutes); the
# derivable article number is 6370.7 = 6335.7 + 34 + 1... no — the
# derivable one used below is 12671.4 = 6335.7 * 2 (a product pair
# derivation the deterministic check must accept).
# ----------------------------------------------------------------------

def make_sources():
    return [
        {"title": "Biruni's Measurement of the Earth",
         "doi": "10.1000/xyz123", "url": "https://example.org/a",
         "source_type": "article", "index": "test",
         "abstract": "A study of the Nandana measurement.",
         "fulltext_excerpt": (
             "At Nandana the dip of the horizon was measured as "
             "34 arc-minutes. The computed radius of the Earth was "
             "6335.7 km, close to the modern value."),
         "passages": True, "fulltext": "ia-passages"},
        {"title": "The Formation of Empirical Methods",
         "doi": "", "url": "https://example.org/b",
         "source_type": "article", "index": "test",
         "abstract": "Methods in medieval astronomy.",
         "fulltext_excerpt": (
             "The method relied on a mountain of known height and "
             "careful observation of the horizon."),
         "passages": True, "fulltext": "index-fulltext"},
    ]


def make_context(**over):
    ctx = {"field": "Astronomy and mathematics in the works of "
                    "Abu Rayhan al-Biruni",
           "topic": "Biruni's method for determining the radius "
                    "of the Earth",
           "language": "en", "article_type": "isi",
           "title": "Biruni's Horizon-Dip Method for the Earth's "
                    "Radius: A Reconstruction",
           "sources": make_sources(), "word_target": (100, 300),
           "api_key": "k", "model": "m"}
    ctx.update(over)
    return ctx


def clean_article():
    return (
        "# Biruni's Horizon-Dip Method for the Earth's Radius: "
        "A Reconstruction\n\n"
        "## Abstract\n\n"
        "Background: the horizon-dip method. Objective: to "
        "reconstruct the computation. Method: geometric analysis "
        "of the reported observations. Findings: the radius "
        "follows from the dip angle. Conclusion: the method is "
        "sound [1].\n\n"
        "## Keywords\n\n"
        "Biruni; Earth radius; horizon dip\n\n"
        "## Introduction\n\n"
        "The measurement at Nandana used the dip of the horizon "
        "[1]. The empirical tradition behind it is discussed in "
        "the literature [2].\n\n"
        "## The Observation\n\n"
        "The dip of the horizon was measured as 34 arc-minutes "
        "[1].\n\n"
        "## The Computation\n\n"
        "The computed radius of the Earth was 6335.7 km [1]. "
        "Doubling the value gives 12671.4 km as a scale check.\n\n"
        "## Discussion\n\n"
        "The result is close to the modern value and shows the "
        "strength of the method [1][2].\n\n"
        "## Conclusion\n\n"
        "The horizon-dip method yields a reliable radius.\n\n"
        "## References\n\n"
        "[1] Biruni's Measurement of the Earth. "
        "https://doi.org/10.1000/xyz123\n"
        "[2] The Formation of Empirical Methods. "
        "https://example.org/b\n")


# ----------------------------------------------------------------------
# Scripted fake brain: answers by prompt marker. Reviewer prompts
# carry their role line; the writer repair prompt carries "FLAGGED
# PROBLEMS"; the claims reviewer is answered from the SENTENCE list
# (all supported unless SCRIPT says otherwise).
# ----------------------------------------------------------------------

SCRIPT = {"citations": [], "numbers": [], "structure": [],
          "language": [], "supervisor": [],
          "claims_supported": True, "claims_central": False,
          "fail_roles": set(), "repair": None}


def fake_llm(prompt, api_key, model, temperature=0.4,
             json_mode=False, provider=None, max_tokens=None):
    def role_of(marker):
        return marker in prompt

    if "FLAGGED PROBLEMS" in prompt:
        out = SCRIPT["repair"]
        if out is None:
            raise ba.Fail("no repair scripted")
        return out
    if "LANGUAGE REVIEWER" in prompt \
            and "reliable radius [1]" in prompt:
        # The scripted terminology fix is present in this draft —
        # a real reviewer judges the CURRENT text and stops
        # flagging it.
        return json.dumps({"issues": []})
    for marker, key in (("CITATIONS REVIEWER", "citations"),
                        ("NUMBERS REVIEWER", "numbers"),
                        ("STRUCTURE REVIEWER", "structure"),
                        ("LANGUAGE REVIEWER", "language"),
                        ("SUPERVISOR of a scholarly", "supervisor")):
        if role_of(marker):
            if key in SCRIPT["fail_roles"]:
                raise ba.Fail("brain down (scripted)")
            return json.dumps({"issues": SCRIPT[key]})
    if "CLAIMS REVIEWER" in prompt:
        if "claims" in SCRIPT["fail_roles"]:
            raise ba.Fail("brain down (scripted)")
        verdicts = []
        for line in prompt.splitlines():
            if line.startswith("SENTENCE "):
                i = int(line.split(":", 1)[0].split()[1])
                v = {"i": i,
                     "supported": SCRIPT["claims_supported"],
                     "central": SCRIPT["claims_central"]}
                if not SCRIPT["claims_supported"] \
                        and SCRIPT.get("claims_evidenced", True):
                    # §45 evidence anchors: a verbatim span of the
                    # flagged sentence + the no-coverage statement.
                    sent = line.split(": ", 1)[1] \
                        if ": " in line else ""
                    v["quote"] = sent[:50]
                    v["evidence"] = "no passage covers this claim"
                verdicts.append(v)
        return json.dumps({"verdicts": verdicts})
    raise AssertionError("unexpected prompt: " + prompt[:120])


def reset_script(**over):
    SCRIPT.update({"citations": [], "numbers": [], "structure": [],
                   "language": [], "supervisor": [],
                   "claims_supported": True, "claims_central": False,
                   "claims_evidenced": True,
                   "fail_roles": set(), "repair": None})
    SCRIPT.update(over)


ba.call_llm = fake_llm
SENT = []
ba.tg_call = lambda method, token, payload: (
    SENT.append((method, payload)) or {"message_id": 1})


# ----------------------------------------------------------------------
# (a) deterministic citation check
# ----------------------------------------------------------------------

def test_citation_code():
    ctx = make_context()
    srcs = ctx["sources"]
    bad = clean_article().replace(
        "[2] The Formation of Empirical Methods.",
        "[2] A Completely Invented Treatise on Nothing.")
    issues = ba._citations_code_issues(bad, srcs)
    check("(a1) invented reference caught",
          any(i["severity"] == "fatal" for i in issues),
          str(issues))
    bad2 = clean_article().replace(
        "the literature [2].", "the literature [9].")
    issues2 = ba._citations_code_issues(bad2, srcs)
    check("(a2) out-of-range in-text citation caught",
          any(i["severity"] == "fatal" for i in issues2),
          str(issues2))
    good = ba._citations_code_issues(clean_article(), srcs)
    check("(a3) clean article has no citation code issues",
          good == [], str(good))


# ----------------------------------------------------------------------
# (b) deterministic numbers check
# ----------------------------------------------------------------------

def test_numbers_code():
    counted = make_sources()
    bad = clean_article().replace(
        "The computed radius of the Earth was 6335.7 km [1].",
        "The computed radius of the Earth was 9876.5 km [1].")
    issues = ba._numbers_code_issues(bad, counted)
    check("(b1) number absent from passages caught",
          len(issues) == 1 and issues[0]["severity"] == "fixable",
          str(issues))
    good = ba._numbers_code_issues(clean_article(), counted)
    check("(b2) passage + derivable numbers pass (12671.4 = "
          "6335.7*2)", good == [], str(good))


# ----------------------------------------------------------------------
# (c)-(g) supervisor aggregation through run_review_layer
# ----------------------------------------------------------------------

def test_fatal_fails_at_once():
    reset_script(supervisor=[{"where": "abstract vs conclusion",
                              "problem": "the abstract and the "
                                         "conclusion contradict",
                              "severity": "fatal"}])
    res = ba.run_review_layer(clean_article(), make_sources(),
                              make_context())
    check("(c1) fatal issue fails the layer", not res["ok"]
          and res["verdict"] == "fail", str(res.get("verdict")))
    check("(c2) fatal means no repair round was used",
          res["rounds"] == 0, str(res["rounds"]))


def test_fixable_repair_loop():
    art = clean_article()
    fixed = art.replace("reliable radius.", "reliable radius [1].")
    reset_script(
        language=[{"where": "Conclusion",
                   "problem": "the term 'radius' is used "
                              "inconsistently",
                   "severity": "fixable"}],
        repair=fixed)
    res = ba.run_review_layer(art, make_sources(), make_context())
    check("(d1) fixable issue repaired -> pass", res["ok"]
          and res["verdict"] == "pass", str(res.get("verdict")))
    check("(d2) exactly one repair round", res["rounds"] == 1,
          str(res["rounds"]))
    check("(d3) issue counted found and fixed",
          res["issues_found"] == 1 and res["issues_fixed"] == 1,
          str((res["issues_found"], res["issues_fixed"])))
    check("(d4) the repaired article is returned",
          res["article"] == fixed.strip())


def test_unfixable_fails_after_max_rounds():
    reset_script(
        language=[{"where": "Conclusion",
                   "problem": "persistent terminology problem",
                   "severity": "fixable"}],
        repair=clean_article())
    res = ba.run_review_layer(clean_article(), make_sources(),
                              make_context())
    check("(e1) unresolved fixable fails closed", not res["ok"]
          and res["verdict"] == "fail", str(res.get("verdict")))
    check("(e2) exactly REVIEW_MAX_REPAIR_ROUNDS rounds",
          res["rounds"] == ba.REVIEW_MAX_REPAIR_ROUNDS,
          str(res["rounds"]))


def test_reviewer_unavailable_blocks():
    reset_script(fail_roles={"numbers"})
    res = ba.run_review_layer(clean_article(), make_sources(),
                              make_context())
    check("(f1) unavailable reviewer blocks publication",
          not res["ok"] and res["verdict"] == "blocked",
          str(res.get("verdict")))
    check("(f2) the blocked role is named",
          res["blocked_roles"] == ["numbers"],
          str(res["blocked_roles"]))


def test_clean_passes_zero_rounds():
    reset_script()
    res = ba.run_review_layer(clean_article(), make_sources(),
                              make_context())
    check("(g1) clean article passes", res["ok"]
          and res["verdict"] == "pass", str(res.get("verdict")))
    check("(g2) zero rounds, zero issues",
          res["rounds"] == 0 and res["issues_found"] == 0,
          str((res["rounds"], res["issues_found"])))
    check("(g3) all six roles reported ok",
          set(res["roles"]) == {"citations", "numbers", "claims",
                                "structure", "language",
                                "supervisor"}
          and all(v["ok"] for v in res["roles"].values()),
          str(res["roles"].keys()))


def test_unsupported_central_claim_fatal():
    reset_script(claims_supported=False, claims_central=True)
    res = ba.run_review_layer(clean_article(), make_sources(),
                              make_context())
    check("(h1) unsupported central claims are fatal -> fail",
          not res["ok"] and res["verdict"] == "fail",
          str(res.get("verdict")))
    check("(h2) claims role carries the fatals",
          any(i["severity"] == "fatal"
              for i in res["roles"]["claims"]["issues"]),
          str(res["roles"]["claims"]))


def test_language_script_check():
    ctx = make_context(language="fa")
    issues = ba._language_code_issues(clean_article(), ctx)
    check("(i1) English draft for a Persian article is fatal",
          len(issues) == 1 and issues[0]["severity"] == "fatal",
          str(issues))
    ctx_en = make_context(language="en")
    check("(i2) English draft for English passes the script check",
          ba._language_code_issues(clean_article(), ctx_en) == [])


# ----------------------------------------------------------------------
# (j) funnel + report integration
# ----------------------------------------------------------------------

def test_funnel_and_report():
    reset_script()
    res = ba.run_review_layer(clean_article(), make_sources(),
                              make_context())
    funnel = {"candidates": 10, "verified": 8, "with_content": 6,
              "relevant": 5, "counted": 5, "registry_matched": 2,
              "primary_counted": 1, "fulltext_counted": 4}
    text = ba.build_funnel_text("موضوع", funnel, True,
                                word_count=100,
                                word_target=(90, 110), review=res)
    check("(j1) funnel carries the §33 verdict lines",
          "بررسی چندرباتهٔ پس از نگارش (§33)" in text
          and "نتیجهٔ بررسی چندرباته: قبول ✅" in text, text[-400:])
    plain = ba.build_funnel_text("موضوع", funnel, True,
                                 word_count=100,
                                 word_target=(90, 110))
    check("(j2) funnel without a review result is unchanged",
          "§33" not in plain)
    report = ba.build_review_report("موضوع", res)
    check("(j3) itemised review report builds",
          "گزارش بررسی چندرباتهٔ مقاله (§33)" in report
          and "منابع و ارجاع‌ها" in report
          and "ناظر — پیوستگی کل" in report, report[:200])


# ----------------------------------------------------------------------
# (k) main() integration: a blocked draft is never delivered
# ----------------------------------------------------------------------

def test_main_blocks_publication():
    import tempfile
    os.environ["TELEGRAM_BOT_TOKEN"] = "t"
    os.environ["CHAT_ID"] = "-1001"
    os.environ["GEMINI_API_KEY"] = "k"
    os.environ["ARTICLE_CHAT_ID"] = "42"
    counted = [{
        "title": f"S{i}", "doi": "", "url": "u",
        "source_type": "article", "index": "x",
        "abstract": "a", "fulltext_excerpt": "x",
        "passages": True, "fulltext": "ia-passages",
        "verified_via": "crossref", "registry_matched": False,
    } for i in range(5)]
    funnel = {"candidates": 9, "verified": 7, "with_content": 6,
              "relevant": 5, "counted": 5, "registry_matched": 0,
              "primary_counted": 1, "fulltext_counted": 4,
              "drop_language_gate": 0, "drop_irrelevant": 1,
              "drop_no_content": 1, "drop_tradition": 0,
              "drop_disagreement": 0}
    saved = {}

    def fake_fresh(field, topic, language, category, article_type,
                   mode, api_key, model, tg_token):
        return {"counted": counted, "funnel": funnel,
                "disagreement_titles": [], "queries": ["q"],
                "ft_keywords": ["radius"], "verified_count": 7,
                "gate": {"ok": True, "counted": 5,
                         "fulltext_passages": 4, "primary": 1},
                "adaptation": None}

    def fake_write(field, topic, language, sources, api_key, model,
                   guide="", word_target=None):
        return ("T", clean_article(),
                {"title_candidates": [], "next_topics": [],
                 "word_count": 120})

    keep = {n: getattr(ba, n) for n in (
        "_fresh_screening", "write_article", "run_review_layer",
        "send_funnel_dm", "send_detailed_source_report_dm",
        "send_review_report_dm", "send_suggestions_dm",
        "fetch_youtube_support", "load_state", "save_state",
        "consume_stage_checkpoint", "progress_update",
        "progress_fail")}
    ba._fresh_screening = fake_fresh
    ba.write_article = fake_write
    ba.run_review_layer = lambda a, c, ctx: {
        "ok": False, "verdict": "fail", "rounds": 0,
        "issues_found": 1, "issues_fixed": 0, "roles": {},
        "blocked_roles": [], "issue_records": [], "article": a}
    ba.send_funnel_dm = lambda *a, **k: True
    ba.send_detailed_source_report_dm = lambda *a, **k: True
    ba.send_review_report_dm = lambda *a, **k: True
    ba.send_suggestions_dm = lambda *a, **k: None
    ba.fetch_youtube_support = lambda q, k: []
    ba.load_state = lambda: {}
    ba.save_state = lambda s: saved.update(s)
    ba.consume_stage_checkpoint = lambda *a, **k: None
    ba.progress_update = lambda *a, **k: None
    ba.progress_fail = lambda *a, **k: None
    del SENT[:]
    req = {"field": "F", "topic": "T", "language": "en",
           "mode": "review", "category": "intl",
           "article_type": "isi", "chat_id": "42"}
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "req.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(req, f)
        sys.argv = ["build_article.py", path]
        try:
            ba.main()
            rc = 0
        except ba.Fail:
            rc = -1
    for n, v in keep.items():
        setattr(ba, n, v)
    check("(k1) main() fails closed when the review layer blocks",
          rc == -1, f"rc={rc}")
    check("(k2) nothing was delivered to the channel",
          SENT == [], str(SENT[:2]))
    check("(k3) no production was recorded",
          "productions" not in saved, str(saved.keys()))


def main():
    test_citation_code()
    test_numbers_code()
    test_fatal_fails_at_once()
    test_fixable_repair_loop()
    test_unfixable_fails_after_max_rounds()
    test_reviewer_unavailable_blocks()
    test_clean_passes_zero_rounds()
    test_unsupported_central_claim_fatal()
    test_language_script_check()
    test_funnel_and_report()
    test_main_blocks_publication()
    print(f"\n{PASS[0]}/{PASS[0] + FAIL[0]} checks passed")
    return 1 if FAIL[0] else 0


if __name__ == "__main__":
    sys.exit(main())
