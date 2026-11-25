"""Mocked offline checks for §20.6 (stage checkpointing + resume).

Jupiter's law (2026-11-25): wherever a problem appears, the bot
continues FROM THAT POINT, never from zero. When the screening/gate
stage completes, build_article.py persists the counted sources +
funnel to runs/<signature>/checkpoint.json; a dispatch with the
RESUME input set (any non-empty value — the repair agent passes the
failed stage's label) and matching inputs then skips matrix /
source-map / retrieval / screening entirely and goes straight to
the gate check + writing.

Checks: (a) a fresh run reaching screening writes a checkpoint with
the counted records + funnel (writer-capped excerpts); (b) a resume
dispatch with a matching checkpoint performs NO retrieval/screening
calls and reaches the writing stage with the checkpointed sources
(the §20.7 report still goes out); (c) signature mismatch -> fresh
full run; (d) corrupt checkpoint JSON -> fresh run, no crash;
(e) a successful resume consumes the checkpoint; a resume that
fails AFTER screening (in writing) keeps it available.

No network: Telegram, the LLM pipeline stages and the writer are
fakes; the checkpoint dir is a temp dir.
"""
import json
import os
import shutil
import sys
import tempfile

import build_article as ba

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


FIELD = "Test Field"
TOPIC = "Test Topic"
ARGS = (FIELD, TOPIC, "en", "intl", "isi", "review")
SIG = ba.run_signature(*ARGS)

# ---------------------------------------------------------------------------
# Sandbox: checkpoint dir + module fakes
# ---------------------------------------------------------------------------
TMP = tempfile.mkdtemp(prefix="ckpt-test-")
ba.RUNS_DIR = os.path.join(TMP, "runs")

CALLS = {"matrix": 0, "smap": 0, "wave1": 0, "primaries": 0,
         "pipeline": 0, "write": 0}
DM_CALLS = {"funnel": [], "report": [], "suggestions": []}
WRITE_SOURCES = []
WRITE_FAIL = {"on": False}


def reset_calls():
    for k in CALLS:
        CALLS[k] = 0
    WRITE_SOURCES.clear()


def make_source(title, **kw):
    s = {"title": title, "authors": ["A. Author"], "year": 2020,
         "venue": "Some Journal", "doi": "", "url": "https://x/" + title,
         "abstract": "abstract " * 40, "source_type": "article",
         "index": "OpenAlex", "registry_match": "",
         "fulltext": False, "fulltext_route": "", "fulltext_excerpt": "",
         "passages": False, "passage_hits": 0}
    s.update(kw)
    return s


COUNTED = [
    make_source("Primary Book", source_type="primary",
                primary_relation="author", fulltext=True,
                fulltext_route="ia-passages", passages=True,
                passage_hits=42,
                fulltext_excerpt="P" * 20000,
                catalogue_via="archive.org",
                catalogue_id="ia-primary"),
    make_source("Article A", fulltext=True,
                fulltext_route="unpaywall", passages=True,
                passage_hits=11, fulltext_excerpt="A" * 500),
    make_source("Article B", fulltext=True,
                fulltext_route="generic", passages=True,
                passage_hits=7, fulltext_excerpt="B" * 500),
    make_source("Article C", fulltext=True,
                fulltext_route="generic",
                fulltext_excerpt="C" * 500),
    make_source("Article D"),
]
STATS = {"verified": 7, "with_content": 6, "relevant": 5, "counted": 5,
         "drop_irrelevant": 2, "drop_tradition": 0,
         "drop_disagreement": 1, "drop_no_content": 0,
         "registry_matched": 1, "primary_counted": 1,
         "fulltext_counted": 4,
         "disagreement_titles": ["Near Miss Title"]}

ba.get_registry = lambda: {}
ba.llm_provider = lambda: "glm"
ba.llm_chain = lambda: ["glm"]
ba.llm_model = lambda: "mock-model"
ba._brain_key_available = lambda name, key: True
ba.load_state = lambda: {"productions": []}
ba.save_state = lambda st: None
ba.tg_call = lambda method, token, payload: {"message_id": 1}
ba.tg_send_document = lambda *a, **k: {"message_id": 2}
ba.send_funnel_dm = lambda *a, **k: DM_CALLS["funnel"].append(a)
ba.send_detailed_source_report_dm = (
    lambda *a, **k: DM_CALLS["report"].append(a))
