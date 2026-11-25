"""Mocked offline checks for the §20.5 self-repair agent
(repair_agent.py). No network, no git, no real brain: every side
effect goes through a fake `deps` dict, exactly the seam
process_failed_run / attempt_patch use in production.

Scenarios: (i) an HTTP 402 / insufficient-balance log classifies
human-only and is never patched or re-dispatched; (ii) a traceback
inside build_article.py classifies code-defect; (iii) pure 429 /
timeout noise classifies transient and earns ONE plain re-dispatch;
(iv) a patch that breaks the tests is discarded and nothing is
committed; (v) a patch with all tests green is committed once and
re-dispatched once; (vi) a signature already at the attempt cap
escalates with no patch and no dispatch; (vii) the diff validator
rejects patches touching repair_agent.py or a workflow file (and
non-stdlib imports); (viii) the RESUME-FROM-FAILURE law: a GRIS
re-dispatch carries the failed project_id back in, and a Discovery
`resume` input is passed only when discovery.yml declares one.
"""
import repair_agent as ra

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


DISCOVERY_INPUTS = ["field", "topic", "language", "mode", "idea",
                    "batch", "chat_id", "article_category",
                    "article_type", "llm_provider"]
GRIS_INPUTS = ["field", "topic", "language", "mode", "chat_id",
               "article_category", "article_type", "project_id",
               "llm_provider"]

DISC_RUN = {"id": 111, "name": "Build Discovery article",
            "conclusion": "failure", "event": "workflow_dispatch",
            "path": ".github/workflows/discovery.yml",
            "inputs": {"field": "History of astronomy",
                       "topic": "Biruni Earth radius",
                       "language": "fa", "mode": "review",
                       "chat_id": "42", "article_category": "intl",
                       "article_type": "isi"}}
GRIS_RUN = {"id": 222, "name": "Build GRIS sample project",
            "conclusion": "failure", "event": "workflow_dispatch",
            "path": ".github/workflows/gris_sample.yml",
            "inputs": {"field": "History of astronomy",
                       "topic": "Biruni Earth radius",
                       "project_id": ""}}

LOG_402 = (
    "WARNING: brain gemini failed attempt (1/3): HTTP 402 Payment "
    "Required: insufficient balance\n"
    "brain switch: gemini failed after 3 attempts -> next brain\n"
    "FAILED (fail-closed): All brains failed: chain exhausted")

LOG_TB = (
    "retrieval done: 21 verified\n"
    "Traceback (most recent call last):\n"
    '  File "/home/runner/work/discovery_agent/discovery_agent/'
    'build_article.py", line 3906, in screen_sources\n'
    "    stats[\"drop_irrelevant\"] += 1\n"
    "ValueError: unsupported format character\n"
    "FAILED (fail-closed): unsupported format character")

LOG_429 = (
    "WARNING: brain glm failed attempt (1/3): HTTP 429 Too Many "
    "Requests\n"
    "WARNING: brain glm failed attempt (2/3): read timeout\n"
    "WARNING: brain gemini failed attempt (1/3): HTTP 429 Too Many "
    "Requests\n"
    "FAILED (fail-closed): All brains failed: chain exhausted")

LOG_GRIS_429 = (
    "gris project: gris-0007 — Biruni Earth radius\n"
    '"stage": "retrieval"\n'
    "WARNING: brain gemini failed attempt (1/3): HTTP 429 Too Many "
    "Requests\n"
    "FAILED (fail-closed): quota noise everywhere")

GOOD_DIFF = (
    "diff --git a/build_article.py b/build_article.py\n"
    "--- a/build_article.py\n"
    "+++ b/build_article.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-x = 1\n"
    "+x = 2\n")

BAD_DIFF_SELF = (
    "diff --git a/repair_agent.py b/repair_agent.py\n"
    "--- a/repair_agent.py\n"
    "+++ b/repair_agent.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-a = 1\n"
    "+a = 2\n")

BAD_DIFF_WORKFLOW = (
    "diff --git a/.github/workflows/discovery.yml "
    "b/.github/workflows/discovery.yml\n"
    "--- a/.github/workflows/discovery.yml\n"
    "+++ b/.github/workflows/discovery.yml\n"
    "@@ -1,1 +1,1 @@\n"
    "-name: x\n"
    "+name: y\n")

