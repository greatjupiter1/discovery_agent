#!/usr/bin/env python3
"""GRIS Phase-0 sample — one complete research project, topic -> evidence-
backed article -> DOCX/PDF, delivered over Telegram (spec:
GRIS-sample-spec.md, incl. the 2026-11-25 §۹ amendment).

GENERAL by design: nothing in this module is specific to a field, a
topic, an author or a language. All inputs mirror discovery.yml's
(FIELD, TOPIC, LANGUAGE, MODE, ARTICLE_CATEGORY, ARTICLE_TYPE,
ARTICLE_CHAT_ID) plus PROJECT_ID (optional explicit project id).

Architecture: the retrieval -> verification -> full-text -> screening
engine is Discovery's, REUSED BY IMPORT (build_article). This module
adds only the sample's new modules on top of it:

  1. Project manager — every run is a project:
     projects/<project_id>/project.json (status, stage, funnel,
     checkpoints, repair_log, outputs), atomic writes, committed back
     by the workflow with the repo's merge/rebase pattern.
  2. Evidence matrix — projects/<project_id>/evidence.json: one record
     per documented claim, with an HONEST locator. Real page numbers
     come only from IA djvu full texts (form-feed page separators);
     anything not exactly determinable is "unverified". No guessed
     pages, ever.
  3. Claim-mapped writing + a deterministic (non-AI) checker: every
     factual sentence of the article must carry >= 1 valid [E-###]
     marker resolving to evidence.json and a counted source.
     Unsupported claims are reported; after one repair attempt the
     project fails closed rather than passing them silently.
  4. Exporter — DOCX via python-docx (lazily imported, so nothing else
     in this module depends on it) with real RTL bidi for Persian
     paragraphs + the evidence-matrix appendix; PDF via WeasyPrint
     from the same HTML intermediate, fail-soft.
  5. Supervisor (§۹ amendment) — stage checkpointing with
     resume-from-checkpoint, error classification (transient -> retry
     with backoff; brain -> the existing failover chain inside
     build_article; external -> wait/report; human-only -> NO
     auto-repair, the exact one human action is recorded), and a
     repair_log appended to project.json. NO code self-patching.

Inherited laws are unchanged (§§15-20.1 of build_article): the
text-first gate (>= 5 counted, >= 3 with full-text passages, >= 1
primary with passages), the source law, the title law, and the storage
law (book files are never stored; only capped extracted passages).

Secrets come from the environment exactly as in build_article
(TELEGRAM_BOT_TOKEN, CHAT_ID, GEMINI_API_KEY, GLM_API_KEY, ...) and are
never printed or logged.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_article as ba

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")

PASSAGE_MAX_CHARS = 2000          # spec §۵: evidence passage cap
EVIDENCE_PER_SOURCE_MAX = 6       # passages kept per counted source
PASSAGE_SEP = "\n\n[...]\n\n"     # build_article's excerpt separator
HYPOTHESIS_LABEL = "پیشنهاد/فرضیه"
EVIDENCE_ID_RE = re.compile(r"\[\s*(E-\d{3,})\s*\]")
STAGES = ("intake", "retrieval", "evidence", "writing", "check",
          "export", "delivery")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _atomic_write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Module 1 — project manager
# ---------------------------------------------------------------------------

def project_dir(project_id, base_dir=None):
    return os.path.join(base_dir or PROJECTS_DIR, project_id)


def project_path(project_id, base_dir=None):
    return os.path.join(project_dir(project_id, base_dir), "project.json")


def next_project_id(base_dir=None):
    """gris-0001, gris-0002, ... — the next free number on disk."""
    base = base_dir or PROJECTS_DIR
    n = 0
    if os.path.isdir(base):
        for name in os.listdir(base):
            m = re.fullmatch(r"gris-(\d+)", name)
            if m:
                n = max(n, int(m.group(1)))
    return f"gris-{n + 1:04d}"


def create_project(field, topic, language, category, article_type,
                   project_id=None, base_dir=None):
    pid = project_id or next_project_id(base_dir)
    os.makedirs(project_dir(pid, base_dir), exist_ok=True)
    project = {
        "project_id": pid,
        "id": pid,
        "field": field,
        "topic": topic,
        "language": language,
        "category": category,
        "type": article_type,
        "status": "running",          # running | completed | failed_closed
        "stage": "intake",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "finished_at": None,
        "funnel": {"candidates": 0, "verified": 0, "with_content": 0,
                   "relevant": 0, "counted": 0, "registry_matched": 0,
                   "primary_counted": 0, "fulltext_counted": 0,
                   "claims_supported": 0, "claims_total": 0,
                   "paged_evidence": 0, "repairs": 0},
        "checkpoints": {},            # stage -> {status, at, attempts}
        "repair_log": [],             # supervisor entries (§۹)
        "human_action_needed": None,
        "outputs": {"channel_messages": [], "docx": None, "pdf": None,
                    "html": None, "article_md": None},
    }
    save_project(project, base_dir)
    return project


def load_project(project_id, base_dir=None):
    with open(project_path(project_id, base_dir), encoding="utf-8") as f:
        return json.load(f)


def save_project(project, base_dir=None):
    project["updated_at"] = now_iso()
    project["funnel"]["repairs"] = len(project.get("repair_log", []))
    os.makedirs(project_dir(project["project_id"], base_dir),
                exist_ok=True)
    _atomic_write_json(project_path(project["project_id"], base_dir),
                       project)


def set_stage(project, stage, base_dir=None):
    project["stage"] = stage
    save_project(project, base_dir)


def checkpoint(project, stage, status, attempts=1, base_dir=None):
    project.setdefault("checkpoints", {})[stage] = {
        "status": status, "at": now_iso(), "attempts": attempts}
    save_project(project, base_dir)


def stage_done(project, stage):
    return (project.get("checkpoints", {}).get(stage, {})
            .get("status") == "done")


# ---------------------------------------------------------------------------
# Module 5 — supervisor (§۹ amendment): classification, retry, repair log
# ---------------------------------------------------------------------------

class SupervisorStop(ba.Fail):
    """A stop the supervisor will not auto-repair (human-only or
    unclassifiable). Carries the classification for the report."""

    def __init__(self, message, error_class, human_action=None):
        super().__init__(message)
        self.error_class = error_class
        self.human_action = human_action


_HUMAN_MARKERS = (
    "insufficient balance", "balance insufficient", "balance is 0",
    "revoked", "token expired", "api key expired", "key expired",
    "invalid api key", "invalid_api_key", "no usable llm brain",
    "missing required actions secret", "approval tap",
    "permission denied for token",
)
_BRAIN_MARKERS = ("brain", "gemini", "glm", "llm", "model")
_TRANSIENT_MARKERS = (
    "timeout", "timed out", "connection", "temporarily", "try again",
    "503", "502", "500", "429", "rate limit", "unavailable",
    "overloaded", "eof", "reset by peer",
)

_HUMAN_ACTIONS = (
    (("llm brain", "api key", "glm", "gemini"),
     "Renew or replace the LLM API key (GLM_API_KEY / GEMINI_API_KEY) "
     "in the repo's Actions secrets — keys are pasted once, by hand, "
     "in the GitHub dashboard."),
    (("telegram", "bot token"),
     "Re-issue the Telegram bot token with @BotFather and update "
     "TELEGRAM_BOT_TOKEN in the repo's Actions secrets."),
    (("balance",),
     "Top up the LLM provider balance, or switch the repo variable "
     "LLM_PROVIDER / LLM_CHAIN to a funded brain."),
    (("approval",),
     "Tap the pending approval card in the Muse app — approvals "
     "accept only the owner's own tap."),
)


def _human_action_for(text):
    low = text.lower()
    for markers, action in _HUMAN_ACTIONS:
        if any(m in low for m in markers):
            return action
    return ("Manual action required (no automatic repair is allowed "
            "for this class): " + text[:200])


def classify_error(exc):
    """Deterministic error classification (§۹):
    human_only — expired key, revoked token, zero balance, approval
                 tap: NEVER auto-repaired.
    brain      — the LLM layer failed (build_article's chain already
                 failed over; a stage retry is the supervisor's move).
    external   — platform outage / quota / 5xx from an outside service.
    transient  — network/timeout class: retry with backoff.
    unknown    — anything else: no retry, fail closed."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(m in text for m in _HUMAN_MARKERS):
        return "human_only"
    if "401" in text or "unauthorized" in text:
        return "human_only"
    if any(m in text for m in _BRAIN_MARKERS) and (
            "429" in text or "503" in text or "failed" in text
            or "exhausted" in text):
        return "brain"
    if any(m in text for m in _TRANSIENT_MARKERS):
        return "external" if ("503" in text or "429" in text
                              or "unavailable" in text) else "transient"
    return "unknown"


def log_repair(project, stage, error_class, action, detail="",
               base_dir=None):
    entry = {"at": now_iso(), "stage": stage,
             "error_class": error_class, "action": action,
             "detail": str(detail)[:300]}
    project.setdefault("repair_log", []).append(entry)
    save_project(project, base_dir)
    print(f"supervisor: [{error_class}] stage={stage} action={action} "
          f"— {entry['detail'][:120]}")
    return entry


def run_stage(project, stage, fn, base_dir=None, max_retries=2,
              sleep=None, backoff_base=2.0):
    """Run one pipeline stage under the supervisor: checkpoint on
    success; on failure classify the error — transient/external/brain
    are retried with backoff (each retry is a logged repair), human-
    only and unknown errors stop the run fail-closed with the exact
    human action recorded. Returns fn()'s result."""
    sleep = sleep if sleep is not None else time.sleep
    attempt = 0
    while True:
        attempt += 1
        try:
            result = fn()
            checkpoint(project, stage, "done", attempts=attempt,
                       base_dir=base_dir)
            return result
        except ba.Fail as e:
            # A deliberate fail-closed stop from the engine (e.g. the
            # text-first gate) is not a repair case — unless it is a
            # human-only secret/brain problem in disguise.
            if classify_error(e) == "human_only":
                action = _human_action_for(str(e))
                log_repair(project, stage, "human_only",
                           "no auto-repair (human-only class)",
                           str(e), base_dir=base_dir)
                project["human_action_needed"] = action
                save_project(project, base_dir=base_dir)
                raise SupervisorStop(str(e), "human_only", action)
            checkpoint(project, stage, "failed", attempts=attempt,
                       base_dir=base_dir)
            raise
        except Exception as e:
            error_class = classify_error(e)
            if error_class == "human_only":
                action = _human_action_for(str(e))
                log_repair(project, stage, error_class,
                           "no auto-repair (human-only class)",
                           str(e), base_dir=base_dir)
                project["human_action_needed"] = action
                save_project(project, base_dir=base_dir)
                raise SupervisorStop(str(e), error_class, action)
            if error_class in ("transient", "external", "brain") \
                    and attempt <= max_retries:
                wait = backoff_base * (2 ** (attempt - 1))
                log_repair(project, stage, error_class,
                           f"retry {attempt}/{max_retries} after "
                           f"{wait:.0f}s backoff", str(e),
                           base_dir=base_dir)
                sleep(wait)
                continue
            checkpoint(project, stage, "failed", attempts=attempt,
                       base_dir=base_dir)
            log_repair(project, stage, error_class,
                       "no retry (class not auto-repairable or "
                       "retries exhausted)", str(e), base_dir=base_dir)
            raise SupervisorStop(str(e), error_class)


# ---------------------------------------------------------------------------
# Module 2 — evidence matrix with page locators
# ---------------------------------------------------------------------------

def source_id_for(index):
    """Stable id of the n-th counted source (1-based, matching the
    article's reference numbering)."""
    return f"S-{index:02d}"


def evidence_source_type(source):
    """Map a counted Discovery source onto the spec's evidence types.
    Videos are support-only and never become evidence (None)."""
    st = source.get("source_type")
    if st == "primary":
        return "primary"
    if st == "video":
        return None
    text = ((source.get("venue") or "") + " "
            + (source.get("title") or "")).lower()
    if "thesis" in text or "dissertation" in text:
        return "thesis"
    route = source.get("fulltext_route") or ""
    if source.get("index") in ("Internet Archive", "Google Books") \
            or route.startswith("ia-") or route == "libgen":
        return "book_about"
    return "article"


def _page_of(full_text, chunk):
    """The real djvu page number (1-based) of a passage inside an IA
    _djvu.txt full text: form-feed (\\f) separators delimit pages, so
    the page is the count of separators before the passage + 1.
    Returns None when the passage cannot be located exactly — the
    caller then records the locator as unverified. NEVER guesses."""
    if not full_text or not chunk:
        return None
    i = full_text.find(chunk)
    if i >= 0:
        return full_text[:i].count("\f") + 1
    # Whitespace-tolerant fallback: collapse whitespace but protect the
    # form feeds with a sentinel first, so page counting stays exact.
    sentinel = "\x00"
    norm_text = re.sub(r"\s+", " ",
                       full_text.replace("\f", sentinel))
    norm_chunk = re.sub(r"\s+", " ",
                        chunk.replace("\f", sentinel)).strip()
    if len(norm_chunk) < 40:
        return None
    j = norm_text.find(norm_chunk)
    if j < 0:
        # A truncated tail may differ; try the chunk's head only.
        j = norm_text.find(norm_chunk[:200])
        if j < 0:
            return None
    return norm_text[:j].count(sentinel) + 1


def _first_sentence(text, cap=300):
    text = re.sub(r"\s+", " ", (text or "").strip())
    m = re.split(r"(?<=[.!?؟])\s+", text)
    return (m[0] if m else text)[:cap]


def build_evidence(counted, fetch_djvu=None):
    """Build the evidence matrix from the counted sources' extracted
    passages. One record per passage (spec §۵ schema). IA book texts
    are re-fetched (fail-soft) so every passage can be located on its
    real djvu page; article passages get a paragraph locator (their
    position within the extracted passages); a source with no full
    text contributes its abstract as context with an unverified
    locator. `fetch_djvu` is injectable for offline tests."""
    fetch_djvu = fetch_djvu or ba._ia_djvu_text
    records = []
    n = 0
    for i, s in enumerate(counted, 1):
        etype = evidence_source_type(s)
        if etype is None:
            continue
        sid = source_id_for(i)
        excerpt = s.get("fulltext_excerpt") or ""
        chunks = [c.strip() for c in excerpt.split(PASSAGE_SEP)
                  if c.strip()][:EVIDENCE_PER_SOURCE_MAX]
        full = None
        ia_id = ba._ia_id_from_url(s.get("url", "")) \
            if s.get("index") == "Internet Archive" else None
        if chunks and etype in ("primary", "book_about"):
            if ia_id is None and (s.get("fulltext_route") or "") \
                    .startswith("ia-"):
                ia_id = ba._ia_find_by_title(s.get("title", ""))
            if ia_id:
                try:
                    full = fetch_djvu(ia_id,
                                      cap=ba.IA_FULLTEXT_MAX_BYTES)
                except Exception as e:
                    print(f"evidence: djvu re-fetch failed for "
                          f"{ia_id}: {e}")
                    full = None
        if not chunks:
            abstract = (s.get("abstract") or "").strip()
            if not abstract:
                continue
            n += 1
            records.append({
                "evidence_id": f"E-{n:03d}",
                "claim": _first_sentence(abstract),
                "source_id": sid,
                "source_type": etype,
                "locator": {"kind": "unverified", "value": None},
                "passage": abstract[:PASSAGE_MAX_CHARS],
                "relation": "context",
                "confidence": "low",
            })
            continue
        for k, chunk in enumerate(chunks, 1):
            page = _page_of(full, chunk) if full else None
            if page is not None:
                locator = {"kind": "page", "value": page}
                confidence = "high"
            elif etype in ("primary", "book_about"):
                locator = {"kind": "unverified", "value": None}
                confidence = "low"
            else:
                locator = {"kind": "paragraph", "value": k}
                confidence = "medium"
            n += 1
            records.append({
                "evidence_id": f"E-{n:03d}",
                "claim": _first_sentence(chunk),
                "source_id": sid,
                "source_type": etype,
                "locator": locator,
                "passage": chunk[:PASSAGE_MAX_CHARS],
                "relation": "supports",
                "confidence": confidence,
            })
    return records


def save_evidence(project, records, base_dir=None):
    path = os.path.join(project_dir(project["project_id"], base_dir),
                        "evidence.json")
    _atomic_write_json(path, records)
    project["funnel"]["paged_evidence"] = sum(
        1 for r in records if r["locator"]["kind"] == "page")
    save_project(project, base_dir)
    return path


def load_evidence(project, base_dir=None):
    path = os.path.join(project_dir(project["project_id"], base_dir),
                        "evidence.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Module 3 — claim-mapped writing + deterministic checker
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?؟])\s+")