ba.send_suggestions_dm = lambda *a, **k: DM_CALLS["suggestions"].append(a)
ba.fetch_youtube_support = lambda q, k: []


def fake_matrix(f, t, ak, m):
    CALLS["matrix"] += 1
    return {"queries": ["q one"], "core_keywords": ["kw"],
            "name_variants": []}


def fake_smap(f, t, ak, m):
    CALLS["smap"] += 1
    return {"keywords": [], "name_variants": [], "works": []}


def fake_wave1(queries, rstate):
    CALLS["wave1"] += 1
    rstate["total_candidates"] = 9
    return [make_source(f"Verified {i}") for i in range(7)]


def fake_primaries(*a, **k):
    CALLS["primaries"] += 1
    return []


def fake_pipeline(f, t, verified, ak, m, keywords=None):
    CALLS["pipeline"] += 1
    return [dict(s) for s in COUNTED], dict(STATS)


def fake_write(f, t, lang, sources, ak, m, guide="", word_target=None):
    CALLS["write"] += 1
    WRITE_SOURCES.extend(sources)
    if WRITE_FAIL["on"]:
        raise ba.Fail("mock writing failure")
    return ("A Title", "# A Title\n\nbody text",
            {"title_candidates": [], "next_topics": [],
             "word_count": 4})


ba.multilingual_query_matrix = fake_matrix
ba.build_source_map = fake_smap
ba.retrieve_wave1 = fake_wave1
ba.retrieve_primary_sources = fake_primaries
ba._quality_pipeline = fake_pipeline
ba.write_article = fake_write

ENV_KEYS = ["FIELD", "TOPIC", "LANGUAGE", "MODE", "ARTICLE_CATEGORY",
            "ARTICLE_TYPE", "TELEGRAM_BOT_TOKEN", "CHAT_ID",
            "ARTICLE_CHAT_ID", "GEMINI_API_KEY", "RESUME"]
SAVED_ENV = {k: os.environ.get(k) for k in ENV_KEYS}


def set_env(topic=TOPIC, resume=""):
    os.environ.update({
        "FIELD": FIELD, "TOPIC": topic, "LANGUAGE": "en",
        "MODE": "review", "ARTICLE_CATEGORY": "intl",
        "ARTICLE_TYPE": "isi", "TELEGRAM_BOT_TOKEN": "tok",
        "CHAT_ID": "chan", "ARTICLE_CHAT_ID": "",
        "GEMINI_API_KEY": "k", "RESUME": resume})


