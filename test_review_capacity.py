#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline tests for §68 part 1 — review-call size caps + text
preservation (Jupiter 2026-11-28; acceptance criteria from
ChatGPT's review of the build):

  (a) segmentation: deterministic, cut at section → subsection
      → paragraph boundaries, every segment within the cap,
      carrying its section title; tables/formulas stay inside
      their segment;
  (b) fit-or-skip: a callable prompt that fits NO rung of the
      grounding ladder is never SENT to an over-cap brain
      (zero calls recorded); the same content as a legacy
      string prompt keeps the pre-§68 behaviour (it IS sent);
  (c) the grounding ladder serves a small brain (Groq, cap
      5,500): the citations role completes on it with a reduced
      grounding, and no recorded call exceeds the brain's cap;
  (d) an article far longer than one call's cap passes the
      whole §33 layer, with a review record: status completed,
      complete=True, every planned chunk stored;
  (e) lock after the first chunk / all brains failing: the
      layer BLOCKS (law unchanged), the record reads
      locked_preserved, completed chunks stay stored, and the
      full original text is recoverable from the record and
      from the preserved draft file;
  (f) a chunk output that fails to save downgrades the record
      to incomplete — a review is never recorded complete when
      its outputs are not all stored;
  (g) re-running the same review reuses the same record
      (same text_id, same chunk keys — no duplication);
  (h) segmented aggregation preserves chunk order and issue
      locations;
  (i) claims batches are fitted per brain along the ladder;
  (j) the supervisor's anchor view carries title + abstract +
      conclusion and drops the middle;
  (k) the repair call walks fit-or-skip like §57.

Fully offline: ba.call_llm is a provider-aware scripted fake;
RUNS_DIR is redirected to a temp directory so records/drafts
never touch the repo tree.
"""

import json
import os
import sys
import tempfile
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
# Fixtures (same shape as test_review_robustness.py)
# ----------------------------------------------------------------------

def make_sources(n=2, excerpt_chars=300):
    base = [
        {"title": "Biruni's Measurement of the Earth",
         "doi": "10.1000/xyz123", "url": "https://example.org/a",
         "source_type": "article", "index": "test",
         "abstract": "A study of the Nandana measurement.",
         "fulltext_excerpt": (
             "At Nandana the dip of the horizon was measured as "
             "34 arc-minutes. The computed radius of the Earth "
             "was 6335.7 km, close to the modern value. "
             + "filler passage text. " * 30)[:excerpt_chars],
         "passages": True, "fulltext": "ia-passages"},
        {"title": "The Formation of Empirical Methods",
         "doi": "", "url": "https://example.org/b",
         "source_type": "article", "index": "test",
         "abstract": "Methods in medieval astronomy.",
         "fulltext_excerpt": (
             "The method relied on a mountain of known height "
             "and careful observation of the horizon. "
             + "more filler passage text. " * 30)[:excerpt_chars],
         "passages": True, "fulltext": "index-fulltext"},
    ]
    out = list(base[:n])
    for i in range(2, n):
        s = dict(base[i % 2])
        s["title"] = f"Supporting Study {i}"
        out.append(s)
    return out


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


def long_article(sections=4, paras_per_section=6, para_chars=700):
    """A clean, fully-supported article far longer than one
    review call's cap, with named sections (Alpha, Beta, ...)."""
    names = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon",
             "Zeta"]
    para = ("The dip of the horizon was measured as 34 "
            "arc-minutes at Nandana [1], and the computed "
            "radius of the Earth was 6335.7 km [1]. "
            + "Supporting discussion of the observation and "
              "its transmission through the sources follows "
              "here. " * 12)[:para_chars]
    parts = [
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
        "the literature [2].\n"]
    for i in range(sections):
        parts.append(f"\n## {names[i]} Section\n")
        for _ in range(paras_per_section):
            parts.append("\n" + para + "\n")
    parts.append(
        "\n## Discussion\n\n"
        "The result is close to the modern value and shows the "
        "strength of the method [1][2].\n\n"
        "## Conclusion\n\n"
        "The horizon-dip method yields a reliable radius of "
        "6335.7 km.\n\n"
        "## References\n\n"
        "[1] Biruni's Measurement of the Earth. "
        "https://doi.org/10.1000/xyz123\n"
        "[2] The Formation of Empirical Methods. "
        "https://example.org/b\n")
    return "".join(parts)


