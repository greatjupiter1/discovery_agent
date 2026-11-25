#!/usr/bin/env python3
"""Scientific Discovery Agent — topic proposal generator (§12, fail-closed).

One propose run:
  1. Read FIELD, IDEA, BATCH (1..5) and the requesting user's CHAT_ID.
  2. Collect every proposal title already sent to this user (all
     requests/<chat_id>-b*.json files in the repo checkout) — the new
     batch must never repeat one.
  3. Grounding: a quick retrieval on OpenAlex + Crossref + OpenAIRE +
     Europe PMC (what already exists around the idea) is digested and
     fed to the model, so the proposals aim at real gaps instead of
     reinventing published work.
  4. Gemini produces EXACTLY 10 proposals in three tiers (4 safe/close,
     3 angled/new-combination, 3 bold/frontier), strict JSON, validated;
     invalid output is retried with the validation error fed back
     (max 3 attempts), then the run fails loudly.
  5. Quick novelty screen per proposal: one OpenAlex search on the
     English title; a near-identical existing title (token-overlap
     similarity >= 0.8) drops novelty to 1 and flags the gap text.
     (Screening only — the deep novelty check happens at article stage.)
  6. Save requests/<chat_id>-b<batch>.json (the workflow commits it back).
  7. Send the proposals to the user's chat with an inline keyboard:
     pick buttons 1..10, a "10 more" button while batch < 5, and a
     back button (nav:topic_mode).

Retry law (same as build_article.py): EVERY transient failure — network
errors, timeouts, HTTP 429 and 5xx — is retried with backoff, up to 6
attempts, with a 180-second read timeout.

Inputs come from the environment (set by .github/workflows/discovery.yml):
  FIELD, IDEA, BATCH, CHAT_ID, GEMINI_MODEL (default "gemini-3.8-flash")
  LLM_PROVIDER (default "gemini"; "github" selects GitHub Models,
  "glm" selects GLM on z.ai),
  GH_MODELS_MODEL (default "openai/gpt-4o"),
  GLM_MODEL (default "glm-4.7-flash", the free z.ai Flash model)
  GLM_MAX_TOKENS (default 8000, the GLM path's output budget)
  GROQ_MODEL (default "openai/gpt-oss-120b"), OPENROUTER_MODEL
  (default "meta-llama/llama-3.3-70b-instruct:free"),
  CLOUDFLARE_MODEL (default "@cf/meta/llama-3.3-70b-fp8-fast"),
  SAMBANOVA_MODEL (default "Meta-Llama-3.3-70B-Instruct"),
  DASHSCOPE_MODEL (default "qwen-plus"), MODELSCOPE_MODEL (default
  "Qwen/Qwen3-32B"), SILICONFLOW_MODEL (default
  "Qwen/Qwen2.5-7B-Instruct"),
  CF_ACCOUNT_ID (the Cloudflare account id — an identifier, not a
  secret; the cloudflare brain is skipped when it is unset)
Secrets (names only): GEMINI_API_KEY, TELEGRAM_BOT_TOKEN
  GH_MODELS_TOKEN (optional; the GitHub path falls back to GITHUB_TOKEN)
  GLM_API_KEY (the GLM path's key, from z.ai)
  GROQ_API_KEY, OPENROUTER_API_KEY, SAMBANOVA_API_KEY,
  CLOUDFLARE_API_TOKEN, DASHSCOPE_API_KEY, MODELSCOPE_API_KEY,
  SILICONFLOW_API_KEY (optional §22 free-tier brains; each joins the
  chain automatically when its secret exists)
"""

import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REQUESTS_DIR = os.path.join(BASE_DIR, "requests")
READ_TIMEOUT = 180
MAX_ATTEMPTS = 6
NEAR_IDENTICAL = 0.8

FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹"


def fa_num(n):
    return "".join(FA_DIGITS[int(d)] for d in str(n))


class Fail(Exception):
    """A fail-closed stop: the run ends with this message and no output."""


# --------------------------------------------------------------------------
# HTTP with the full retry law (same law as build_article.py)
# --------------------------------------------------------------------------

def http_request(url, method="GET", data=None, headers=None, timeout=READ_TIMEOUT):
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", "discovery-agent/1.0 (research; mailto:research@example.org)")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            if e.code == 429 or 500 <= e.code < 600:
                last_error = Fail(f"HTTP {e.code} from {url.split('?')[0]}: {body[:200]!r}")
            else:
                return e.code, body
        except Exception as e:  # timeouts, DNS, resets — all transient
            last_error = e
        if attempt < MAX_ATTEMPTS:
            time.sleep(30 * attempt)
    raise Fail(f"request failed after {MAX_ATTEMPTS} attempts: {url.split('?')[0]} ({last_error})")


def http_json(url, **kw):
    status, raw = http_request(url, **kw)
    if status != 200:
        raise Fail(f"HTTP {status} from {url.split('?')[0]}")
    return json.loads(raw.decode("utf-8", "replace"))


# --------------------------------------------------------------------------
# Retrieval (grounding + novelty screen) — free indexes only
# --------------------------------------------------------------------------

POLITE_MAILTO = "discoverybot@users.noreply.github.com"


def search_openalex(query, per_page=25):
    # mailto puts us in OpenAlex's polite pool (higher, steadier limits).
    url = ("https://api.openalex.org/works?per-page=" + str(per_page)
           + "&search=" + urllib.parse.quote(query)
           + "&mailto=" + POLITE_MAILTO)
    data = http_json(url)
    out = []
    for w in data.get("results", []):
        out.append({
            "title": (w.get("title") or "").strip(),
            "year": w.get("publication_year"),
        })
    return out


def search_crossref(query, rows=25):
    url = ("https://api.crossref.org/works?rows=" + str(rows)
           + "&query=" + urllib.parse.quote(query))
    data = http_json(url)
    out = []
    for it in data.get("message", {}).get("items", []):
        titles = it.get("title") or []
        if titles:
            out.append({"title": titles[0].strip(), "year": None})
    return out


