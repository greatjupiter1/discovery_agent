#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline tests for §44 (Jupiter 2026-11-26: «میتونه چند مغز
همزمان بنویسه» — «بعد نوشتن دوباره بازبینی هر دو»): parallel
section writing + post-writing double review.

Covered:
  (a) with >= 2 keyed brains the sections are written in
      parallel, rotating through the chain (GLM first): front
      matter on the chain head, body sections alternating;
  (b) a body writer sees ONLY its own section's assigned sources
      (original numbers) — and the planner's unassigned source is
      swept into the smallest section;
  (c) a section whose brain fails is retried by the next brain;
  (d) a section failing on every brain fails the write closed;
  (e) with only one keyed brain the legacy single-brain path runs
      unchanged (no plan call, unpinned part calls);
  (f) a failed / unusable section plan falls back to the
      single-brain path;
  (g) the unification pass runs on the chain's main brain and its
      prompt carries the no-new-claims constraint; unusable
      unification output returns None;
  (h) main() integration: for a parallel production the §33 layer
      is invoked TWICE — on the assembled draft and again on the
      unified text — and a failed second review or a failed
      unification publishes nothing;
  (i) invariants: the fail-closed gate constants, the §33 repair
      budget and roles, and the §43 scope default are unchanged;
  (j) the section-plan validator: range/dedup/sweep/minimums.