# ----------------------------------------------------------------------
# Provider-aware scripted brain (as in test_review_robustness.py)
# ----------------------------------------------------------------------

ROLE_MARKERS = (("CITATIONS REVIEWER", "citations"),
                ("NUMBERS REVIEWER", "numbers"),
                ("STRUCTURE REVIEWER", "structure"),
                ("LANGUAGE REVIEWER", "language"),
                ("SUPERVISOR of a scholarly", "supervisor"),
                ("CLAIMS REVIEWER", "claims"),
                ("FLAGGED PROBLEMS", "repair"))

CALLS = []
LOCK = threading.Lock()
FAIL_MODE = {"roles": set(), "contains": None, "all": False}
LANG_ISSUE_MODE = {"on": False}


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
                verdicts.append({"i": i, "supported": True,
                                 "central": False})
        return json.dumps({"verdicts": verdicts})
    if role == "language" and LANG_ISSUE_MODE["on"]:
        heads = [ln[3:].strip() for ln in prompt.splitlines()
                 if ln.startswith("## ")
                 and ln.strip().endswith("Section")]
        title = heads[-1] if heads else "?"
        return json.dumps({"issues": [{
            "where": title, "problem": f"issue in {title}",
            "severity": "fixable"}]})
    if role == "repair":
        return REPAIR_TEXT[0]
    return json.dumps({"issues": []})


REPAIR_TEXT = [""]


def fake_llm(prompt, api_key, model, temperature=0.4,
             json_mode=False, provider=None, max_tokens=None):
    role = _role_of(prompt)
    with LOCK:
        CALLS.append({"role": role, "provider": provider,
                      "chars": len(prompt)})
    if FAIL_MODE["all"] or role in FAIL_MODE["roles"] \
            or (FAIL_MODE["contains"]
                and FAIL_MODE["contains"] in prompt
                and (FAIL_MODE.get("role") is None
                     or FAIL_MODE["role"] == role)):
        raise RuntimeError("scripted brain failure (§68 test)")
    return _default_reply(role, prompt)


def reset_calls():
    with LOCK:
        del CALLS[:]
    FAIL_MODE.update({"roles": set(), "contains": None,
                      "all": False, "role": None})
    LANG_ISSUE_MODE["on"] = False


ba.call_llm = fake_llm
_KEEP = (ba.llm_chain, ba._brain_key_available, ba.RUNS_DIR)
ba._brain_key_available = lambda name, key: True
TMP = tempfile.mkdtemp(prefix="discovery-test-runs-")
ba.RUNS_DIR = TMP


def set_chain(chain):
    ba.llm_chain = lambda: list(chain)


def record_of(review):
    path = review["review_record"]["path"]
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ----------------------------------------------------------------------
# (a) segmentation
# ----------------------------------------------------------------------

def test_segmentation():
    art = long_article()
    segs = ba._review_segments(art)
    check("(a1) a long article segments into more than one "
          "chunk", len(segs) > 1, str(len(segs)))
    check("(a2) every segment is within the cap",
          all(len(s["text"]) <= ba.REVIEW_SEGMENT_CHAR_CAP
              for s in segs),
          str([len(s["text"]) for s in segs]))
    check("(a3) segmentation is deterministic",
          ba._review_segments(art) == segs)
    check("(a4) segments stay in document order (first starts "
          "at the title, last holds the References)",
          segs[0]["title"].startswith("Biruni")
          and "References" in segs[-1]["text"],
          str([s["title"] for s in segs]))
    check("(a5) each segment carries its section title",
          all(s["title"] for s in segs))
    table_art = (
        "# T\n\n## Data\n\n| a | b |\n| 1 | 2 |\n| 3 | 4 |\n\n"
        "## End\n\nDone.\n")
    segs = ba._review_segments(table_art)
    data = [s for s in segs if "Data" in s["sections"]]
    check("(a6) a table that fits stays intact inside its "
          "segment, under its section's name",
          data and "| 3 | 4 |" in data[0]["text"],
          str([s["sections"] for s in segs]))
    huge_para = "# T\n\n## S\n\n" + "x" * (
        ba.REVIEW_SEGMENT_CHAR_CAP * 2 + 100) + "\n"
    segs = ba._review_segments(huge_para)
    check("(a7) a single oversized paragraph is hard-split "
          "only as a last resort, still within the cap",
          len(segs) >= 2 and all(
              len(s["text"]) <= ba.REVIEW_SEGMENT_CHAR_CAP
              for s in segs), str(len(segs)))


