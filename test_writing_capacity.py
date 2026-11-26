#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline tests for §57 (Jupiter 2026-11-26, after production
run 45853857495 passed the gate and failed at the writing
stage): writing-stage brain capacity — per-brain input caps,
fit-or-skip with excerpt trimming, and truncation treated as a
failed attempt.

Covered:
  (a) token estimation + the capacity table (groq small, glm /
      gemini large ceilings, sane default for the §22 extras);
  (b) fit-by-trimming: a payload over groq's cap is rebuilt down
      the excerpt ladder — source numbering preserved, the lead
      passage kept, the trimmed tail gone, estimate within cap;
  (c) skip-to-next-brain: a payload that cannot fit groq even at
      the smallest rung sends NO call to groq; the next brain
      serves it at FULL size;
  (d) truncation is failure: finish_reason "length" raises in
      the OpenAI-compatible paths (extra brains + GLM), Gemini's
      finishReason "MAX_TOKENS" raises in gemini(), and a
      section whose first brain truncates is served by the next
      brain — the truncated text never becomes the section;
  (e) fail-closed: a section skipped by / failing on every brain
      still raises Fail, with no call sent to a skipped brain;
  (f) legacy behaviour with only large-capacity brains keyed:
      the chain call stays UNPINNED and byte-identical, the
      single-brain path writes its parts unpinned with untrimmed
      cards; the capacity walk engages (pinned, full-size for
      the fitting brain) only when some keyed brain is too
      small;
  (g) the planner and the unifier under capacity: the planner
      walks to the fitting brain; the unifier skips a main brain
      that cannot fit the article (never trimmed) and returns
      None when no keyed brain can take it;
  (h) invariants: REVIEW_MAX_TOKENS and FULLTEXT_CARD_CHARS
      unchanged; the fit ladder starts at the full card size.
Fully offline: ba.call_llm / ba.http_request are scripted fakes;
no real keys.
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

def big_source(n, excerpt_chars=9000, abstract_chars=100):
    return {"title": f"Big Study {n}", "authors": ["A Author"],
            "year": 2020, "venue": "Journal of Tests",
            "doi": "", "url": f"https://example.org/{n}",
            "source_type": "article", "index": "test",
            "abstract": "A" * abstract_chars,
            "fulltext_excerpt": (f"LEAD-{n} passage opening. "
                                 + "x" * excerpt_chars
                                 + f" TAIL-{n}"),
            "passages": True, "fulltext": "ia-passages"}


SECTION_OK = ("## Section\n\n"
              + "This section text is written from the fitted "
                "sources and carries enough words to pass the "
                "minimum length guard for a parallel section "
                "write in the test harness here today. " * 2
              ).strip()


def make_task(sources, numbers, key="body0"):
    def build_prompt(ec):
        return ("Field: Astronomy\nTopic: Earth radius\n\n"
                "THE ONLY SOURCES YOU MAY USE:\n"
                + ba._source_cards_numbered(sources, numbers, ec)
                + "\n\nWrite ONLY your section, headed exactly "
                  "'## Section'.")
    return {"key": key, "start": 0, "build_prompt": build_prompt}


# ----------------------------------------------------------------------
# Scripted fakes
# ----------------------------------------------------------------------

CALLS = []
FAIL_PROVIDERS = {}      # provider -> exception instance to raise


def fake_llm(prompt, api_key, model, temperature=0.4,
             json_mode=False, provider=None, **kw):
    CALLS.append({"prompt": prompt, "provider": provider,
                  "json_mode": json_mode})
    if provider in FAIL_PROVIDERS:
        raise FAIL_PROVIDERS[provider]
    if "design the BODY plan" in prompt:
        return json.dumps({"sections": [
            {"heading": "One", "focus": "f1", "sources": [1, 2]},
            {"heading": "Two", "focus": "f2", "sources": [3, 4]},
            {"heading": "Three", "focus": "f3", "sources": [5]}]})
    if "propose SIX narrow" in prompt:
        return json.dumps({
            "candidates": ["A Narrow Title for the Test Article"],
            "best": 0, "next_topics": ["A", "B", "C"]})
    if "UNIFICATION pass" in prompt:
        return ("# A Title\n\n## Abstract\n\n"
                + "Unified wording fills this paragraph with "
                  "smooth and consistent prose throughout the "
                  "whole article text. " * 500
                + "\n\n## References\n\n[1] A Author. Big Study 1. "
                  "https://example.org/1\n")
    if "Write PART 1 of a scholarly article" in prompt:
        return ("## Abstract\n\n" + "Front matter text. " * 30
                + "\n\n## Keywords\n\nk1; k2\n\n## Introduction\n\n"
                + "Intro text. " * 30)
    if "Write PART 2 of the same article" in prompt:
        return "## Body\n\n" + "Body text. " * 40
    if "Write PART 3 of the same article" in prompt:
        return ("## Discussion\n\n" + "Discussion text. " * 30
                + "\n\n## Conclusion\n\nDone.\n\n## Limitations\n\n"
                  "Limits.")
    return SECTION_OK


