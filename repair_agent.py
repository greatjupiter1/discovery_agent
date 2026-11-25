#!/usr/bin/env python3
"""§20.5 — self-repair agent for the Discovery / GRIS engine.

THE LAW (Jupiter, 2026-11-25): the bot must diagnose and repair its
own problems itself, always — no waiting for a human order per
defect. Standing honest limits (accepted by Jupiter): expired API
keys, revoked tokens, empty balances, platform outages and approval
taps are HUMAN-ONLY — never auto-repaired, only precisely reported.

RESUME-FROM-FAILURE LAW (Jupiter, 2026-11-25): wherever the problem
appeared, the bot continues FROM THAT POINT — never restarts from
zero. GRIS re-dispatches therefore carry the failed project's
project_id back in, so the new run resumes from the project's stage
checkpoints; Discovery has no resume input yet (§20.6 will add one),
so its failed stage is recorded and a `resume` input is passed only
if/when discovery.yml declares one.

How it runs: .github/workflows/repair.yml triggers on workflow_run
(completed) of the two production workflows and proceeds only for
conclusion == failure; it can also be dispatched manually with a
run_id. Pipeline:

  1. gather evidence — the failed run (Actions API) + its full log;
  2. classify — deterministic rules first (human-only / code-defect /
     external / transient / unclassified); a brain is used only to
     DRAFT a patch, never to decide the class;
  3. act —
     human-only : report the one human action needed; never patch;
     external   : report; at most ONE resume/re-dispatch;
     transient  : ONE resume/re-dispatch with the original inputs;
     code-defect: brain drafts a MINIMAL unified diff against ONE
                  patchable file; the patch ships only if the FULL
                  offline test suite passes on the patched tree —
                  otherwise everything is discarded (rollback);
     unclassified: report only (fail-closed, like the engine);
  4. loop guard — repairs/repair_state.json (committed to the repo)
     counts attempts per problem signature; max 2, then escalate to
     the owner with the diagnosis and what was tried;
  5. report — every outcome is recorded in repair_state.json and
     sent to the owner on Telegram (Persian, short), fail-soft.

Safety rails: patch <= 200 changed lines; the patcher may touch ONLY
{build_article.py, gris_sample.py, propose_topics.py} — never
repair_agent.py, workflows, state files or secrets; no new non-stdlib
imports; never force-push; commits land on main through git in the
runner with the same pull --rebase retry pattern as the producers.

Stdlib only, like the rest of the repo. build_article is IMPORTED
(never edited) for its brain chain (call_llm).
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
import io

REPO_DEFAULT = "greatjupiter1/discovery_agent"
API = "https://api.github.com"

PATCHABLE_FILES = ("build_article.py", "gris_sample.py",
                   "propose_topics.py")
FORBIDDEN_FILES = {"repair_agent.py", "test_repair_agent.py"}
FORBIDDEN_PREFIXES = (".github/", "repairs/", "projects/")
MAX_PATCH_CHANGED_LINES = 200
MAX_ATTEMPTS = 2
STATE_REPO_PATH = "repairs/repair_state.json"
WORKFLOW_FILES = {
    "Build Discovery article": "discovery.yml",
    "Build GRIS sample project": "gris_sample.yml",
}
# Modules a patch may import: the stdlib, plus the repo's own modules.
REPO_MODULES = {"build_article", "gris_sample", "propose_topics"}
try:
    STDLIB_MODULES = set(sys.stdlib_module_names)
except AttributeError:  # very old Python — conservative fallback list
    STDLIB_MODULES = {
        "os", "sys", "re", "json", "time", "io", "base64", "hashlib",
        "subprocess", "urllib", "zipfile", "datetime", "math", "random",
        "collections", "itertools", "functools", "html", "xml", "csv",
        "threading", "concurrent", "pathlib", "tempfile", "shutil",
        "unicodedata", "textwrap", " difflib".strip(), "typing",
        "dataclasses", "contextlib", "traceback", "warnings", "glob",
    }


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------
# GitHub API (runner: GITHUB_TOKEN; the agent never prints it)
# ------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Returning None makes urlopen raise HTTPError for a redirect,
    leaving the Location header readable on the exception."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _gh_headers():
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "discovery-self-repair"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def gh_api(method, path, body=None):
    data = (json.dumps(body).encode("utf-8")
            if body is not None else None)
    req = urllib.request.Request(API + path, data=data,
                                 headers=_gh_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw[:500]}
        return e.code, payload


def fetch_run(repo, run_id):
    """The failed run's record: workflow name, conclusion, workflow
    file path and — for workflow_dispatch runs — its inputs."""
    status, payload = gh_api(
        "GET", f"/repos/{repo}/actions/runs/{run_id}")
    if status != 200:
        raise RuntimeError(f"fetch run {run_id}: HTTP {status}")
    return {"id": payload.get("id"),
            "name": payload.get("name") or "",
            "conclusion": payload.get("conclusion"),
            "event": payload.get("event"),
            "path": payload.get("path") or "",
            "inputs": payload.get("inputs") or {}}


def fetch_run_log_text(repo, run_id):
    """Full log text of a run: GET .../logs answers 302 to a signed
    zip URL; capture Location, download unauthenticated, unzip and
    concatenate the step logs in filename order."""
    req = urllib.request.Request(
        API + f"/repos/{repo}/actions/runs/{run_id}/logs",
        headers=_gh_headers(), method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    location = None
    try:
        with opener.open(req, timeout=60) as resp:
            location = resp.headers.get("Location")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            location = e.headers.get("Location")
        else:
            raise
    if not location:
        raise RuntimeError(f"no log download URL for run {run_id}")
    with urllib.request.urlopen(location, timeout=120) as resp:
        blob = resp.read()
    parts = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in sorted(zf.namelist()):
            if name.endswith(".txt"):
                parts.append(zf.read(name).decode("utf-8", "replace"))
    text = "\n".join(parts)
    if len(text) > 3_000_000:  # keep head + tail, drop the middle
        text = text[:500_000] + "\n...[log middle elided]...\n" \
            + text[-2_500_000:]
    return text


# ------------------------------------------------------------------
# Evidence extraction
# ------------------------------------------------------------------

KEY_LINE_HINTS = ("FAILED", "Traceback", "Error", "HTTP ",
                  "deterministic drop", "catalogue", "second judge",
                  "brain ", "brain:", "unreachable", "ia-passages",
                  "مرحله", "منابع", "Traceback", "raise ", "Fail")


def extract_stop_stage(text):
    """The stage the run stopped at: the funnel's own stop line
    first, then a project.json stage echo, then the fail reason."""
    m = re.findall(r"تولید متوقف شد — مرحله: (.+)", text)
    if m:
        return m[-1].strip()[:120]
    m = re.findall(r'"stage":\s*"([A-Za-z_]+)"', text)
    if m:
        return m[-1].strip()
    m = re.findall(r"FAILED \(fail-closed\): (.+)", text)
    if m:
        return m[-1].strip()[:120]
    return ""


def extract_resume_stage(text):
    """For GRIS: the project stage to resume from, when the log
    echoes it (project.json stage or the stage function name)."""
    m = re.findall(r'"stage":\s*"([a-z_]+)"', text)
    if m:
        return m[-1]
    for stage in ("retrieval", "evidence", "writing", "checking",
                  "export", "delivery", "intake"):
        if f"stage_{stage}" in text:
            return stage
    return ""


def extract_project_id(inputs, log_text):
    """GRIS resume key: the failed run's project_id — from its
    dispatch inputs when present, else from the log."""
    pid = str((inputs or {}).get("project_id") or "").strip()
    if pid:
        return pid
    m = re.search(r"project_id[:= ]+(gris-\d+)", log_text)
    if m:
        return m.group(1)
    m = re.search(r"\b(gris-\d{4})\b", log_text)
    return m.group(1) if m else ""


def extract_evidence(text):
    lines = text.splitlines()
    key = [ln for ln in lines
           if any(h in ln for h in KEY_LINE_HINTS)]
    return {"tail": text[-6000:],
            "key_lines": "\n".join(key[-140:]),
            "stop_stage": extract_stop_stage(text)}


# ------------------------------------------------------------------
# Classification — deterministic rules FIRST; a brain never decides
# the class, it only drafts patches for the code-defect class.
# ------------------------------------------------------------------

def traceback_frames(text):
    return [(m.group(1), int(m.group(2)), m.group(3))
            for m in re.finditer(
                r'File "([^"]+)", line (\d+)(?:, in (\w+))?', text)]


def _lines_with(text, needle):
    return [ln for ln in text.splitlines() if needle in ln]


def classify_failure(log_text, workflow_name=""):
    """Returns {klass, markers, target_file, line_numbers,
    functions}. klass is one of: human-only, code-defect, external,
    transient, unclassified."""
    text = log_text or ""
    low = text.lower()
    out = {"klass": "unclassified", "markers": [],
           "target_file": None, "line_numbers": [], "functions": []}

    # --- 1) human-only: credentials / balance — never auto-repair.
    human = []
    if re.search(r"insufficient[ _]?(balance|funds|credit)", low) \
            or "balance insufficient" in low:
        human.append("insufficient balance (HTTP 402 class)")
    if re.search(r"(http|status|error|code)[^\n]{0,24}402\b", low) \
            or "payment required" in low:
        human.append("HTTP 402 payment required")
    if re.search(r"(http|status|error|code)[^\n]{0,24}401\b", low) \
            or "401 client error" in low:
        human.append("HTTP 401 unauthorized")
    for phrase in ("invalid api key", "api key not valid",
                   "api_key_invalid", "invalid_api_key",
                   "bad credentials", "token has been revoked",
                   "revoked token", "invalid authentication",
                   "authentication failed", "key is invalid"):
        if phrase in low:
            human.append(f"credential failure: {phrase}")
    for ln in _lines_with(text, "403"):
        ll = ln.lower()
        if any(w in ll for w in ("key", "token", "auth", "permission",
                                 "generativelanguage", "z.ai", "glm",
                                 "gemini", "openai", "api")):
            human.append("HTTP 403 on a credentialed API call")
            break
    if human:
        out.update(klass="human-only", markers=human)
        return out

    # --- 2) code-defect: the engine's own logic failed.
    defects = []
    frames = traceback_frames(text)
    repo_frames = [f for f in frames
                   if os.path.basename(f[0]) in PATCHABLE_FILES]
    if "traceback (most recent call last)" in low and repo_frames:
        base, line, func = repo_frames[-1]
        defects.append(
            f"traceback in {os.path.basename(base)}:{line} ({func})")
        out["target_file"] = os.path.basename(base)
        out["line_numbers"] = [f[1] for f in repo_frames
                               if os.path.basename(f[0])
                               == out["target_file"]]
        out["functions"] = [f[2] for f in repo_frames if f[2]]
    if "unknown llm provider" in low:
        defects.append("judge/brain-chain construction: "
                       "unknown LLM provider")
    if "deterministic drop" in text and re.search(
            r"ia-passages \([1-9]\d* keyword hits", text):
        defects.append("passage-carrying source killed by the "
                       "deterministic drop")
    if re.search(r"catalogue[^\n]*(not confirmed|unconfirmed)",
                 low) or "not confirmed by any catalogue" in low:
        defects.append("catalogue-matching rejection of a "
                       "mapped primary work")
    if defects:
        out.update(klass="code-defect", markers=defects)
        if not out["target_file"]:
            out["target_file"] = "build_article.py"
        return out

    # --- 3) external: a platform outage, not our code.
    ext = []
    five_xx = re.findall(r"(?:HTTP|status|error) (5\d\d)\b", text)
    providers = set()
    for ln in text.splitlines():
        if re.search(r"(?:HTTP|status|error) 5\d\d", ln):
            ll = ln.lower()
            for hint in ("gemini", "generativelanguage", "glm",
                         "z.ai", "groq", "openrouter", "sambanova",
                         "cloudflare", "github"):
                if hint in ll:
                    providers.add(hint)
    if len(five_xx) >= 6 and len(providers) >= 2:
        ext.append(f"5xx outage across providers "
                   f"({', '.join(sorted(providers))})")
    if "all brains failed" in low and len(five_xx) >= 3:
        ext.append("brain chain exhausted on 5xx responses")
    if text.count("unreachable") >= 3:
        ext.append("a whole source class unreachable "
                   "(all mirrors failed)")
    if ext:
        out.update(klass="external", markers=ext)
        return out

    # --- 4) transient: throttling / timeouts only.
    tr = []
    if re.search(r"(?:HTTP|status|error) 429\b|rate ?limit|"
                 r"too many requests|resource_exhausted", text):
        tr.append("HTTP 429 / rate limiting")
    if re.search(r"timed out|timeout|TimeoutError|read timeout",
                 text):
        tr.append("network timeouts")
    if tr:
        out.update(klass="transient", markers=tr)
        return out

    return out


def _normalise_signature_text(s):
    s = (s or "").lower()
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:120]


def problem_signature(workflow_name, cls, stop_stage):
    basis = "|".join([workflow_name or "",
                      _normalise_signature_text(stop_stage),
                      ",".join(sorted(cls.get("markers") or []))])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def human_action_for(markers):
    joined = " ".join(markers).lower()
    if "balance" in joined or "402" in joined:
        return ("موجودی سرویس مغز/کلید تمام شده — حساب را شارژ کن "
                "یا کلید تازه بساز و در repo secrets جایگزین کن.")
    if "401" in joined or "403" in joined or "credential" in joined \
            or "revoked" in joined:
        return ("کلید API نامعتبر/منقضی/باطل شده — کلید تازه بساز و "
                "در repo secrets جایگزین کن.")
    return "اعتبارنامهٔ سرویس را بررسی و در repo secrets تجدید کن."


# ------------------------------------------------------------------
# Repair state (repairs/repair_state.json, committed to the repo)
# ------------------------------------------------------------------

def empty_state():
    return {"version": 1, "signatures": {}}


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict) \
                and isinstance(state.get("signatures"), dict):
            return state
    except Exception:
        pass
    return empty_state()


def save_state_file(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ------------------------------------------------------------------
# Patch drafting + validation (the safety rails live here)
# ------------------------------------------------------------------

class PatchRejected(Exception):
    pass


def parse_unified_diff(diff_text):
    """Parse a unified diff → {files, added_lines, changed}.
    Raises PatchRejected on malformed input."""
    files, added, changed = [], [], 0
    in_hunk = False
    for line in (diff_text or "").splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            if not m:
                raise PatchRejected(f"bad diff header: {line[:80]}")
            if m.group(2) not in files:
                files.append(m.group(2))
            in_hunk = False
        elif line.startswith("+++ b/"):
            p = line[6:].strip()
            if p not in files:
                files.append(p)
        elif line.startswith("--- "):
            pass
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            added.append(line[1:])
            changed += 1
        elif in_hunk and line.startswith("-"):
            changed += 1
    if not files:
        raise PatchRejected("no file headers in diff")
    return {"files": files, "added_lines": added, "changed": changed}


def validate_patch(diff_text):
    """(ok, reason, info). Enforces: patchable file set only, size
    cap, stdlib-only new imports."""
    try:
        info = parse_unified_diff(diff_text)
    except PatchRejected as e:
        return False, str(e), None
    for path in info["files"]:
        if path in FORBIDDEN_FILES \
                or path.startswith(FORBIDDEN_PREFIXES) \
                or path not in PATCHABLE_FILES:
            return False, f"patch touches non-patchable file: {path}", \
                info
    if info["changed"] > MAX_PATCH_CHANGED_LINES:
        return False, (f"patch too large: {info['changed']} changed "
                       f"lines > {MAX_PATCH_CHANGED_LINES}"), info
    for line in info["added_lines"]:
        m = re.match(r"\s*(?:import|from)\s+([A-Za-z_][\w.]*)", line)
        if m:
            top = m.group(1).split(".")[0]
            if top not in STDLIB_MODULES and top not in REPO_MODULES:
                return False, f"non-stdlib import added: {top}", info
    return True, "ok", info


def extract_diff(reply):
    """Pull the unified diff out of a brain reply (fenced or raw)."""
    if not reply:
        return ""
    m = re.search(r"```(?:diff)?\s*\n(.*?)```", reply, re.S)
    candidate = m.group(1) if m else reply
    idx = candidate.find("diff --git ")
    if idx == -1:
        idx = candidate.find("--- a/")
    return candidate[idx:].strip() if idx != -1 else ""


def extract_excerpt(file_text, line_numbers=(), keywords=()):
    """The context a patch-drafting brain gets: ±90 lines around each
    implicated line, plus whole def-blocks mentioning a keyword.
    Capped, rendered with line numbers."""
    lines = (file_text or "").splitlines()
    ranges = []
    for ln in line_numbers or ():
        ranges.append((max(1, ln - 90), min(len(lines), ln + 90)))
    if keywords:
        def_starts = [i for i, ln in enumerate(lines)
                      if re.match(r"def \w+\(", ln)]
        for j, start in enumerate(def_starts):
            end = (def_starts[j + 1] if j + 1 < len(def_starts)
                   else len(lines))
            block = "\n".join(lines[start:end])
            if any(k in block for k in keywords):
                ranges.append((start + 1, min(end, start + 220)))
    if not ranges:
        ranges = [(1, min(len(lines), 200))]
    ranges.sort()
    merged = []
    for a, b in ranges:
        if merged and a <= merged[-1][1] + 5:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    out, total = [], 0
    for a, b in merged:
        for n in range(a, b + 1):
            out.append(f"{n:5d}| {lines[n - 1]}")
            total += 1
            if total >= 500:
                break
        out.append("  ...|")
        if total >= 500:
            break
    return "\n".join(out)


def excerpt_keywords(cls):
    keys = set(cls.get("functions") or [])
    joined = " ".join(cls.get("markers") or []).lower()
    if "catalogue" in joined:
        keys.update({"verify_work_in_catalogue", "build_source_map",
                     "catalogue"})
    if "deterministic" in joined:
        keys.update({"screen_sources", "deterministic"})
    if "provider" in joined or "chain" in joined:
        keys.update({"call_llm", "llm_chain"})
    return sorted(keys)


def build_patch_prompt(workflow_name, cls, evidence, target_file,
                       excerpt):
    return (
        "You are the self-repair patcher for a fail-closed research "
        "engine. A production run failed and the failure was "
        "classified as a CODE DEFECT in the engine itself.\n\n"
        f"Workflow: {workflow_name}\n"
        f"Diagnosis markers: {'; '.join(cls.get('markers') or [])}\n"
        f"Stop stage: {evidence.get('stop_stage') or 'unknown'}\n\n"
        "LOG EVIDENCE (key lines):\n"
        f"{evidence.get('key_lines') or '(none)'}\n\n"
        "LOG TAIL:\n"
        f"{(evidence.get('tail') or '')[-3000:]}\n\n"
        f"TARGET FILE: {target_file} (excerpt, line-numbered):\n"
        f"{excerpt}\n\n"
        "Write a MINIMAL unified diff that fixes ONLY this defect. "
        "Rules: reply with ONLY a ```diff fenced unified diff "
        f"(--- a/{target_file} / +++ b/{target_file}); touch no "
        "other file; at most 200 changed lines; Python stdlib "
        "imports only; preserve the engine's fail-closed laws "
        "(no fabrication, verification gates unchanged); do not "
        "weaken any quality gate to make the failure disappear — "
        "fix the logic that mis-handled a legitimate case.")


def brain_draft_patch(prompt):
    """Draft a patch through the engine's own brain chain
    (build_article.call_llm, imported — never edited)."""
    import build_article as ba
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = (os.environ.get("GEMINI_MODEL")
             or "gemini-3.5-flash").strip()
    return ba.call_llm(prompt, api_key, model, temperature=0.2)


def attempt_patch(deps, workflow_name, cls, evidence):
    """Code-defect path up to (not including) the commit: draft,
    validate, apply, run the FULL offline suite. On any failure the
    patched files are restored and nothing is committed by this
    function. Returns an outcome dict."""
    target = cls.get("target_file") or "build_article.py"
    try:
        original = deps["read_repo_file"](target)
    except Exception as e:
        return {"ok": False, "target": target,
                "reason": f"cannot read {target}: {e}"}
    excerpt = extract_excerpt(original, cls.get("line_numbers") or (),
                              excerpt_keywords(cls))
    prompt = build_patch_prompt(workflow_name, cls, evidence, target,
                                excerpt)
    try:
        reply = deps["brain_patch"](prompt)
    except Exception as e:
        return {"ok": False, "target": target,
                "reason": f"brain chain failed while drafting: {e}"}
    diff = extract_diff(reply)
    if not diff:
        return {"ok": False, "target": target,
                "reason": "brain reply carried no unified diff"}
    ok, reason, info = validate_patch(diff)
    if not ok:
        return {"ok": False, "target": target,
                "reason": f"patch rejected: {reason}"}
    if not deps["apply_patch"](diff):
        deps["restore_files"](info["files"])
        return {"ok": False, "target": target,
                "reason": "diff did not apply cleanly — discarded"}
    tests_ok, results = deps["run_tests"]()
    if not tests_ok:
        deps["restore_files"](info["files"])
        failed = [n for n, rc in results if rc != 0]
        return {"ok": False, "target": target,
                "reason": "offline tests failed on the patched "
                          f"tree: {', '.join(failed)} — discarded",
                "test_results": results}
    return {"ok": True, "target": target, "files": info["files"],
            "changed": info["changed"], "diff": diff,
            "test_results": results}


# ------------------------------------------------------------------
# Git + test suite + dispatch (runner-side real implementations)
# ------------------------------------------------------------------

def _git(repo_dir, *args, check=True):
    r = subprocess.run(["git", "-C", repo_dir, *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: "
                           f"{r.stderr.strip()[:300]}")
    return r


def git_commit_push(repo_dir, paths, message):
    """Commit the given paths and push to main with the producers'
    pull --rebase retry pattern. Returns True when a commit landed
    (or there was nothing to commit). Never force-pushes."""
    _git(repo_dir, "config", "user.name", "discovery-self-repair",
         check=False)
    _git(repo_dir, "config", "user.email",
         "discovery-self-repair@users.noreply.github.com", check=False)
    _git(repo_dir, "add", "--", *paths)
    if _git(repo_dir, "diff", "--cached", "--quiet",
            check=False).returncode == 0:
        return True  # nothing staged — nothing to do
    _git(repo_dir, "commit", "-m", message)
    for attempt in range(3):
        _git(repo_dir, "pull", "--rebase", check=False)
        if _git(repo_dir, "push", check=False).returncode == 0:
            return True
        time.sleep(5)
    return False


def apply_patch_git(repo_dir, diff_text):
    patch_path = os.path.join(repo_dir, ".repair-agent.patch")
    try:
        with open(patch_path, "w", encoding="utf-8") as f:
            f.write(diff_text)
        r = _git(repo_dir, "apply", "--whitespace=nowarn",
                 patch_path, check=False)
        return r.returncode == 0
    finally:
        if os.path.exists(patch_path):
            os.remove(patch_path)


def git_restore(repo_dir, files):
    if files:
        _git(repo_dir, "checkout", "--", *files, check=False)


def run_test_suite(repo_dir):
    """Run EVERY test_*.py in the repo root, offline, one process
    each. Returns (all_ok, [(name, returncode), ...])."""
    import glob
    results = []
    for path in sorted(glob.glob(os.path.join(repo_dir, "test_*.py"))):
        name = os.path.basename(path)
        try:
            r = subprocess.run([sys.executable, path], cwd=repo_dir,
                               capture_output=True, text=True,
                               timeout=900)
            results.append((name, r.returncode))
        except subprocess.TimeoutExpired:
            results.append((name, -1))
    return all(rc == 0 for _, rc in results), results


def dispatch_workflow(repo, workflow_file, inputs):
    body = {"ref": "main",
            "inputs": {k: str(v) for k, v in inputs.items()}}
    status, _ = gh_api(
        "POST",
        f"/repos/{repo}/actions/workflows/{workflow_file}/dispatches",
        body)
    if status not in (201, 204):
        raise RuntimeError(f"dispatch {workflow_file}: HTTP {status}")
    return True


def parse_workflow_inputs(yaml_text):
    """Declared workflow_dispatch input names, parsed by indentation
    (stdlib has no YAML parser; the repo's workflows are regular)."""
    keys = []
    in_wd = in_inputs = False
    wd_indent = inputs_indent = 0
    for line in (yaml_text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if stripped == "workflow_dispatch:":
            in_wd, in_inputs = True, False
            wd_indent = indent
            continue
        if in_wd and indent <= wd_indent:
            in_wd = in_inputs = False
        if in_wd and stripped == "inputs:":
            in_inputs = True
            inputs_indent = indent
            continue
        if in_inputs:
            if indent <= inputs_indent:
                in_inputs = False
                continue
            if indent == inputs_indent + 2 and stripped.endswith(":"):
                keys.append(stripped[:-1].strip())
    return keys


def build_dispatch_inputs(workflow_name, declared, orig_inputs,
                          resume):
    """Re-dispatch inputs: the original inputs (filtered to what the
    workflow declares), plus the RESUME-FROM-FAILURE law — GRIS gets
    its project_id back (resume from checkpoints); Discovery gets a
    `resume` input ONLY if discovery.yml declares one (§20.6)."""
    declared = list(declared or [])
    inputs = {k: v for k, v in (orig_inputs or {}).items()
              if k in declared}
    if workflow_name == "Build GRIS sample project":
        pid = (resume or {}).get("project_id") or ""
        if pid and "project_id" in declared:
            inputs["project_id"] = pid
    if workflow_name == "Build Discovery article":
        if "resume" in declared and (resume or {}).get("failed_stage"):
            inputs["resume"] = resume["failed_stage"]
    return inputs


# ------------------------------------------------------------------
# Owner reporting (Telegram, fail-soft; the token is never printed)
# ------------------------------------------------------------------

CLASS_FA = {"human-only": "نیازمند اقدام انسانی",
            "external": "اختلال بیرونی (سرویس‌دهنده)",
            "transient": "خطای گذرا",
            "code-defect": "نقص کد موتور",
            "unclassified": "نامشخص"}


def report_owner(text):
    print(text)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("CHAT_ID", "")
    if not token or not chat:
        return
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat, "text": text}
                            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        print(f"WARNING: owner report via Telegram failed: {e}")


def _report_text(workflow_name, run_id, cls, action_line, resume):
    lines = ["🛠 گزارش خودتعمیر (§۲۰٫۵)",
             f"اجرا: {workflow_name} — شماره {run_id}",
             f"طبقهٔ مشکل: {CLASS_FA.get(cls['klass'], cls['klass'])}",
             "نشانه‌ها: " + ("؛ ".join(cls.get("markers") or [])
                             or "—"),
             action_line]
    if resume and resume.get("project_id"):
        lines.append(f"ادامه از همان نقطه: پروژه "
                     f"{resume['project_id']}"
                     + (f"، مرحلهٔ {resume['resume_stage']}"
                        if resume.get("resume_stage") else ""))
    return "\n".join(lines)


# ------------------------------------------------------------------
# Orchestration
# ------------------------------------------------------------------

def real_deps(repo, repo_dir):
    state_path = os.path.join(repo_dir, STATE_REPO_PATH)

    def declared_inputs(workflow_file):
        try:
            with open(os.path.join(repo_dir, ".github", "workflows",
                                   workflow_file),
                      encoding="utf-8") as f:
                return parse_workflow_inputs(f.read())
        except Exception:
            return []

    return {
        "fetch_run": lambda run_id: fetch_run(repo, run_id),
        "fetch_log": lambda run_id: fetch_run_log_text(repo, run_id),
        "load_state": lambda: load_state(state_path),
        "save_state": lambda state: save_state_file(state_path, state),
        "commit_paths": lambda paths, msg: git_commit_push(
            repo_dir, paths, msg),
        "read_repo_file": lambda rel: open(
            os.path.join(repo_dir, rel), encoding="utf-8").read(),
        "brain_patch": brain_draft_patch,
        "apply_patch": lambda diff: apply_patch_git(repo_dir, diff),
        "restore_files": lambda files: git_restore(repo_dir, files),
        "run_tests": lambda: run_test_suite(repo_dir),
        "dispatch": lambda wf_file, inputs: dispatch_workflow(
            repo, wf_file, inputs),
        "declared_inputs": declared_inputs,
        "report": report_owner,
    }


def process_failed_run(run_id, deps):
    """Full pipeline for one failed run. Every side effect goes
    through `deps`, so the offline tests drive this with fakes."""
    run = deps["fetch_run"](run_id)
    if not run:
        return {"action": "error", "detail": "run not found"}
    if run.get("conclusion") != "failure":
        return {"action": "skipped",
                "detail": f"conclusion={run.get('conclusion')}"}
    workflow_name = run.get("name") or ""
    workflow_file = WORKFLOW_FILES.get(
        workflow_name, os.path.basename(run.get("path") or ""))
    log_text = deps["fetch_log"](run_id)
    evidence = extract_evidence(log_text)
    cls = classify_failure(log_text, workflow_name)
    stop_stage = evidence["stop_stage"]
    sig = problem_signature(workflow_name, cls, stop_stage)
    orig_inputs = run.get("inputs") or {}
    resume = {"project_id": extract_project_id(orig_inputs, log_text),
              "failed_stage": stop_stage,
              "resume_stage": extract_resume_stage(log_text)}
    result = {"run_id": run_id, "workflow": workflow_name,
              "klass": cls["klass"], "signature": sig,
              "markers": cls["markers"], "stop_stage": stop_stage,
              "resume": resume}

    state = deps["load_state"]()
    entry = state["signatures"].setdefault(sig, {
        "workflow": workflow_name, "klass": cls["klass"],
        "attempts": 0, "escalated": False, "history": []})
    entry.update({"klass": cls["klass"], "last_run_id": run_id,
                  "stop_stage": stop_stage,
                  "resume_project_id": resume["project_id"] or None,
                  "resume_stage": resume["resume_stage"] or None})

    def persist(action, detail, commit_msg):
        entry["history"].append({"ts": _now(), "run_id": run_id,
                                 "action": action, "detail": detail})
        deps["save_state"](state)
        deps["commit_paths"]([STATE_REPO_PATH], commit_msg)

    def redispatch():
        declared = deps["declared_inputs"](workflow_file)
        inputs = build_dispatch_inputs(workflow_name, declared,
                                       orig_inputs, resume)
        deps["dispatch"](workflow_file, inputs)
        return inputs

    klass = cls["klass"]

    # Loop guard: this signature has used up its repair attempts.
    if klass in ("transient", "code-defect", "external") \
            and entry["attempts"] >= MAX_ATTEMPTS:
        entry["escalated"] = True
        detail = ("تلاش‌های قبلی: "
                  + "؛ ".join(h.get("action", "")
                              for h in entry["history"][-4:])
                  + " — اقدام لازم: بررسی انسانیِ همین نشانه‌ها.")
        persist("escalated", detail,
                f"[self-repair] escalate signature {sig}")
        deps["report"](_report_text(
            workflow_name, run_id, cls,
            "⛔ سقف تلاش خودتعمیر پر شد؛ دیگر خودکار دست نمی‌زنم. "
            + detail, resume))
        result.update(action="escalated", detail=detail)
        return result

    if klass == "human-only":
        action_needed = human_action_for(cls["markers"])
        persist("reported_human_only", action_needed,
                f"[self-repair] human-only failure on run {run_id}")
        deps["report"](_report_text(
            workflow_name, run_id, cls,
            "✋ خودکار تعمیر نمی‌شود. اقدام لازم از طرف تو: "
            + action_needed, resume))
        result.update(action="reported_human_only",
                      detail=action_needed)
        return result

    if klass == "unclassified":
        detail = ("نشانه‌ها با هیچ طبقه‌ای جور نشد؛ طبق قانون "
                  "fail-closed هیچ وصله و هیچ اجرای تازه‌ای نزد.")
        persist("reported_unclassified", detail,
                f"[self-repair] unclassified failure on run {run_id}")
        deps["report"](_report_text(
            workflow_name, run_id, cls,
            "❔ " + detail + f" مرحلهٔ توقف: {stop_stage or '—'}",
            resume))
        result.update(action="reported_unclassified", detail=detail)
        return result

    if klass == "external":
        if entry["attempts"] >= 1:
            detail = ("اختلال بیرونی ادامه دارد و یک بار قبلاً "
                      "از همان نقطه ادامه داده شد؛ دیگر تکرار "
                      "نمی‌کنم تا سرویس برگردد.")
            persist("reported_external", detail,
                    f"[self-repair] external failure on run {run_id}")
            deps["report"](_report_text(
                workflow_name, run_id, cls, "📡 " + detail, resume))
            result.update(action="reported_external", detail=detail)
            return result
        entry["attempts"] += 1
        redispatch()
        detail = ("اختلال از طرف سرویس‌دهنده است، نه کد؛ یک بار از "
                  "همان نقطه ادامه داده شد.")
        persist("redispatched_external", detail,
                f"[self-repair] re-dispatch after external failure "
                f"on run {run_id}")
        deps["report"](_report_text(
            workflow_name, run_id, cls, "📡 " + detail, resume))
        result.update(action="redispatched_external", detail=detail)
        return result

    if klass == "transient":
        entry["attempts"] += 1
        redispatch()
        detail = ("خطای گذرا (سهمیه/وقفهٔ شبکه)؛ بدون هیچ وصله‌ای "
                  "یک بار از همان نقطه ادامه داده شد.")
        persist("redispatched_transient", detail,
                f"[self-repair] re-dispatch after transient failure "
                f"on run {run_id}")
        deps["report"](_report_text(
            workflow_name, run_id, cls, "🔁 " + detail, resume))
        result.update(action="redispatched_transient", detail=detail)
        return result

    # klass == "code-defect": the patch path.
    entry["attempts"] += 1
    outcome = attempt_patch(deps, workflow_name, cls, evidence)
    if not outcome["ok"]:
        detail = outcome["reason"]
        persist("patch_failed", detail,
                f"[self-repair] failed patch attempt for run "
                f"{run_id}")
        deps["report"](_report_text(
            workflow_name, run_id, cls,
            "🧪 وصله ساخته شد ولی رد شد و هیچ تغییری ثبت نشد: "
            + detail, resume))
        result.update(action="patch_failed", detail=detail)
        return result
    diagnosis = "; ".join(cls["markers"])[:120]
    entry["history"].append({
        "ts": _now(), "run_id": run_id, "action": "patched",
        "detail": f"{outcome['files']} ({outcome['changed']} lines)"})
    deps["save_state"](state)
    deps["commit_paths"](
        list(outcome["files"]) + [STATE_REPO_PATH],
        f"[self-repair] {diagnosis}")
    redispatch()
    detail = (f"وصله روی {', '.join(outcome['files'])} نشست "
              f"({outcome['changed']} خط)، همهٔ تست‌های آفلاین سبز "
              "شدند، ثبت و از همان نقطه ادامه داده شد.")
    deps["report"](_report_text(
        workflow_name, run_id, cls, "✅ " + detail, resume))
    result.update(action="patched_and_redispatched", detail=detail)
    return result


def main():
    run_id = os.environ.get("REPAIR_RUN_ID") or (
        sys.argv[1] if len(sys.argv) > 1 else "")
    if not run_id:
        print("REPAIR_RUN_ID is not set — nothing to repair")
        return 2
    repo = os.environ.get("GITHUB_REPOSITORY") or REPO_DEFAULT
    repo_dir = os.environ.get("REPAIR_REPO_DIR") or os.path.dirname(
        os.path.abspath(__file__))
    deps = real_deps(repo, repo_dir)
    try:
        result = process_failed_run(str(run_id), deps)
    except Exception as e:
        print(f"self-repair agent itself failed: {e}")
        report_owner("🛠 گزارش خودتعمیر (§۲۰٫۵)\n"
                     f"اجرا: {run_id}\n"
                     f"⚠️ خودِ عامل تعمیر خطا خورد: {e}")
        return 0  # fail-soft: never mask the original failure
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
