#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline tests for the §71 article-structure template
(Jupiter 2026-11-28; ChatGPT's "Master Manuscript Architecture"
adopted with the Proposal-1 article as the reference sample):

  (a) THREE separate concepts — article_type / publication_
      profile / word_budget — and "ISI" is a profile, not a type;
  (b) size-matches-type: the 6,000–8,000 budget belongs to the
      RESEARCH type only and never leaks into review /
      historical / short forms; budgets are planning ranges
      (drift = warning), a supplied journal limit is FINAL and
      its breach is a destination-adaptation task, never a
      licence to delete scientific content;
  (c) the counting rule (title page, abstract, keywords,
      references and appendices excluded from the main count);
  (d) fixed-core conformance: required sections present and in
      order; a merged Discussion/Limitations/Conclusion heading
      satisfies all three slots; the appendix counts only after
      the References;
  (e) the reference sample (inline skeleton with the Proposal-1
      article's exact headings, plus the real file when present)
      PASSES with zero errors under both its historical and the
      research template — conformance, not a mould;
  (f) §70 over the standard: reviewed-but-uncited sources are
      never flagged for deletion; the two data sets (register /
      bibliography) stay distinct in the template's policy;
  (g) the §33 structure role is gated on the template: legacy
      contexts keep the old behaviour, templated contexts gain
      the deterministic §71 conformance issues.

Fully offline; the structure-role checks use a scripted fake
brain exactly like test_review_robustness.py.
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


GOAL_SAMPLE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "goals",
    "scientific-discovery-agent-multilingual-isi-article-bot",
    "files", "full-article-proposal1-2026-11-27.md")


def sample_skeleton():
    """The reference article's exact heading structure with
    filler body text (the conformance shape, without depending
    on the goals tree)."""
    filler = ("word " * 40).strip()
    return (
        "# Recording Conventions and Computational Paths in "
        "al-Biruni's Two Nandana Earth-Radius Accounts: A "
        "Sexagesimal Interval Reconstruction and Chain Audit\n\n"
        "**Research article**\n\n"
        "## Abstract\n\n"
        "**Background.** " + filler + "\n\n"
        "**Aim.** " + filler + "\n\n"
        "**Method.** " + filler + "\n\n"
        "**Results.** " + filler + "\n\n"
        "**Conclusion.** " + filler + "\n\n"
        "**Keywords:** al-Biruni; Nandana; Earth radius; "
        "sexagesimal; interval reconstruction\n\n"
        "## 1. Introduction\n\n" + filler + "\n\n"
        "## 2. Prior work and the novelty boundary\n\n"
        + filler + "\n\n"
        "## 3. Layer 1 — The texts: collation of the two "
        "accounts\n\n" + filler + "\n\n"
        "## 4. Layer 2 — Geometric reconstruction of the "
        "depression-angle method\n\n" + filler + "\n\n"
        "## 5. Layer 3 — Interval reconstruction and separation "
        "of the two accounts\n\n" + filler + "\n\n"
        "## 6. Discussion, limitations, and conclusion\n\n"
        + filler + "\n\n"
        "## References\n\n"
        "[1] A Source. A Study. https://example.org/1\n\n"
        "## Methods and reproducibility note\n\n" + filler
        + "\n")


# ----------------------------------------------------------------------
# (a)+(b) the three concepts and the size law
# ----------------------------------------------------------------------

