"""Mocked offline checks for §20.10 (topic adaptation).

Jupiter's law (2026-11-26): when a requested topic fails the
text-first gate because genuinely relevant counted sources are too
few for that exact framing, the engine must adapt the topic instead
of simply stopping — candidates grounded ONLY in the evidence in
hand, re-screened through the SAME pipeline, the SAME gate, at most
2 attempts, the change visible in the reports, the article and
state.json. If nothing passes, fail closed exactly as before.

Checks: (a) gate passes originally -> adaptation never called;
(b) gate fails, first adapted candidate passes -> production
proceeds with the adapted topic, report lines + article note +
state fields present, both checkpoints consumed; (c) all
candidates fail -> fail-closed, adaptations listed; (d) brain/parse
failure in candidate generation -> fail-closed as before, no crash;
(e) thresholds unchanged (counted=4 never accepted); (f) the
candidate-generation prompt is grounded in the counted sources.

No network: _fresh_screening, the brain (call_llm), the writer and
Telegram are fakes.
"""
import json
import os
import sys

import build_article as ba

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


FIELD = "Test Field"
TOPIC = "Original Narrow Topic"
ADAPTED = "Adapted Narrow Topic"
ADAPTED2 = "Second Adapted Topic"

CALLS = {"fresh": [], "llm_prompts": [], "write_topics": [],
         "consume": []}
DM = {"funnel": [], "report": [], "suggestions": []}
SENT_TEXTS = []
SAVED_STATE = {}
BRAIN = {"reply": "[]", "fail": False}


def make_source(title, **kw):
    s = {"title": title, "authors": ["A. Author"], "year": 2020,
         "venue": "Some Journal", "doi": "", "url": "https://x/" + title,
         "abstract": "abstract about " + title, "source_type": "article",
         "index": "OpenAlex", "registry_match": "",
         "fulltext": False, "fulltext_route": "", "fulltext_excerpt": "",
         "passages": False, "passage_hits": 0}
    s.update(kw)
    return s


def passing_counted():
    return [
        make_source("Primary Book", source_type="primary",
                    fulltext=True, fulltext_route="ia-passages",
                    passages=True, passage_hits=42,
                    fulltext_excerpt="primary passage text"),
        make_source("Counted Article Alpha", fulltext=True,
                    fulltext_route="unpaywall", passages=True,
                    passage_hits=11,
                    fulltext_excerpt="alpha passage text"),
        make_source("Counted Article Beta", fulltext=True,
                    passages=True, passage_hits=7,
                    fulltext_excerpt="beta passage text"),
        make_source("Article C", fulltext=True),
        make_source("Article D"),
    ]


def failing_counted():
    return [
        make_source("Counted Article Alpha", fulltext=True,
                    fulltext_route="unpaywall", passages=True,
                    passage_hits=11,
                    fulltext_excerpt="alpha passage text"),
        make_source("Lone Primary", source_type="primary",
                    fulltext=True, passages=True, passage_hits=5,
                    fulltext_excerpt="primary passage text"),
    ]


def four_counted():
    # counted=4 but otherwise gate-shaped: must NEVER be accepted (§e).
    return passing_counted()[:4]


SCREEN_PLAN = {}


def fake_fresh(field, topic, language, category, article_type, mode,
               api_key, model, tg_token):
    CALLS["fresh"].append(topic)
    counted = [dict(s) for s in SCREEN_PLAN[topic]]
    gate = ba.text_first_gate(counted)
    funnel = {"candidates": 9, "verified": 7, "with_content": 6,
              "relevant": gate["counted"], "counted": gate["counted"],
              "drop_irrelevant": 2, "drop_tradition": 0,
              "drop_disagreement": 0, "drop_no_content": 0,
              "registry_matched": 0, "primary_counted": 1,
              "fulltext_counted": gate["fulltext_passages"],
              "drop_language_gate": 0}
    return {"counted": counted, "funnel": funnel,
            "disagreement_titles": ["Near Miss Title"],
            "queries": ["q one"], "ft_keywords": ["kw"],
            "source_map": {"works": [{"title": "Primary Book"}]},
            "verified_count": 7, "gate": gate}


def fake_llm(prompt, api_key, model, temperature=0.4, json_mode=False,
             provider=None):
    CALLS["llm_prompts"].append(prompt)
    if BRAIN["fail"]:
        raise ba.Fail("mock brain failure")
    return BRAIN["reply"]


def fake_write(f, t, lang, sources, ak, m, guide="", word_target=None):
    CALLS["write_topics"].append(t)
    return ("A Title", "# A Title\n\nbody text",
            {"title_candidates": [], "next_topics": [],
             "word_count": 4})


def fake_tg_call(method, token, payload):
    if method == "sendMessage":
        SENT_TEXTS.append(payload.get("text", ""))
    return {"message_id": 1}


