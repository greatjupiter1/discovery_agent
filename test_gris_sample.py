"""Mocked offline checks for the GRIS Phase-0 sample (gris_sample.py):
project lifecycle, djvu page-locator evidence extraction, the evidence
schema, the deterministic claim checker, the DOCX exporter, and the
supervisor (classification, repair log, retry, resume-from-checkpoint).
No network: the djvu fetch and all pipeline stages are fakes."""
import json
import os
import sys
import tempfile

import gris_sample as g

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


TMP = tempfile.mkdtemp(prefix="gris-test-")


def src(title, **extra):
    s = {"title": title, "authors": ["A. Scholar"], "year": 2010,
         "venue": "Journal", "doi": None, "url": "https://example.org/x",
         "abstract": "", "arxiv_id": None, "index": "OpenAlex"}
    s.update(extra)
    return s


# ---------------------------------------------------------------------------
# 1. Project lifecycle
# ---------------------------------------------------------------------------
p1 = g.create_project("Physics", "Topic one", "fa", "intl", "isi",
                      base_dir=TMP)
check("project id auto-sequenced gris-0001",
      p1["project_id"] == "gris-0001")
check("project carries the spec fields",
      all(k in p1 for k in ("project_id", "field", "topic", "language",
                            "category", "type", "status", "stage",
                            "funnel", "outputs", "repair_log",
                            "created_at", "updated_at", "checkpoints")))
check("project starts running at intake",
      p1["status"] == "running" and p1["stage"] == "intake")
p2 = g.create_project("History", "Topic two", "en", "intl", "isi",
                      base_dir=TMP)
check("second project is gris-0002", p2["project_id"] == "gris-0002")
g.set_stage(p1, "evidence", base_dir=TMP)
g.checkpoint(p1, "evidence", "done", attempts=2, base_dir=TMP)
back = g.load_project("gris-0001", base_dir=TMP)
check("save/load roundtrip keeps stage + checkpoint",
      back["stage"] == "evidence"
      and back["checkpoints"]["evidence"]["status"] == "done"
      and back["checkpoints"]["evidence"]["attempts"] == 2)
check("stage_done helper", g.stage_done(back, "evidence")
      and not g.stage_done(back, "writing"))
pj = os.path.join(g.project_dir("gris-0001", TMP), "project.json")
with open(pj, encoding="utf-8") as f:
    raw = json.load(f)
check("project.json on disk is valid commit-shape JSON",
      raw["project_id"] == "gris-0001" and "funnel" in raw)

# ---------------------------------------------------------------------------
# 2. Evidence extraction with djvu page locators
# ---------------------------------------------------------------------------
CHUNK_P3 = ("The dip angle was measured as thirty four minutes and "
            "the height of the mountain was recorded carefully.")
FULL_DJVU = ("cover page text\f"
             "preface and contents\f"
             "chapter one begins. " + CHUNK_P3 + " More text follows."
             "\f"
             "chapter two text\f")
book = src("An Old Book", index="Internet Archive",
           url="https://archive.org/details/oldbook123",
           source_type="primary", fulltext_route="ia-passages",
           fulltext_excerpt=CHUNK_P3, passages=True)
records = g.build_evidence([book],
                           fetch_djvu=lambda ia_id, cap=None: FULL_DJVU)
check("one evidence record from the book passage", len(records) == 1)
r0 = records[0]
check("passage on djvu page 3 -> locator page 3",
      r0["locator"] == {"kind": "page", "value": 3})
check("book evidence type is primary", r0["source_type"] == "primary")
check("evidence id format E-001", r0["evidence_id"] == "E-001")

missing = src("Another Book", index="Internet Archive",
              url="https://archive.org/details/otherbook",
              source_type="primary", fulltext_route="ia-passages",
              fulltext_excerpt="A passage that is not in the text.",
              passages=True)
records2 = g.build_evidence(
    [missing], fetch_djvu=lambda ia_id, cap=None: FULL_DJVU)