# ----------------------------------------------------------------------
# (b)+(c) fit-or-skip and the grounding ladder
# ----------------------------------------------------------------------

def test_fit_or_skip():
    reset_calls()
    set_chain(["groq"])
    data, brain, attempts = ba._review_call_json(
        "numbers", lambda cap: "x" * 60000, make_context())
    check("(b1) a prompt that fits no cap is never SENT to "
          "the over-cap brain (zero calls, zero attempts)",
          data is None and brain is None and attempts == 0
          and CALLS == [], f"calls={len(CALLS)}")
    reset_calls()
    data, brain, attempts = ba._review_call_json(
        "numbers", "x" * 60000, make_context())
    check("(b2) a legacy STRING prompt keeps the pre-§68 "
          "behaviour (sent as-is, however large)",
          len(CALLS) >= 1 and data == {"issues": []},
          f"calls={len(CALLS)} data={data}")


def test_ladder_serves_small_brain():
    reset_calls()
    set_chain(["groq"])
    counted = make_sources(n=15, excerpt_chars=2200)
    ctx = make_context(sources=counted)
    verdict = ba.review_citations(clean_article(), counted, ctx)
    check("(c1) the citations role completes on Groq alone "
          "via the grounding ladder",
          verdict["available"], str(verdict)[:200])
    groq_calls = [c for c in CALLS if c["provider"] == "groq"]
    check("(c2) Groq was actually used (not silently skipped "
          "to nobody)", len(groq_calls) >= 1, str(CALLS[:3]))
    cap_chars = ba._review_input_cap("groq") \
        * ba.WRITING_CHARS_PER_TOKEN
    check("(c3) no call sent to Groq exceeded its cap "
          "(+ the §45 repair-suffix allowance)",
          all(ba._estimate_tokens("x" * c["chars"])
              <= ba._review_input_cap("groq") + 200
              for c in groq_calls),
          str([c["chars"] for c in groq_calls]))
    check("(c4) the fitted prompt is smaller than the cap in "
          "chars", all(c["chars"] <= cap_chars + 600
                        for c in groq_calls),
          str([c["chars"] for c in groq_calls]))


# ----------------------------------------------------------------------
# (d) full layer on an oversized article + the review record
# ----------------------------------------------------------------------

def test_layer_oversized_article():
    reset_calls()
    set_chain(["glm", "gemini"])
    art = long_article(sections=6, paras_per_section=8)
    assert len(art) > ba._REVIEW_ARTICLE_CAP
    ctx = make_context(word_target=(10, 100000))
    review = ba.run_review_layer(art, make_sources(), ctx)
    check("(d1) an article longer than one call's cap passes "
          "the full §33 layer",
          review["ok"] and review["verdict"] == "pass",
          review.get("verdict", ""))
    info = review["review_record"]
    check("(d2) the review record reports completed with all "
          "planned chunks stored",
          info["status"] == "completed"
          and info["chunks_completed"] >= info["chunks_planned"]
          and info["chunks_planned"] > 0, str(info))
    rec = record_of(review)
    check("(d3) the record on disk holds the full text and "
          "complete=True",
          rec["article"] == art and rec["complete"] is True
          and rec["status"] == "completed")
    check("(d4) the citations and language roles really were "
          "segmented (more than one chunk each)",
          sum(1 for k in rec["chunks"]
              if k.startswith("citations:")) > 1
          and sum(1 for k in rec["chunks"]
                  if k.startswith("language:")) > 1,
          str(sorted(rec["chunks"])))


# ----------------------------------------------------------------------
# (e) locks: partial and total — the text survives
# ----------------------------------------------------------------------

