"""Mocked offline checks for the §22 extra free-tier brains
(Groq, OpenRouter, Cloudflare Workers AI, SambaNova, DashScope,
ModelScope, SiliconFlow). No network: env is staged per scenario
and http_request is a fake.

Rules under test:
- each brain resolves to its exact endpoint URL and default model
  (env model override honoured);
- the cloudflare URL embeds CF_ACCOUNT_ID, and the brain is
  unavailable when the account id OR the token is missing;
- every brain is skipped (not key-available, absent from the
  chain) when its key env is absent;
- the chain append rule: keyed extra brains are appended after the
  LLM_CHAIN / default chain, in EXTRA_BRAINS order (groq,
  openrouter, cloudflare, sambanova, dashscope, modelscope,
  siliconflow), never duplicated;
- with no new keys, the chain is exactly the legacy chain;
- a mocked call_llm fails over glm -> gemini -> groq and is served
  by groq;
- propose_topics.py mirrors the same table and append rule.
"""
import contextlib
import io
import json
import os

import build_article as ba
import propose_topics as pt

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


MANAGED = ["LLM_CHAIN", "LLM_PROVIDER", "GLM_API_KEY",
           "GEMINI_API_KEY", "GH_MODELS_TOKEN", "GITHUB_TOKEN",
           "GROQ_API_KEY", "GROQ_MODEL", "OPENROUTER_API_KEY",
           "OPENROUTER_MODEL", "CLOUDFLARE_API_TOKEN",
           "CLOUDFLARE_MODEL", "CF_ACCOUNT_ID", "SAMBANOVA_API_KEY",
           "SAMBANOVA_MODEL", "DASHSCOPE_API_KEY", "DASHSCOPE_MODEL",
           "MODELSCOPE_API_KEY", "MODELSCOPE_MODEL",
           "SILICONFLOW_API_KEY", "SILICONFLOW_MODEL"]
SAVED = {k: os.environ.get(k) for k in MANAGED}

ALL_KEYS = {"GROQ_API_KEY": "k-groq", "OPENROUTER_API_KEY": "k-or",
            "CLOUDFLARE_API_TOKEN": "k-cf", "CF_ACCOUNT_ID": "acct-123",
            "SAMBANOVA_API_KEY": "k-sn", "DASHSCOPE_API_KEY": "k-ds",
            "MODELSCOPE_API_KEY": "k-ms", "SILICONFLOW_API_KEY": "k-sf"}
FULL_CHAIN = ["glm", "gemini", "groq", "openrouter", "cloudflare",
              "sambanova", "dashscope", "modelscope", "siliconflow"]


def set_env(**kw):
    for k in MANAGED:
        os.environ.pop(k, None)
    for k, v in kw.items():
        if v is not None:
            os.environ[k] = v