def read_checkpoint(sig=SIG):
    with open(ba.checkpoint_path(sig), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Signature unit checks
# ---------------------------------------------------------------------------
check("(s1) signature is stable and normalises case/whitespace",
      ba.run_signature("test  field", "Test Topic", "EN", "intl",
                       "isi", "review") == SIG)
check("(s2) signature differs when an input differs",
      ba.run_signature(FIELD, "Other Topic", "en", "intl", "isi",
                       "review") != SIG)
check("(s3) load with no checkpoint file -> None (fail-soft)",
      ba.load_stage_checkpoint(*ARGS) is None)

# ---------------------------------------------------------------------------
# (a) fresh run reaching screening writes the checkpoint
# ---------------------------------------------------------------------------
reset_calls()
set_env(resume="")
rc = ba.main()
ck = read_checkpoint()
check("(a1) fresh run succeeds", rc == 0)
check("(a2) fresh run ran matrix + pipeline exactly once",
      CALLS["matrix"] == 1 and CALLS["pipeline"] == 1)
check("(a3) checkpoint file written at runs/<signature>/",
      os.path.exists(ba.checkpoint_path(SIG)))
check("(a4) checkpoint carries the counted records",
      len(ck["counted"]) == 5
      and ck["counted"][0]["title"] == "Primary Book")
check("(a5) checkpoint carries the funnel + disagreement titles",
      ck["funnel"]["counted"] == 5
      and ck["disagreement_titles"] == ["Near Miss Title"])
check("(a6) checkpoint stage/inputs/verified count recorded",
      ck["stage_reached"] == "screened"
      and ck["inputs"]["topic"] == TOPIC
      and ck["verified_count"] == 7)
check("(a7) stored excerpt capped at the writer's cap (12000)",
      len(ck["counted"][0]["fulltext_excerpt"])
      == ba.FULLTEXT_CARD_CHARS)
check("(a8) successful fresh run consumed its own checkpoint",
      ck["status"] == "consumed")

# Unit: save -> load round-trip restores an available checkpoint.
ba.save_stage_checkpoint(FIELD, TOPIC, "en", "intl", "isi", "review",
                         ["q one"], {"works": []},
                         {"core_keywords": ["kw"],
                          "name_variants": []},
                         ["kw"], COUNTED,
                         dict(STATS, candidates=9,
                              drop_language_gate=0),
                         ["Near Miss Title"], 7)
loaded = ba.load_stage_checkpoint(*ARGS)
check("(a9) save/load round-trip: available checkpoint loads",
      loaded is not None and loaded["status"] == "available"
      and len(loaded["counted"]) == 5)

# ---------------------------------------------------------------------------
# (b) resume dispatch: NO retrieval/screening, straight to writing
# ---------------------------------------------------------------------------
reset_calls()
DM_CALLS["report"].clear()
set_env(resume="گزینش منابع مرتبط و محتوادار")  # any non-empty value
rc = ba.main()
check("(b1) resume run succeeds", rc == 0)
check("(b2) resume performed NO matrix/source-map/retrieval/screening",
      CALLS["matrix"] == 0 and CALLS["smap"] == 0
      and CALLS["wave1"] == 0 and CALLS["primaries"] == 0
      and CALLS["pipeline"] == 0)
check("(b3) resume reached the writing stage once", CALLS["write"] == 1)
check("(b4) writer received the checkpointed sources (primary first)",
      [s["title"] for s in WRITE_SOURCES]
      == ["Primary Book", "Article A", "Article B", "Article C",
          "Article D"])
check("(b5) §20.7 detailed report still sent on a resumed run",
      len(DM_CALLS["report"]) == 1)

# ---------------------------------------------------------------------------
# (e1) the successful resume consumed the checkpoint
# ---------------------------------------------------------------------------
check("(e1) successful resume marked the checkpoint consumed",
      read_checkpoint()["status"] == "consumed")
check("(e1b) a consumed checkpoint no longer loads for resume",
      ba.load_stage_checkpoint(*ARGS) is None)

# ---------------------------------------------------------------------------
# (e2) a resume failing AFTER screening keeps the checkpoint
# ---------------------------------------------------------------------------
ba.save_stage_checkpoint(FIELD, TOPIC, "en", "intl", "isi", "review",
                         ["q one"], {"works": []},
                         {"core_keywords": ["kw"],
                          "name_variants": []},
                         ["kw"], COUNTED,
                         dict(STATS, candidates=9,
                              drop_language_gate=0),
                         ["Near Miss Title"], 7)
reset_calls()
WRITE_FAIL["on"] = True
set_env(resume="screened")
try:
    ba.main()
    raised = False
except ba.Fail:
    raised = True
WRITE_FAIL["on"] = False
check("(e2a) writing failure on a resume run propagates (fail-closed)",
      raised)
check("(e2b) the failed resume still skipped screening",
      CALLS["pipeline"] == 0 and CALLS["write"] == 1)
check("(e2c) checkpoint kept available for another resume",
      read_checkpoint()["status"] == "available")

# ---------------------------------------------------------------------------
# (c) signature mismatch -> fresh full run
# ---------------------------------------------------------------------------
reset_calls()
set_env(topic="Another Topic", resume="screened")
rc = ba.main()
check("(c1) mismatched inputs run fresh and succeed", rc == 0)
check("(c2) fresh path ran the pipeline for the new signature",
      CALLS["matrix"] == 1 and CALLS["pipeline"] == 1)

# ---------------------------------------------------------------------------
# (d) corrupt checkpoint JSON -> fresh run, no crash
# ---------------------------------------------------------------------------
path = ba.checkpoint_path(SIG)
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w", encoding="utf-8") as fh:
    fh.write("{ this is not json")
reset_calls()
set_env(resume="screened")
rc = ba.main()
check("(d1) corrupt checkpoint -> fresh run, no crash, success",
      rc == 0)
check("(d2) corrupt checkpoint -> pipeline ran fresh",
      CALLS["pipeline"] == 1)
check("(d3) the fresh run overwrote the corrupt checkpoint",
      read_checkpoint()["status"] == "consumed")

# ---------------------------------------------------------------------------
for k, v in SAVED_ENV.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v
shutil.rmtree(TMP, ignore_errors=True)

failed = [n for n, ok in PASS if not ok]
print(f"\n{len(PASS) - len(failed)}/{len(PASS)} checks passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