def reset_fake():
    del CALLS[:]
    FAIL_PROVIDERS.clear()


ba.call_llm = fake_llm
ba.progress_update = lambda *a, **k: None


# ----------------------------------------------------------------------
# (a) estimation + capacity table
# ----------------------------------------------------------------------

def test_estimation_and_caps():
    check("(a1) empty text estimates 1 token",
          ba._estimate_tokens("") == 1)
    check("(a2) 300 chars at 3 chars/token estimates 101",
          ba._estimate_tokens("x" * 300) == 101)
    check("(a3) estimate grows with length",
          ba._estimate_tokens("x" * 3000)
          > ba._estimate_tokens("x" * 300))
    check("(a4) groq cap is the small evidenced one (5500)",
          ba._writing_input_cap("groq") == 5500)
    check("(a5) glm / gemini caps are generous ceilings",
          ba._writing_input_cap("glm") == 100_000
          and ba._writing_input_cap("gemini") == 100_000)
    check("(a6) extra brains get the sane default cap",
          ba._writing_input_cap("openrouter")
          == ba.WRITING_INPUT_TOKEN_CAP_DEFAULT
          and ba._writing_input_cap("siliconflow")
          == ba.WRITING_INPUT_TOKEN_CAP_DEFAULT
          and ba._writing_input_cap("some-future-brain")
          == ba.WRITING_INPUT_TOKEN_CAP_DEFAULT)


# ----------------------------------------------------------------------
# (b) fit-by-trimming
# ----------------------------------------------------------------------

def test_fit_by_trimming():
    sources = [big_source(1), big_source(2)]
    task = make_task(sources, [1, 2])
    full_est = ba._estimate_tokens(task["build_prompt"](
        ba.FULLTEXT_CARD_CHARS))
    check("(b1) the full-size payload really is over groq's cap",
          full_est > ba._writing_input_cap("groq"),
          f"est={full_est}")
    fitted = ba._fit_writing_prompt(task["build_prompt"], "groq",
                                    "test section")
    check("(b2) a fitted prompt exists for groq",
          fitted is not None)
    prompt, est, excerpt_chars = fitted
    check("(b3) the fit used a trimmed rung below the full size",
          excerpt_chars < ba.FULLTEXT_CARD_CHARS,
          f"rung={excerpt_chars}")
    check("(b4) the fitted estimate is within groq's cap",
          est <= ba._writing_input_cap("groq"), f"est={est}")
    check("(b5) source numbering preserved after trimming",
          "[1] Big Study 1" in prompt and "[2] Big Study 2" in prompt)
    check("(b6) the lead passage of each source survives trimming",
          "LEAD-1 passage opening." in prompt
          and "LEAD-2 passage opening." in prompt)
    check("(b7) the trimmed tail is gone",
          "TAIL-1" not in prompt and "TAIL-2" not in prompt)
    check("(b8) the abstract lines survive trimming",
          "A" * 100 in prompt)


# ----------------------------------------------------------------------
# (c) skip-to-next-brain, (e) fail-closed
# ----------------------------------------------------------------------

def untrimmable_sources():
    # Abstracts are never trimmed, so 20 sources x 1500-char
    # abstracts cannot fit groq at ANY rung.
    return [big_source(n, excerpt_chars=2000, abstract_chars=1500)
            for n in range(1, 21)]


def test_skip_to_next_brain():
    reset_fake()
    sources = untrimmable_sources()
    task = make_task(sources, list(range(1, 21)))
    text, brain = ba._write_section(task, ["groq", "gemini"],
                                    "k", "m")
    providers = [c["provider"] for c in CALLS]
    check("(c1) groq was skipped — no call was sent to it",
          "groq" not in providers, str(providers))
    check("(c2) the next brain served the section",
          brain == "gemini" and text == SECTION_OK)
    gemini_prompt = [c["prompt"] for c in CALLS
                     if c["provider"] == "gemini"][0]
    check("(c3) the serving brain got the FULL-size payload "
          "(skip does not degrade the next brain)",
          "TAIL-20" in gemini_prompt)