def _openaire_text(node):
    """OpenAIRE wraps scalars as {"$": value} and repeats elements as
    lists; return the first plain string found."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        v = node.get("$")
        return v if isinstance(v, str) else ""
    if isinstance(node, list):
        for item in node:
            t = _openaire_text(item)
            if t:
                return t
    return ""


def search_openaire(query):
    """OpenAIRE grounding (no key, no registration)."""
    url = ("https://api.openaire.eu/search/publications?format=json"
           "&size=25&page=1&title=" + urllib.parse.quote(query))
    data = http_json(url)
    results = (((data.get("response") or {}).get("results") or {})
               .get("result")) or []
    if isinstance(results, dict):
        results = [results]
    out = []
    for r in results:
        md = (((r.get("metadata") or {}).get("oaf:entity") or {})
              .get("oaf:result")) or {}
        if not md:
            continue
        title = _openaire_text(md.get("title")).strip()
        date = _openaire_text(md.get("dateofacceptance"))
        if title:
            out.append({"title": title,
                        "year": int(date[:4]) if date[:4].isdigit() else None})
    return out


def search_europe_pmc(query):
    """Europe PMC grounding (no key, no registration)."""
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
           "?format=json&resultType=core&pageSize=25&query="
           + urllib.parse.quote('TITLE:"' + query.replace('"', "") + '"'))
    data = http_json(url)
    out = []
    for r in (data.get("resultList") or {}).get("result", []) or []:
        title = (r.get("title") or "").strip()
        if not title:
            continue
        year = r.get("pubYear")
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        out.append({"title": title, "year": year})
    return out


# --------------------------------------------------------------------------
# Source registries (§17) — credibility / whitelist SIGNAL only.
# Four curated registries sit beside this script in the repo:
#   sources_registry.json        (registry 1, registry schema)
#   sources_registry_500_more.json (500 DOAJ journals, compact schema:
#                                   {id, name, url, type, fields,
#                                    languages, country, checked,
#                                    http_status} — no verified/api)
#   sources_registry_2.json      (registry 2, registry schema)
#   thesis_registry.json         (thesis-HOSTING sources, registry
#                                   schema — repositories and ETD
#                                   archives, not individual theses)
# Both schemas normalise into one record {name, url, domain, type,
# fields, languages, registry}. A registry match is a ranking SIGNAL
# ONLY: it never verifies a source (the DOI / Crossref / publisher
# verification law is unchanged) and never counts toward the source
# bar. Loading is fail-soft: a missing or unreadable registry file
# logs one line and the run continues with the rest.
# --------------------------------------------------------------------------

REGISTRY_FILES = ["sources_registry.json",
                  "sources_registry_500_more.json",
                  "sources_registry_2.json",
                  "thesis_registry.json"]
_REGISTRY_DIR = os.path.dirname(os.path.abspath(__file__))


def _registry_norm_url(url):
    """Normalised URL key for dedupe: lowercase host, leading www.
    stripped, default ports (80/443) stripped, trailing slash
    stripped from the path."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urllib.parse.urlsplit(raw)
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        port = parts.port
    except ValueError:
        return raw.lower().rstrip("/")
    if not host:
        return raw.lower().rstrip("/")
    netloc = host
    if port and port not in (80, 443):
        netloc = f"{host}:{port}"
    path = (parts.path or "").rstrip("/")
    out = netloc + path
    if parts.query:
        out += "?" + parts.query
    return out


def _registry_domain(url):
    """Normalised domain (host) of a URL, or '' when unparsable."""
    key = _registry_norm_url(url)
    return key.split("/")[0].split("?")[0] if key else ""


def load_source_registry(base_dir=None):
    """Load all registry files; returns (domain_index, url_index,
    stats). domain_index / url_index map the normalised domain / URL
    to the normalised record (first record wins). Fail-soft per
    file: a missing or unreadable registry is logged and skipped."""
    base = base_dir or _REGISTRY_DIR
    domain_index, url_index = {}, {}
    stats = {"loaded": 0, "unique_urls": 0, "unique_domains": 0,
             "duplicates": 0, "files_ok": 0}
    for fname in REGISTRY_FILES:
        path = os.path.join(base, fname)
        try:
            with open(path, encoding="utf-8") as f:
                entries = json.load(f)
            if not isinstance(entries, list):
                raise ValueError("registry root is not a list")
        except Exception as e:
            print(f"registry: {fname} unavailable ({e}) — continuing "
                  "without it")
            continue
        stats["files_ok"] += 1
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = (entry.get("url") or "").strip()
            if not url:
                continue
            stats["loaded"] += 1
            record = {
                "name": str(entry.get("name") or "").strip(),
                "url": url,
                "domain": _registry_domain(url),
                "type": str(entry.get("type") or "").strip(),
                "fields": [str(x) for x in
                           (entry.get("fields") or [])
                           if x is not None],
                "languages": [str(x) for x in
                              (entry.get("languages") or [])
                              if x is not None],
                "registry": fname,
            }
            key = _registry_norm_url(url)
            if key in url_index:
                stats["duplicates"] += 1
                continue
            url_index[key] = record
            if record["domain"] and \
                    record["domain"] not in domain_index:
                domain_index[record["domain"]] = record
    stats["unique_urls"] = len(url_index)
    stats["unique_domains"] = len(domain_index)
    print(f"registry: loaded {stats['loaded']} entries from "
          f"{stats['files_ok']} files, {stats['unique_urls']} unique "
          f"URLs, {stats['unique_domains']} unique domains, "
          f"{stats['duplicates']} duplicate URLs dropped")
    return domain_index, url_index, stats


_REGISTRY_CACHE = None


def get_registry():
    """Load the registries once per run (fail-soft) and cache them."""
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = load_source_registry()
    return _REGISTRY_CACHE