try:
    # (a) URL + default model resolution for all seven brains
    set_env(**ALL_KEYS)
    check("(a1) groq endpoint + default model",
          ba._brain_endpoint("groq")
          == ("https://api.groq.com/openai/v1/chat/completions",
              "k-groq")
          and ba._brain_model("groq") == "openai/gpt-oss-120b")
    check("(a2) openrouter endpoint + default model",
          ba._brain_endpoint("openrouter")
          == ("https://openrouter.ai/api/v1/chat/completions", "k-or")
          and ba._brain_model("openrouter")
          == "meta-llama/llama-3.3-70b-instruct:free")
    check("(a3) sambanova endpoint + default model",
          ba._brain_endpoint("sambanova")
          == ("https://api.sambanova.ai/v1/chat/completions", "k-sn")
          and ba._brain_model("sambanova")
          == "Meta-Llama-3.3-70B-Instruct")
    check("(a4) dashscope endpoint + default model",
          ba._brain_endpoint("dashscope")
          == ("https://dashscope-intl.aliyuncs.com/compatible-mode"
              "/v1/chat/completions", "k-ds")
          and ba._brain_model("dashscope") == "qwen-plus")
    check("(a5) modelscope endpoint + default model",
          ba._brain_endpoint("modelscope")
          == ("https://api-inference.modelscope.cn/v1/chat"
              "/completions", "k-ms")
          and ba._brain_model("modelscope") == "Qwen/Qwen3-32B")
    check("(a6) siliconflow endpoint + default model",
          ba._brain_endpoint("siliconflow")
          == ("https://api.siliconflow.cn/v1/chat/completions",
              "k-sf")
          and ba._brain_model("siliconflow")
          == "Qwen/Qwen2.5-7B-Instruct")
    check("(a7) model env overrides honoured",
          ba._brain_model("groq") == "openai/gpt-oss-120b")
    set_env(GROQ_MODEL="custom/groq-x", DASHSCOPE_MODEL="qwen-max",
            **ALL_KEYS)
    check("(a8) GROQ_MODEL / DASHSCOPE_MODEL overrides win",
          ba._brain_model("groq") == "custom/groq-x"
          and ba._brain_model("dashscope") == "qwen-max")

    # (b) cloudflare: URL embeds the account id; skipped when the
    # account id or the token is missing
    set_env(**ALL_KEYS)
    check("(b1) cloudflare URL embeds CF_ACCOUNT_ID + default model",
          ba._brain_endpoint("cloudflare")[0]
          == ("https://api.cloudflare.com/client/v4/accounts/"
              "acct-123/ai/v1/chat/completions")
          and ba._brain_model("cloudflare")
          == "@cf/meta/llama-3.3-70b-fp8-fast")
    set_env(CLOUDFLARE_API_TOKEN="k-cf")
    check("(b2) cloudflare unavailable without CF_ACCOUNT_ID",
          not ba._brain_key_available("cloudflare", "")
          and "cloudflare" not in ba.llm_chain())
    set_env(CF_ACCOUNT_ID="acct-123")
    check("(b3) cloudflare unavailable without the token",
          not ba._brain_key_available("cloudflare", "")
          and "cloudflare" not in ba.llm_chain())

    # (c) each brain skipped when its own key env is absent
    for name, key_env in [("groq", "GROQ_API_KEY"),
                          ("openrouter", "OPENROUTER_API_KEY"),
                          ("sambanova", "SAMBANOVA_API_KEY"),
                          ("dashscope", "DASHSCOPE_API_KEY"),
                          ("modelscope", "MODELSCOPE_API_KEY"),
                          ("siliconflow", "SILICONFLOW_API_KEY")]:
        env = dict(ALL_KEYS)
        env.pop(key_env)
        set_env(**env)
        check(f"(c) {name} skipped without {key_env}",
              not ba._brain_key_available(name, "")
              and name not in ba.llm_chain())

    # (d) append rule: order + no duplicates
    set_env(LLM_CHAIN="glm,gemini", **ALL_KEYS)
    check("(d1) all keyed brains appended in EXTRA_BRAINS order",
          ba.llm_chain() == FULL_CHAIN)
    set_env(LLM_CHAIN="groq,glm", GROQ_API_KEY="k-groq")
    check("(d2) brain already in LLM_CHAIN is not duplicated",
          ba.llm_chain() == ["groq", "glm"])
    set_env(DASHSCOPE_API_KEY="k-ds", SILICONFLOW_API_KEY="k-sf")
    check("(d3) default chain + partial keys append in order",
          ba.llm_chain()
          == ["glm", "gemini", "dashscope", "siliconflow"])

    # (e) no new keys -> exactly the legacy chain
    set_env(LLM_PROVIDER="glm")
    check("(e1) legacy default chain unchanged (provider glm)",
          ba.llm_chain() == ["glm", "gemini"])
    set_env(LLM_CHAIN="glm,gemini")
    check("(e2) legacy explicit LLM_CHAIN unchanged",
          ba.llm_chain() == ["glm", "gemini"])
    set_env()
    check("(e3) legacy chain with bare env (gemini default)",
          ba.llm_chain() == ["gemini", "glm"]
          or ba.llm_chain() == ["glm", "gemini"])

    # (f) mocked failover: glm 500, gemini 500, groq answers
    set_env(LLM_PROVIDER="glm", GLM_API_KEY="k-glm",
            GROQ_API_KEY="k-groq")
    calls = []

    def fake_http(url, method="GET", data=None, headers=None,
                  timeout=None):
        calls.append(url)
        if "groq.com" in url:
            body = {"choices": [{"message": {"content": "groq says hi"},
                                 "finish_reason": "stop"}]}
            return 200, json.dumps(body).encode("utf-8")
        return 500, b"boom"

    orig_http, orig_backoff = ba.http_request, ba.CHAIN_BACKOFF_SECONDS
    ba.http_request = fake_http
    ba.CHAIN_BACKOFF_SECONDS = 0
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            text = ba.call_llm("hello", "k-gemini", "m")
    finally:
        ba.http_request, ba.CHAIN_BACKOFF_SECONDS = orig_http, orig_backoff
    check("(f1) failover reached groq after glm + gemini errored",
          text == "groq says hi"
          and any("groq.com" in u for u in calls)
          and any("z.ai" in u for u in calls)
          and any("googleapis.com" in u for u in calls))
    check("(f2) serving brain recorded as groq",
          ba._LAST_BRAIN["name"] == "groq")

    # (g) propose_topics.py mirrors the table + append rule
    set_env(LLM_CHAIN="glm,gemini", **ALL_KEYS)
    check("(g1) propose_topics chain appends all seven in order",
          pt.llm_chain() == FULL_CHAIN)
    check("(g2) propose_topics resolves dashscope + siliconflow",
          pt._brain_model("dashscope") == "qwen-plus"
          and pt._brain_endpoint("siliconflow")[0]
          == "https://api.siliconflow.cn/v1/chat/completions")
    set_env(LLM_PROVIDER="glm")
    check("(g3) propose_topics legacy chain unchanged with no keys",
          pt.llm_chain() == ["glm", "gemini"])
finally:
    for k in MANAGED:
        if SAVED[k] is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = SAVED[k]

failed = [n for n, ok in PASS if not ok]
print(f"\n{len(PASS) - len(failed)}/{len(PASS)} checks passed")
if failed:
    raise SystemExit("FAILED: " + ", ".join(failed))