BAD_DIFF_IMPORT = (
    "diff --git a/build_article.py b/build_article.py\n"
    "--- a/build_article.py\n"
    "+++ b/build_article.py\n"
    "@@ -1,1 +1,2 @@\n"
    " x = 1\n"
    "+import requests\n")


def make_deps(run, log_text, state=None, brain_reply="",
              tests_ok=True):
    calls = {"commit": [], "dispatch": [], "report": [],
             "brain": 0, "restore": [], "save_state": 0}

    def brain_patch(prompt):
        calls["brain"] += 1
        return brain_reply

    def commit_paths(paths, msg):
        calls["commit"].append((list(paths), msg))
        return True

    def dispatch(wf_file, inputs):
        calls["dispatch"].append((wf_file, dict(inputs)))
        return True

    deps = {
        "fetch_run": lambda run_id: run,
        "fetch_log": lambda run_id: log_text,
        "load_state": lambda: (state if state is not None
                               else ra.empty_state()),
        "save_state": lambda s: calls.__setitem__(
            "save_state", calls["save_state"] + 1),
        "commit_paths": commit_paths,
        "read_repo_file": lambda rel: "x = 1\n",
        "brain_patch": brain_patch,
        "apply_patch": lambda diff: True,
        "restore_files": lambda files: calls["restore"].append(
            list(files)),
        "run_tests": lambda: (tests_ok,
                              [("test_dummy.py", 0 if tests_ok
                                else 1)]),
        "dispatch": dispatch,
        "declared_inputs": lambda wf: list(
            DISCOVERY_INPUTS if wf == "discovery.yml"
            else GRIS_INPUTS),
        "report": lambda text: calls["report"].append(text),
    }
    return deps, calls


# (i) 402 / insufficient balance -> human-only, hands off
cls = ra.classify_failure(LOG_402, "Build Discovery article")
check("(i1) 402 log classifies human-only",
      cls["klass"] == "human-only")
deps, calls = make_deps(DISC_RUN, LOG_402)
res = ra.process_failed_run(111, deps)
check("(i2) human-only is reported, never patched/dispatched",
      res["action"] == "reported_human_only"
      and calls["brain"] == 0 and calls["dispatch"] == [])
check("(i3) human-only records state only",
      len(calls["commit"]) == 1
      and calls["commit"][0][0] == [ra.STATE_REPO_PATH])

# (ii) traceback in build_article.py -> code-defect
cls = ra.classify_failure(LOG_TB, "Build Discovery article")
check("(ii1) traceback in repo code classifies code-defect",
      cls["klass"] == "code-defect")
check("(ii2) target file taken from the deepest repo frame",
      cls["target_file"] == "build_article.py"
      and 3906 in cls["line_numbers"])

# (iii) pure 429/timeout noise -> transient, one plain re-dispatch
cls = ra.classify_failure(LOG_429, "Build Discovery article")
check("(iii1) 429/timeout noise classifies transient",
      cls["klass"] == "transient")
deps, calls = make_deps(DISC_RUN, LOG_429)
res = ra.process_failed_run(111, deps)
check("(iii2) transient earns exactly one re-dispatch, no patch",
      res["action"] == "redispatched_transient"
      and len(calls["dispatch"]) == 1 and calls["brain"] == 0)
check("(iii3) re-dispatch carries the original inputs",
      calls["dispatch"][0][1].get("topic") == "Biruni Earth radius"
      and calls["dispatch"][0][0] == "discovery.yml")

# (iv) patch gate: tests fail -> discarded, nothing committed
deps, calls = make_deps(DISC_RUN, LOG_TB, brain_reply=GOOD_DIFF,
                        tests_ok=False)
cls = ra.classify_failure(LOG_TB, "Build Discovery article")
outcome = ra.attempt_patch(deps, "Build Discovery article", cls,
                           ra.extract_evidence(LOG_TB))
check("(iv1) failing tests discard the patch",
      outcome["ok"] is False and "tests failed" in outcome["reason"])
check("(iv2) patched files are restored, commit never called",
      calls["restore"] == [["build_article.py"]]
      and calls["commit"] == [])