def registry_match_for(candidate):
    """The registry record matching a candidate, or None. Exact
    normalised-URL match first, then domain match, over the
    candidate's landing-page URLs. doi.org links are skipped (the
    DOI resolver domain says nothing about the publisher)."""
    if not isinstance(candidate, dict):
        return None
    domain_index, url_index, _stats = get_registry()
    urls = []
    for key in ("url", "landing_url", "landing_page_url",
                "publisher_url"):
        u = candidate.get(key)
        if u and u not in urls:
            urls.append(u)
    for u in urls:
        rec = url_index.get(_registry_norm_url(u))
        if rec:
            return rec
    for u in urls:
        dom = _registry_domain(u)
        if dom and dom not in ("doi.org", "dx.doi.org"):
            rec = domain_index.get(dom)
            if rec:
                return rec
    return None


def registry_tag(candidate):
    """Tag a candidate in place with its registry match (display
    string) and return the record or None. Signal only — this never
    touches verification or counting."""
    rec = registry_match_for(candidate)
    if rec:
        candidate["registry_match"] = (
            f"{rec['name']} ({rec['type']}, {rec['registry']})")
    return rec



def grounding_digest(field, idea, api_key, model):
    """Titles of what already exists around the idea (best effort)."""
    queries = []
    for q in (idea, f"{field} {idea}"):
        if q not in queries:
            queries.append(q)
    if any(ord(ch) > 127 for ch in idea):
        english_q = translate_query_to_english(f"{field} {idea}", api_key, model)
        if english_q and english_q not in queries:
            queries.append(english_q)
    # Registry signal (§17): grounding results from a searcher whose
    # home domain is in a curated registry are tagged and preferred
    # when the digest cap forces a choice (stable sort, matched
    # first). Grounding only — nothing here verifies anything.
    domain_index, _url_index, _rstats = get_registry()
    searcher_domains = ((search_openalex, "openalex.org"),
                        (search_crossref, "crossref.org"),
                        (search_openaire, "openaire.eu"),
                        (search_europe_pmc, "europepmc.org"))
    seen, titles = set(), []
    for q in queries:
        for searcher, sdomain in searcher_domains:
            try:
                results = searcher(q)
            except Fail as e:
                print(f"WARNING: grounding search failed for {q!r}: {e}")
                continue
            rec = domain_index.get(sdomain)
            for r in results:
                key = normalise_title(r["title"])
                if r["title"] and key not in seen:
                    seen.add(key)
                    if rec:
                        r["registry_match"] = (
                            f"{rec['name']} ({rec['type']}, "
                            f"{rec['registry']})")
                    titles.append(r)
                if len(titles) >= 120:
                    break
    titles.sort(key=lambda t: 0 if t.get("registry_match") else 1)
    titles = titles[:30]
    print(f"grounding registry-matched: "
          f"{sum(1 for t in titles if t.get('registry_match'))} of "
          f"{len(titles)} digest works")
    lines = [f"- {t['title']}" + (f" ({t['year']})" if t.get("year") else "")
             for t in titles]
    return "\n".join(lines), len(titles)


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------

def gemini(prompt, api_key, model):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }).encode("utf-8")
    status, raw = http_request(url, method="POST", data=body, headers={
        "Content-Type": "application/json", "x-goog-api-key": api_key})
    if status != 200:
        raise Fail(f"Gemini HTTP {status}: {raw[:300]!r}")
    data = json.loads(raw.decode("utf-8", "replace"))
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except Exception:
        raise Fail(f"Gemini returned an unexpected shape: {raw[:300]!r}")
    if not text:
        raise Fail("Gemini returned empty text")
    return text


