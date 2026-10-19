# Tater Brave Local

A custom web search integration for [Tater](https://github.com/TaterTotterson/Tater) that replaces the stock
`Brave Search` provider with Brave's **LLM Context** endpoint, geo-targeted to *your* location.

## Why it exists

Asked via Tater's chat a spoken question about when a nearby store closes, the stock Brave Search
integration searched the raw planner query verbatim on Brave's standard web endpoint with no location,
and returned an unrelated result (a school's schedule) instead of the store's hours.

The same question through Home Assistant's *Tools for Assist* (`llm_intents` with the "Brave LLM Context"
provider) answered correctly — because that pipeline (1) uses Brave's LLM Context grounding endpoint,
(2) sends your latitude/longitude/timezone/postal code as `X-Loc-*` headers, and (3) lets the LLM rewrite
the spoken request into a clean search query. It also (4) benefits from the voice profile's *system
prompt*, which carries an explicit speech-to-text correction map ("spoken -> correct" name pairs) and a
default address anchor, so mangled names are fixed *deterministically* before they ever reach the model.

**Brave Local replicates all four inside Tater's existing `websearch` tool, with zero changes to Tater
core** — the correction map lives in the integration's **Local Name Corrections** settings field, and the
rewrite composes the query as a `search_web` tool call, the same way HA's conversation agent does.

## What it does

- Calls `https://api.search.brave.com/res/v1/llm/context` (grounding snippets built for LLM context, not
  web-page links) with your exact llm_intents-style settings: results, max snippets/result,
  max tokens/result, and context threshold mode (strict / balanced / lenient).
- Sends `X-Loc-Lat`, `X-Loc-Long`, `X-Loc-Timezone`, `X-Loc-Country`, `X-Loc-Postal-Code` headers so results
  are geo-targeted to you.
- **Local name corrections (optional, deterministic):** a settings field for "spoken -> correct" pairs,
  applied to the query before anything else — the same trick an HA voice profile system prompt uses to fix
  names your speech-to-text keeps mangling (e.g. `Stawbuck's -> Starbucks`). Matching is
  case-insensitive and word-boundary aware, so correcting one name never rewrites the middle of another.
- **LLM query rewrite (on by default):** before searching, Tater's own configured LLM turns the (already
  corrected) request into a compact search query, composed as a `search_web` tool call — the same shape
  Home Assistant's agent uses, which local models follow more reliably than free text, with a plain-text
  fallback. Uses Tater's LLM synchronously (`_chat_sync`, or a direct OpenAI-compatible
  `/chat/completions` call for server-backed models), so it is safe on Tater's event-loop thread. Any
  failure silently falls back to the raw query. Thinking/reasoning models are handled: the rewrite call
  follows Tater's global LLM thinking setting (and can be overridden per-integration via
  `TATER_BRAVE_LOCAL_REWRITE_REASONING`, see *Environment variables*).
- **Home Assistant location prefill:** if the Tater *Home Assistant* integration is enabled and connected,
  the latitude/longitude/timezone are read from HA's `/api/config` and pre-filled into the settings UI and
  the `X-Loc-*` headers — as long as you haven't typed your own values. Cached in Redis (10 min positive,
  5 min negative) so searches never wait on HA.
- **API key fallback:** uses the key you enter in its own settings field; if that's empty it reuses the key
  from the stock Brave Search integration (settings hash or legacy redis string), so nothing breaks if you
  switch providers.
- Returns the standard Tater `search_web` result contract, so the built-in `research_web` evaluator,
  `inspect_webpage` follow-ups, and the chat UI all work unchanged.

## Settings

| Field | Default | Notes |
|---|---|---|
| Brave Search API Key | *(falls back to Brave Search integration's key)* | From <https://brave.com/search/api/> |
| Country Code | `US` | 2-letter ISO code |
| Latitude / Longitude | *(Home Assistant value pre-fills)* | Your location |
| Timezone | *(Home Assistant value pre-fills)* | `America/Los_Angeles` or `Pacific Time` |
| Postal Code | *(manual)* | Your ZIP/postal code |
| Local Name Corrections | *(empty)* | One `spoken -> correct` pair per line, e.g. `Stawbuck's -> Starbucks`. `=>` and `=` work too. |
| Number of Results | `3` | 1–10 |
| Max Snippets Per Result | `3` | 1–5 |
| Max Tokens Per Result | `2048` | 128–4096 |
| Context Threshold Mode | `balanced` | `strict` / `balanced` / `lenient` |
| Rewrite Query With LLM | `true` | Turn off to search the (still name-corrected) query verbatim |

A **Test Search** action runs one query end-to-end and reports which corrections and the rewritten query
were applied.

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `TATER_BRAVE_LOCAL_REWRITE_REASONING` | *(follows Tater's LLM thinking toggle)* | Force the rewrite call's reasoning mode: `none` / `low` / `medium` / `high`, or `server` to send no control and let the LLM server decide. |
| `TATER_BRAVE_LOCAL_REWRITE_TIMEOUT_SECONDS` | `30` | Timeout for the LLM rewrite call. |
| `TATER_BRAVE_LOCAL_REWRITE_MAX_TOKENS` | `2048` | Completion budget for the rewrite call; thinking models spend most of it on chain-of-thought, so keep this comfortably above the query itself. |

## Install

### Option A — manual copy (fastest)

1. Copy `integrations/brave_local.py` into your Tater install's `integrations/` directory
   (the one Tater loads from; overridable with the `TATER_INTEGRATION_DIR` env var).
2. In Tater's WebUI go to **Settings → Integrations** and enable **Brave Local**.
3. Pick it as your web search provider (Settings → Web Search) and press **Test Search**.

No restart is required for the module to appear, but if Tater already cached the integrations package, a
restart is the sure thing.

### Option B — as an extra shop manifest

1. Put this repository somewhere persistent (or host it on GitHub).
2. In Tater's integration shop settings, add the raw manifest URL as an additional manifest source, e.g.
   `https://raw.githubusercontent.com/heapsoftware/tater-brave-local/main/manifest.json`.
3. Install **Brave Local** from the shop like any other integration.

Tater also auto-detects a `manifest.json` in a `Tater_Integrations` folder next to the Tater app directory,
or in the directory named by `TATER_LOCAL_INTEGRATION_SHOP_DIR` — so dropping this repo's contents into a
`Tater_Integrations/` folder beside your install works without any shop configuration.

### Requirements

- Tater (this is an integration module; no Tater core changes needed).
- A Brave Search API key (the LLM Context endpoint uses the same key).
- Optional: the Tater *Home Assistant* integration, enabled and connected, for location prefill.