check("unlocatable passage -> unverified, never a guessed page",
      records2[0]["locator"] == {"kind": "unverified", "value": None})

article = src("A Journal Paper", index="OpenAlex",
              fulltext_excerpt="First passage about the method.\n\n"
                               "[...]\n\nSecond passage with results.",
              passages=True)
records3 = g.build_evidence([article],
                            fetch_djvu=lambda ia_id, cap=None: None)
check("article passages get paragraph locators",
      [r["locator"] for r in records3]
      == [{"kind": "paragraph", "value": 1},
          {"kind": "paragraph", "value": 2}])

abstract_only = src("Abstract Only Paper",
                    abstract="This study examines the method in "
                             "detail and reports new results.")
records4 = g.build_evidence([abstract_only])
check("abstract-only source -> context record, unverified locator",
      len(records4) == 1
      and records4[0]["relation"] == "context"
      and records4[0]["locator"]["kind"] == "unverified")

video = src("A Lecture", source_type="video",
            fulltext_excerpt="transcript text", passages=True)
check("videos never become evidence",
      g.build_evidence([video]) == [])

# ---------------------------------------------------------------------------
# 3. Evidence record schema
# ---------------------------------------------------------------------------
check("evidence schema keys exact",
      set(r0.keys()) == {"evidence_id", "claim", "source_id",
                         "source_type", "locator", "passage",
                         "relation", "confidence"})
check("locator schema keys exact",
      set(r0["locator"].keys()) == {"kind", "value"})
check("passage capped at 2000 chars",
      all(len(r["passage"]) <= 2000
          for r in records + records2 + records3 + records4))
long_src = src("Long Passage Paper",
               fulltext_excerpt="x" * 5000, passages=True)
check("over-long passage truncated to the cap",
      len(g.build_evidence([long_src])[0]["passage"]) == 2000)
check("enums valid",
      r0["relation"] in ("supports", "contradicts", "context")
      and r0["confidence"] in ("high", "medium", "low")
      and r0["source_type"] in ("primary", "article", "thesis",
                                "book_about"))

# ---------------------------------------------------------------------------
# 4. Claim checker
# ---------------------------------------------------------------------------
EVID = [
    {"evidence_id": "E-001", "claim": "", "source_id": "S-01",
     "source_type": "primary", "locator": {"kind": "page", "value": 3},
     "passage": "p", "relation": "supports", "confidence": "high"},
    {"evidence_id": "E-002", "claim": "", "source_id": "S-02",
     "source_type": "article",
     "locator": {"kind": "paragraph", "value": 1},
     "passage": "p", "relation": "supports", "confidence": "medium"},
]
GOOD = ("# Title\n\n## Abstract\n\n"
        "The measured value was 6335 km in the year 1020 according "
        "to the primary text. [E-001]\n\n"
        "## Body\n\n"
        "The second study confirms the result with modern data and "
        "careful analysis of all the surviving records. [E-002]\n\n"
        "## References\n\n[1] Someone. A Book.\n")
rep = g.check_claims(GOOD, EVID)
check("fully-supported article passes the checker",
      rep["ok"] and rep["total"] == 2 and rep["supported"] == 2)
check("cited ids collected", rep["cited_ids"] == ["E-001", "E-002"])

BAD = ("# Title\n\n## Body\n\n"
       "The measured value was 6335 km in the year 1020 according "
       "to the primary text. [E-001]\n\n"
       "The instrument was built in 1017 and used by three "
       "assistants at the site.\n")
rep2 = g.check_claims(BAD, EVID)
check("unsupported factual claim detected",
      not rep2["ok"] and len(rep2["unsupported"]) == 1
      and rep2["supported"] == 1)
labelled = g.label_unsupported(BAD, rep2)
check("labelling route marks the claim as hypothesis",
      g.HYPOTHESIS_LABEL in labelled
      and g.check_claims(labelled, EVID)["ok"])
