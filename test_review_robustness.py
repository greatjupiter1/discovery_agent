#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline tests for the §45 review-layer robustness fixes
(Jupiter 2026-11-26, after the live local production where §33
blocked three drafts in a row on a single weak brain):

  (a) tolerant verdict parsing — prose-wrapped / fenced JSON is
      extracted by its outermost balanced span; a truncated reply
      still raises;
  (b) ONE repair retry on the same brain after a parse failure;
  (c) failover to the NEXT keyed brain when a brain yields no
      parseable verdict, with the serving brain + attempt count
      recorded in the verdict and the review report;
  (d) a role blocks ONLY after every keyed brain fails;
  (e) evidence-anchored claim flags: an unsupported/fatal claims
      flag WITHOUT the required verbatim article quote is an
      UNEVIDENCED flag — recorded, visible, never blocking —
      while the same flag WITH its quote keeps full fatal force;
  (f) reviewer calls run under REVIEW_MAX_TOKENS (§45 budget),
      passed through call_llm's max_tokens.

Fully offline: ba.call_llm is replaced by a provider-aware
scripted fake; the brain chain is pinned to [glm, gemini].
"""

import json
import os
import sys
import threading

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
# Fixtures (same shape as test_review_layer.py)
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
# Provider-aware scripted brain. REPLIES maps (role, provider) to
# a queue of replies; a reply is a string (returned) or an
# Exception instance (raised). Unscripted calls get the role's
# clean default. Every call's provider + max_tokens is recorded.
# ----------------------------------------------------------------------

ROLE_MARKERS = (("CITATIONS REVIEWER", "citations"),
                ("NUMBERS REVIEWER", "numbers"),
                ("STRUCTURE REVIEWER", "structure"),
                ("LANGUAGE REVIEWER", "language"),
                ("SUPERVISOR of a scholarly", "supervisor"),
                ("CLAIMS REVIEWER", "claims"))

REPLIES = {}
CALLS = []
LOCK = threading.Lock()
CLAIMS_MODE = {"supported": True, "central": False,
               "evidenced": True}


def _role_of(prompt):
    for marker, role in ROLE_MARKERS:
        if marker in prompt:
            return role
    return None


def _default_reply(role, prompt):
    if role == "claims":
        verdicts = []
        for line in prompt.splitlines():
            if line.startswith("SENTENCE "):
                i = int(line.split(":", 1)[0].split()[1])
                v = {"i": i, "supported": CLAIMS_MODE["supported"],
                     "central": CLAIMS_MODE["central"]}
                if not CLAIMS_MODE["supported"] \
                        and CLAIMS_MODE["evidenced"]:
                    sent = line.split(": ", 1)[1] \
                        if ": " in line else ""
                    v["quote"] = sent[:50]
                    v["evidence"] = "no passage covers this claim"
                verdicts.append(v)
        return json.dumps({"verdicts": verdicts})
    return json.dumps({"issues": []})


def fake_llm(prompt, api_key, model, temperature=0.4,
             json_mode=False, provider=None, max_tokens=None):
    role = _role_of(prompt)
    with LOCK:
        CALLS.append({"role": role, "provider": provider,
                      "max_tokens": max_tokens,
                      "repair": "could not be parsed" in prompt})
        queue = REPLIES.get((role, provider))
        reply = queue.pop(0) if queue else None
    if isinstance(reply, Exception):
        raise reply
    if reply is not None:
        return reply
    return _default_reply(role, prompt)


def reset(**claims):
    with LOCK:
        REPLIES.clear()
        del CALLS[:]
    CLAIMS_MODE.update({"supported": True, "central": False,
                        "evidenced": True})
    CLAIMS_MODE.update(claims)


ba.call_llm = fake_llm
_KEEP = (ba.llm_chain, ba._brain_key_available)
ba.llm_chain = lambda: ["glm", "gemini"]
ba._brain_key_available = lambda name, key: True


# ----------------------------------------------------------------------
# (a) tolerant parsing
# ----------------------------------------------------------------------

def test_tolerant_parse():
    d = ba._review_parse_json(
        'Here is the verdict: {"issues": []} — hope this helps [1]')
    check("(a1) prose-wrapped JSON extracted", d == {"issues": []},
          str(d))
    d = ba._review_parse_json(
        'Notes [see p. 3]: {"issues": [{"problem": "x"}]} '
        'trailing } junk')
    check("(a2) balanced span beats bracketed prose",
          d == {"issues": [{"problem": "x"}]}, str(d))
    d = ba._review_parse_json('```json\n{"a": 1}\n```')
    check("(a3) fenced JSON extracted", d == {"a": 1}, str(d))
    try:
        ba._review_parse_json('{"issues": [{"problem": "x"')
        raised = False
    except Exception:
        raised = True
    check("(a4) truncated JSON still raises", raised)


# ----------------------------------------------------------------------
# (b) repair retry on the same brain
# ----------------------------------------------------------------------

def test_repair_retry():
    reset()
    REPLIES[("citations", "glm")] = [
        "I am sorry, I cannot answer in JSON today.",
        json.dumps({"issues": []})]
    v = ba.review_citations(clean_article(), make_sources(),
                            make_context())
    check("(b1) repair retry rescues the verdict", v["available"]
          and v["ok"], str(v))
    check("(b2) verdict brain is the repaired brain",
          v["brain"] == "glm", str(v.get("brain")))
    check("(b3) two attempts recorded", v["attempts"] == 2,
          str(v.get("attempts")))
    check("(b4) the retry carried the repair instruction",
          any(c["repair"] for c in CALLS if c["role"] == "citations"),
          str(CALLS))


# ----------------------------------------------------------------------
# (c) failover to the next keyed brain
# ----------------------------------------------------------------------

def test_failover():
    reset()
    REPLIES[("numbers", "glm")] = ["garbage, not json",
                                   "still { not json"]
    v = ba.review_numbers(clean_article(), make_sources(),
                          make_context())
    check("(c1) failover yields a verdict", v["available"]
          and v["ok"], str(v))
    check("(c2) verdict brain is the second brain",
          v["brain"] == "gemini", str(v.get("brain")))
    check("(c3) attempts span both brains (2 + 1)",
          v["attempts"] == 3, str(v.get("attempts")))


# ----------------------------------------------------------------------
# (d) block only after every brain fails
# ----------------------------------------------------------------------

def test_block_after_all_brains_fail():
    reset()
    for provider in ("glm", "gemini"):
        REPLIES[("numbers", provider)] = ["junk", "junk again"]
    res = ba.run_review_layer(clean_article(), make_sources(),
                              make_context())
    check("(d1) all brains failing blocks the layer",
          not res["ok"] and res["verdict"] == "blocked",
          str(res.get("verdict")))
    check("(d2) the blocked role is numbers",
          res["blocked_roles"] == ["numbers"],
          str(res.get("blocked_roles")))
    check("(d3) numbers verdict records the attempts (2 brains "
          "x 2 calls)", res["roles"]["numbers"]["attempts"] == 4,
          str(res["roles"]["numbers"]))


# ----------------------------------------------------------------------
# (e) evidence-anchored claim flags
# ----------------------------------------------------------------------

def test_unevidenced_flag_does_not_block():
    reset(supported=False, central=True, evidenced=False)
    res = ba.run_review_layer(clean_article(), make_sources(),
                              make_context())
    check("(e1) unevidenced fatal flags do not block or fail",
          res["ok"] and res["verdict"] == "pass",
          str(res.get("verdict")))
    check("(e2) the flags are recorded as unevidenced",
          len(res["unevidenced_flags"]) > 0
          and all(f["role"] == "claims"
                  for f in res["unevidenced_flags"]),
          str(res.get("unevidenced_flags")))
    check("(e3) claims role carries no counted issues",
          res["roles"]["claims"]["issues"] == [],
          str(res["roles"]["claims"]))
    report = ba.build_review_report("موضوع", res)
    check("(e4) unevidenced flags are visible in the report",
          "پرچم‌های بدون شاهد (§45)" in report, report[:300])


def test_evidenced_flag_still_fatal():
    reset(supported=False, central=True, evidenced=True)
    res = ba.run_review_layer(clean_article(), make_sources(),
                              make_context())
    check("(e5) evidenced unsupported central claims still fail",
          not res["ok"] and res["verdict"] == "fail",
          str(res.get("verdict")))
    check("(e6) claims role carries the fatals",
          any(i["severity"] == "fatal"
              for i in res["roles"]["claims"]["issues"]),
          str(res["roles"]["claims"]))
    check("(e7) nothing recorded as unevidenced",
          res["unevidenced_flags"] == [],
          str(res.get("unevidenced_flags")))


# ----------------------------------------------------------------------
# (f) budget + report provenance
# ----------------------------------------------------------------------

def test_budget_and_provenance():
    reset()
    res = ba.run_review_layer(clean_article(), make_sources(),
                              make_context())
    check("(f1) clean run passes", res["ok"], str(res.get("verdict")))
    check("(f2) every role verdict names its brain + attempts",
          all(v.get("brain") == "glm" and v.get("attempts") == 1
              for v in res["roles"].values()),
          str({r: (v.get("brain"), v.get("attempts"))
               for r, v in res["roles"].items()}))
    check("(f3) reviewer calls ran under REVIEW_MAX_TOKENS",
          bool(CALLS) and all(c["max_tokens"] == ba.REVIEW_MAX_TOKENS
                              for c in CALLS),
          str({c["max_tokens"] for c in CALLS}))
    check("(f4) the §45 budget exceeds every legacy brain budget",
          ba.REVIEW_MAX_TOKENS > 8192
          and ba.REVIEW_MAX_TOKENS > ba.GLM_DEFAULT_MAX_TOKENS
          and ba.REVIEW_MAX_TOKENS > ba.EXTRA_BRAINS_MAX_TOKENS
          and ba.REVIEW_MAX_TOKENS > ba.GH_MODELS_MAX_TOKENS,
          str(ba.REVIEW_MAX_TOKENS))
    report = ba.build_review_report("موضوع", res)
    check("(f5) report shows per-role brain + attempts",
          "مغز و تلاش هر داور (§45)" in report and "glm" in report,
          report[:300])


def main():
    test_tolerant_parse()
    test_repair_retry()
    test_failover()
    test_block_after_all_brains_fail()
    test_unevidenced_flag_does_not_block()
    test_evidenced_flag_still_fatal()
    test_budget_and_provenance()
    ba.llm_chain, ba._brain_key_available = _KEEP
    print(f"\n{PASS[0]}/{PASS[0] + FAIL[0]} checks passed")
    return 1 if FAIL[0] else 0


if __name__ == "__main__":
    sys.exit(main())