# (v) patch gate: tests green -> commit once + re-dispatch once
deps, calls = make_deps(DISC_RUN, LOG_TB, brain_reply=GOOD_DIFF,
                        tests_ok=True)
res = ra.process_failed_run(111, deps)
check("(v1) green patch is committed and re-dispatched",
      res["action"] == "patched_and_redispatched")
check("(v2) exactly one commit, carrying the patched file + state",
      len(calls["commit"]) == 1
      and "build_article.py" in calls["commit"][0][0]
      and ra.STATE_REPO_PATH in calls["commit"][0][0]
      and calls["commit"][0][1].startswith("[self-repair]"))
check("(v3) exactly one re-dispatch after the commit",
      len(calls["dispatch"]) == 1)

# (vi) loop guard: signature at the cap -> escalate, hands off
cls = ra.classify_failure(LOG_TB, "Build Discovery article")
stop = ra.extract_stop_stage(LOG_TB)
sig = ra.problem_signature("Build Discovery article", cls, stop)
state = {"version": 1, "signatures": {sig: {
    "workflow": "Build Discovery article", "klass": "code-defect",
    "attempts": 2, "escalated": False,
    "history": [{"action": "patched"}, {"action": "patch_failed"}]}}}
deps, calls = make_deps(DISC_RUN, LOG_TB, state=state,
                        brain_reply=GOOD_DIFF)
res = ra.process_failed_run(111, deps)
check("(vi1) capped signature escalates",
      res["action"] == "escalated")
check("(vi2) escalation means no patch and no dispatch",
      calls["brain"] == 0 and calls["dispatch"] == [])

# (vii) diff validator rails
ok, reason, _ = ra.validate_patch(BAD_DIFF_SELF)
check("(vii1) a patch touching repair_agent.py is rejected",
      not ok and "repair_agent.py" in reason)
ok, reason, _ = ra.validate_patch(BAD_DIFF_WORKFLOW)
check("(vii2) a patch touching a workflow file is rejected",
      not ok)
ok, reason, _ = ra.validate_patch(BAD_DIFF_IMPORT)
check("(vii3) a patch adding a non-stdlib import is rejected",
      not ok and "requests" in reason)
ok, reason, info = ra.validate_patch(GOOD_DIFF)
check("(vii4) a minimal stdlib-clean patch passes validation",
      ok and info["files"] == ["build_article.py"])

# (viii) resume-from-failure law
deps, calls = make_deps(GRIS_RUN, LOG_GRIS_429)
res = ra.process_failed_run(222, deps)
check("(viii1) GRIS re-dispatch resumes the failed project",
      res["action"] == "redispatched_transient"
      and calls["dispatch"][0][1].get("project_id") == "gris-0007")
inputs = ra.build_dispatch_inputs(
    "Build Discovery article", DISCOVERY_INPUTS,
    {"field": "F", "topic": "T"}, {"failed_stage": "گزینش منابع"})
check("(viii2) no resume input while discovery.yml lacks one",
      "resume" not in inputs)
inputs = ra.build_dispatch_inputs(
    "Build Discovery article", DISCOVERY_INPUTS + ["resume"],
    {"field": "F", "topic": "T"}, {"failed_stage": "گزینش منابع"})
check("(viii3) resume input passed once the workflow declares it",
      inputs.get("resume") == "گزینش منابع")
keys = ra.parse_workflow_inputs(
    "on:\n  workflow_dispatch:\n    inputs:\n      field:\n"
    "        type: string\n      project_id:\n        type: string\n"
    "permissions:\n  contents: write\n")
check("(viii4) workflow input parser reads declared inputs",
      keys == ["field", "project_id"])

# a successful run is never touched
deps, calls = make_deps({**DISC_RUN, "conclusion": "success"},
                        LOG_429)
res = ra.process_failed_run(111, deps)
check("(ix) a non-failure run is skipped entirely",
      res["action"] == "skipped" and calls["dispatch"] == []
      and calls["commit"] == [])

failed = [n for n, ok in PASS if not ok]
print(f"\n{len(PASS) - len(failed)}/{len(PASS)} checks passed")
if failed:
    raise SystemExit("FAILED: " + ", ".join(failed))