removed = g.remove_unsupported(BAD, rep2)
check("removal route drops the unsupported sentence",
      "instrument was built" not in removed
      and g.check_claims(removed, EVID)["ok"])

INVALID = ("# Title\n\n## Body\n\n"
           "The measured value was 6335 km in the year 1020 "
           "somewhere. [E-999]\n")
rep3 = g.check_claims(INVALID, EVID)
check("invalid evidence id fails the checker",
      not rep3["ok"] and rep3["invalid_ids"] == ["E-999"])

filled = g.fill_claims_from_article(EVID, GOOD)
check("claims filled from the citing sentences",
      filled[0]["claim"].startswith("The measured value was 6335")
      and "[E-001]" not in filled[0]["claim"])

# ---------------------------------------------------------------------------
# 5. Supervisor: classification, retry, repair log
# ---------------------------------------------------------------------------
check("classification: timeout is transient",
      g.classify_error(TimeoutError("read timed out")) == "transient")
check("classification: 503 is external",
      g.classify_error(Exception("HTTP 503 Service Unavailable"))
      == "external")
check("classification: invalid key is human-only",
      g.classify_error(Exception("401 unauthorized: invalid api key"))
      == "human_only")
check("classification: zero balance is human-only",
      g.classify_error(Exception("insufficient balance"))
      == "human_only")
check("classification: missing secret is human-only",
      g.classify_error(Exception("Missing required Actions secret(s): "
                                 "TELEGRAM_BOT_TOKEN")) == "human_only"
      or g.classify_error(Exception("No usable LLM brain: chain glm"))
      == "human_only")
check("classification: unknown error is unknown",
      g.classify_error(ValueError("totally weird")) == "unknown")

proj = g.create_project("Field", "Supervisor topic", "fa", "intl",
                        "isi", project_id="gris-0901", base_dir=TMP)
calls = {"n": 0}


def flaky():
    calls["n"] += 1
    if calls["n"] < 3:
        raise TimeoutError("connection timed out")
    return "done"


out = g.run_stage(proj, "retrieval", flaky, base_dir=TMP,
                  sleep=lambda s: None)
check("supervisor retries transient failures until success",
      out == "done" and calls["n"] == 3)
check("each retry is a logged repair",
      len(proj["repair_log"]) == 2
      and all(e["error_class"] == "transient"
              for e in proj["repair_log"]))
check("checkpoint marked done after repair",
      g.stage_done(proj, "retrieval"))
on_disk = g.load_project("gris-0901", base_dir=TMP)
check("repair log persisted to project.json",
      len(on_disk["repair_log"]) == 2
      and on_disk["funnel"]["repairs"] == 2)

human_calls = {"n": 0}


def human_fail():
    human_calls["n"] += 1
    raise Exception("403: token revoked by provider")


try:
    g.run_stage(proj, "writing", human_fail, base_dir=TMP,
                sleep=lambda s: None)
    stopped = None
except g.SupervisorStop as e:
    stopped = e
check("human-only failure stops without any retry",
      stopped is not None and human_calls["n"] == 1
      and stopped.error_class == "human_only")
check("the exact human action is recorded",
      bool(proj.get("human_action_needed"))
      and proj["repair_log"][-1]["error_class"] == "human_only")

# ---------------------------------------------------------------------------
# 6. Resume from checkpoint
# ---------------------------------------------------------------------------
proj2 = g.create_project("Field", "Resume topic", "fa", "intl", "isi",
                         project_id="gris-0902", base_dir=TMP)
pdir = g.project_dir("gris-0902", TMP)
with open(os.path.join(pdir, "sources.json"), "w",
          encoding="utf-8") as f:
    json.dump([src("Counted One")], f)
with open(os.path.join(pdir, "evidence.json"), "w",
          encoding="utf-8") as f:
    json.dump(EVID, f)