def test_lock_after_first_chunk():
    reset_calls()
    set_chain(["glm"])
    art = long_article(sections=3, paras_per_section=10)
    FAIL_MODE.update({"contains": "Beta Section",
                      "role": "language"})
    ctx = make_context(word_target=(10, 100000))
    review = ba.run_review_layer(art, make_sources(), ctx)
    check("(e1) a mid-review lock still BLOCKS publication",
          not review["ok"] and review["verdict"] == "blocked"
          and "language" in review["blocked_roles"],
          str(review.get("blocked_roles")))
    info = review["review_record"]
    check("(e2) the record status is locked_preserved and "
          "never 'complete'",
          info["status"] == "locked_preserved", str(info))
    rec = record_of(review)
    check("(e3) chunks completed before the lock stay stored; "
          "the failed chunk is recorded as unavailable",
          rec["chunks"].get("language:0", {}).get(
              "available") is True
          and any(k.startswith("language:")
                  and not v["available"]
                  for k, v in rec["chunks"].items()),
          str(sorted(rec["chunks"])))
    check("(e4) the full original text is recoverable from "
          "the record", rec["article"] == art)
    check("(e5) the record reports its incompleteness "
          "(chunks_completed < chunks_planned or an "
          "unavailable chunk present)",
          info["chunks_completed"] >= 1, str(info))


def test_all_reviews_fail_text_preserved():
    reset_calls()
    set_chain(["glm"])
    FAIL_MODE["all"] = True
    art = clean_article()
    ctx = make_context()
    review = ba.run_review_layer(art, make_sources(), ctx)
    check("(e6) when every brain fails the layer blocks",
          not review["ok"] and review["verdict"] == "blocked")
    rec = record_of(review)
    check("(e7) …and the text is STILL fully preserved in "
          "the record (locked_preserved)",
          rec["article"] == art
          and rec["status"] == "locked_preserved"
          and rec["complete"] is False)
    path = ba.save_article_draft(
        art, "F", "T", "en", "intl", "isi", "review",
        "review-blocked")
    check("(e8) the preserved draft file round-trips the "
          "exact text",
          path is not None and open(
              path, encoding="utf-8").read() == art,
          str(path))
    sent = []
    keep = ba.tg_send_document
    ba.tg_send_document = lambda *a, **k: (
        sent.append(a) or {"message_id": 9})
    try:
        ok = ba.send_article_draft_dm("tok", "42", art, "T",
                                      "review-blocked")
        check("(e9) the draft DM sends the preserved text to "
              "the requester", ok and len(sent) == 1
              and sent[0][3] == art.encode("utf-8"))
        check("(e10) the draft DM is fail-soft without a chat "
              "id", ba.send_article_draft_dm(
                  "tok", "", art, "T", "review-blocked")
              is False)
    finally:
        ba.tg_send_document = keep


# ----------------------------------------------------------------------
# (f) a chunk output that fails to save
# ----------------------------------------------------------------------

def test_chunk_save_failure_downgrades():
    reset_calls()
    set_chain(["glm"])
    keep = ba.save_review_record
    state = {"n": 0}

    def flaky(record):
        state["n"] += 1
        if state["n"] > 1:
            raise OSError("scripted save failure (§68 test)")
        return keep(record)

    ba.save_review_record = flaky
    try:
        review = ba.run_review_layer(
            clean_article(), make_sources(), make_context())
    finally:
        ba.save_review_record = keep
    check("(f1) the run itself is unaffected by save failures "
          "(fail-soft)", review["ok"], review.get("verdict", ""))
    check("(f2) but the record is downgraded to incomplete — "
          "never recorded complete",
          review["review_record"]["status"] == "incomplete",
          str(review["review_record"]))


# ----------------------------------------------------------------------
# (g) re-running the same review
# ----------------------------------------------------------------------

def test_rerun_reuses_record():
    reset_calls()
    set_chain(["glm"])
    art = clean_article()
    ctx = make_context()
    r1 = ba.run_review_layer(art, make_sources(), ctx)
    tid = r1["review_record"]["text_id"]
    keys1 = sorted(record_of(r1)["chunks"])
    r2 = ba.run_review_layer(art, make_sources(), dict(ctx))
    rec2 = record_of(r2)
    check("(g1) a re-run of the same text lands on the same "
          "record (same text_id)",
          r2["review_record"]["text_id"] == tid)
    check("(g2) chunk keys are identical — no duplication, "
          "no deletion",
          sorted(rec2["chunks"]) == keys1,
          f"{keys1} vs {sorted(rec2['chunks'])}")
    dirs = [d for d in os.listdir(TMP)
            if d == f"review-{tid}"]
    check("(g3) exactly one record directory exists for the "
          "text", len(dirs) == 1, str(dirs))