def test_all_skipped_or_failed_closed():
    reset_fake()
    sources = untrimmable_sources()
    task = make_task(sources, list(range(1, 21)))
    raised = None
    try:
        ba._write_section(task, ["groq"], "k", "m")
    except ba.Fail as e:
        raised = e
    check("(e1) a section no brain can fit fails closed",
          raised is not None and "over capacity" in str(raised),
          str(raised))
    check("(e2) no call was sent to the skipped brain",
          CALLS == [], str(CALLS))
    reset_fake()
    FAIL_PROVIDERS["groq"] = ba.Fail("boom")
    FAIL_PROVIDERS["gemini"] = ba.Fail("boom")
    small = make_task([big_source(1, excerpt_chars=100)], [1])
    raised = None
    try:
        ba._write_section(small, ["groq", "gemini"], "k", "m")
    except ba.Fail as e:
        raised = e
    check("(e3) a section failing on every brain fails closed",
          raised is not None)


# ----------------------------------------------------------------------
# (d) truncation is failure
# ----------------------------------------------------------------------

HTTP_MODE = ["stop"]    # stop | length | maxtokens


def fake_http(url, method="POST", data=None, headers=None,
              **kw):
    if "generativelanguage" in url:
        finish = ("MAX_TOKENS" if HTTP_MODE[0] == "maxtokens"
                  else "STOP")
        body = {"candidates": [{
            "content": {"parts": [{"text": "gemini text"}]},
            "finishReason": finish}]}
    else:
        finish = "length" if HTTP_MODE[0] == "length" else "stop"
        body = {"choices": [{"message": {"content": "brain text"},
                             "finish_reason": finish}]}
    return 200, json.dumps(body).encode("utf-8")


def test_truncation_detection():
    real_http = ba.http_request
    ba.http_request = fake_http
    try:
        set_env(GLM_API_KEY="g", GROQ_API_KEY="gk")
        HTTP_MODE[0] = "length"
        for name, fn in (("extra-brain groq",
                          lambda: ba._extra_brain_once(
                              "groq", "prompt")),
                         ("glm", lambda: ba._glm_once("prompt"))):
            raised = None
            try:
                fn()
            except ba.Fail as e:
                raised = e
            check(f"(d1) {name}: finish_reason=length raises Fail",
                  raised is not None
                  and "truncated" in str(raised), str(raised))
        HTTP_MODE[0] = "maxtokens"
        raised = None
        try:
            ba.gemini("prompt", "k", "m")
        except ba.Fail as e:
            raised = e
        check("(d2) gemini: finishReason=MAX_TOKENS raises Fail",
              raised is not None and "MAX_TOKENS" in str(raised),
              str(raised))
        HTTP_MODE[0] = "stop"
        check("(d3) non-truncated replies still return their text",
              ba._extra_brain_once("groq", "prompt") == "brain text"
              and ba._glm_once("prompt") == "brain text"
              and ba.gemini("prompt", "k", "m") == "gemini text")
    finally:
        ba.http_request = real_http


def test_truncation_failover():
    reset_fake()
    FAIL_PROVIDERS["groq"] = ba.Fail(
        "Groq reply truncated at 8000 tokens "
        "(finish_reason=length)")
    task = make_task([big_source(1, excerpt_chars=100)], [1])
    text, brain = ba._write_section(task, ["groq", "gemini"],
                                    "k", "m")
    check("(d4) a truncated attempt fails over to the next brain",
          brain == "gemini" and text == SECTION_OK)
    check("(d5) the truncated text never became the section",
          "truncated" not in text)


# ----------------------------------------------------------------------
# (f) legacy behaviour with only large-capacity brains
# ----------------------------------------------------------------------

def test_legacy_chain_call():
    reset_fake()
    set_env(GLM_API_KEY="g", GEMINI_API_KEY="k")
    out = ba._writing_chain_call(lambda ec: "small prompt", "k",
                                 "m", "test call")
    check("(f1) small payload on large brains -> one UNPINNED "
          "chain call, as legacy",
          out == SECTION_OK and len(CALLS) == 1
          and CALLS[0]["provider"] is None, str(CALLS))


def test_legacy_single_path():
    reset_fake()
    set_env(GEMINI_API_KEY="k")     # one keyed brain -> legacy path
    sources = [big_source(1, excerpt_chars=5000),
               big_source(2, excerpt_chars=5000)]
    title, article, meta = ba.write_article(
        "Astronomy", "Earth radius", "en", sources, "k", "m",
        guide="", word_target=(600, 900))
    parts = [c for c in CALLS if "Write PART" in c["prompt"]]
    check("(f2) single-brain path taken",
          meta.get("parallel_writing") is False)
    check("(f3) the three parts were written UNPINNED",
          len(parts) == 3
          and all(c["provider"] is None for c in parts),
          str([(c["provider"]) for c in parts]))
    check("(f4) the cards reached the writer UNTRIMMED",
          all("TAIL-1" in c["prompt"] and "TAIL-2" in c["prompt"]
              for c in parts))