def test_three_concepts():
    t = ba.article_template_for("isi")
    check("(a1) default isi profile resolves to the research "
          "type", t["article_type"] == "research",
          t["article_type"])
    check("(a2) profile is carried separately",
          t["publication_profile"] == "isi")
    check("(a3) research type budget is 6000–8000",
          t["word_budget"]["type"] == (6000, 8000),
          str(t["word_budget"]))
    check("(a4) profile target is a separate layer (isi "
          "6000–8000 here)",
          t["word_budget"]["profile"] == (6000, 8000))
    check("(a5) no journal limit by default",
          t["word_budget"]["journal"] is None)
    t = ba.article_template_for("isi", architecture="historical")
    check("(a6) ISI is not a type: an isi-profile historical "
          "article takes the historical type + budget",
          t["article_type"] == "historical"
          and t["word_budget"]["type"] == (6000, 9000)
          and t["publication_profile"] == "isi",
          str(t["word_budget"]))
    t = ba.article_template_for("review")
    check("(a7) the review profile maps to the review type "
          "(8000–10000), not the research budget",
          t["article_type"] == "review"
          and t["word_budget"]["type"] == (8000, 10000))
    t = ba.article_template_for("conf")
    check("(a8) a short-form profile keeps its own small "
          "profile target while the type budget stays the "
          "research type's — the layers never merge",
          t["word_budget"]["profile"] == (2500, 3500)
          and t["word_budget"]["type"] == (6000, 8000))
    keep = os.environ.get("ARTICLE_ARCHITECTURE")
    keep_j = os.environ.get("ARTICLE_JOURNAL_WORD_LIMIT")
    try:
        os.environ["ARTICLE_ARCHITECTURE"] = "historical"
        os.environ["ARTICLE_JOURNAL_WORD_LIMIT"] = "5000"
        t = ba.article_template_for("isi")
        check("(a9) ARTICLE_ARCHITECTURE / "
              "ARTICLE_JOURNAL_WORD_LIMIT envs are honoured",
              t["article_type"] == "historical"
              and t["word_budget"]["journal"] == 5000,
              str(t["word_budget"]))
    finally:
        if keep is None:
            os.environ.pop("ARTICLE_ARCHITECTURE", None)
        else:
            os.environ["ARTICLE_ARCHITECTURE"] = keep
        if keep_j is None:
            os.environ.pop("ARTICLE_JOURNAL_WORD_LIMIT", None)
        else:
            os.environ["ARTICLE_JOURNAL_WORD_LIMIT"] = keep_j
    guide = ba.render_structure_guide(
        ba.article_template_for("isi"))
    check("(a10) the writer guide names the type, the core and "
          "the research budget",
          "ARTICLE STRUCTURE (§71)" in guide
          and "research" in guide and "6000–8000" in guide)
    guide = ba.render_structure_guide(
        ba.article_template_for("review"))
    check("(a11) the review guide carries the review budget, "
          "not the research one",
          "8000–10000" in guide and "6000–8000" not in guide)


# ----------------------------------------------------------------------
# (c)+(d) counting rule + fixed-core conformance
# ----------------------------------------------------------------------