_MARKER_ONLY_RE = re.compile(
    r"^(\[\s*E-\d{3,}\s*\]|\[\d+\])([\s.,;:]*(\[\s*E-\d{3,}\s*\]|"
    r"\[\d+\]))*[\s.,;:]*$")


def split_sentences(text):
    out = []
    for para in (text or "").split("\n"):
        para = para.strip()
        if not para:
            continue
        for sent in _SENTENCE_SPLIT_RE.split(para):
            sent = sent.strip()
            if not sent:
                continue
            # A fragment that is only citation markers belongs to the
            # sentence before it ("... text. [E-001]").
            if out and _MARKER_ONLY_RE.match(sent):
                out[-1] = out[-1] + " " + sent
            else:
                out.append(sent)
    return out


def is_factual_sentence(sentence):
    """Deterministic factual-claim heuristic for the checker: a
    sentence counts as a factual claim when it carries a number/date,
    an evidence or source marker, or is a full declarative sentence
    (>= 12 words). Questions and sentences the writer explicitly
    labelled as hypothesis are not factual claims."""
    s = (sentence or "").strip()
    if not s:
        return False
    if s.startswith(HYPOTHESIS_LABEL) or \
            s.lower().startswith("hypothesis"):
        return False
    if s.endswith("?") or s.endswith("؟"):
        return False
    if re.search(r"\d", s):
        return True
    if EVIDENCE_ID_RE.search(s) or re.search(r"\[\d+\]", s):
        return True
    return len(s.split()) >= 12


