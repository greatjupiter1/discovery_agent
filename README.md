# Scientific Discovery Agent — v1

An on-demand bot that produces a scholarly **review / synthesis article** for
any field and delivers it to a Telegram channel.

**How it works (fail-closed, no fabrication):**

1. **Retrieval** — candidate works are collected from free indexes only:
   OpenAlex, Crossref, arXiv and Semantic Scholar (no API keys needed).
   Queries are built from the topic as given (its own language) plus a
   best-effort English query.
2. **Verification law** — a source is kept only if its DOI resolves through
   Crossref, or its arXiv page exists, or its publisher landing page answers.
   Anything unverifiable is discarded. If fewer than 5 sources survive, the
   run fails loudly instead of writing.
3. **Writing** — Gemini synthesises the article *only* from the verified
   sources, in the standard structure (title, structured abstract, keywords,
   introduction, thematic body, discussion, conclusion, limitations), with
   every factual claim cited as `[n]`. The reference list is formatted by the
   script from the verified records — never invented by the model.
4. **Delivery** — a header message, the article in chunks, and the full
   article as a `.md` document are sent to the Telegram chat in `CHAT_ID`
   (the «Discovery» channel, via @DiscoveryWorldbot).
5. **State** — each completed production is recorded in `state.json` and
   committed back. The same field + topic + language is never produced twice.

All transient errors (network, timeouts, HTTP 429 / 5xx) are retried with
backoff, up to 6 attempts, with a 180-second read timeout.

## How to trigger a production

1. Open the **Actions** tab of this repo:
   https://github.com/greatjupiter1/discovery_agent/actions
2. Choose **“Build Discovery article”** → **Run workflow**.
3. Fill in:
   - **field** — the discipline, e.g. `Medicine`
   - **topic** — the article topic, in any language
   - **language** — output language code, default `fa`
   - **mode** — `review` (the only mode in v1)
4. The finished article arrives in the Telegram channel when the run ends.

## Required secrets (set once, by the repo owner)

The workflow needs exactly three Actions secrets. Set them here:

**https://github.com/greatjupiter1/discovery_agent/settings/secrets/actions**

| Secret name | What it is |
|---|---|
| `TELEGRAM_BOT_TOKEN` | The @DiscoveryWorldbot bot token from BotFather |
| `GEMINI_API_KEY` | The Gemini API key used for writing/translation |
| `CHAT_ID` | The destination channel id (`-1003527278024`) |

Secret *values* are never stored in this repo — only the names above.

## Files

- `build_article.py` — the whole pipeline (standard library only).
- `.github/workflows/discovery.yml` — the on-demand workflow.
- `state.json` — the production ledger (field, topic, language, date,
  message ids).