def gemini_text(prompt, api_key, model):
    """Plain-text Gemini call (used for query translation)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 256},
    }).encode("utf-8")
    status, raw = http_request(url, method="POST", data=body, headers={
        "Content-Type": "application/json", "x-goog-api-key": api_key})
    if status != 200:
        raise Fail(f"Gemini HTTP {status}: {raw[:300]!r}")
    data = json.loads(raw.decode("utf-8", "replace"))
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


# --------------------------------------------------------------------------
# LLM providers — Gemini (default), GitHub Models or GLM / z.ai (both
# OpenAI-compatible), selected by env LLM_PROVIDER. EVERY model call
# goes through call_llm: json_mode=True maps to gemini() (JSON mode,
# temperature 0.7) on the Gemini path, json_mode=False maps to
# gemini_text() (temperature 0.2).
# --------------------------------------------------------------------------

GH_MODELS_URL = "https://models.github.ai/inference/chat/completions"
GH_MODELS_DEFAULT_MODEL = "openai/gpt-4o"
GH_MODELS_MAX_TOKENS = 4000

GLM_URL = "https://api.z.ai/api/paas/v4/chat/completions"
GLM_DEFAULT_MODEL = "glm-4.7-flash"
GLM_DEFAULT_MAX_TOKENS = 8000

# §22: seven free-tier OpenAI-compatible backup brains (mirrors
# build_article.py). Each is used only when its credential exists
# in the environment (see the chain append rule in llm_chain());
# with no keys set, behaviour is exactly the legacy glm/gemini chain.
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_DEFAULT_MODEL = "openai/gpt-oss-120b"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
CLOUDFLARE_DEFAULT_MODEL = "@cf/meta/llama-3.3-70b-fp8-fast"
SAMBANOVA_URL = "https://api.sambanova.ai/v1/chat/completions"
SAMBANOVA_DEFAULT_MODEL = "Meta-Llama-3.3-70B-Instruct"
DASHSCOPE_URL = ("https://dashscope-intl.aliyuncs.com"
                 "/compatible-mode/v1/chat/completions")
DASHSCOPE_DEFAULT_MODEL = "qwen-plus"
MODELSCOPE_URL = "https://api-inference.modelscope.cn/v1/chat/completions"
MODELSCOPE_DEFAULT_MODEL = "Qwen/Qwen3-32B"
SILICONFLOW_URL = "https://api.siliconflow.cn/v1/chat/completions"
SILICONFLOW_DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"
EXTRA_BRAINS_MAX_TOKENS = 8000

# Display labels for the §22 brains (log / error messages).
_EXTRA_BRAIN_LABELS = {"groq": "Groq", "openrouter": "OpenRouter",
                       "cloudflare": "Cloudflare Workers AI",
                       "sambanova": "SambaNova",
                       "dashscope": "DashScope",
                       "modelscope": "ModelScope",
                       "siliconflow": "SiliconFlow"}


def _cloudflare_url():
    """The Workers AI OpenAI-compatible endpoint for the configured
    account; "" when CF_ACCOUNT_ID is unset (the cloudflare brain is
    then unavailable, exactly like a missing key)."""
    account = (os.environ.get("CF_ACCOUNT_ID") or "").strip()
    if not account:
        return ""
    return ("https://api.cloudflare.com/client/v4/accounts/"
            f"{account}/ai/v1/chat/completions")


def _brain_endpoint(name):
    """(url, api_key) for a §22 OpenAI-compatible brain, resolved
    from the environment at call time; either element is "" when the
    brain is not configured. ("" , "") for any other name."""
    if name == "groq":
        return (GROQ_URL,
                (os.environ.get("GROQ_API_KEY") or "").strip())
    if name == "openrouter":
        return (OPENROUTER_URL,
                (os.environ.get("OPENROUTER_API_KEY") or "").strip())
    if name == "cloudflare":
        return (_cloudflare_url(),
                (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip())
    if name == "sambanova":
        return (SAMBANOVA_URL,
                (os.environ.get("SAMBANOVA_API_KEY") or "").strip())
    if name == "dashscope":
        return (DASHSCOPE_URL,
                (os.environ.get("DASHSCOPE_API_KEY") or "").strip())
    if name == "modelscope":
        return (MODELSCOPE_URL,
                (os.environ.get("MODELSCOPE_API_KEY") or "").strip())
    if name == "siliconflow":
        return (SILICONFLOW_URL,
                (os.environ.get("SILICONFLOW_API_KEY") or "").strip())
    return ("", "")


def llm_provider():
    return (os.environ.get("LLM_PROVIDER") or "gemini").strip().lower()


def _brain_model(name):
    """The model brain `name` calls with: its own env override, else
    its default. Each brain resolves its OWN model, so a backup brain
    never receives the primary brain's model name."""
    if name == "glm":
        return (os.environ.get("GLM_MODEL")
                or GLM_DEFAULT_MODEL).strip()
    if name == "github":
        return (os.environ.get("GH_MODELS_MODEL")
                or GH_MODELS_DEFAULT_MODEL).strip()
    if name == "groq":
        return (os.environ.get("GROQ_MODEL")
                or GROQ_DEFAULT_MODEL).strip()
    if name == "openrouter":
        return (os.environ.get("OPENROUTER_MODEL")
                or OPENROUTER_DEFAULT_MODEL).strip()
    if name == "cloudflare":
        return (os.environ.get("CLOUDFLARE_MODEL")
                or CLOUDFLARE_DEFAULT_MODEL).strip()
    if name == "sambanova":
        return (os.environ.get("SAMBANOVA_MODEL")
                or SAMBANOVA_DEFAULT_MODEL).strip()
    if name == "dashscope":
        return (os.environ.get("DASHSCOPE_MODEL")
                or DASHSCOPE_DEFAULT_MODEL).strip()
    if name == "modelscope":
        return (os.environ.get("MODELSCOPE_MODEL")
                or MODELSCOPE_DEFAULT_MODEL).strip()
    if name == "siliconflow":
        return (os.environ.get("SILICONFLOW_MODEL")
                or SILICONFLOW_DEFAULT_MODEL).strip()
    return (os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash").strip()


def llm_model():
    return _brain_model(llm_provider())


def _github_models_once(prompt, temperature=0.4, json_mode=False):
    """One single GitHub Models chat completion attempt (no retries —
    call_llm's brain chain owns retrying and failover). Token:
    GH_MODELS_TOKEN if set, else GITHUB_TOKEN. A truncated reply
    (finish_reason "length") raises Fail like any other failed
    attempt: truncated text never silently enters an article."""
    token = (os.environ.get("GH_MODELS_TOKEN") or "").strip() \
        or (os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        raise Fail("GitHub Models brain has no model token available: "
                   "set the GH_MODELS_TOKEN repo secret or pass GITHUB_TOKEN.")
    body = json.dumps({
        "model": _brain_model("github"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": GH_MODELS_MAX_TOKENS,
        **({"response_format": {"type": "json_object"}} if json_mode else {}),
    }).encode("utf-8")
    status, raw = http_request(GH_MODELS_URL, method="POST", data=body,
                               headers={
                                   "Content-Type": "application/json",
                                   "Authorization": f"Bearer {token}"})
    if status != 200:
        raise Fail(f"GitHub Models HTTP {status}: {raw[:300]!r}")
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise Fail(f"GitHub Models returned a non-JSON body "
                   f"(HTTP {status}): {raw[:300]!r}")
    try:
        choice = data["choices"][0]
        text = (choice["message"]["content"] or "").strip()
        finish = choice.get("finish_reason")
    except Exception:
        raise Fail(f"GitHub Models returned an unexpected shape: {raw[:300]!r}")
    if finish == "length":
        raise Fail(f"GitHub Models reply truncated at "
                   f"{GH_MODELS_MAX_TOKENS} tokens (finish_reason=length)")
    if not text:
        raise Fail("GitHub Models returned empty text")
    return text


def glm_max_tokens():
    """The GLM path's output budget: env GLM_MAX_TOKENS overrides the
    8000-token default (an invalid value falls back to the default).
    glm-4.7-flash is a reasoning model whose hidden thinking shares
    this budget; thinking is disabled on our calls (see glm()), but
    long structured outputs still need more than the old 4000."""
    raw = (os.environ.get("GLM_MAX_TOKENS") or "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
        print(f"WARNING: ignoring invalid GLM_MAX_TOKENS {raw!r}; "
              f"using {GLM_DEFAULT_MAX_TOKENS}")
    return GLM_DEFAULT_MAX_TOKENS


def _glm_once(prompt, temperature=0.4, json_mode=False):
    """One single GLM (z.ai) chat completion attempt (no retries —
    call_llm's brain chain owns retrying and failover). Endpoint and
    auth verified against the official docs (docs.z.ai): POST
    https://api.z.ai/api/paas/v4/chat/completions with an
    "Authorization: Bearer <key>" header. Key: GLM_API_KEY. The default
    model glm-4.7-flash is z.ai's free tier (GLM_MODEL overrides it).
    Deep thinking is DISABLED on every call ("thinking":
    {"type": "disabled"} — the docs.z.ai Deep Thinking page supports
    the GLM-4.7 series and reserves the disabled-rejection for
    GLM-5.3 / GLM-5.3-FLASH only): all our call sites are structured
    generation/extraction, where the reasoning model's hidden thinking
    only burns the shared output budget and truncates the visible
    reply. A truncated reply (finish_reason "length") raises Fail like
    any other failed attempt: truncated text never silently enters an
    article."""
    api_key = (os.environ.get("GLM_API_KEY") or "").strip()
    if not api_key:
        raise Fail("GLM brain has no GLM_API_KEY available: "
                   "set the GLM_API_KEY repo secret.")
    max_tokens = glm_max_tokens()
    body = json.dumps({
        "model": _brain_model("glm"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},
        **({"response_format": {"type": "json_object"}} if json_mode else {}),
    }).encode("utf-8")
    status, raw = http_request(GLM_URL, method="POST", data=body,
                               headers={
                                   "Content-Type": "application/json",
                                   "Authorization": f"Bearer {api_key}"})
    if status != 200:
        raise Fail(f"GLM HTTP {status}: {raw[:300]!r}")
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise Fail(f"GLM returned a non-JSON body "
                   f"(HTTP {status}): {raw[:300]!r}")
    try:
        choice = data["choices"][0]
        text = (choice["message"]["content"] or "").strip()
        finish = choice.get("finish_reason")
    except Exception:
        raise Fail(f"GLM returned an unexpected shape: {raw[:300]!r}")
    if finish == "length":
        raise Fail(f"GLM reply truncated at {max_tokens} tokens "
                   "(finish_reason=length)")
    if not text:
        raise Fail("GLM returned empty text")
    return text


def _extra_brain_once(name, prompt, temperature=0.4, json_mode=False):
    """One single attempt on a §22 OpenAI-compatible brain (Groq,
    OpenRouter, Cloudflare Workers AI, SambaNova, DashScope,
    ModelScope or SiliconFlow — no retries;
    call_llm's brain chain owns retrying and failover). Auth is an
    "Authorization: Bearer <key>" header with the brain's own env
    key. Unlike GLM, no "thinking" field is sent: these endpoints
    are plain OpenAI-compatible chat completions and the field is a
    z.ai extension. A truncated reply (finish_reason "length")
    raises Fail like any other failed attempt."""
    label = _EXTRA_BRAIN_LABELS.get(name, name)
    url, api_key = _brain_endpoint(name)
    if not url:
        raise Fail(f"{label} brain has no endpoint available: "
                   "set the CF_ACCOUNT_ID repo variable.")
    if not api_key:
        raise Fail(f"{label} brain has no API key available.")
    body = json.dumps({
        "model": _brain_model(name),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": EXTRA_BRAINS_MAX_TOKENS,
        **({"response_format": {"type": "json_object"}} if json_mode else {}),
    }).encode("utf-8")
    status, raw = http_request(url, method="POST", data=body,
                               headers={
                                   "Content-Type": "application/json",
                                   "Authorization": f"Bearer {api_key}"})
    if status != 200:
        raise Fail(f"{label} HTTP {status}: {raw[:300]!r}")
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise Fail(f"{label} returned a non-JSON body "
                   f"(HTTP {status}): {raw[:300]!r}")
    try:
        choice = data["choices"][0]
        text = (choice["message"]["content"] or "").strip()
        finish = choice.get("finish_reason")
    except Exception:
        raise Fail(f"{label} returned an unexpected shape: {raw[:300]!r}")
    if finish == "length":
        raise Fail(f"{label} reply truncated at "
                   f"{EXTRA_BRAINS_MAX_TOKENS} tokens "
                   "(finish_reason=length)")
    if not text:
        raise Fail(f"{label} returned empty text")
    return text

# ----------------------------------------------------------------------
# Brain chain: every logical LLM call walks an ordered list of brains
# (env LLM_CHAIN, comma-separated; default: LLM_PROVIDER, then glm,
# then gemini — the retired "github" service is never in the default
# chain). Each brain gets CHAIN_ATTEMPTS attempts with a
# CHAIN_BACKOFF_SECONDS x attempt pause between them; a brain whose
# key is missing is skipped. Only when every brain is skipped or
# exhausted does the call fail — fail-closed, as before.
#
# §22 append rule (mirrors build_article.py): any of the seven
# free-tier brains (EXTRA_BRAINS) whose credential is present in
# the environment is APPENDED to the chain — after whatever
# LLM_CHAIN / the default produced, in EXTRA_BRAINS order, without
# duplicating a name already listed — so a new brain joins the run
# the moment its secret exists, with no further configuration.
# With no new keys set, the chain is exactly the legacy chain.
# ----------------------------------------------------------------------

CHAIN_ATTEMPTS = 3
CHAIN_BACKOFF_SECONDS = 15
KNOWN_BRAINS = ("glm", "gemini", "github",
               "groq", "openrouter", "cloudflare", "sambanova",
               "dashscope", "modelscope", "siliconflow")
EXTRA_BRAINS = ("groq", "openrouter", "cloudflare", "sambanova",
                "dashscope", "modelscope", "siliconflow")


def llm_chain():
    """The ordered brain chain for this run. LLM_CHAIN (e.g.
    "glm,gemini") wins when set; otherwise the chain is LLM_PROVIDER
    (when set) followed by glm and gemini, deduplicated, order
    preserved. "github" is excluded from the DEFAULT chain (GitHub
    Models is retired); it is honoured only when named explicitly in
    LLM_CHAIN. Finally the §22 append rule adds every EXTRA_BRAINS
    brain whose credential is present, in EXTRA_BRAINS order."""
    raw = (os.environ.get("LLM_CHAIN") or "").strip()
    if raw:
        names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    else:
        names = []
        primary = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
        if primary and primary != "github":
            names.append(primary)
        names.extend(["glm", "gemini"])
    chain = []
    for name in names:
        if name not in chain:
            chain.append(name)
    for name in EXTRA_BRAINS:
        if name not in chain and _brain_key_available(name, ""):
            chain.append(name)
    return chain


def _brain_key_available(name, api_key):
    """True when brain `name` has the credential it needs. The Gemini
    key is the api_key argument call_llm already receives (with the
    GEMINI_API_KEY env var as fallback)."""
    if name == "glm":
        return bool((os.environ.get("GLM_API_KEY") or "").strip())
    if name == "gemini":
        return bool((api_key or "").strip()
                    or (os.environ.get("GEMINI_API_KEY") or "").strip())
    if name == "github":
        return bool((os.environ.get("GH_MODELS_TOKEN") or "").strip()
                    or (os.environ.get("GITHUB_TOKEN") or "").strip())
    if name in EXTRA_BRAINS:
        url, key = _brain_endpoint(name)
        return bool(url and key)
    return False


def _call_brain(name, prompt, api_key, model, temperature, json_mode):
    """One single attempt on brain `name`. The Gemini path is the
    original gemini()/gemini_text() above, unchanged (their
    temperatures are built in); the GitHub Models and GLM paths honour
    the temperature / json_mode of the call site."""
    if name == "glm":
        return _glm_once(prompt, temperature=temperature,
                         json_mode=json_mode)
    if name == "github":
        return _github_models_once(prompt, temperature=temperature,
                                   json_mode=json_mode)
    if name in EXTRA_BRAINS:
        return _extra_brain_once(name, prompt,
                                 temperature=temperature,
                                 json_mode=json_mode)
    if json_mode:
        return gemini(prompt, api_key, model)
    return gemini_text(prompt, api_key, model)


def call_llm(prompt, api_key, model, temperature=0.4, json_mode=False):
    """One logical LLM call, served by the brain chain (llm_chain()):
    brains are tried in order, each for up to CHAIN_ATTEMPTS attempts
    with a CHAIN_BACKOFF_SECONDS x attempt pause between attempts.
    Truncation, HTTP errors, transport errors and empty replies all
    count as failed attempts. A brain whose key is missing is skipped.
    The call raises Fail only when every brain in the chain was
    skipped or exhausted — fail-closed, exactly as the single-brain
    path was."""
    chain = llm_chain()
    failures = []
    for name in chain:
        if name not in KNOWN_BRAINS:
            raise Fail(f"Unknown LLM provider {name!r} in chain "
                       f"{chain} (expected one of: "
                       f"{', '.join(KNOWN_BRAINS)})")
        if not _brain_key_available(name, api_key):
            print(f"brain skipped: {name} (no key)")
            failures.append(f"{name} (no key)")
            continue
        last_error = None
        for attempt in range(1, CHAIN_ATTEMPTS + 1):
            try:
                text = _call_brain(name, prompt, api_key, model,
                                   temperature, json_mode)
            except Exception as e:
                last_error = e
                print(f"WARNING: brain {name} failed attempt "
                      f"({attempt}/{CHAIN_ATTEMPTS}): {e}")
                if attempt < CHAIN_ATTEMPTS:
                    time.sleep(CHAIN_BACKOFF_SECONDS * attempt)
                continue
            print(f"brain ok: {name}")
            return text
        print(f"brain switch: {name} failed after {CHAIN_ATTEMPTS} "
              "attempts -> next brain")
        failures.append(f"{name} ({last_error})")
    raise Fail(f"All brains failed: chain {chain} exhausted — "
               + "; ".join(failures))


def translate_query_to_english(text, api_key, model):
    try:
        out = call_llm(
            "Translate this research topic into a short English search query "
            "(keywords only, no quotes, no explanation):\n" + text,
            api_key, model, temperature=0.2)
        return out.strip().strip('"')[:200]
    except Exception as e:
        print(f"WARNING: query translation failed ({e}); continuing without it")
        return None


# --------------------------------------------------------------------------
# Proposal generation + validation
# --------------------------------------------------------------------------

def normalise_title(t):
    return re.sub(r"[^a-z0-9\u0600-\u06FF]+", " ", (t or "").lower()).strip()


def load_excluded_titles(chat_id):
    excluded, normalised = [], set()
    pattern = os.path.join(REQUESTS_DIR, f"{chat_id}-b*.json")
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"WARNING: could not read {path}: {e}")
            continue
        for p in data.get("proposals", []):
            for key in ("title", "title_en"):
                t = (p.get(key) or "").strip()
                if t:
                    excluded.append(t)
                    normalised.add(normalise_title(t))
    return excluded, normalised


def build_prompt(field, idea, digest, excluded_titles, feedback=None):
    excluded_block = "\n".join(f"- {t}" for t in excluded_titles) or "(none yet)"
    prompt = (
        "You are the topic-ideation module of a scientific discovery agent. "
        "Your job: propose research-article topics that fill REAL gaps — "
        "angles that, as far as the existing literature below shows, nobody "
        "has studied yet. Never claim absolute certainty; judge novelty "
        "honestly against the existing works listed.\n\n"
        f"FIELD: {field}\n"
        f"THE USER'S IDEA / AREA (write titles, gap and appeal in THIS "
        f"language): {idea}\n\n"
        "EXISTING WORKS AROUND THIS AREA (from OpenAlex/Crossref — do NOT "
        "propose these again, and do not propose trivial rewordings of "
        f"them):\n{digest or '(no works found)'}\n\n"
        "TITLES ALREADY PROPOSED TO THIS USER IN EARLIER BATCHES (never "
        f"repeat any of them):\n{excluded_block}\n\n"
        "Produce EXACTLY 10 proposals, in this exact tier order:\n"
        "- proposals 1-4: tier \"safe\" — close to the user's idea, "
        "feasible, lower risk.\n"
        "- proposals 5-7: tier \"angled\" — a new combination or angle: "
        "connecting two areas, a new method on an old problem, an "
        "overlooked population/region/data source.\n"
        "- proposals 8-10: tier \"bold\" — frontier, high-risk high-impact "
        "ideas that would strongly attract reviewers and readers if done.\n\n"
        "Each proposal is an object with EXACTLY these fields:\n"
        "- \"title\": the article topic/title, in the language of the "
        "user's idea above.\n"
        "- \"title_en\": the same title in English.\n"
        "- \"gap\": 1-2 sentences, same language as the idea: what exactly "
        "is unstudied here.\n"
        "- \"appeal\": 1 sentence, same language as the idea: why this "
        "attracts reviewers and readers.\n"
        "- \"novelty\": integer 1-5 (5 = no close prior work found).\n"
        "- \"feasibility\": integer 1-5 (5 = doable with free/open sources "
        "and standard methods).\n"
        "- \"tier\": \"safe\" | \"angled\" | \"bold\" matching the position "
        "above.\n\n"
        "Output ONLY a JSON object of the form {\"proposals\": [ ... ]}. "
        "No markdown fences, no commentary, no extra keys."
    )
    if feedback:
        prompt += (
            "\n\nYOUR PREVIOUS OUTPUT WAS INVALID: " + feedback +
            "\nOutput ONLY the corrected JSON object."
        )
    return prompt


def validate_proposals(raw_text, excluded_normalised):
    """Returns (proposals, error). Exactly one of the two is not None."""
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except Exception as e:
        return None, f"output is not valid JSON ({e})"
    proposals = data.get("proposals") if isinstance(data, dict) else data
    if not isinstance(proposals, list):
        return None, "top-level 'proposals' array is missing"
    if len(proposals) != 10:
        return None, f"exactly 10 proposals are required, got {len(proposals)}"
    tiers = ["safe"] * 4 + ["angled"] * 3 + ["bold"] * 3
    cleaned = []
    for i, p in enumerate(proposals):
        if not isinstance(p, dict):
            return None, f"proposal {i + 1} is not an object"
        for field_name in ("title", "title_en", "gap", "appeal"):
            if not str(p.get(field_name) or "").strip():
                return None, f"proposal {i + 1} is missing '{field_name}'"
        for score in ("novelty", "feasibility"):
            v = p.get(score)
            if not isinstance(v, int) or not (1 <= v <= 5):
                return None, f"proposal {i + 1} has invalid '{score}' (must be int 1..5)"
        if normalise_title(p["title"]) in excluded_normalised or \
                normalise_title(p["title_en"]) in excluded_normalised:
            return None, f"proposal {i + 1} repeats an already-proposed title: {p['title']}"
        cleaned.append({
            "title": str(p["title"]).strip(),
            "title_en": str(p["title_en"]).strip(),
            "gap": str(p["gap"]).strip(),
            "appeal": str(p["appeal"]).strip(),
            "novelty": p["novelty"],
            "feasibility": p["feasibility"],
            "tier": tiers[i],
        })
    return cleaned, None


def generate_proposals(field, idea, digest, excluded_titles,
                       excluded_normalised, api_key, model):
    feedback = None
    last_error = None
    for attempt in range(1, 4):
        prompt = build_prompt(field, idea, digest, excluded_titles, feedback)
        raw = call_llm(prompt, api_key, model, temperature=0.7, json_mode=True)
        proposals, error = validate_proposals(raw, excluded_normalised)
        if proposals is not None:
            return proposals
        last_error = error
        feedback = error
        print(f"WARNING: proposal attempt {attempt} invalid: {error}")
    raise Fail(f"Gemini did not produce a valid set of 10 proposals "
               f"after 3 attempts. Last error: {last_error}")


# --------------------------------------------------------------------------
# Quick novelty screen
# --------------------------------------------------------------------------

def title_tokens(t):
    return set(re.findall(r"[a-z0-9]+", (t or "").lower()))


def token_overlap(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a), len(b))


def novelty_screen(proposals):
    for p in proposals:
        try:
            results = search_openalex(p["title_en"], per_page=5)
        except Fail as e:
            print(f"WARNING: novelty screen failed for {p['title_en'][:60]!r}: {e}")
            continue
        mine = title_tokens(p["title_en"])
        for r in results:
            if token_overlap(mine, title_tokens(r["title"])) >= NEAR_IDENTICAL:
                p["novelty"] = 1
                p["gap"] = (p["gap"].rstrip()
                            + " ⚠️ عنوان بسیار مشابهی در منابع پیدا شد")
                print(f"  screen: near-identical title found for {p['title_en'][:60]!r}")
                break
    return proposals


# --------------------------------------------------------------------------
# Telegram delivery (same helpers' law as build_article.py)
# --------------------------------------------------------------------------

def tg_call(method, token, payload):
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(payload).encode("utf-8")
    status, raw = http_request(url, method="POST", data=body,
                               headers={"Content-Type": "application/json"})
    data = json.loads(raw.decode("utf-8", "replace"))
    if status != 200 or not data.get("ok"):
        raise Fail(f"Telegram {method} failed: HTTP {status}: {raw[:300]!r}")
    return data["result"]


def split_text(text, limit=3900):
    chunks, current = [], ""
    for para in text.split("\n\n"):
        piece = para if not current else current + "\n\n" + para
        if len(piece) <= limit:
            current = piece
        else:
            if current:
                chunks.append(current)
            while len(para) > limit:
                cut = para.rfind("\n", 0, limit)
                if cut < limit // 2:
                    cut = limit
                chunks.append(para[:cut])
                para = para[cut:].lstrip("\n")
            current = para
    if current:
        chunks.append(current)
    return chunks


TIER_HEADINGS = {0: "🟢 امن و نزدیک", 4: "🟡 زاویه‌دار", 7: "🔴 جسورانه و مرزی"}


def format_listing(field, idea, batch, proposals):
    parts = [
        f"💡 ۱۰ پیشنهاد موضوع — دستهٔ {fa_num(batch)} از {fa_num(5)}\n"
        f"رشته: {field}\n"
        f"ایدهٔ تو: {idea}"
    ]
    for i, p in enumerate(proposals):
        if i in TIER_HEADINGS:
            parts.append(TIER_HEADINGS[i])
        parts.append(
            f"{fa_num(i + 1)}) {p['title']}\n"
            f"خلأ: {p['gap']}\n"
            f"جذابیت: {p['appeal']}\n"
            f"تازگی: {fa_num(p['novelty'])}/{fa_num(5)} — "
            f"شدنی‌بودن: {fa_num(p['feasibility'])}/{fa_num(5)}"
        )
    return "\n\n".join(parts)


def proposals_keyboard(batch):
    rows = []
    row = []
    for idx in range(10):
        row.append({"text": fa_num(idx + 1),
                    "callback_data": f"pick:{batch}:{idx}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if batch < 5:
        rows.append([{"text": "🔄 ۱۰ پیشنهاد دیگر",
                      "callback_data": f"more:{batch}"}])
    rows.append([{"text": "⬅️ مرحلهٔ قبل", "callback_data": "nav:topic_mode"}])
    return {"inline_keyboard": rows}


def deliver(tg_token, chat_id, field, idea, batch, proposals):
    message_ids = []
    listing = format_listing(field, idea, batch, proposals)
    for chunk in split_text(listing):
        r = tg_call("sendMessage", tg_token, {"chat_id": chat_id, "text": chunk})
        message_ids.append(r["message_id"])
        time.sleep(0.3)
    closing = "یکی از پیشنهادها رو با دکمهٔ شماره‌ش انتخاب کن 👇"
    if batch >= 5:
        closing += ("\nاین آخرین دسته بود؛ در مجموع ۵۰ پیشنهاد ساخته شد. "
                    "اگه هیچ‌کدوم رو نپسندیدی، می‌تونی موضوع رو دستی بنویسی "
                    "یا ایده‌ت رو دقیق‌تر کنی.")
    r = tg_call("sendMessage", tg_token, {
        "chat_id": chat_id, "text": closing,
        "reply_markup": proposals_keyboard(batch)})
    message_ids.append(r["message_id"])
    return message_ids


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    field = os.environ.get("FIELD", "").strip()
    idea = os.environ.get("IDEA", "").strip()
    batch_raw = (os.environ.get("BATCH") or "1").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()
    model = (os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash").strip()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    provider = llm_provider()
    chain = llm_chain()
    print(f"LLM provider: {provider} (model {llm_model()}); "
          f"brain chain: {chain}")
    get_registry()  # §17: loads + logs the registry counts (fail-soft)

    if not field or not idea:
        raise Fail("FIELD and IDEA inputs are required.")
    try:
        batch = int(batch_raw)
    except ValueError:
        raise Fail(f"BATCH must be an integer 1..5, got {batch_raw!r}.")
    if not (1 <= batch <= 5):
        raise Fail(f"BATCH must be 1..5, got {batch}.")
    if not chat_id:
        raise Fail("CHAT_ID (the requesting user's chat) is required.")
    if not any(_brain_key_available(name, api_key) for name in chain):
        raise Fail("No usable LLM brain: chain " + ", ".join(chain)
                   + " — none of its providers has its key available "
                   "(GLM_API_KEY / GEMINI_API_KEY / GH_MODELS_TOKEN).")
    missing = [n for n, v in [("TELEGRAM_BOT_TOKEN", tg_token)] if not v]
    if missing:
        raise Fail("Missing required Actions secret(s): " + ", ".join(missing))

    excluded_titles, excluded_normalised = load_excluded_titles(chat_id)
    print(f"Excluded titles from earlier batches: {len(excluded_titles)}")

    digest, n_works = grounding_digest(field, idea, api_key, model)
    print(f"Grounding digest: {n_works} existing works")

    proposals = generate_proposals(field, idea, digest, excluded_titles,
                                   excluded_normalised, api_key, model)
    print(f"Generated 10 proposals for batch {batch}")

    proposals = novelty_screen(proposals)

    os.makedirs(REQUESTS_DIR, exist_ok=True)
    record = {
        "field": field,
        "idea": idea,
        "batch": batch,
        "chat_id": int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proposals": proposals,
    }
    out_path = os.path.join(REQUESTS_DIR, f"{chat_id}-b{batch}.json")
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=1)
    os.replace(tmp, out_path)
    print(f"Saved {out_path}")

    message_ids = deliver(tg_token, chat_id, field, idea, batch, proposals)
    print(f"Done. Proposals sent, message_ids={message_ids}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        print(f"FAILED (fail-closed): {e}", file=sys.stderr)
        sys.exit(1)