ba.get_registry = lambda: {}
ba.llm_provider = lambda: "glm"
ba.llm_chain = lambda: ["glm"]
ba.llm_model = lambda: "mock-model"
ba._brain_key_available = lambda name, key: True
ba.load_state = lambda: {"productions": []}
ba.save_state = lambda st: SAVED_STATE.update(st)
ba.tg_call = fake_tg_call
ba.tg_send_document = lambda *a, **k: {"message_id": 2}
ba.send_funnel_dm = lambda *a, **k: DM["funnel"].append((a, k))
ba.send_detailed_source_report_dm = (
    lambda *a, **k: DM["report"].append((a, k)))
ba.send_suggestions_dm = lambda *a, **k: DM["suggestions"].append((a, k))
ba.fetch_youtube_support = lambda q, k: []
# §33: the review layer is stubbed to a clean pass — these tests
# exercise main()'s checkpoint/adaptation behaviour, not the
# reviewers (test_review_layer.py covers them).
ba.run_review_layer = lambda a, c, ctx: {
    "ok": True, "verdict": "pass", "rounds": 0,
    "issues_found": 0, "issues_fixed": 0, "roles": {},
    "blocked_roles": [], "issue_records": [], "article": a}
ba.send_review_report_dm = lambda *a, **k: True
ba._fresh_screening = fake_fresh
ba.call_llm = fake_llm
ba.write_article = fake_write
ba.consume_stage_checkpoint = (
    lambda f, t, *a, **k: CALLS["consume"].append(t))

ENV_KEYS = ["FIELD", "TOPIC", "LANGUAGE", "MODE", "ARTICLE_CATEGORY",
            "ARTICLE_TYPE", "TELEGRAM_BOT_TOKEN", "CHAT_ID",
            "ARTICLE_CHAT_ID", "GEMINI_API_KEY", "RESUME"]
SAVED_ENV = {k: os.environ.get(k) for k in ENV_KEYS}
os.environ.update({
    "FIELD": FIELD, "TOPIC": TOPIC, "LANGUAGE": "en",
    "MODE": "review", "ARTICLE_CATEGORY": "intl",
    "ARTICLE_TYPE": "isi", "TELEGRAM_BOT_TOKEN": "tok",
    "CHAT_ID": "chan", "ARTICLE_CHAT_ID": "",
    "GEMINI_API_KEY": "k", "RESUME": ""})


def reset():
    for v in CALLS.values():
        v.clear()
    for v in DM.values():
        v.clear()
    SENT_TEXTS.clear()
    SAVED_STATE.clear()
    BRAIN["fail"] = False


def run_main():
    try:
        return ba.main(), None
    except ba.Fail as e:
        return None, e


# ---------------------------------------------------------------------------
# (a) gate passes originally -> adaptation never called
# ---------------------------------------------------------------------------
reset()
SCREEN_PLAN[TOPIC] = passing_counted()
BRAIN["reply"] = json.dumps([{"topic": ADAPTED, "reason": "unused"}])
rc, err = run_main()
check("(a1) original pass -> run succeeds", rc == 0 and err is None)
check("(a2) adaptation never called (no adaptation brain call, "
      "one screening; the only brain call is the §70 novelty "
      "assessment, which runs on every production)",
      CALLS["fresh"] == [TOPIC]
      and all("FAILED a strict text-first" not in p
              for p in CALLS["llm_prompts"])
      and all("novelty assessor" in p
              for p in CALLS["llm_prompts"])
      and len(CALLS["llm_prompts"]) == 1)
prod = SAVED_STATE["productions"][0]
check("(a3) state record: adapted_topic is null, original kept",
      prod["adapted_topic"] is None
      and prod["original_topic"] == TOPIC and prod["topic"] == TOPIC)

# ---------------------------------------------------------------------------
# (b) gate fails, first adapted candidate passes re-screening
# ---------------------------------------------------------------------------
reset()
SCREEN_PLAN[TOPIC] = failing_counted()
SCREEN_PLAN[ADAPTED] = passing_counted()
BRAIN["reply"] = json.dumps(
    [{"topic": ADAPTED,
      "reason": "Counted Article Alpha and Lone Primary support it"}])
rc, err = run_main()
check("(b1) adapted run succeeds", rc == 0 and err is None)
check("(b2) re-screening ran for the adapted topic via same pipeline",
      CALLS["fresh"] == [TOPIC, ADAPTED])
check("(b3) writer received the ADAPTED topic",
      CALLS["write_topics"] == [ADAPTED])
prod = SAVED_STATE["productions"][0]
check("(b4) state record stores original + adapted topics",
      prod["original_topic"] == TOPIC
      and prod["adapted_topic"] == ADAPTED
      and prod["topic"] == ADAPTED)
check("(b5) article carries the adaptation note naming both topics",
      any(TOPIC in t and ADAPTED in t and "adapted" in t
          for t in SENT_TEXTS))