def _checkable_lines(article):
    """Article lines the checker judges: body prose only — headings,
    the title, the keywords line and everything from the References
    heading on are structural, not claims."""
    lines = []
    in_refs = False
    for line in (article or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("## References") or \
                stripped.startswith("## منابع"):
            in_refs = True
        if in_refs or not stripped or stripped.startswith("#"):
            continue
        if stripped.lower().startswith("keywords") or \
                stripped.startswith("کلیدواژه"):
            continue
        lines.append(line)
    return lines


def check_claims(article, evidence, counted_source_ids=None):
    """The deterministic (non-AI) claim checker (spec §۶). Every
    factual sentence must carry >= 1 evidence id that (a) exists in
    evidence.json and (b) belongs to a counted source. Returns a
    report dict: {total, supported, unsupported[], invalid_ids[],
    cited_ids[], ok}. ok requires ZERO invalid ids and ZERO
    unsupported factual sentences — nothing passes silently."""
    by_id = {r["evidence_id"]: r for r in evidence}
    counted_ids = set(counted_source_ids) if counted_source_ids \
        is not None else {r["source_id"] for r in evidence}
    total = supported = 0
    unsupported, invalid = [], []
    cited = set()
    for line in _checkable_lines(article):
        for sent in split_sentences(line):
            if not is_factual_sentence(sent):
                continue
            total += 1
            ids = EVIDENCE_ID_RE.findall(sent)
            good = False
            for eid in ids:
                rec = by_id.get(eid)
                if rec is None or rec["source_id"] not in counted_ids:
                    if eid not in invalid:
                        invalid.append(eid)
                else:
                    good = True
                    cited.add(eid)
            if good:
                supported += 1
            else:
                unsupported.append(sent)
    return {"total": total, "supported": supported,
            "unsupported": unsupported, "invalid_ids": invalid,
            "cited_ids": sorted(cited),
            "ok": not invalid and not unsupported and total > 0}


def label_unsupported(article, report):
    """Explicitly label every unsupported factual sentence as a
    hypothesis («پیشنهاد/فرضیه») — the spec's alternative to removal.
    The GRIS pipeline itself fails closed instead (see run_pipeline);
    this exists so the labelling route is available and tested."""
    out = article
    for sent in report.get("unsupported", []):
        labelled = f"{HYPOTHESIS_LABEL}: {sent}"
        out = out.replace(sent, labelled, 1)
    return out


def remove_unsupported(article, report):
    """Remove every unsupported factual sentence from the article."""
    out = article
    for sent in report.get("unsupported", []):
        out = out.replace(sent, "", 1)
    return re.sub(r"\n{3,}", "\n\n", out)


def fill_claims_from_article(evidence, article):
    """After a passing check, set each cited record's `claim` to the
    article sentence that actually cites it (markers stripped) — the
    matrix then documents the article's claims, not just passages."""
    by_id = {r["evidence_id"]: r for r in evidence}
    for line in _checkable_lines(article):
        for sent in split_sentences(line):
            for eid in EVIDENCE_ID_RE.findall(sent):
                rec = by_id.get(eid)
                if rec is not None:
                    rec["claim"] = EVIDENCE_ID_RE.sub(
                        "", sent).strip()[:500]
    return evidence


def evidence_cards_text(counted, evidence):
    """Writer input: per counted source, its evidence records (id,
    locator, passage digest). The writer may cite ONLY these ids."""
    by_source = {}
    for rec in evidence:
        by_source.setdefault(rec["source_id"], []).append(rec)
    lines = []
    for i, s in enumerate(counted, 1):
        sid = source_id_for(i)
        authors = ", ".join((s.get("authors") or [])[:6]) \
            or "Unknown authors"
        lines.append(
            f"[{i}] {s.get('title')}\n"
            f"    Authors: {authors}\n"
            f"    Year: {s.get('year') or 'n.d.'} | Venue: "
            f"{s.get('venue') or 'n/a'} | DOI: {s.get('doi') or 'none'}"
            f" | URL: {s.get('url')}")
        for rec in by_source.get(sid, []):
            loc = rec["locator"]
            loc_txt = (f"{loc['kind']} {loc['value']}"
                       if loc["value"] is not None else "unverified")
            lines.append(
                f"    {rec['evidence_id']} (source {sid}, locator: "
                f"{loc_txt}): {rec['passage'][:1200]}")
    return "\n".join(lines)


def write_claim_mapped_article(field, topic, language, counted,
                               evidence, api_key, model, guide="",
                               word_target=None):
    """The claim-mapped writer: same 3-part structure and brain chain
    as build_article.write_article, but the citation unit is the
    EVIDENCE ID — every factual sentence must end with its [E-###]
    markers, and anything the evidence does not cover must be omitted
    or explicitly labelled as a hypothesis. Returns (title, article,
    meta) like write_article."""
    lang_name = ba.LANGUAGE_NAMES.get(language.lower(), language)
    cards = evidence_cards_text(counted, evidence)
    if not word_target:
        word_target = (6000, 8000)
    lo, hi = word_target
    mid = (lo + hi) // 2
    hard_rules = (
        "HARD RULES — follow exactly:\n"
        "- Use ONLY the evidence records below. Never mention any "
        "work, author, number, journal or result that is not in them.\n"
        "- CLAIM MAPPING (GRIS): every factual sentence MUST end with "
        "the evidence id(s) that support it, written like [E-014]. A "
        "factual sentence without an evidence id is forbidden.\n"
        "- If a point is your own interpretation or a hypothesis the "
        "evidence does not prove, you may write it ONLY as a sentence "
        f"starting with the label «{HYPOTHESIS_LABEL}:» — never as a "
        "plain factual claim.\n"
        "- Synthesise in your own words; never copy a sentence from a "
        "passage.\n"
        "- DEPTH: work with the passages' ACTUAL content — specific "
        "data, formulas, numerical values, named methods and results "
        "exactly as the evidence gives them. Compare sources "
        "head-to-head: agreement, contradiction, and by how much.\n"
        "- NUMERIC DERIVATION: any numeric or formula-based claim "
        "must be RE-DERIVED step by step in the text from the "
        "evidence's stated inputs, and the derived value compared "
        "with the value the source itself reports.\n"
        "- NO GENERIC FILLER: any paragraph that could be pasted "
        "unchanged into an article on a different topic is forbidden.\n"
        f"- TARGET LENGTH: the complete article must reach "
        f"{lo}–{hi} words.\n"
        f"- Write the ENTIRE output in {lang_name}.\n")
    common = (f"Field: {field}\nTopic: {topic}\n\n{hard_rules}\n"
              + (f"{guide}\n" if guide else "")
              + f"THE ONLY EVIDENCE YOU MAY USE:\n{cards}\n")
    part1 = ba.call_llm(common +
        f"\nWrite PART 1 of a scholarly article (about "
        f"{int(mid * 0.18)} words):\n"
        "1) '## Abstract' — a structured abstract with labelled "
        "parts: Background, Objective, Method, Findings, Conclusion — "
        "based only on the evidence. The Findings must state concrete "
        "results from the evidence (named methods, values, dates), "
        "never generalities.\n"
        "2) '## Keywords' — 5 to 7 keywords on one line.\n"
        "3) '## Introduction' — broad context narrowing to the "
        "specific problem, the gap visible in this evidence, and the "
        "article's precise aim. Do NOT write a title.",
        api_key, model)
    ba.progress_update(55, "✍️ نگارش بخش اول")
    part2 = ba.call_llm(common +
        f"\nWrite PART 2 of the same article: the body (about "
        f"{int(mid * 0.52)} words). Organise the synthesis into 3 to "
        "5 thematic sections, each with a '## ' heading. In every "
        "section present the specific evidence (data, formulas, "
        "numbers, named methods and results), compare the sources, "
        "and analyse errors, uncertainties and disagreements where "
        "the evidence allows. Every factual sentence keeps its "
        "[E-###] markers. Do NOT repeat the abstract or introduction.",
        api_key, model)
    ba.progress_update(75, "✍️ نگارش بخش دوم")
    part3 = ba.call_llm(common +
        f"\nWrite PART 3 of the same article (about "
        f"{int(mid * 0.30)} words):\n"
        "1) '## Discussion' — what the combined evidence means and "
        "how it compares across the sources.\n"
        "2) '## Conclusion' — the takeaways the evidence actually "
        "supports.\n"
        "3) '## Limitations' — honest limits (evidence coverage, "
        "abstract/full-text level, free-access only).\n"
        "Do NOT write a reference list; it is added separately.",
        api_key, model)
    ba.progress_update(90, "✍️ نگارش بخش پایانی")
    candidates, best, next_topics = ba.generate_titles(
        field, topic, language, part1, part3, api_key, model)
    title = ba.select_title(candidates, best, field, topic)
    references = "\n".join(ba.format_reference(i, s)
                           for i, s in enumerate(counted, 1))
    article = (f"# {title}\n\n{part1}\n\n{part2}\n\n{part3}\n\n"
               f"## References\n\n{references}\n")
    word_count = len(article.split())
    print(f"gris writing: word count {word_count} "
          f"(target {lo}–{hi})")
    return title, article, {"title_candidates": candidates,
                            "next_topics": next_topics,
                            "word_count": word_count}


def repair_unsupported_claims(article, report, evidence, api_key,
                              model, language="fa"):
    """ONE writer repair attempt for the checker's unsupported
    sentences: the brain must rewrite each one either properly cited
    from the evidence or explicitly labelled as a hypothesis.
    Deterministic substitution of the returned sentences. Fail-soft:
    any failure returns the article unchanged (the checker will then
    fail the run closed)."""
    unsupported = report.get("unsupported") or []
    if not unsupported:
        return article
    lang_name = ba.LANGUAGE_NAMES.get(language.lower(), language)
    digest = "\n".join(
        f"{r['evidence_id']}: {r['passage'][:300]}"
        for r in evidence)
    numbered = "\n".join(f"{i + 1}. {s}"
                         for i, s in enumerate(unsupported))
    prompt = (
        "The sentences below appeared in a scholarly article WITHOUT "
        "evidence citations. Rewrite EACH sentence in "
        f"{lang_name}: either (a) keep its meaning and end it with "
        "the correct evidence id(s) like [E-014], using ONLY the "
        "evidence listed, or (b) if the evidence does not support "
        f"it, prefix it with «{HYPOTHESIS_LABEL}:». Reply with JSON "
        "only: {\"rewrites\": [one rewritten sentence per input "
        "sentence, same order]}.\n\n"
        f"SENTENCES:\n{numbered}\n\nEVIDENCE:\n{digest}\n")
    try:
        raw = ba.call_llm(prompt, api_key, model, temperature=0.2,
                          json_mode=True)
        data = ba._parse_json_value(raw)
        rewrites = [str(x).strip() for x in data.get("rewrites", [])]
    except Exception as e:
        print(f"gris repair: writer repair failed ({e}) — unchanged")
        return article
    if len(rewrites) != len(unsupported):
        print("gris repair: rewrite count mismatch — unchanged")
        return article
    out = article
    for old, new in zip(unsupported, rewrites):
        if new:
            out = out.replace(old, new, 1)
    return out


# ---------------------------------------------------------------------------
# Funnel report (§۱۶.۳ + the sample's three new lines)
# ---------------------------------------------------------------------------

def build_gris_funnel_text(topic, funnel, success, stage=None,
                           word_count=None, word_target=None):
    base = ba.build_funnel_text(topic, funnel, success, stage=stage,
                                word_count=word_count,
                                word_target=word_target)
    extra = [
        f"ادعاهای مستند: {funnel.get('claims_supported', 0)} از "
        f"{funnel.get('claims_total', 0)}",
        f"شواهد دارای صفحه: {funnel.get('paged_evidence', 0)}",
        f"تعمیرات ناظر: {funnel.get('repairs', 0)}",
    ]
    lines = base.splitlines()
    return "\n".join(lines[:-1] + extra + lines[-1:])


def send_gris_funnel_dm(token, chat_id, topic, funnel, success,
                        stage=None, word_count=None, word_target=None):
    """Fail-soft, mirroring build_article.send_funnel_dm."""
    text = build_gris_funnel_text(topic, funnel, success, stage=stage,
                                  word_count=word_count,
                                  word_target=word_target)
    for line in text.splitlines():
        print(f"funnel: {line}")
    try:
        chat_id = (chat_id or "").strip()
        if not chat_id or not token:
            print("funnel DM skipped: no ARTICLE_CHAT_ID/token")
            return False
        for chunk in ba.split_text(text, 3900):
            ba.tg_call("sendMessage", token,
                       {"chat_id": chat_id, "text": chunk})
        print(f"funnel DM sent to chat {chat_id}")
        return True
    except Exception as e:
        print(f"WARNING: funnel DM failed (ignored): {e}")
        return False


# ---------------------------------------------------------------------------
# Module 4 — exporter (DOCX via python-docx, PDF via WeasyPrint)
# ---------------------------------------------------------------------------

def _md_blocks(article):
    """Split the article markdown into (kind, text) blocks: h1, h2,
    h3, para. Deliberately minimal — the writer's structure only."""
    blocks = []
    para = []

    def flush():
        if para:
            blocks.append(("para", " ".join(para).strip()))
            para.clear()

    for line in (article or "").splitlines():
        s = line.strip()
        if s.startswith("### "):
            flush()
            blocks.append(("h3", s[4:].strip()))
        elif s.startswith("## "):
            flush()
            blocks.append(("h2", s[3:].strip()))
        elif s.startswith("# "):
            flush()
            blocks.append(("h1", s[2:].strip()))
        elif not s:
            flush()
        else:
            para.append(s)
    flush()
    return [b for b in blocks if b[1]]


def _is_rtl_text(text, language=""):
    if (language or "").lower() in ("fa", "ar", "ur"):
        return True
    return ba._arabic_script_ratio(text or "") > 0.3


def _esc(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def article_to_html(title, article, evidence, language="fa"):
    """The HTML intermediate: the PDF is rendered from exactly this,
    and it is also delivered as-is when the PDF step fails."""
    parts = ["<!DOCTYPE html><html><head><meta charset=\"utf-8\">",
             f"<title>{_esc(title)}</title>",
             "<style>body{font-family:sans-serif;line-height:1.7;"
             "margin:2em}h1{font-size:1.5em}h2{font-size:1.25em;"
             "margin-top:1.4em}table{border-collapse:collapse;"
             "font-size:.8em}td,th{border:1px solid #999;"
             "padding:3px 6px}</style></head><body>"]
    for kind, text in _md_blocks(article):
        rtl = _is_rtl_text(text, language)
        dirattr = ' dir="rtl"' if rtl else ""
        if kind == "para":
            parts.append(f"<p{dirattr}>{_esc(text)}</p>")
        else:
            parts.append(f"<{kind}{dirattr}>{_esc(text)}</{kind}>")
    parts.append(evidence_matrix_html(evidence, language))
    parts.append("</body></html>")
    return "\n".join(parts)


def evidence_matrix_html(evidence, language="fa"):
    heading = "ماتریس شواهد" if language == "fa" else "Evidence Matrix"
    rtl = ' dir="rtl"' if language == "fa" else ""
    rows = [f"<h2{rtl}>{_esc(heading)}</h2>",
            "<table><tr><th>ID</th><th>Source</th><th>Type</th>"
            "<th>Locator</th><th>Relation</th><th>Confidence</th>"
            "<th>Passage</th></tr>"]
    for rec in evidence:
        loc = rec["locator"]
        loc_txt = (f"{loc['kind']} {loc['value']}"
                   if loc["value"] is not None else "unverified")
        rows.append(
            "<tr>"
            f"<td>{_esc(rec['evidence_id'])}</td>"
            f"<td>{_esc(rec['source_id'])}</td>"
            f"<td>{_esc(rec['source_type'])}</td>"
            f"<td>{_esc(loc_txt)}</td>"
            f"<td>{_esc(rec['relation'])}</td>"
            f"<td>{_esc(rec['confidence'])}</td>"
            f"<td>{_esc(rec['passage'][:300])}</td></tr>")
    rows.append("</table>")
    return "\n".join(rows)


def export_docx(path, title, article, evidence, language="fa"):
    """DOCX export. python-docx is imported LAZILY here — it is the
    sample's only new dependency and nothing else in this module may
    depend on it. Persian/Arabic paragraphs get real RTL settings
    (w:bidi + right alignment). The evidence matrix is appended as
    the «ماتریس شواهد» appendix table."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    def set_rtl(paragraph):
        ppr = paragraph._p.get_or_add_pPr()
        bidi = OxmlElement("w:bidi")
        bidi.set(qn("w:val"), "1")
        ppr.append(bidi)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT

    doc = Document()
    for kind, text in _md_blocks(article):
        if kind == "para":
            p = doc.add_paragraph(text)
            if _is_rtl_text(text, language):
                set_rtl(p)
        else:
            level = {"h1": 0, "h2": 1, "h3": 2}[kind]
            h = doc.add_heading(text, level=level)
            if _is_rtl_text(text, language):
                set_rtl(h)
    heading = "ماتریس شواهد" if language == "fa" else "Evidence Matrix"
    h = doc.add_heading(heading, level=1)
    if language == "fa":
        set_rtl(h)
    table = doc.add_table(rows=1, cols=7)
    table.style = "Table Grid"
    headers = ["ID", "Source", "Type", "Locator", "Relation",
               "Confidence", "Passage"]
    for cell, text in zip(table.rows[0].cells, headers):
        cell.text = text
    for rec in evidence:
        loc = rec["locator"]
        loc_txt = (f"{loc['kind']} {loc['value']}"
                   if loc["value"] is not None else "unverified")
        cells = table.add_row().cells
        values = [rec["evidence_id"], rec["source_id"],
                  rec["source_type"], loc_txt, rec["relation"],
                  rec["confidence"], rec["passage"][:300]]
        for cell, text in zip(cells, values):
            cell.text = text
    doc.save(path)
    return path


def export_pdf(html_text, path):
    """PDF via WeasyPrint from the HTML intermediate — FAIL-SOFT:
    returns (path, None) on success, (None, error) on any failure
    (missing library included); the caller still delivers DOCX+HTML
    and reports the failure honestly."""
    try:
        from weasyprint import HTML
    except Exception as e:
        return None, f"WeasyPrint unavailable: {e}"
    try:
        HTML(string=html_text).write_pdf(path)
        return path, None
    except Exception as e:
        return None, f"WeasyPrint render failed: {e}"


# ---------------------------------------------------------------------------
# Pipeline — Discovery's stage functions, orchestrated per project
# ---------------------------------------------------------------------------

def _persist_json(ctx, name, data):
    path = os.path.join(project_dir(ctx["project"]["project_id"],
                                    ctx["base_dir"]), name)
    _atomic_write_json(path, data)
    return path


def _read_json(ctx, name):
    path = os.path.join(project_dir(ctx["project"]["project_id"],
                                    ctx["base_dir"]), name)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _persist_text(ctx, name, text):
    path = os.path.join(project_dir(ctx["project"]["project_id"],
                                    ctx["base_dir"]), name)
    _atomic_write_text(path, text)
    return path


def _read_text(ctx, name):
    path = os.path.join(project_dir(ctx["project"]["project_id"],
                                    ctx["base_dir"]), name)
    with open(path, encoding="utf-8") as f:
        return f.read()


def stage_retrieval(ctx):
    """Retrieval -> verification -> full-text -> screen, exactly the
    Discovery orchestration (ba.main's stage functions, incl. the
    tiered wave 2), ending at the §20 text-first gate."""
    project = ctx["project"]
    field, topic = ctx["field"], ctx["topic"]
    api_key, model = ctx["api_key"], ctx["model"]
    ba.get_registry()  # §17 registry (fail-soft inside)
    matrix = ba.multilingual_query_matrix(field, topic, api_key, model)
    queries = list(matrix["queries"])
    if not queries:
        queries = ba.english_first_queries(field, topic, api_key, model)
    if not queries:
        def add_query(q):
            if q and q not in queries:
                queries.append(q)
        add_query(topic)
        add_query(f"{field} {topic}")
        add_query(ba.translate_query(topic, api_key, model, "Arabic"))
        add_query(ba.translate_query_to_english(topic, api_key, model))
    print(f"gris queries: {queries}")
    smap = ba.build_source_map(field, topic, api_key, model)
    ft_keywords = []
    for kw in (list(smap.get("keywords") or [])
               + list(smap.get("name_variants") or [])
               + list(matrix["core_keywords"])
               + list(matrix["name_variants"])):
        if kw and kw not in ft_keywords:
            ft_keywords.append(kw)
    ctx["ft_keywords"] = ft_keywords
    ba.progress_update(15, "🔎 جست‌وجوی منابع")
    rstate = ba._new_retrieval_state()
    verified = ba.retrieve_wave1(queries, rstate)
    primaries = ba.retrieve_primary_sources(
        smap, rstate, extra_keywords=matrix["core_keywords"])
    if primaries:
        verified = verified + primaries
    ba.progress_update(35, "📚 راستی‌آزمایی منابع")
    funnel = {"candidates": 0, "verified": 0, "with_content": 0,
              "relevant": 0, "counted": 0, "drop_irrelevant": 0,
              "drop_tradition": 0, "drop_disagreement": 0,
              "drop_no_content": 0, "registry_matched": 0,
              "primary_counted": 0, "fulltext_counted": 0,
              "drop_language_gate": 0}
    counted, sstats = ba._quality_pipeline(
        field, topic, verified, api_key, model, keywords=ft_keywords)
    ba._merge_funnel(funnel, sstats)
    disagreement_titles = list(sstats.get("disagreement_titles")
                               or [])
    ba.progress_update(45, "🎯 گزینش منابع مرتبط")
    gate = ba.text_first_gate(counted)
    if not gate["ok"]:
        before = rstate["total_candidates"]
        w2_queries = list(queries) + [q for q in ba._wave2_queries(
            field, topic, api_key, model) if q not in queries]
        print(f"gris wave 2: gate not met after wave 1 "
              f"(counted={gate['counted']}, full-text passages="
              f"{gate['fulltext_passages']}, primary passages="
              f"{gate['primary_passages']})")
        book_first = [ba.search_internet_archive,
                      ba.search_annas_archive]
        order2 = book_first + [s for s in ba.SEARCHERS
                               if s not in book_first]
        verified2 = ba.collect_verified(w2_queries, state=rstate,
                                        searcher_order=order2)
        primaries2 = ba.retrieve_primary_sources(
            smap, rstate, extra_keywords=matrix["core_keywords"],
            max_new=ba.PRIMARY_MAX_VERIFIED - len(primaries))
        if primaries2:
            verified2 = verified2 + primaries2
        counted2, sstats2 = ba._quality_pipeline(
            field, topic, verified2, api_key, model,
            keywords=ft_keywords)
        ba._merge_funnel(funnel, sstats2)
        disagreement_titles += list(
            sstats2.get("disagreement_titles") or [])
        verified = verified + verified2
        counted = counted + counted2
        print(f"gris wave 2: +{rstate['total_candidates'] - before} "
              f"candidates, +{len(verified2)} verified, "
              f"+{len(counted2)} counted")
        ba.progress_update(48, "🎯 موج دوم گزینش منابع")
        gate = ba.text_first_gate(counted)
    funnel["candidates"] = rstate["total_candidates"]
    funnel["drop_language_gate"] = rstate.get("lang_gate_dropped", 0)
    for k, v in funnel.items():
        project["funnel"][k] = v
    # §20.7 mirror: the itemised source report is built at the gate
    # point (pass OR fail), printed to the log and stored on the
    # project; main() DMs it alongside the funnel. Read-only —
    # changes no screening behaviour.
    ctx["disagreement_titles"] = disagreement_titles
    ctx["counted"] = counted
    report = ba.build_detailed_source_report(
        topic, counted, disagreement_titles)
    project["source_report"] = report
    ctx["source_report"] = report
    for line in report.splitlines():
        print(f"source-report: {line}")
    save_project(project, ctx["base_dir"])
    ctx["funnel"] = funnel
    ctx["verified_count"] = len(verified)
    if not gate["ok"]:
        if gate["counted"] < ba.MIN_SOURCES:
            ctx["fail_stage"] = "گزینش منابع مرتبط و محتوادار"
            reason = (f"Only {gate['counted']} relevant, "
                      f"content-bearing sources (minimum "
                      f"{ba.MIN_SOURCES}). Refusing to write on "
                      "irrelevant or title-only material.")
        elif gate["fulltext_passages"] < ba.MIN_FULLTEXT_SOURCES:
            ctx["fail_stage"] = "دروازهٔ متن‌محور: متن کامل کافی نیست"
            reason = (f"Text-first gate (§20): only "
                      f"{gate['fulltext_passages']} counted sources "
                      f"carry full-text passages (minimum "
                      f"{ba.MIN_FULLTEXT_SOURCES}).")
        else:
            ctx["fail_stage"] = "دروازهٔ متن‌محور: منبع اصلیِ متن‌دار نیست"
            reason = ("Text-first gate (§20): no primary source with "
                      "passages extracted from its full text.")
        raise ba.Fail(reason)
    counted.sort(key=lambda s: 0 if s.get("passages")
                 else (1 if s.get("fulltext") else 2))
    ctx["counted"] = counted
    _persist_json(ctx, "sources.json", counted)


def _load_retrieval(ctx):
    ctx["counted"] = _read_json(ctx, "sources.json")
    ctx["funnel"] = dict(ctx["project"]["funnel"])
    return True


def stage_evidence(ctx):
    records = build_evidence(ctx["counted"])
    ctx["evidence"] = records
    save_evidence(ctx["project"], records, ctx["base_dir"])
    paged = sum(1 for r in records
                if r["locator"]["kind"] == "page")
    print(f"gris evidence: {len(records)} records, {paged} with "
          f"real page locators")


def _load_evidence(ctx):
    ctx["evidence"] = load_evidence(ctx["project"], ctx["base_dir"])
    return True


def stage_writing(ctx):
    project = ctx["project"]
    title, article, meta = write_claim_mapped_article(
        ctx["field"], ctx["topic"], ctx["language"], ctx["counted"],
        ctx["evidence"], ctx["api_key"], ctx["model"],
        guide=ba.TYPE_GUIDE[ctx["article_type"]],
        word_target=ba.TYPE_WORD_TARGETS.get(ctx["article_type"]))
    ctx["title"], ctx["article"], ctx["meta"] = title, article, meta
    _persist_text(ctx, "article_raw.md", article)
    _persist_json(ctx, "article_meta.json", {
        "title": title, "title_candidates": meta["title_candidates"],
        "next_topics": meta["next_topics"],
        "word_count": meta["word_count"]})


def _load_writing(ctx):
    ctx["article"] = _read_text(ctx, "article_raw.md")
    meta = _read_json(ctx, "article_meta.json")
    ctx["title"] = meta["title"]
    ctx["meta"] = {"title_candidates": meta["title_candidates"],
                  "next_topics": meta["next_topics"],
                  "word_count": meta["word_count"]}
    return True


def stage_check(ctx):
    project = ctx["project"]
    counted_ids = {source_id_for(i)
                   for i in range(1, len(ctx["counted"]) + 1)}
    report = check_claims(ctx["article"], ctx["evidence"], counted_ids)
    if report["invalid_ids"]:
        ctx["fail_stage"] = "راستی‌آزمایی ادعاها (نگاشت شواهد)"
        raise ba.Fail("Claim checker: invalid evidence id(s) cited: "
                      + ", ".join(report["invalid_ids"]))
    if report["unsupported"]:
        log_repair(project, "check", "content",
                   "one writer repair attempt for unsupported "
                   "claims", f"{len(report['unsupported'])} "
                   "unsupported sentence(s)", base_dir=ctx["base_dir"])
        repaired = repair_unsupported_claims(
            ctx["article"], report, ctx["evidence"], ctx["api_key"],
            ctx["model"], language=ctx["language"])
        if repaired != ctx["article"]:
            ctx["article"] = repaired
            _persist_text(ctx, "article_raw.md", repaired)
            report = check_claims(repaired, ctx["evidence"],
                                  counted_ids)
    _persist_json(ctx, "claim_report.json", {
        "total": report["total"], "supported": report["supported"],
        "unsupported": report["unsupported"],
        "invalid_ids": report["invalid_ids"],
        "cited_ids": report["cited_ids"], "ok": report["ok"]})
    ctx["claim_report"] = report
    project["funnel"]["claims_total"] = report["total"]
    project["funnel"]["claims_supported"] = report["supported"]
    save_project(project, ctx["base_dir"])
    if not report["ok"]:
        ctx["fail_stage"] = "راستی‌آزمایی ادعاها (نگاشت شواهد)"
        raise ba.Fail(
            "Claim checker: coverage incomplete — "
            f"{len(report['unsupported'])} factual claim(s) without "
            "valid evidence: "
            + " | ".join(s[:120] for s in report["unsupported"][:5]))
    fill_claims_from_article(ctx["evidence"], ctx["article"])
    save_evidence(project, ctx["evidence"], ctx["base_dir"])
    _persist_text(ctx, "article.md", ctx["article"])
    print(f"gris check: {report['supported']}/{report['total']} "
          "factual claims documented")


def _load_check(ctx):
    ctx["claim_report"] = _read_json(ctx, "claim_report.json")
    ctx["article"] = _read_text(ctx, "article.md")
    return True


def stage_export(ctx):
    project = ctx["project"]
    pid = project["project_id"]
    pdir = project_dir(pid, ctx["base_dir"])
    html_text = article_to_html(ctx["title"], ctx["article"],
                                ctx["evidence"], ctx["language"])
    html_name = f"{pid}-article.html"
    _persist_text(ctx, html_name, html_text)
    docx_name = f"{pid}-article.docx"
    export_docx(os.path.join(pdir, docx_name), ctx["title"],
                ctx["article"], ctx["evidence"], ctx["language"])
    pdf_name = f"{pid}-article.pdf"
    pdf_path, pdf_error = export_pdf(
        html_text, os.path.join(pdir, pdf_name))
    project["outputs"]["html"] = html_name
    project["outputs"]["docx"] = docx_name
    project["outputs"]["pdf"] = pdf_name if pdf_path else None
    project["outputs"]["pdf_error"] = pdf_error
    project["outputs"]["article_md"] = "article.md"
    save_project(project, ctx["base_dir"])
    print(f"gris export: docx={docx_name} pdf="
          f"{pdf_name if pdf_path else 'FAILED (fail-soft): ' + str(pdf_error)}")


def _load_export(ctx):
    project = ctx["project"]
    pid = project["project_id"]
    pdir = project_dir(pid, ctx["base_dir"])
    docx_name = f"{pid}-article.docx"
    if not os.path.exists(os.path.join(pdir, docx_name)):
        return False
    project["outputs"]["docx"] = docx_name
    project["outputs"]["html"] = f"{pid}-article.html"
    pdf_name = f"{pid}-article.pdf"
    project["outputs"]["pdf"] = pdf_name if os.path.exists(
        os.path.join(pdir, pdf_name)) else None
    project["outputs"]["article_md"] = "article.md"
    return True


def stage_delivery(ctx):
    project = ctx["project"]
    pid = project["project_id"]
    pdir = project_dir(pid, ctx["base_dir"])
    token, chat_id = ctx["tg_token"], ctx["chat_id"]
    outputs = project["outputs"]
    sent = outputs.setdefault("delivery", {})
    message_ids = outputs.setdefault("channel_messages", [])
    title = ctx["title"]
    header = (
        f"📚 Scientific Discovery Agent — GRIS sample ({pid})\n"
        f"Field: {ctx['field']}\nTopic: {ctx['topic']}\n"
        f"Language: {ctx['language']}\n"
        f"Verified sources: {ctx.get('verified_count', 0)} "
        f"(relevant, content-bearing: {len(ctx['counted'])})\n"
        f"نوع مقاله: {ba.TYPE_LABELS[ctx['article_type']]}\n\n{title}")
    if not sent.get("header"):
        r = ba.tg_call("sendMessage", token,
                       {"chat_id": chat_id, "text": header})
        message_ids.append(r["message_id"])
        sent["header"] = True
        save_project(project, ctx["base_dir"])
    if not sent.get("chunks"):
        for chunk in ba.split_text(ctx["article"]):
            r = ba.tg_call("sendMessage", token,
                           {"chat_id": chat_id, "text": chunk})
            message_ids.append(r["message_id"])
            time.sleep(0.3)
        sent["chunks"] = True
        save_project(project, ctx["base_dir"])
    docs = sent.setdefault("docs", [])
    for name in ("article_md", "docx", "pdf"):
        fname = outputs.get(name)
        if not fname or fname in docs:
            continue
        path = os.path.join(pdir, fname)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            content = f.read()
        r = ba.tg_send_document(token, chat_id, fname, content,
                                caption=title)
        message_ids.append(r["message_id"])
        docs.append(fname)
        save_project(project, ctx["base_dir"])
    # Record the production in state.json (the repo's registry),
    # exactly like build_article.main does, plus the project link.
    state = ba.load_state()
    key = (ba.norm(ctx["category"]), ba.norm(ctx["article_type"]),
           ba.norm(ctx["field"]), ba.norm(ctx["topic"]),
           ba.norm(ctx["language"]))
    already = any(
        (ba.norm(p.get("category") or "intl"),
         ba.norm(p.get("type") or "isi"), ba.norm(p.get("field")),
         ba.norm(p.get("topic")), ba.norm(p.get("language"))) == key
        for p in state.get("productions", []))
    if not already:
        state.setdefault("productions", []).append({
            "category": ctx["category"], "type": ctx["article_type"],
            "field": ctx["field"], "topic": ctx["topic"],
            "language": ctx["language"], "mode": "gris-sample",
            "title": title,
            "date": now_iso(),
            "sources": len(ctx["counted"]),
            "message_ids": message_ids,
            "project_id": pid,
        })
        ba.save_state(state)
    ba.progress_update(100, "انتشار در کانال")
    ba.send_suggestions_dm(
        token, os.environ.get("ARTICLE_CHAT_ID", ""), title,
        ctx["meta"].get("title_candidates", []),
        ctx["meta"].get("next_topics", []))
    # §20.7: itemised source report DM alongside the funnel.
    ba.send_detailed_source_report_dm(
        token, os.environ.get("ARTICLE_CHAT_ID", ""), ctx["topic"],
        ctx.get("counted", []), ctx.get("disagreement_titles"))
    send_gris_funnel_dm(
        token, os.environ.get("ARTICLE_CHAT_ID", ""), ctx["topic"],
        project["funnel"], True,
        word_count=ctx["meta"].get("word_count"),
        word_target=ba.TYPE_WORD_TARGETS.get(ctx["article_type"]))
    project["status"] = "completed"
    project["finished_at"] = now_iso()
    save_project(project, ctx["base_dir"])
    print(f"gris delivery: done, message_ids={message_ids}")


def _load_delivery(ctx):
    return ctx["project"].get("status") == "completed"


_STAGE_FUNCS = {"retrieval": stage_retrieval, "evidence": stage_evidence,
                "writing": stage_writing, "check": stage_check,
                "export": stage_export, "delivery": stage_delivery}
_STAGE_LOADERS = {"retrieval": _load_retrieval,
                  "evidence": _load_evidence, "writing": _load_writing,
                  "check": _load_check, "export": _load_export,
                  "delivery": _load_delivery}


def run_pipeline(ctx, stage_funcs=None, loaders=None):
    """Run (or resume) the project pipeline under the supervisor.
    Stages whose checkpoint is 'done' and whose artifacts load are
    skipped — a crashed run resumes from its failed stage, not from
    zero (§۹)."""
    funcs = dict(_STAGE_FUNCS)
    if stage_funcs:
        funcs.update(stage_funcs)
    loads = dict(_STAGE_LOADERS)
    if loaders:
        loads.update(loaders)
    project = ctx["project"]
    for stage in STAGES[1:]:
        set_stage(project, stage, ctx["base_dir"])
        if stage_done(project, stage) and stage in loads:
            try:
                if loads[stage](ctx):
                    print(f"gris resume: stage '{stage}' already "
                          "done — artifacts loaded, skipped")
                    continue
            except Exception as e:
                print(f"gris resume: artifacts for '{stage}' "
                      f"unusable ({e}) — re-running the stage")
        run_stage(project, stage, lambda f=funcs[stage]: f(ctx),
                  base_dir=ctx["base_dir"])
    return ctx


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    field = os.environ.get("FIELD", "").strip()
    topic = os.environ.get("TOPIC", "").strip()
    category = (os.environ.get("ARTICLE_CATEGORY") or "intl") \
        .strip().lower()
    if category not in ("ir", "intl"):
        category = "intl"
    article_type = (os.environ.get("ARTICLE_TYPE") or "isi") \
        .strip().lower()
    if article_type not in ba.TYPE_GUIDE:
        article_type = "isi"
    language = (os.environ.get("LANGUAGE") or "fa").strip()
    model = (os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash") \
        .strip()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()

    project_id = os.environ.get("PROJECT_ID", "").strip() or None
    if project_id and os.path.exists(project_path(project_id)):
        project = load_project(project_id)
        print(f"gris: resuming project {project_id} "
              f"(stage={project['stage']}, "
              f"status={project['status']})")
        field = project["field"]
        topic = project["topic"]
        language = project["language"]
        category = project["category"]
        article_type = project["type"]
    else:
        if not field or not topic:
            raise ba.Fail("FIELD and TOPIC inputs are required.")
        project = create_project(field, topic, language, category,
                                 article_type, project_id=project_id)
        print(f"gris: created project {project['project_id']}")

    ctx = {"project": project, "base_dir": None, "field": field,
           "topic": topic, "language": language, "category": category,
           "article_type": article_type, "api_key": api_key,
           "model": model, "tg_token": tg_token, "chat_id": chat_id,
           "counted": [], "evidence": [], "article": "", "title": "",
           "meta": {}, "funnel": dict(project["funnel"]),
           "fail_stage": None}

    def intake():
        chain = ba.llm_chain()
        if not any(ba._brain_key_available(name, api_key)
                   for name in chain):
            raise ba.Fail(
                "No usable LLM brain: chain " + ", ".join(chain)
                + " — none of its providers has its key available "
                "(GLM_API_KEY / GEMINI_API_KEY / GH_MODELS_TOKEN).")
        missing = [n for n, v in [("TELEGRAM_BOT_TOKEN", tg_token),
                                  ("CHAT_ID", chat_id)] if not v]
        if missing:
            raise ba.Fail("Missing required Actions secret(s): "
                          + ", ".join(missing))

    ba.progress_start(os.environ.get("ARTICLE_CHAT_ID", ""), tg_token)
    try:
        run_stage(project, "intake", intake)
        run_pipeline(ctx)
    except SupervisorStop as e:
        project["status"] = "failed_closed"
        project["finished_at"] = now_iso()
        save_project(project)
        stage = ctx.get("fail_stage") or project["stage"]
        if e.error_class == "human_only" and e.human_action:
            print(f"gris HUMAN ACTION NEEDED: {e.human_action}")
        ba.send_detailed_source_report_dm(
            tg_token, os.environ.get("ARTICLE_CHAT_ID", ""), topic,
            ctx.get("counted", []), ctx.get("disagreement_titles"))
        send_gris_funnel_dm(tg_token,
                            os.environ.get("ARTICLE_CHAT_ID", ""),
                            topic, project["funnel"], False,
                            stage=stage)
        ba.progress_fail()
        print(f"FAILED (fail-closed, supervisor class "
              f"{e.error_class}): {e}", file=sys.stderr)
        return 1
    except ba.Fail as e:
        project["status"] = "failed_closed"
        project["finished_at"] = now_iso()
        save_project(project)
        ba.send_detailed_source_report_dm(
            tg_token, os.environ.get("ARTICLE_CHAT_ID", ""), topic,
            ctx.get("counted", []), ctx.get("disagreement_titles"))
        send_gris_funnel_dm(tg_token,
                            os.environ.get("ARTICLE_CHAT_ID", ""),
                            topic, project["funnel"], False,
                            stage=ctx.get("fail_stage")
                            or project["stage"])
        ba.progress_fail()
        print(f"FAILED (fail-closed): {e}", file=sys.stderr)
        return 1
    print(f"gris: project {project['project_id']} completed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ba.Fail as e:
        ba.progress_fail()
        print(f"FAILED (fail-closed): {e}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        ba.progress_fail()
        raise