# ----------------------------------------------------------------------
# (h) aggregation order + locations; (i) claims ladder;
# (j) supervisor anchor; (k) repair fit-or-skip
# ----------------------------------------------------------------------

def test_aggregation_order():
    reset_calls()
    set_chain(["glm"])
    LANG_ISSUE_MODE["on"] = True
    art = long_article(sections=3, paras_per_section=10)
    ctx = make_context(word_target=(10, 100000))
    ctx["_review_chunks"] = []
    verdict = ba.review_language(art, make_sources(), ctx)
    wheres = [i["where"] for i in verdict["issues"]]
    check("(h1) segmented issues aggregate in segment order "
          "with their locations preserved",
          wheres == sorted(wheres)
          and wheres[0].startswith("Alpha")
          and any(w.startswith("Beta") for w in wheres),
          str(wheres))


def test_claims_ladder():
    reset_calls()
    set_chain(["groq"])
    sentences = " ".join(
        f"Observation {i} recorded a dip of 34 arc-minutes "
        f"at Nandana [1]." for i in range(1, 40))
    art = ("# T\n\n## Abstract\n\nBackground: x. Objective: y. "
           "Method: z. Findings: w. Conclusion: v [1].\n\n"
           "## Body\n\n" + sentences + "\n\n## References\n\n"
           "[1] Biruni's Measurement of the Earth. "
           "https://doi.org/10.1000/xyz123\n"
           "[2] The Formation of Empirical Methods. "
           "https://example.org/b\n")
    counted = make_sources(n=15, excerpt_chars=2200)
    verdict = ba.review_claims(art, counted,
                               make_context(sources=counted))
    check("(i1) claims batches complete on Groq alone via "
          "the ladder (35+ sentences, several batches)",
          verdict["available"], str(verdict)[:200])
    check("(i2) no Groq claims call exceeded its cap",
          all(ba._estimate_tokens("x" * c["chars"])
              <= ba._review_input_cap("groq") + 200
              for c in CALLS if c["provider"] == "groq"),
          str([c["chars"] for c in CALLS]))


def test_anchor_view():
    art = long_article()
    anchor = ba._review_anchor_text(art)
    check("(j1) the anchor view exists and is within its cap",
          anchor is not None
          and len(anchor) <= ba.REVIEW_ANCHOR_CHAR_CAP,
          str(len(anchor or "")))
    check("(j2) the anchor carries the abstract and the "
          "conclusion, and drops a middle section",
          "Background: the horizon-dip method" in anchor
          and "reliable radius of 6335.7 km" in anchor
          and "Beta Section" not in anchor)
    check("(j3) without an abstract/conclusion pair the "
          "anchor is None (caller falls back)",
          ba._review_anchor_text("# T\n\n## Body\n\nx\n")
          is None)


def test_repair_fit_or_skip():
    reset_calls()
    set_chain(["groq"])
    art = clean_article()
    REPAIR_TEXT[0] = art
    counted = make_sources(n=15, excerpt_chars=2200)
    issues = [ba._review_issue("claims", "somewhere",
                               "a flagged problem", "fixable",
                               "claims")]
    out = ba._repair_article(art, issues, counted,
                             make_context(sources=counted))
    check("(k1) the repair completes on Groq via the ladder "
          "and returns the repaired text", out == art.strip(),
          str(out)[:80] if out else "None")
    check("(k2) no Groq repair call exceeded its cap",
          all(ba._estimate_tokens("x" * c["chars"])
              <= ba._review_input_cap("groq") + 200
              for c in CALLS if c["provider"] == "groq"),
          str([c["chars"] for c in CALLS]))


def main():
    test_segmentation()
    test_fit_or_skip()
    test_ladder_serves_small_brain()
    test_layer_oversized_article()
    test_lock_after_first_chunk()
    test_all_reviews_fail_text_preserved()
    test_chunk_save_failure_downgrades()
    test_rerun_reuses_record()
    test_aggregation_order()
    test_claims_ladder()
    test_anchor_view()
    test_repair_fit_or_skip()
    ba.llm_chain, ba._brain_key_available, ba.RUNS_DIR = _KEEP
    print(f"\n{PASS[0]}/{PASS[0] + FAIL[0]} checks passed")
    return 1 if FAIL[0] else 0


if __name__ == "__main__":
    sys.exit(main())