def test_counting_and_conformance():
    art = (
        "# A Title Here\n\n"
        "## Abstract\n\n" + " ".join(["a"] * 10) + "\n\n"
        "**Keywords:** one; two\n\n"
        "## Introduction\n\n" + " ".join(["b"] * 100) + "\n\n"
        "## Prior work and the novelty boundary\n\n"
        + " ".join(["c"] * 50) + "\n\n"
        "## Methods\n\n" + " ".join(["d"] * 50) + "\n\n"
        "## Results\n\n" + " ".join(["e"] * 50) + "\n\n"
        "## Discussion, limitations, and conclusion\n\n"
        + " ".join(["f"] * 50) + "\n\n"
        "## References\n\n" + " ".join(["g"] * 50) + "\n\n"
        "## Appendix\n\n" + " ".join(["h"] * 40) + "\n")
    t = ba.article_template_for("isi")
    v = ba.validate_article_structure(art, t)
    check("(c1) the counting rule excludes title page, "
          "abstract, keywords, references and appendix from "
          "the main count (100+50+50+50+50 = 300)",
          v["counts"]["main_words"] == 300,
          str(v["counts"]))
    check("(c2) the abstract is counted separately",
          v["counts"]["abstract_words"] == 10,
          str(v["counts"]))
    check("(d1) a merged Discussion/Limitations/Conclusion "
          "heading satisfies all three slots — no errors",
          v["ok"] and not v["errors"], str(v["errors"]))
    check("(d2) size drift against the type budget is a "
          "warning, never an error",
          v["ok"] and any("type budget" in w
                          for w in v["warnings"]),
          str(v["warnings"]))
    broken = art.replace(
        "## Discussion, limitations, and conclusion\n\n"
        + " ".join(["f"] * 50) + "\n\n", "")
    v = ba.validate_article_structure(broken, t)
    check("(d3) a missing required section is an error naming "
          "the slot",
          not v["ok"] and any(
              e["slot"] in ("discussion", "conclusion")
              for e in v["errors"]), str(v["errors"]))
    reordered = art.replace("## Introduction", "## References2") \
        .replace("## References\n", "## Introduction\n") \
        .replace("## References2", "## References\n")
    v = ba.validate_article_structure(reordered, t)
    check("(d4) sections out of order are an error",
          not v["ok"] and any("out of order" in e["problem"]
                              for e in v["errors"]),
          str(v["errors"]))
    early_appendix = art.replace(
        "## Appendix\n\n" + " ".join(["h"] * 40) + "\n",
        "").replace("## Results",
                    "## Appendix\n\n" + " ".join(["h"] * 40)
                    + "\n\n## Results")
    v = ba.validate_article_structure(early_appendix, t)
    check("(d5) an appendix heading BEFORE the References does "
          "not satisfy the appendix slot (it stays optional, "
          "so still no error) — and nothing else breaks",
          v["ok"] and "appendix" not in v["found"],
          str(v["found"]))


def test_journal_limit():
    t = ba.article_template_for("isi", journal_word_limit=100)
    v = ba.validate_article_structure(sample_skeleton(), t)
    errs = [e for e in v["errors"] if e["slot"] == "word_budget"]
    check("(b1) a supplied journal limit is FINAL: breaching "
          "it is an error",
          len(errs) == 1, str(v["errors"]))
    check("(b2) the breach is framed as destination "
          "adaptation — never as deleting content",
          errs and "destination adaptation" in errs[0]["problem"]
          and "never by deleting" in errs[0]["problem"],
          errs[0]["problem"] if errs else "")
    t = ba.article_template_for("isi")
    v = ba.validate_article_structure(sample_skeleton(), t)
    check("(b3) without a journal limit the same text has no "
          "word-budget error",
          not any(e["slot"] == "word_budget"
                  for e in v["errors"]), str(v["errors"]))


# ----------------------------------------------------------------------
# (e) the reference sample conformance
# ----------------------------------------------------------------------

def test_reference_sample():
    skel = sample_skeleton()
    for arch in ("historical", "research"):
        t = ba.article_template_for("isi", architecture=arch)
        v = ba.validate_article_structure(skel, t)
        check(f"(e1-{arch}) the reference skeleton passes the "
              f"{arch} template with zero errors",
              v["ok"] and not v["errors"], str(v["errors"]))
        check(f"(e2-{arch}) every required slot is found",
              all(s["slot"] in v["found"]
                  for s in t["slots"] if s["required"]),
              str(v["found"]))
    if os.path.exists(GOAL_SAMPLE):
        real = open(GOAL_SAMPLE, encoding="utf-8").read()
        t = ba.article_template_for("isi",
                                    architecture="historical")
        v = ba.validate_article_structure(real, t)
        check("(e3) the REAL Proposal-1 article passes the "
              "historical template with zero errors",
              v["ok"] and not v["errors"], str(v["errors"]))
        check("(e4) under the counting rule its main text is "
              "inside the type budget (the raw 8,032 total is "
              "not the budgeted number)",
              6000 <= v["counts"]["main_words"] <= 9000,
              str(v["counts"]["main_words"]))
        check("(e5) its over-budget abstract (a known, "
              "ChatGPT-certified deviation) surfaces as a "
              "warning only",
              any("Abstract" in w for w in v["warnings"]),
              str(v["warnings"]))
    else:
        print("SKIP (e3–e5): the goals-tree sample is not "
              "present in this checkout")