def test_capacity_walk_engages():
    reset_fake()
    set_env(GLM_API_KEY="g", GROQ_API_KEY="gk")
    sources = [big_source(n) for n in range(1, 6)]

    def build(ec):
        return ("THE ONLY SOURCES YOU MAY USE:\n"
                + ba.source_cards_text(sources, ec) + "\n")
    out = ba._writing_chain_call(build, "", "m", "test walk")
    check("(f5) over-groq payload engages the walk: pinned call",
          len(CALLS) == 1 and CALLS[0]["provider"] == "glm",
          str([(c["provider"]) for c in CALLS]))
    check("(f6) the fitting brain (glm) got the FULL-size prompt",
          "TAIL-5" in CALLS[0]["prompt"])


# ----------------------------------------------------------------------
# (g) planner + unifier under capacity
# ----------------------------------------------------------------------

def test_planner_capacity():
    reset_fake()
    set_env(GLM_API_KEY="g", GROQ_API_KEY="gk")
    sources = [big_source(n) for n in range(1, 6)]
    plan = ba._plan_sections("Astronomy", "Earth radius", "en",
                             sources, "", "m")
    planner_calls = [c for c in CALLS if "design the BODY plan"
                     in c["prompt"]]
    check("(g1) the planner still returns a usable plan",
          plan is not None and len(plan) == 3, str(plan))
    check("(g2) the planner call went pinned to the fitting "
          "brain (glm), not to groq",
          planner_calls and planner_calls[0]["provider"] == "glm",
          str([(c["provider"]) for c in planner_calls]))


def test_unifier_capacity():
    big_article = ("# A Title\n\n## Abstract\n\n"
                   + "word " * 12000
                   + "\n\n## References\n\n[1] A Author. Big Study 1. "
                     "https://example.org/1\n")
    check("(g3) the fixture article really exceeds the default "
          "cap but fits glm",
          ba._writing_input_cap("openrouter")
          < ba._estimate_tokens(big_article)
          <= ba._writing_input_cap("glm"),
          f"est={ba._estimate_tokens(big_article)}")
    reset_fake()
    set_env(LLM_CHAIN="openrouter,glm", OPENROUTER_API_KEY="o",
            GLM_API_KEY="g")
    out = ba.unify_article(big_article, "", "m")
    unify_calls = [c for c in CALLS if "UNIFICATION pass"
                   in c["prompt"]]
    check("(g4) unifier skipped the too-small main brain and "
          "was served by glm",
          out is not None and unify_calls
          and unify_calls[0]["provider"] == "glm",
          str([(c["provider"]) for c in unify_calls]))
    reset_fake()
    set_env(GROQ_API_KEY="gk")
    out = ba.unify_article(big_article, "", "m")
    check("(g5) no keyed brain can fit the article -> None "
          "(fail closed), and no call was sent",
          out is None and CALLS == [], str(CALLS))


# ----------------------------------------------------------------------
# (h) invariants
# ----------------------------------------------------------------------

def test_invariants():
    check("(h1) REVIEW_MAX_TOKENS untouched (§45)",
          ba.REVIEW_MAX_TOKENS == 16000)
    check("(h2) FULLTEXT_CARD_CHARS untouched",
          ba.FULLTEXT_CARD_CHARS == 12_000)
    check("(h3) the fit ladder starts at the full card size and "
          "strictly descends",
          ba.WRITING_FIT_EXCERPT_CAPS[0] == ba.FULLTEXT_CARD_CHARS
          and all(a > b for a, b in zip(
              ba.WRITING_FIT_EXCERPT_CAPS,
              ba.WRITING_FIT_EXCERPT_CAPS[1:])))
    check("(h4) scope strings untouched (§57 adds no searcher)",
          "§§32–50" in ba.search_scope_note("en")
          or "§§32-50" in ba.search_scope_note("en"))


def main():
    try:
        test_estimation_and_caps()
        test_fit_by_trimming()
        test_skip_to_next_brain()
        test_all_skipped_or_failed_closed()
        test_truncation_detection()
        test_truncation_failover()
        test_legacy_chain_call()
        test_legacy_single_path()
        test_capacity_walk_engages()
        test_planner_capacity()
        test_unifier_capacity()
        test_invariants()
    finally:
        restore_env()
    print(f"\n{PASS[0]}/{PASS[0] + FAIL[0]} checks passed")
    return 1 if FAIL[0] else 0


if __name__ == "__main__":
    sys.exit(main())