g.checkpoint(proj2, "retrieval", "done", base_dir=TMP)
g.checkpoint(proj2, "evidence", "done", base_dir=TMP)
ran = []

def stub(name):
    def fn(ctx):
        ran.append(name)
        if name == "writing":
            ctx["title"] = "T"
            ctx["article"] = "# T\n"
            ctx["meta"] = {}
    return fn

ctx = {"project": proj2, "base_dir": TMP, "field": "Field",
       "topic": "Resume topic", "language": "fa", "category": "intl",
       "article_type": "isi", "api_key": "", "model": "m",
       "tg_token": "", "chat_id": "", "counted": [], "evidence": [],
       "article": "", "title": "", "meta": {},
       "funnel": {}, "fail_stage": None}
g.run_pipeline(ctx, stage_funcs={
    "retrieval": stub("retrieval"), "evidence": stub("evidence"),
    "writing": stub("writing"), "check": stub("check"),
    "export": stub("export"), "delivery": stub("delivery")})
check("resume skips checkpointed stages (artifacts loaded)",
      "retrieval" not in ran and "evidence" not in ran
      and len(ctx["counted"]) == 1 and len(ctx["evidence"]) == 2)
check("resume runs the remaining stages in order",
      ran == ["writing", "check", "export", "delivery"])

# ---------------------------------------------------------------------------
# 7. Funnel carries the three new lines
# ---------------------------------------------------------------------------
funnel = dict(proj["funnel"])
funnel.update({"claims_supported": 7, "claims_total": 9,
               "paged_evidence": 4})
text = g.build_gris_funnel_text("Topic", funnel, False, stage="x")
check("funnel carries documented-claims line",
      "ادعاهای مستند: 7 از 9" in text)
check("funnel carries paged-evidence line", "شواهد دارای صفحه: 4" in text)
check("funnel carries supervisor-repairs line",
      f"تعمیرات ناظر: {len(proj['repair_log'])}" in text)

# ---------------------------------------------------------------------------
# 8. DOCX smoke test (real when python-docx is available)
# ---------------------------------------------------------------------------
try:
    import docx  # noqa: F401
    HAVE_DOCX = True
except ImportError:
    HAVE_DOCX = False
if HAVE_DOCX:
    art = ("# عنوان مقاله\n\n## چکیده\n\n"
           "این یک پاراگراف فارسی برای آزمون راست‌به‌چپ است و شامل "
           "عدد ۱۲۳ می‌شود.\n\n## Body\n\n"
           "An English paragraph with the value 42 inside it.\n")
    out_path = os.path.join(TMP, "gris-0001-article.docx")
    g.export_docx(out_path, "عنوان مقاله", art, EVID, language="fa")
    doc = docx.Document(out_path)
    texts = [p.text for p in doc.paragraphs]
    check("docx opens and carries the article sections",
          any("عنوان مقاله" in t for t in texts)
          and any("ماتریس شواهد" in t for t in texts))
    check("docx appendix table carries the evidence ids",
          len(doc.tables) == 1
          and doc.tables[0].rows[1].cells[0].text == "E-001")
    fa_para = [p for p in doc.paragraphs
               if "پاراگراف فارسی" in p.text][0]
    check("persian paragraph has real RTL bidi in its XML",
          "w:bidi" in fa_para._p.xml)
    html_text = g.article_to_html("T", art, EVID, language="fa")
    check("html intermediate carries the evidence matrix",
          "ماتریس شواهد" in html_text and "E-001" in html_text)
    pdf_path, pdf_err = g.export_pdf(html_text,
                                     os.path.join(TMP, "x.pdf"))
    check("pdf step is fail-soft (path or honest error)",
          (pdf_path is not None and os.path.exists(pdf_path))
          or (pdf_path is None and bool(pdf_err)))
else:
    print("SKIP docx smoke test: python-docx not installed locally")

failed = [n for n, ok in PASS if not ok]
print(f"\n{len(PASS) - len(failed)}/{len(PASS)} checks passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