# ----------------------------------------------------------------------
# (f) §70 over the standard
# ----------------------------------------------------------------------

def test_prior_work_policy():
    check("(f1) the template keeps the two §70 data sets "
          "distinct and forbids auto-deletion",
          "REGISTER" in ba.PRIOR_WORK_REGISTER_POLICY
          and "BIBLIOGRAPHY" in ba.PRIOR_WORK_REGISTER_POLICY
          and "never auto-deleted"
          in ba.PRIOR_WORK_REGISTER_POLICY)
    art = sample_skeleton().replace(
        "[1] A Source. A Study. https://example.org/1",
        "[1] A Source. A Study. https://example.org/1\n"
        "[2] An Uncited Reviewed Source. "
        "https://example.org/2")
    t = ba.article_template_for("isi", architecture="historical")
    v = ba.validate_article_structure(art, t)
    check("(f2) validation never flags a reviewed-but-uncited "
          "bibliography entry",
          not any("uncited" in e["problem"]
                  or "never cited" in e["problem"]
                  for e in v["errors"]), str(v["errors"]))
    check("(f3) the QC checklist has exactly 17 items and its "
          "last item is the §70 no-deletion rule",
          len(ba.ARTICLE_QC_CHECKLIST) == 17
          and "never" in ba.ARTICLE_QC_CHECKLIST[-1]
          or "silently deleted" in ba.ARTICLE_QC_CHECKLIST[-1],
          str(len(ba.ARTICLE_QC_CHECKLIST)))


# ----------------------------------------------------------------------
# (g) §33 structure-role gating
# ----------------------------------------------------------------------

def _fake_llm(prompt, api_key, model, temperature=0.4,
              json_mode=False, provider=None, max_tokens=None):
    return json.dumps({"issues": []})


def test_structure_role_gating():
    keep = (ba.call_llm, ba.llm_chain, ba._brain_key_available)
    ba.call_llm = _fake_llm
    ba.llm_chain = lambda: ["glm"]
    ba._brain_key_available = lambda name, key: True
    try:
        art = sample_skeleton()
        ctx = {"field": "F", "topic": "T", "language": "en",
               "article_type": "isi", "title": "T",
               "sources": [], "word_target": (10, 100000),
               "api_key": "k", "model": "m"}
        verdict = ba.review_structure(art, [], ctx)
        check("(g1) legacy context (no template): the role "
              "behaves as before — available, no §71 issues",
              verdict["available"] and not any(
                  "§71" in i["problem"]
                  for i in verdict["issues"]),
              str(verdict["issues"])[:200])
        ctx2 = dict(ctx)
        ctx2["structure_template"] = ba.article_template_for(
            "isi", architecture="historical")
        verdict = ba.review_structure(art, [], ctx2)
        check("(g2) templated context + conforming article: "
              "still no §71 issues",
              verdict["available"] and not any(
                  "§71" in i["problem"]
                  for i in verdict["issues"]),
              str(verdict["issues"])[:200])
        broken = art.replace(
            "## 6. Discussion, limitations, and conclusion",
            "## 6. Discussion")
        verdict = ba.review_structure(broken, [], ctx2)
        check("(g3) templated context + a missing core "
              "section: a FATAL §71 issue is raised",
              any("§71" in i["problem"]
                  and i["severity"] == "fatal"
                  for i in verdict["issues"]),
              str(verdict["issues"])[:300])
    finally:
        ba.call_llm, ba.llm_chain, ba._brain_key_available = keep


def main():
    test_three_concepts()
    test_counting_and_conformance()
    test_journal_limit()
    test_reference_sample()
    test_prior_work_policy()
    test_structure_role_gating()
    print(f"\n{PASS[0]}/{PASS[0] + FAIL[0]} checks passed")
    return 1 if FAIL[0] else 0


if __name__ == "__main__":
    sys.exit(main())