Fully offline: ba.call_llm is a scripted fake; no real keys.
"""

import json
import os
import re
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
# Environment control: the brain pool is read from env at call time.
# ----------------------------------------------------------------------

MANAGED = ["LLM_CHAIN", "LLM_PROVIDER", "GLM_API_KEY",
           "GEMINI_API_KEY", "GH_MODELS_TOKEN", "GITHUB_TOKEN",
           "GROQ_API_KEY", "OPENROUTER_API_KEY",
           "CLOUDFLARE_API_TOKEN", "CF_ACCOUNT_ID",
           "SAMBANOVA_API_KEY", "DASHSCOPE_API_KEY",
           "MODELSCOPE_API_KEY", "SILICONFLOW_API_KEY",
           "SEARCH_SCOPE", "TELEGRAM_BOT_TOKEN", "CHAT_ID",
           "ARTICLE_CHAT_ID", "FIELD", "TOPIC", "LANGUAGE",
           "MODE", "ARTICLE_CATEGORY", "ARTICLE_TYPE", "RESUME"]
SAVED_ENV = {k: os.environ.get(k) for k in MANAGED}


def set_env(**kw):
    for k in MANAGED:
        os.environ.pop(k, None)
    for k, v in kw.items():
        if v is not None:
            os.environ[k] = v


def restore_env():
    for k in MANAGED:
        if SAVED_ENV[k] is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = SAVED_ENV[k]


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

def make_sources():
    def src(n, title, stype="article", passages=True):
        return {"title": title, "authors": ["A Author"],
                "year": 2020, "venue": "Journal of Tests",
                "doi": "", "url": f"https://example.org/{n}",
                "source_type": stype, "index": "test",
                "abstract": f"Abstract of {title}.",
                "fulltext_excerpt": f"Passage text of {title}.",
                "passages": passages, "fulltext": "ia-passages"}
    return [
        src(1, "Alpha Study of Horizon Dip"),
        src(2, "Beta Treatise on Mountain Height"),
        src(3, "Gamma Analysis of Refraction"),
        src(4, "Delta Commentary on the Tahdid", stype="primary"),
        src(5, "Epsilon Survey of Instruments"),
        src(6, "Zeta Notes on Error Bounds"),
    ]


H1 = "The Horizon-Dip Observation"
H2 = "Height and Refraction"
H3 = "Instruments and Error Bounds"

OUTLINE = {"sections": [
    {"heading": H1, "focus": "the dip measurement at Nandana",
     "sources": [1, 4]},
    {"heading": H2, "focus": "height and refraction corrections",
     "sources": [2, 3]},
    {"heading": H3, "focus": "instruments and error bounds",
     "sources": [5]},          # source 6 unassigned -> swept here
]}

FRONT_TEXT = (
    "## Abstract\n\n"
    "Background: the horizon-dip method stands at the centre of "
    "this study. Objective: to reconstruct the determination of "
    "the Earth's radius. Method: geometric analysis of the "
    "reported observations. Findings: the radius follows from "
    "the dip angle and the mountain height. Conclusion: the "
    "method is sound and its error can be bounded [1].\n\n"
    "## Keywords\n\n"
    "Biruni; horizon dip; Earth radius; Tahdid\n\n"
    "## Introduction\n\n"
    "The measurement tradition behind the horizon-dip method is "
    "documented in the primary commentary [4] and surveyed by "
    "the modern studies [1]. This article reconstructs the "
    "method step by step from those sources alone.")

CLOSING_TEXT = (
    "## Discussion\n\n"
    "The combined evidence shows a coherent method whose parts "
    "support one another across the sources [1][4]. The "
    "remaining disagreements concern the size of the refraction "
    "correction rather than the geometry itself.\n\n"
    "## Conclusion\n\n"
    "The horizon-dip determination is reproducible from the "
    "reported inputs, and its accuracy is limited mainly by "
    "the observational error bounds.\n\n"
    "## Limitations\n\n"
    "Only relevant, content-bearing open-access sources were "
    "used; some survive only as excerpts rather than full "
    "texts, which limits the precision of the reconstruction.")

LEGACY_BODY_TEXT = (
    "## The Method\n\n"
    "The legacy single-brain body discusses the dip, the height "
    "and the refraction together in one continuous synthesis "
    "drawn from all of the numbered sources at once [1][2].")

PRIORWORK_TEXT = (
    "## Prior Work\n\n"
    "The primary commentary reports the original observation "
    "and its setting in full [4]. The modern study of the dip "
    "confirms the reported angle against the observational "
    "record [1]. The treatise on the mountain height gives the "
    "measurement procedure step by step [2]. The refraction "
    "analysis compares several reported values against one "
    "another [3]. The instrument survey describes the sighting "
    "devices and their practical limits [5]. The notes on error "
    "bounds quantify how those limits propagate into the final "
    "value [6]. Together these six works fix the evidence base "
    "on which the present reconstruction rests.")

SECTION_TEXTS = {
    H1: ("## " + H1 + "\n\n"         "The dip of the horizon was observed from the mountain "
         "and recorded with care in the primary commentary [4]. "
         "The modern study confirms the reported angle and its "
         "observational setting in detail [1]. Together the two "
         "sources fix the first input of the computation beyond "
         "reasonable doubt for this reconstruction."),
    H2: ("## " + H2 + "\n\n"
         "The mountain height enters the computation directly, "
         "and the treatise on its measurement gives the procedure "
         "step by step [2]. The refraction analysis then corrects "
         "the apparent dip, comparing several reported values "
         "against one another [3]. The correction is small but "
         "systematic, and it shifts the final radius slightly."),
    H3: ("## " + H3 + "\n\n"
         "The instrument survey describes the sighting devices "
         "used for the observations and their practical limits "
         "[5]. The notes on error bounds quantify how those "
         "limits propagate into the final value [6]. Both sources "
         "together show the uncertainty is dominated by the "
         "angular measurement rather than the height."),
}

UNIFIED_OK = (
    "# A Title\n\n"
    "## Abstract\n\n"
    + "Unified wording fills this paragraph with smooth and "
      "consistent prose throughout the whole article text. " * 6
    + "\n\n## References\n\n[1] A Author. Alpha Study. "
      "https://example.org/1\n")


# ----------------------------------------------------------------------
# Scripted fake brain: answers by prompt marker, records every call
# (including the pinned provider), fails where FAIL_ON says so.
# ----------------------------------------------------------------------

CALLS = []
FAIL_ON = set()          # {(heading, provider)} for section writes
OUTLINE_MODE = ["ok"]    # ok | raise | garbage
UNIFY_MODE = ["ok"]      # ok | short | raise


def fake_llm(prompt, api_key, model, temperature=0.4,
             json_mode=False, provider=None):
    CALLS.append({"prompt": prompt, "provider": provider,
                  "json_mode": json_mode})
    if "design the BODY plan" in prompt:
        if OUTLINE_MODE[0] == "raise":
            raise ba.Fail("planner brain down (scripted)")
        if OUTLINE_MODE[0] == "garbage":
            return json.dumps({"sections": []})
        return json.dumps(OUTLINE)
    if "propose SIX narrow" in prompt:
        return json.dumps({
            "candidates": ["Biruni's Horizon-Dip Determination "
                           "of the Earth's Radius at Nandana"],
            "best": 0, "next_topics": ["A", "B", "C"]})
    if "UNIFICATION pass" in prompt:
        if UNIFY_MODE[0] == "raise":
            raise ba.Fail("unifier brain down (scripted)")
        if UNIFY_MODE[0] == "short":
            return "tiny"
        return UNIFIED_OK
    if "Write ONLY your section" in prompt:
        m = re.search(r"headed exactly '## ([^']+)'", prompt)
        heading = m.group(1) if m else "?"
        if (heading, provider) in FAIL_ON:
            raise ba.Fail(f"brain {provider} down (scripted)")
        return SECTION_TEXTS[heading]
    if "headed exactly '## Prior Work'" in prompt:
        # §70: the fixed Prior Work task of the parallel path.
        if ("Prior Work", provider) in FAIL_ON:
            raise ba.Fail(f"brain {provider} down (scripted)")
        return PRIORWORK_TEXT
    if "Write PART 1 of a scholarly article" in prompt:
        return FRONT_TEXT
    if "Write PART 2 of the same article" in prompt:
        return LEGACY_BODY_TEXT
    if "Write PART 3 of the same article" in prompt:
        return CLOSING_TEXT
    raise AssertionError("unexpected prompt: " + prompt[:120])


def reset_fake():
    del CALLS[:]
    FAIL_ON.clear()
    OUTLINE_MODE[0] = "ok"
    UNIFY_MODE[0] = "ok"


ba.call_llm = fake_llm
ba.progress_update = lambda *a, **k: None


def section_calls():
    return [c for c in CALLS if "Write ONLY your section"
            in c["prompt"]]


def heading_provider_map():
    out = {}
    for c in section_calls():
        m = re.search(r"headed exactly '## ([^']+)'", c["prompt"])
        out[m.group(1)] = c["provider"]
    return out


def write(topic="Biruni's Earth radius"):
    return ba.write_article("Astronomy", topic, "en",
                            make_sources(), "k", "m", guide="",
                            word_target=(600, 900))


# ----------------------------------------------------------------------
# (a) parallel rotation
# ----------------------------------------------------------------------

def test_parallel_rotation():
    reset_fake()
    set_env(GLM_API_KEY="g", GEMINI_API_KEY="k")
    title, article, meta = write()
    check("(a1) parallel path taken with two keyed brains",
          meta.get("parallel_writing") is True, str(meta.keys()))
    check("(a2) exactly the three planned body sections written",
          len(section_calls()) == 3, str(len(section_calls())))
    mp = heading_provider_map()
    check("(a3) rotation: sections start on alternating brains "
          "(body0=gemini, body1=glm, body2=gemini)",
          mp == {H1: "gemini", H2: "glm", H3: "gemini"}, str(mp))
    front = [c for c in CALLS
             if "Write PART 1 of a scholarly article"
             in c["prompt"]]
    closing = [c for c in CALLS
               if "Write PART 3 of the same article" in c["prompt"]]
    check("(a4) front matter on the chain head (glm), closing "
          "rotated to glm",
          front and front[0]["provider"] == "glm"
          and closing and closing[0]["provider"] == "glm",
          str([(c["provider"]) for c in front + closing]))
    check("(a5) at least two distinct brains wrote sections",
          len(set(mp.values())) >= 2, str(mp))
    check("(a6) article assembled with every section + references",
          all(f"## {h}" in article for h in (H1, H2, H3))
          and "## References" in article
          and "[6] A Author. Zeta Notes on Error Bounds."
          in article, article[:200])
    check("(a7) meta records which brain wrote which section",
          meta.get("section_brains", {}).get("body1") == "glm",
          str(meta.get("section_brains")))


# ----------------------------------------------------------------------
# (b) per-section source isolation + sweep
# ----------------------------------------------------------------------

def test_source_isolation():
    reset_fake()
    set_env(GLM_API_KEY="g", GEMINI_API_KEY="k")
    write()
    by_head = {}
    for c in section_calls():
        m = re.search(r"headed exactly '## ([^']+)'", c["prompt"])
        by_head[m.group(1)] = c["prompt"]
    p1 = by_head[H1]
    check("(b1) section 1 sees exactly its assigned sources",
          "[1] Alpha Study of Horizon Dip" in p1
          and "[4] Delta Commentary on the Tahdid" in p1
          and "[2] Beta Treatise" not in p1
          and "[3] Gamma Analysis" not in p1
          and "[5] Epsilon Survey" not in p1
          and "[6] Zeta Notes" not in p1)
    p3 = by_head[H3]
    check("(b2) unassigned source 6 swept into the smallest "
          "section (section 3)",
          "[5] Epsilon Survey of Instruments" in p3
          and "[6] Zeta Notes on Error Bounds" in p3
          and "[1] Alpha Study" not in p3)


# ----------------------------------------------------------------------
# (c) per-section fallback, (d) all-brains failure
# ----------------------------------------------------------------------

def test_section_fallback():
    reset_fake()
    set_env(GLM_API_KEY="g", GEMINI_API_KEY="k")
    FAIL_ON.add((H2, "glm"))
    title, article, meta = write()
    mp = heading_provider_map()
    attempts = [c for c in section_calls()
                if f"headed exactly '## {H2}'" in c["prompt"]]
    check("(c1) failed section retried on the next brain and the "
          "article still completes",
          mp.get(H2) == "gemini"
          and meta.get("parallel_writing") is True
          and len(attempts) == 2
          and attempts[0]["provider"] == "glm"
          and attempts[1]["provider"] == "gemini",
          str([(a["provider"]) for a in attempts]))
    check("(c2) the section's text comes from the retry brain",
          meta.get("section_brains", {}).get("body1") == "gemini",
          str(meta.get("section_brains")))


def test_section_all_fail():
    reset_fake()
    set_env(GLM_API_KEY="g", GEMINI_API_KEY="k")
    FAIL_ON.update({(H2, "glm"), (H2, "gemini")})
    try:
        write()
        raised = None
    except ba.Fail as e:
        raised = e
    check("(d1) a section failing on every brain fails the "
          "writing stage closed", raised is not None)


# ----------------------------------------------------------------------
# (e) single-brain fallback, (f) plan failure fallback
# ----------------------------------------------------------------------

def test_single_brain_fallback():
    reset_fake()
    set_env(GEMINI_API_KEY="k")     # glm unkeyed -> one brain
    title, article, meta = write()
    check("(e1) one keyed brain -> legacy path (parallel off)",
          meta.get("parallel_writing") is False, str(meta.keys()))
    check("(e2) no planning call was made",
          not any("design the BODY plan" in c["prompt"]
                  for c in CALLS))
    parts = [c for c in CALLS if "Write PART" in c["prompt"]]
    check("(e3) legacy parts written unpinned through the chain",
          len(parts) == 3
          and all(c["provider"] is None for c in parts),
          str([(c["provider"]) for c in parts]))


def test_plan_failure_fallback():
    for mode in ("raise", "garbage"):
        reset_fake()
        set_env(GLM_API_KEY="g", GEMINI_API_KEY="k")
        OUTLINE_MODE[0] = mode
        title, article, meta = write()
        check(f"(f-{mode}) unusable plan -> single-brain fallback",
              meta.get("parallel_writing") is False
              and "## References" in article)


# ----------------------------------------------------------------------
# (g) unification pass
# ----------------------------------------------------------------------

def test_unification():
    reset_fake()
    set_env(GLM_API_KEY="g", GEMINI_API_KEY="k")
    article_in = ("# A Title\n\n## Abstract\n\n"
                  + "Original wording fills this paragraph with "
                    "plain prose for the editor to smooth. " * 6
                  + "\n\n## References\n\n[1] A Author. Alpha "
                    "Study. https://example.org/1\n")
    out = ba.unify_article(article_in, "k", "m")
    unify_calls = [c for c in CALLS if "UNIFICATION pass"
                   in c["prompt"]]
    check("(g1) unification returns the edited article",
          out == UNIFIED_OK.strip(), str(out)[:80])
    check("(g2) unification pinned to the chain's main brain",
          unify_calls and unify_calls[0]["provider"] == "glm",
          str([(c["provider"]) for c in unify_calls]))
    check("(g3) unification prompt forbids new claims/sources/"
          "numbers/quotes",
          unify_calls
          and "NEVER add a new claim, fact, source, citation "
              "[n], number, formula, date or quote"
              in unify_calls[0]["prompt"])
    reset_fake()
    UNIFY_MODE[0] = "short"
    check("(g4) unusable unification output -> None (fail "
          "closed upstream)",
          ba.unify_article(article_in, "k", "m") is None)
    reset_fake()
    set_env(GEMINI_API_KEY="k")     # glm unkeyed
    ba.unify_article(article_in, "k", "m")
    unify_calls = [c for c in CALLS if "UNIFICATION pass"
                   in c["prompt"]]
    check("(g5) with glm unkeyed the main brain is the first "
          "KEYED chain brain (gemini)",
          unify_calls and unify_calls[0]["provider"] == "gemini",
          str([(c["provider"]) for c in unify_calls]))


# ----------------------------------------------------------------------
# (h) main() integration: double review
# ----------------------------------------------------------------------

ARTICLE_TEXT = (
    "# A Title\n\n## Abstract\n\nBody of the draft article "
    "written in parallel sections.\n\n## References\n\n"
    "[1] A Author. Alpha Study. https://example.org/1\n")
UNIFIED_TEXT = ARTICLE_TEXT + "\nUnified wording applied."


def run_main(review_results, unify_result):
    """Drive ba.main() (env-var inputs, as in
    test_stage_checkpoint) with the production pipeline stubbed.
    Returns (rc, review_calls, sent, saved, funnel_reviews)."""
    set_env(TELEGRAM_BOT_TOKEN="t", CHAT_ID="-1001",
            GEMINI_API_KEY="k", ARTICLE_CHAT_ID="42",
            FIELD="F", TOPIC="T", LANGUAGE="en", MODE="review",
            ARTICLE_CATEGORY="intl", ARTICLE_TYPE="isi",
            RESUME="")
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
    review_calls = []
    sent = []
    saved = {}
    funnel_reviews = []

    def fake_fresh(field, topic, language, category, article_type,
                   mode, api_key, model, tg_token):
        return {"counted": counted, "funnel": funnel,
                "disagreement_titles": [], "queries": ["q"],
                "ft_keywords": ["radius"], "verified_count": 7,
                "gate": {"ok": True, "counted": 5,
                         "fulltext_passages": 4, "primary": 1},
                "adaptation": None}

    def fake_write(field, topic, language, sources, api_key,
                   model, guide="", word_target=None):
        return ("T", ARTICLE_TEXT,
                {"title_candidates": [], "next_topics": [],
                 "word_count": 120, "parallel_writing": True,
                 "section_brains": {"front": "glm"}})

    def fake_unify(article, api_key, model):
        return unify_result

    def fake_review(article, counted_, ctx):
        review_calls.append(article)
        res = dict(review_results[len(review_calls) - 1])
        res["article"] = article
        return res

    def fake_funnel(token, chat_id, topic, funnel_, success,
                    **kw):
        if kw.get("review") is not None:
            funnel_reviews.append(kw["review"])
        return True

    keep = {n: getattr(ba, n) for n in (
        "_fresh_screening", "write_article", "run_review_layer",
        "unify_article", "send_funnel_dm",
        "send_detailed_source_report_dm", "send_review_report_dm",
        "send_suggestions_dm", "fetch_youtube_support",
        "load_state", "save_state", "consume_stage_checkpoint",
        "progress_update", "progress_fail", "tg_call",
        "tg_send_document")}
    ba._fresh_screening = fake_fresh
    ba.write_article = fake_write
    ba.unify_article = fake_unify
    ba.run_review_layer = fake_review
    ba.send_funnel_dm = fake_funnel
    ba.send_detailed_source_report_dm = lambda *a, **k: True
    ba.send_review_report_dm = lambda *a, **k: True
    ba.send_suggestions_dm = lambda *a, **k: None
    ba.fetch_youtube_support = lambda q, k: []
    ba.load_state = lambda: {}
    ba.save_state = lambda s: saved.update(s)
    ba.consume_stage_checkpoint = lambda *a, **k: None
    ba.progress_update = lambda *a, **k: None
    ba.progress_fail = lambda *a, **k: None
    ba.tg_call = lambda method, token, payload: (
        sent.append((method, payload)) or {"message_id": 1})
    ba.tg_send_document = lambda *a, **k: (
        sent.append(("document", a)) or {"message_id": 2})
    try:
        rc = ba.main()
    except ba.Fail:
        rc = -1
    for n, v in keep.items():
        setattr(ba, n, v)
    return rc, review_calls, sent, saved, funnel_reviews


PASS_REVIEW = {"ok": True, "verdict": "pass", "rounds": 0,
               "issues_found": 0, "issues_fixed": 0, "roles": {},
               "blocked_roles": [], "issue_records": []}
FAIL_REVIEW = {"ok": False, "verdict": "fail", "rounds": 0,
               "issues_found": 1, "issues_fixed": 0, "roles": {},
               "blocked_roles": [], "issue_records": []}


def test_main_double_review():
    rc, calls, sent, saved, frev = run_main(
        [PASS_REVIEW, PASS_REVIEW], UNIFIED_TEXT)
    check("(h1) parallel production succeeds through both "
          "reviews", rc == 0, f"rc={rc}")
    check("(h2) the §33 layer ran exactly twice",
          len(calls) == 2, str(len(calls)))
    # §70: pass 1 reviews the assembled draft WITH the
    # novelty-boundary block inserted (the scripted brain gives
    # no novelty verdict, so the block is the UNCONFIRMED
    # fallback for topic "T" with closest counted title "S0").
    fb = ba._novelty_fallback(
        "F", "T", [{"title": "S0", "in_counted": True}])
    expected1 = ba.insert_novelty_block(
        ARTICLE_TEXT, ba.format_novelty_block(
            fb, "en",
            evidence={"verified": 7, "relevant": 5,
                      "prior_work": 5, "full_text_checked": 5,
                      "bib_only": 0}))
    check("(h3) pass 1 reviewed the assembled draft (+ §70 "
          "novelty block), pass 2 the unified text",
          len(calls) == 2 and calls[0] == expected1
          and calls[1] == UNIFIED_TEXT)
    texts = "".join(str(p.get("text", ""))
                    for m, p in sent if m == "sendMessage")
    docs = [a for m, a in sent if m == "document"]
    check("(h4) the delivered text is the twice-reviewed unified "
          "article",
          "Unified wording applied." in texts
          and docs and b"Unified wording applied." in docs[0][3],
          str(sent)[:1])
    final = frev[-1] if frev else {}
    check("(h5) the reported review carries the first-pass record "
          "(§44)",
          bool(final.get("first_pass"))
          and final.get("unification") is True, str(final.keys()))
    rc2, calls2, sent2, _, _ = run_main(
        [PASS_REVIEW, FAIL_REVIEW], UNIFIED_TEXT)
    check("(h6) a failed SECOND review publishes nothing",
          rc2 == -1 and len(calls2) == 2 and sent2 == [],
          f"rc={rc2} calls={len(calls2)} sent={len(sent2)}")
    rc3, calls3, sent3, _, _ = run_main([PASS_REVIEW], None)
    check("(h7) a failed unification publishes nothing and no "
          "second review runs",
          rc3 == -1 and len(calls3) == 1 and sent3 == [],
          f"rc={rc3} calls={len(calls3)} sent={len(sent3)}")


# ----------------------------------------------------------------------
# (i) invariants
# ----------------------------------------------------------------------

def test_invariants():
    check("(i1) gate constants unchanged (>=5 counted, >=3 "
          "full-text passages)",
          ba.MIN_SOURCES == 5 and ba.MIN_FULLTEXT_SOURCES == 3,
          f"{ba.MIN_SOURCES}/{ba.MIN_FULLTEXT_SOURCES}")

    def mk(n, passages_n, primary_passages):
        out = []
        for i in range(n):
            out.append({
                "source_type": "primary"
                if i < primary_passages else "article",
                "passages": i < passages_n or i < primary_passages,
                "fulltext": "ia-passages"})
        return out
    check("(i2) gate passes at the exact thresholds",
          ba.text_first_gate(mk(5, 3, 1))["ok"] is True)
    check("(i3) gate fails without a primary with passages",
          ba.text_first_gate(mk(5, 3, 0))["ok"] is False)
    check("(i4) gate fails below 3 passage-bearing sources",
          ba.text_first_gate(mk(5, 2, 1))["ok"] is False)
    check("(i5) gate fails below 5 counted sources",
          ba.text_first_gate(mk(4, 3, 1))["ok"] is False)
    check("(i6) §33 roles (§70 adds the novelty reviewer) + "
          "repair budget unchanged",
          ba.REVIEW_ROLES == ("citations", "numbers", "claims",
                              "structure", "language", "novelty")
          and ba.REVIEW_MAX_REPAIR_ROUNDS == 2)
    set_env()
    check("(i7) §43 scope default unchanged (full)",
          ba.search_scope() == "full", ba.search_scope())


# ----------------------------------------------------------------------
# (j) plan validator
# ----------------------------------------------------------------------

def test_plan_validator():
    plan = ba._validate_section_plan({"sections": [
        {"heading": "A", "focus": "", "sources": [2, 2, 99, 0]},
        {"heading": "B", "focus": "", "sources": [1]},
        {"heading": "C", "focus": "", "sources": []}]}, 3)
    check("(j1) out-of-range dropped, duplicates removed, "
          "unassigned swept to the smallest section",
          plan is not None
          and plan[0]["sources"] == [2]
          and plan[1]["sources"] == [1]
          and plan[2]["sources"] == [3], str(plan))
    check("(j2) fewer than two sections -> None",
          ba._validate_section_plan(
              {"sections": [{"heading": "A", "sources": [1]}]},
              3) is None)
    check("(j3) a section left sourceless -> None",
          ba._validate_section_plan({"sections": [
              {"heading": "A", "sources": [1, 2]},
              {"heading": "B", "sources": []},
              {"heading": "C", "sources": []}]}, 2) is None)
    check("(j4) non-dict plan -> None",
          ba._validate_section_plan(["x"], 3) is None)


def main():
    try:
        test_parallel_rotation()
        test_source_isolation()
        test_section_fallback()
        test_section_all_fail()
        test_single_brain_fallback()
        test_plan_failure_fallback()
        test_unification()
        test_main_double_review()
        test_invariants()
        test_plan_validator()
    finally:
        restore_env()
    print(f"\n{PASS[0]}/{PASS[0] + FAIL[0]} checks passed")
    return 1 if FAIL[0] else 0


if __name__ == "__main__":
    sys.exit(main())
