"""Mocked offline checks for the §20.2 second-judge availability
fallback. No network: the brain chain is a fake.

Scenario (the 2026-11-25 live failure, run 45642977319): chain
[glm, gemini]; GLM is the first judge and keeps a borderline
candidate; Gemini — the ONLY different brain — returns HTTP 429 on
every retry. Before the fix the keep died as "judge disagreement"
because the first judge's brain was excluded from the second-judge
candidates. After the fix, GLM is tried LAST as a logged
availability fallback and its verdict decides.
"""
import contextlib
import io

import build_article as ba

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


def cand(title, abstract=""):
    return {"title": title, "authors": ["A. Scholar"], "year": 2010,
            "venue": "Journal", "doi": "10.4000/x",
            "url": "https://example.org/x", "abstract": abstract,
            "arxiv_id": None, "index": "OpenAlex"}


EN_ABSTRACT = ("A study of al-Biruni's qibla determination method "
               "and its spherical trigonometry. " * 4)
TOPIC_FA = "تعیین سمت قبلهٔ غزنه به روش ابوریحان بیرونی"
FIELD = "History of astronomy"

ba.llm_chain = lambda: ["glm", "gemini"]
ba._brain_key_available = lambda name, api_key: True

calls = []


def make_brain(gemini_verdict, glm_pinned_verdict):
    """gemini_verdict / glm_pinned_verdict: True (keep), False
    (drop) or "error" (provider failure)."""

    def fake(prompt, api_key, model, temperature=0.4, json_mode=False,
             provider=None):
        calls.append(provider)
        if provider is None:
            ba._LAST_BRAIN["name"] = "glm"
            return ('[{"i": 1, "keep": true, "reason": '
                    '"genuinely about the method"}]')
        verdict = (gemini_verdict if provider == "gemini"
                   else glm_pinned_verdict)
        if verdict == "error":
            raise RuntimeError("HTTP 429: quota exceeded")
        return ('[{"i": 1, "keep": %s, "reason": "second judge %s"}]'
                % ("true" if verdict else "false",
                   "agrees" if verdict else "disagrees"))

    return fake


def run_screen():
    calls.clear()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        counted, stats = ba.screen_sources(
            FIELD, TOPIC_FA,
            [cand("Al-Biruni's qibla method applied with modern "
                  "tools", abstract=EN_ABSTRACT)], "k", "m")
    return counted, stats, out.getvalue()


# (a1) different brain 429s -> fallback brain (glm) KEEPS -> survives
ba.call_llm = make_brain("error", True)
counted, stats, log = run_screen()
check("(a1) fallback second judge (glm) was tried after gemini 429",
      calls == [None, "gemini", "glm"])
check("(a2) keep SURVIVED on the fallback brain's keep verdict",
      len(counted) == 1 and stats["drop_disagreement"] == 0)
check("(c1) fallback use is logged loudly",
      "second judge via fallback brain: glm" in log)

# (a2) different brain 429s -> fallback brain DROPS -> real verdict
# of "irrelevant" from the fallback still drops the source
ba.call_llm = make_brain("error", False)
counted, stats, log = run_screen()
check("(a3) fallback brain's drop verdict still drops the source",
      len(counted) == 0 and stats["drop_disagreement"] == 1)

# (b) ALL brains error -> fail-closed "unjudged", nothing counted
ba.call_llm = make_brain("error", "error")
counted, stats, log = run_screen()
check("(b1) every brain genuinely tried (gemini, then glm fallback)",
      calls == [None, "gemini", "glm"])
check("(b2) all brains failed -> keep counts as unjudged, dropped",
      len(counted) == 0 and stats["drop_disagreement"] == 1)
check("(b3) all-failed outcome is logged",
      "ALL chain brains failed" in log)

# (d) normal path unchanged: different brain answers -> no fallback
ba.call_llm = make_brain(True, "error")
counted, stats, log = run_screen()
check("(d1) different brain's verdict used, fallback never called",
      calls == [None, "gemini"] and len(counted) == 1)
check("(d2) no fallback log line on the normal path",
      "fallback brain" not in log)

failed = [n for n, ok in PASS if not ok]
print(f"\n{len(PASS) - len(failed)}/{len(PASS)} checks passed")
if failed:
    raise SystemExit("FAILED: " + ", ".join(failed))