adapt_b = DM["funnel"][-1][1].get("adaptation")
check("(b6) funnel DM received the adaptation info",
      adapt_b is not None and adapt_b["adapted_topic"] == ADAPTED)
funnel_text = ba.build_funnel_text(ADAPTED, {"candidates": 9}, True,
                                   adaptation=adapt_b)
check("(b7) funnel text carries original/adapted/reason lines",
      f"موضوع اولیه: {TOPIC}" in funnel_text
      and f"موضوع تغییر‌یافته: {ADAPTED}" in funnel_text
      and "دلیل تغییر موضوع:" in funnel_text)
report_text = ba.build_detailed_source_report(
    ADAPTED, passing_counted(), [], adaptation=adapt_b)
check("(b8) §20.7 report carries the adaptation lines",
      f"موضوع اولیه: {TOPIC}" in report_text
      and f"موضوع تغییر‌یافته: {ADAPTED}" in report_text)
check("(b9) both checkpoints consumed (adapted + original)",
      CALLS["consume"] == [ADAPTED, TOPIC])

# (f) candidate generation is grounded in the counted sources
prompt_b = CALLS["llm_prompts"][0] if CALLS["llm_prompts"] else ""
check("(f1) counted titles reach the brain prompt",
      "Counted Article Alpha" in prompt_b
      and "Lone Primary" in prompt_b)
check("(f2) counted passage excerpts reach the brain prompt",
      "alpha passage text" in prompt_b)
check("(f3) near-miss titles reach the brain prompt",
      "Near Miss Title" in prompt_b)

# ---------------------------------------------------------------------------
# (c) all candidates fail the gate -> fail-closed, adaptations listed
# ---------------------------------------------------------------------------
reset()
SCREEN_PLAN[TOPIC] = failing_counted()
SCREEN_PLAN[ADAPTED] = failing_counted()
SCREEN_PLAN[ADAPTED2] = failing_counted()
BRAIN["reply"] = json.dumps(
    [{"topic": ADAPTED, "reason": "r1"},
     {"topic": ADAPTED2, "reason": "r2"},
     {"topic": "Third Candidate Never Tried", "reason": "r3"}])
rc, err = run_main()
check("(c1) all candidates fail -> fail-closed (Fail raised)",
      rc is None and isinstance(err, ba.Fail))
check("(c2) at most 2 adaptation attempts were screened",
      CALLS["fresh"] == [TOPIC, ADAPTED, ADAPTED2])
adapt_c = DM["funnel"][-1][1].get("adaptation")
funnel_text = ba.build_funnel_text(TOPIC, {"candidates": 9}, False,
                                   adaptation=adapt_c)
check("(c3) final funnel lists the adaptations tried + counts",
      f"موضوع اولیه: {TOPIC}" in funnel_text
      and ADAPTED in funnel_text and ADAPTED2 in funnel_text
      and "شمارش‌شده: 2" in funnel_text)
check("(c4) nothing was written or saved as a production",
      CALLS["write_topics"] == [] and "productions" not in SAVED_STATE)

# ---------------------------------------------------------------------------
# (d) brain failure during candidate generation -> fail-closed, no crash
# ---------------------------------------------------------------------------
reset()
SCREEN_PLAN[TOPIC] = failing_counted()
BRAIN["fail"] = True
rc, err = run_main()
check("(d1) brain failure -> original Fail, no other crash",
      rc is None and isinstance(err, ba.Fail)
      and "relevant" in str(err))
check("(d2) no candidate was screened after the brain failure",
      CALLS["fresh"] == [TOPIC])

reset()
BRAIN["reply"] = "this is not json at all"
rc, err = run_main()
check("(d3) unparseable brain reply -> fail-closed as before",
      rc is None and isinstance(err, ba.Fail)
      and CALLS["fresh"] == [TOPIC])

# ---------------------------------------------------------------------------
# (e) thresholds unchanged: a counted=4 candidate is never accepted
# ---------------------------------------------------------------------------
reset()
SCREEN_PLAN[TOPIC] = failing_counted()
SCREEN_PLAN[ADAPTED] = four_counted()
BRAIN["reply"] = json.dumps([{"topic": ADAPTED, "reason": "almost"}])
rc, err = run_main()
check("(e1) counted=4 candidate rejected -> fail-closed",
      rc is None and isinstance(err, ba.Fail))
check("(e2) gate constants unchanged",
      ba.MIN_SOURCES == 5 and ba.MIN_FULLTEXT_SOURCES == 3
      and ba.MAX_TOPIC_ADAPTATIONS == 2)

# ---------------------------------------------------------------------------
for k, v in SAVED_ENV.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v

failed = [n for n, ok in PASS if not ok]
print(f"\n{len(PASS) - len(failed)}/{len(PASS)} checks passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
