from __future__ import annotations
__version__ = "1.0.0"

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from helpers import redis_client

BRAVE_LOCAL_SETTINGS_KEY = "brave_local_settings"
BRAVE_SEARCH_SETTINGS_KEY = "brave_search_settings"
BRAVE_LOCAL_HA_LOCATION_CACHE_KEY = "brave_local_ha_location_cache"
BRAVE_LOCAL_API_URL = "https://api.search.brave.com/res/v1/llm/context"
BRAVE_LOCAL_DEFAULT_TIMEOUT_SECONDS = 20
BRAVE_LOCAL_REWRITE_TIMEOUT_SECONDS = int(os.getenv("TATER_BRAVE_LOCAL_REWRITE_TIMEOUT_SECONDS", "30"))
# Reasoning variants of LLMs spend most of the budget on chain-of-thought before
# emitting the query, so the rewrite needs a budget well above the query itself.
BRAVE_LOCAL_REWRITE_MAX_TOKENS = int(os.getenv("TATER_BRAVE_LOCAL_REWRITE_MAX_TOKENS", "2048"))
# Optional override for the rewrite call's reasoning mode: none/low/medium/high,
# or "server" to let the LLM server use its own default. Unset follows Tater's
# global LLM thinking toggle.
BRAVE_LOCAL_REWRITE_REASONING_ENV = "TATER_BRAVE_LOCAL_REWRITE_REASONING"
BRAVE_LOCAL_REWRITE_REASONING_MODES = ("none", "low", "medium", "high")
BRAVE_LOCAL_MAX_NAME_CORRECTIONS = 25
BRAVE_LOCAL_MAX_CORRECTION_CHARS = 60
BRAVE_LOCAL_MAX_RESPONSE_BYTES = int(os.getenv("TATER_BRAVE_LOCAL_MAX_RESPONSE_BYTES", "2000000"))
BRAVE_LOCAL_MAX_SNIPPET_CHARS = int(os.getenv("TATER_BRAVE_LOCAL_MAX_SNIPPET_CHARS", "2400"))
BRAVE_LOCAL_HA_CACHE_TTL_SECONDS = 600
BRAVE_LOCAL_HA_NEGATIVE_TTL_SECONDS = 300
BRAVE_LOCAL_CONTEXT_THRESHOLD_MODES = ("balanced", "strict", "lenient")
BRAVE_LOCAL_TIMEZONE_ALIASES = {
    "pacific time": "America/Los_Angeles",
    "pacific": "America/Los_Angeles",
    "pt": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "mountain time": "America/Denver",
    "mountain": "America/Denver",
    "mt": "America/Denver",
    "mst": "America/Denver",
    "mdt": "America/Denver",
    "central time": "America/Chicago",
    "central": "America/Chicago",
    "ct": "America/Chicago",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "eastern time": "America/New_York",
    "eastern": "America/New_York",
    "et": "America/New_York",
    "est": "America/New_York",
    "edt": "America/New_York",
}

REWRITE_SYSTEM_PROMPT = (
    "You are a search assistant. The user asked a question, possibly transcribed from speech.\n"
    "Call the search_web tool exactly once with one optimized search query. Do not answer the question yourself.\n"
    "Rules for the query:\n"
    "- Keep the user's intent and every place they mention.\n"
    "- If a brand, business, or place name sounds like a mangled or misspelled version of a well-known one, use the correct well-known name.\n"
    "- If a place name looks mangled, unfamiliar, or cannot be identified with confidence, omit it from the query: the search is already geo-targeted to the user's location.\n"
    "- Use normal search phrasing, for example: \"Starbucks store hours\".\n"
    "- Keep the query under 12 words.\n"
)

# Tool-call schema for the rewrite step. Mirrors the way Home Assistant's
# conversation agent invokes llm_intents' search_web: the LLM composes the query
# as a tool argument instead of free text, which local models follow more
# reliably.
REWRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": "Search the web to find up-to-date information that answers the user's question.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "One optimized search query (under 12 words).",
                },
            },
            "required": ["query"],
        },
    },
}

INTEGRATION = {
    "id": "brave_local",
    "name": "Brave Local",
    "description": "Adds Brave LLM Context Search, geo-targeted to your location (Home Assistant location is used to pre-fill when available), as a web search provider for Tater.",
    "badge": "BL",
    "order": 11,
    "capabilities": ["web_search"],
    "fields": [
        {"key": "brave_api_key", "label": "Brave Search API Key", "type": "password", "default": ""},
        {
            "key": "country_code",
            "label": "Country Code",
            "type": "text",
            "default": "US",
            "placeholder": "US",
        },
        {
            "key": "latitude",
            "label": "Latitude",
            "type": "text",
            "default": "",
            "placeholder": "Your latitude (Home Assistant value pre-fills this)",
        },
        {
            "key": "longitude",
            "label": "Longitude",
            "type": "text",
            "default": "",
            "placeholder": "Your longitude (Home Assistant value pre-fills this)",
        },
        {
            "key": "timezone",
            "label": "Timezone",
            "type": "text",
            "default": "",
            "placeholder": "America/Los_Angeles or Pacific Time",
        },
        {
            "key": "post_code",
            "label": "Postal Code",
            "type": "text",
            "default": "",
            "placeholder": "Your postal code",
        },
        {
            "key": "name_corrections",
            "label": "Local Name Corrections",
            "type": "textarea",
            "default": "",
            "rows": 4,
            "placeholder": "Stawbuck's -> Starbucks\nKrispy Creme -> Krispy Kreme",
            "description": (
                "Optional. One 'spoken -> correct' pair per line, applied to the query "
                "before searching. Use it for the names your speech-to-text keeps mangling, "
                "the same way an HA voice profile system prompt would."
            ),
        },
        {
            "key": "num_results",
            "label": "Number of Results",
            "type": "number",
            "default": 3,
            "min": 1,
            "max": 10,
        },
        {
            "key": "max_snippets_per_url",
            "label": "Max Snippets Per Result",
            "type": "number",
            "default": 3,
            "min": 1,
            "max": 5,
        },
        {
            "key": "max_tokens_per_url",
            "label": "Max Tokens Per Result",
            "type": "number",
            "default": 2048,
            "min": 128,
            "max": 4096,
        },
        {
            "key": "context_threshold_mode",
            "label": "Context Threshold Mode",
            "type": "select",
            "options": list(BRAVE_LOCAL_CONTEXT_THRESHOLD_MODES),
            "default": "balanced",
        },
        {
            "key": "rewrite_query",
            "label": "Rewrite Query With LLM",
            "type": "select",
            "options": ["true", "false"],
            "default": "true",
        },
    ],
    "actions": [
        {"id": "test", "label": "Test Search", "status": "Runs one Brave Local search query."},
    ],
}


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="ignore").strip()
    return str(value or "").strip()


def _boolish(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    token = _text(value).lower()
    if not token:
        return bool(default)
    if token in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if token in {"0", "false", "no", "n", "off", "disabled"}:
        return False
    return bool(default)


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(_text(value)))
    except Exception:
        parsed = int(default)
    return max(int(minimum), min(int(maximum), parsed))


def _bounded_float(value: Any) -> Optional[float]:
    try:
        return float(_text(value))
    except Exception:
        return None


def _read_hash(key: str) -> Dict[str, Any]:
    try:
        raw = redis_client.hgetall(key) or {}
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def _safe_redis_get(key: str) -> str:
    try:
        return _text(redis_client.get(key))
    except Exception:
        return ""


def _legacy_brave_api_key() -> str:
    for source in (
        _read_hash(BRAVE_SEARCH_SETTINGS_KEY).get("BRAVE_API_KEY"),
        _read_hash(BRAVE_SEARCH_SETTINGS_KEY).get("brave_api_key"),
        _safe_redis_get("tater:web_search:brave_api_key"),
        os.getenv("TATER_WEB_SEARCH_BRAVE_API_KEY") or os.getenv("BRAVE_SEARCH_API_KEY"),
    ):
        value = _text(source)
        if value:
            return value
    return ""


def _normalize_timezone(value: str) -> str:
    token = _text(value)
    if not token:
        return ""
    return BRAVE_LOCAL_TIMEZONE_ALIASES.get(token.casefold(), token)


def _normalize_threshold_mode(value: str) -> str:
    token = _text(value).lower()
    return token if token in BRAVE_LOCAL_CONTEXT_THRESHOLD_MODES else "balanced"


def _ha_location_cache_store(payload: Dict[str, str]) -> Dict[str, str]:
    row = {
        "TS": str(int(time.time())),
        "LATITUDE": _text(payload.get("latitude")),
        "LONGITUDE": _text(payload.get("longitude")),
        "TIMEZONE": _text(payload.get("timezone")),
    }
    try:
        redis_client.hset(BRAVE_LOCAL_HA_LOCATION_CACHE_KEY, mapping=row)
    except Exception:
        pass
    return row


def _ha_location_cache_read() -> Optional[Dict[str, str]]:
    cached = _read_hash(BRAVE_LOCAL_HA_LOCATION_CACHE_KEY)
    if not cached:
        return None
    try:
        cached_ts = float(cached.get("TS") or 0)
    except Exception:
        cached_ts = 0.0
    has_location = bool(_text(cached.get("LATITUDE")) and _text(cached.get("LONGITUDE")))
    ttl = BRAVE_LOCAL_HA_CACHE_TTL_SECONDS if has_location else BRAVE_LOCAL_HA_NEGATIVE_TTL_SECONDS
    if cached_ts <= 0 or (time.time() - cached_ts) >= ttl:
        return None
    return cached


def _homeassistant_runtime_connected() -> bool:
    try:
        status = _read_hash("tater:integration_runtime:status")
        token = _text(status.get("homeassistant_connected")).lower()
        if token:
            return token in {"1", "true", "yes", "on"}
    except Exception:
        pass
    return True  # Status hash is not authoritative before the runtime has written it.


def _homeassistant_location() -> Dict[str, str]:
    """Best-effort location from the Home Assistant integration (/api/config).

    Cached in Redis so searches and settings reads do not hammer HA. A failed
    lookup is negatively cached for a shorter window so a down HA box does not
    slow every search.
    """
    cached = _ha_location_cache_read()
    if cached is not None:
        return {
            "latitude": _text(cached.get("LATITUDE")),
            "longitude": _text(cached.get("LONGITUDE")),
            "timezone": _text(cached.get("TIMEZONE")),
            "source": "homeassistant" if _text(cached.get("LATITUDE")) and _text(cached.get("LONGITUDE")) else "",
        }

    empty = {"latitude": "", "longitude": "", "timezone": ""}
    try:
        from tateros import integration_store

        if not integration_store.get_integration_enabled("homeassistant"):
            return {**_ha_location_cache_store(empty), "source": ""}

        from integrations import homeassistant as ha_module

        conf = ha_module.load_homeassistant_config(required=False)
        if not _text(conf.get("token")) or not _homeassistant_runtime_connected():
            return {**_ha_location_cache_store(empty), "source": ""}
        config = ha_module._homeassistant_rest_get(
            conf.get("base"), conf.get("token"), "/api/config", timeout_s=5.0
        )
        if not isinstance(config, dict):
            return {**_ha_location_cache_store(empty), "source": ""}
        latitude = _bounded_float(config.get("latitude"))
        longitude = _bounded_float(config.get("longitude"))
        if latitude is None or longitude is None:
            return {**_ha_location_cache_store(empty), "source": ""}
        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            return {**_ha_location_cache_store(empty), "source": ""}
        stored = _ha_location_cache_store(
            {
                "latitude": str(latitude),
                "longitude": str(longitude),
                "timezone": _normalize_timezone(_text(config.get("time_zone"))),
            }
        )
        return {
            "latitude": _text(stored.get("LATITUDE")),
            "longitude": _text(stored.get("LONGITUDE")),
            "timezone": _text(stored.get("TIMEZONE")),
            "source": "homeassistant",
        }
    except Exception:
        return {**_ha_location_cache_store(empty), "source": ""}


def read_brave_local_settings() -> Dict[str, str]:
    current = _read_hash(BRAVE_LOCAL_SETTINGS_KEY)
    api_key = _text(current.get("BRAVE_API_KEY") or current.get("brave_api_key")) or _legacy_brave_api_key()
    settings = {
        "BRAVE_API_KEY": api_key,
        "COUNTRY_CODE": _text(current.get("COUNTRY_CODE") or current.get("country_code")) or "US",
        "LATITUDE": _text(current.get("LATITUDE") or current.get("latitude")),
        "LONGITUDE": _text(current.get("LONGITUDE") or current.get("longitude")),
        "TIMEZONE": _normalize_timezone(_text(current.get("TIMEZONE") or current.get("timezone"))),
        "POST_CODE": _text(current.get("POST_CODE") or current.get("post_code")),
        "NAME_CORRECTIONS": _text(current.get("NAME_CORRECTIONS") or current.get("name_corrections")),
        "NUM_RESULTS": _text(current.get("NUM_RESULTS") or current.get("num_results")) or "3",
        "MAX_SNIPPETS_PER_URL": _text(current.get("MAX_SNIPPETS_PER_URL") or current.get("max_snippets_per_url")) or "3",
        "MAX_TOKENS_PER_URL": _text(current.get("MAX_TOKENS_PER_URL") or current.get("max_tokens_per_url")) or "2048",
        "CONTEXT_THRESHOLD_MODE": _normalize_threshold_mode(
            _text(current.get("CONTEXT_THRESHOLD_MODE") or current.get("context_threshold_mode")) or "balanced"
        ),
        "REWRITE_QUERY": _text(current.get("REWRITE_QUERY") or current.get("rewrite_query")) or "true",
    }
    # Pre-fill location from Home Assistant when the user has not set it.
    if not settings["LATITUDE"] or not settings["LONGITUDE"] or not settings["TIMEZONE"]:
        ha = _homeassistant_location()
        if ha.get("latitude") and ha.get("longitude"):
            if not settings["LATITUDE"]:
                settings["LATITUDE"] = ha["latitude"]
            if not settings["LONGITUDE"]:
                settings["LONGITUDE"] = ha["longitude"]
            if not settings["TIMEZONE"]:
                settings["TIMEZONE"] = _normalize_timezone(ha.get("timezone") or "")
            settings["LOCATION_SOURCE"] = "homeassistant"
    return settings


def save_brave_local_settings(payload: Dict[str, Any]) -> Dict[str, str]:
    payload = payload or {}
    current = _read_hash(BRAVE_LOCAL_SETTINGS_KEY)

    def _next(field_key: str, stored_key: str, *, default: str = "") -> str:
        if field_key in payload and payload.get(field_key) is not None:
            return _text(payload.get(field_key))
        return _text(current.get(stored_key) or current.get(field_key)) or default

    next_settings = {
        "BRAVE_API_KEY": _next("brave_api_key", "BRAVE_API_KEY"),
        "COUNTRY_CODE": _next("country_code", "COUNTRY_CODE", default="US").upper(),
        "LATITUDE": _next("latitude", "LATITUDE"),
        "LONGITUDE": _next("longitude", "LONGITUDE"),
        "TIMEZONE": _normalize_timezone(_next("timezone", "TIMEZONE")),
        "POST_CODE": _next("post_code", "POST_CODE"),
        "NAME_CORRECTIONS": _next("name_corrections", "NAME_CORRECTIONS"),
        "NUM_RESULTS": _next("num_results", "NUM_RESULTS", default="3"),
        "MAX_SNIPPETS_PER_URL": _next("max_snippets_per_url", "MAX_SNIPPETS_PER_URL", default="3"),
        "MAX_TOKENS_PER_URL": _next("max_tokens_per_url", "MAX_TOKENS_PER_URL", default="2048"),
        "CONTEXT_THRESHOLD_MODE": _normalize_threshold_mode(_next("context_threshold_mode", "CONTEXT_THRESHOLD_MODE", default="balanced")),
        "REWRITE_QUERY": _next("rewrite_query", "REWRITE_QUERY", default="true"),
    }
    redis_client.hset(BRAVE_LOCAL_SETTINGS_KEY, mapping=next_settings)
    return read_brave_local_settings()


def read_integration_settings() -> Dict[str, Any]:
    settings = read_brave_local_settings()
    return {
        "brave_api_key": settings.get("BRAVE_API_KEY", ""),
        "country_code": settings.get("COUNTRY_CODE", "US"),
        "latitude": settings.get("LATITUDE", ""),
        "longitude": settings.get("LONGITUDE", ""),
        "timezone": settings.get("TIMEZONE", ""),
        "post_code": settings.get("POST_CODE", ""),
        "name_corrections": settings.get("NAME_CORRECTIONS", ""),
        "num_results": settings.get("NUM_RESULTS", "3"),
        "max_snippets_per_url": settings.get("MAX_SNIPPETS_PER_URL", "3"),
        "max_tokens_per_url": settings.get("MAX_TOKENS_PER_URL", "2048"),
        "context_threshold_mode": settings.get("CONTEXT_THRESHOLD_MODE", "balanced"),
        "rewrite_query": settings.get("REWRITE_QUERY", "true"),
        "location_source": settings.get("LOCATION_SOURCE", ""),
    }


def save_integration_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    save_brave_local_settings(payload or {})
    return read_integration_settings()


def integration_status() -> Dict[str, Any]:
    settings = read_brave_local_settings()
    configured = bool(settings.get("BRAVE_API_KEY"))
    located = bool(settings.get("LATITUDE") and settings.get("LONGITUDE"))
    message = "Brave Local is configured." if configured else "Brave Local needs a Brave Search API key."
    if configured and not located:
        message = "Brave Local is configured, but no latitude/longitude is available, so results are not geo-targeted."
    return {
        "configured": configured,
        "located": located,
        "location_source": settings.get("LOCATION_SOURCE", ""),
        "rewrite_enabled": _boolish(settings.get("REWRITE_QUERY"), default=True),
        "message": message,
    }


def _clean_snippet(value: Any) -> str:
    """Clean a grounding snippet the same way llm_intents prepares LLM context."""
    text = _text(value)
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\[Image: [^\]]+\]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _display_url(url: str) -> str:
    parsed = urllib.parse.urlparse(_text(url))
    return parsed.netloc or _text(url)


def _truncate_snippet(value: Any) -> str:
    snippet = _text(value)
    if len(snippet) > BRAVE_LOCAL_MAX_SNIPPET_CHARS:
        snippet = snippet[:BRAVE_LOCAL_MAX_SNIPPET_CHARS].rstrip() + "..."
    return snippet


def _parse_rewrite_query(content: Any, original_query: str) -> str:
    text = _text(content)
    if not text:
        return ""
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                text = _text(parsed.get("query") or parsed.get("search_query"))
        except Exception:
            pass
    text = re.sub(r"^(?:query|search query)\s*[:=]\s*", "", text.strip(), flags=re.IGNORECASE)
    text = text.strip().strip("\"'").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    text = lines[0] if lines else ""
    text = re.sub(r"\s+", " ", text).strip().strip("\"'").strip()
    if not text or len(text) > 160:
        return ""
    if text.casefold() == original_query.casefold():
        return ""
    return text


def _parse_name_corrections(raw: Any) -> List[Tuple[str, str]]:
    """Parse the Local Name Corrections field.

    One pair per line: ``spoken -> correct`` (``=>`` and ``=`` also work).
    Lines that do not split into two non-empty parts are ignored.
    """
    pairs: List[Tuple[str, str]] = []
    seen = set()
    for line in _text(raw).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        old = new = ""
        for arrow in (" -> ", " => ", " ->", " =>", " = ", "="):
            if arrow in line:
                old, _, new = line.partition(arrow)
                break
        old = old.strip()
        new = new.strip()
        if not old or not new:
            continue
        if len(old) > BRAVE_LOCAL_MAX_CORRECTION_CHARS or len(new) > BRAVE_LOCAL_MAX_CORRECTION_CHARS:
            continue
        key = (old.casefold(), new.casefold())
        if key in seen:
            continue
        seen.add(key)
        pairs.append((old, new))
        if len(pairs) >= BRAVE_LOCAL_MAX_NAME_CORRECTIONS:
            break
    return pairs


def _apply_name_corrections(query: str, corrections: List[Tuple[str, str]]) -> Tuple[str, bool]:
    """Apply spoken-name corrections deterministically, like an HA voice-profile map.

    Matching is case-insensitive and word-boundary aware so a correction for
    one name never rewrites the middle of another word.
    """
    text = _text(query)
    if not text or not corrections:
        return text, False
    applied = False
    for old, new in corrections:
        pattern = r"(?<![\w'])" + re.escape(old) + r"(?![\w'])"
        replaced, count = re.subn(pattern, lambda _match: new, text, flags=re.IGNORECASE)
        if count:
            text = replaced
            applied = True
    return text, applied


def _parse_tool_call_query(message: Dict[str, Any], original_query: str) -> str:
    """Extract the search query from an assistant tool call, if present."""
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    if not isinstance(tool_calls, list):
        return ""
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = _text(function.get("name"))
        if name and name != "search_web":
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            query_text = _text(arguments.get("query") or arguments.get("search_query"))
        else:
            raw_args = _text(arguments)
            if not raw_args:
                continue
            try:
                parsed = json.loads(raw_args)
                query_text = _text(parsed.get("query") or parsed.get("search_query")) if isinstance(parsed, dict) else ""
            except Exception:
                query_text = ""
        return _parse_rewrite_query(query_text, original_query)
    return ""


def _rewrite_reasoning_control() -> Optional[str]:
    """reasoning_effort for the rewrite call, mirroring Tater's own client.

    Tater's OpenAI-compatible client follows Tater's global LLM thinking toggle
    and fails open when a server rejects the field; this does the same. An env
    override wins: none/low/medium/high force a mode, "server" sends no control.
    """
    override = _text(os.getenv(BRAVE_LOCAL_REWRITE_REASONING_ENV)).lower()
    if override in BRAVE_LOCAL_REWRITE_REASONING_MODES:
        return override
    if override == "server":
        return None
    try:
        import helpers

        get_state = getattr(helpers, "hydra_llm_thinking_enabled", None)
        if callable(get_state):
            return "medium" if get_state() else "none"
    except Exception:
        pass
    return None


def _openai_compat_chat_sync(
    host: str,
    model: str,
    api_key: str,
    messages: List[Dict[str, Any]],
    timeout_val: int,
    *,
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> Dict[str, Any]:
    """Synchronous OpenAI-compatible chat call for the rewrite step.

    Returns the assistant message dict (content / tool_calls) or {} on failure.
    Like Tater's own client, it sends reasoning_effort when a mode is requested
    and retries once without it when the server rejects the field.
    """
    base = str(host or "").strip().rstrip("/")
    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    def _build_payload(reasoning: Optional[str]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": 0.0,
            "max_tokens": BRAVE_LOCAL_REWRITE_MAX_TOKENS,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
        if reasoning:
            payload["reasoning_effort"] = reasoning
        return payload

    attempts = [reasoning_effort, None] if reasoning_effort else [None]
    for index, attempt_reasoning in enumerate(attempts):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key and api_key != "not-needed":
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(
            url,
            data=json.dumps(_build_payload(attempt_reasoning), ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_val) as resp:
                raw = resp.read(BRAVE_LOCAL_MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            # Server rejected the reasoning control (unknown field); retry once
            # without it, the same way Tater's OpenAI-compatible client does.
            if index == 0 and attempt_reasoning is not None and exc.code in (400, 404, 422):
                continue
            return {}
        except Exception:
            return {}
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            return {}
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return {}
        message = choices[0].get("message")
        return message if isinstance(message, dict) else {}
    return {}


def _rewrite_query_with_llm(query: str, timeout_val: int = BRAVE_LOCAL_REWRITE_TIMEOUT_SECONDS) -> str:
    """Use Tater's own LLM to turn a spoken request into a compact search query.

    The model composes the query as a search_web tool call (the same shape
    Home Assistant's agent uses with llm_intents), with a plain-text fallback.
    Any failure falls back to the raw query. The call is fully synchronous
    (the client's _chat_sync, or urllib /chat/completions), which is required
    because Tater invokes search providers from its sync tool path.
    """
    messages = [
        {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
        {"role": "user", "content": query},
    ]
    reasoning_control = _rewrite_reasoning_control()
    try:
        import helpers

        get_client = getattr(helpers, "get_llm_client_from_env", None)
        if not callable(get_client):
            return ""
        llm_client = get_client()
        chat_sync = getattr(llm_client, "_chat_sync", None)
        if callable(chat_sync):
            kwargs: Dict[str, Any] = {
                "timeout": timeout_val,
                "max_tokens": BRAVE_LOCAL_REWRITE_MAX_TOKENS,
                "temperature": 0.0,
                "tools": [REWRITE_TOOL],
                "tool_choice": "auto",
            }
            if reasoning_control:
                # Force the mode on wrappers that otherwise apply Tater's default.
                kwargs["chat_template_kwargs"] = (
                    {"enable_thinking": False, "reasoning_budget": 0}
                    if reasoning_control == "none"
                    else {"enable_thinking": True}
                )
            result = chat_sync(messages, **kwargs)
            message = (result or {}).get("message") if isinstance(result, dict) else None
            if not isinstance(message, dict):
                return ""
            rewritten = _parse_tool_call_query(message, query)
            return rewritten or _parse_rewrite_query(message.get("content"), query)
        host = str(getattr(llm_client, "host", "") or "").strip()
        model = str(getattr(llm_client, "model", "") or "").strip()
        api_key = str(getattr(llm_client, "api_key", "") or "").strip()
        if not host or not model:
            return ""
        message = _openai_compat_chat_sync(
            host,
            model,
            api_key,
            messages,
            timeout_val,
            tools=[REWRITE_TOOL],
            tool_choice="auto",
            reasoning_effort=reasoning_control,
        )
        if not message:
            return ""
        rewritten = _parse_tool_call_query(message, query)
        return rewritten or _parse_rewrite_query(message.get("content"), query)
    except Exception:
        return ""


def _api_request(
    api_url: str,
    params: Dict[str, Any],
    *,
    headers: Dict[str, str],
    timeout_val: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    req = urllib.request.Request(
        api_url + "?" + urllib.parse.urlencode(params, doseq=True),
        headers={
            "Accept": "application/json",
            "User-Agent": "Tater-BraveLocal/1.0",
            **headers,
        },
    )
    return _read_json(req, timeout_val=timeout_val)


def _read_json(req: urllib.request.Request, *, timeout_val: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    try:
        with urllib.request.urlopen(req, timeout=timeout_val) as resp:
            raw = resp.read(BRAVE_LOCAL_MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(1000).decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        return None, _error(f"Brave Local request failed ({exc.code}): {body or exc}")
    except Exception as exc:
        return None, _error(f"Brave Local failed: {exc}")
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None, _error("Invalid response from Brave Local.")
    return payload if isinstance(payload, dict) else {"data": payload}, None


def _payload_error_message(payload: Dict[str, Any]) -> str:
    error_payload = payload.get("error")
    if isinstance(error_payload, dict):
        return _text(error_payload.get("detail") or error_payload.get("message") or error_payload.get("code"))
    if isinstance(error_payload, str):
        return error_payload
    return ""


def _error(message: str, *, needs: Optional[List[str]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "tool": "search_web",
        "ok": False,
        "provider": "brave_local",
        "provider_label": "Brave Local",
        "error": message,
    }
    if needs:
        out["needs"] = needs
    return out


def _normalize_result(item: Dict[str, Any], *, max_snippets_per_url: int) -> Optional[Dict[str, Any]]:
    title = _text(item.get("title"))
    url = _text(item.get("url"))
    raw_snippets = item.get("snippets") if isinstance(item.get("snippets"), list) else []
    snippets = [cleaned for cleaned in (_clean_snippet(snippet) for snippet in raw_snippets) if cleaned]
    snippets = snippets[:max_snippets_per_url]
    if not title and not url:
        return None
    return {
        "title": title or url,
        "url": url,
        "snippet": _truncate_snippet("\n".join(snippets)),
        "snippets": snippets,
        "display_url": _display_url(url),
    }


def _success(
    *,
    query: str,
    max_results: int,
    results: List[Dict[str, Any]],
    site_val: str,
    effective_query: str,
    rewrite_applied: bool,
    corrected_query: str,
    name_corrections_applied: bool,
    search_time_sec: Optional[float],
) -> Dict[str, Any]:
    return {
        "tool": "search_web",
        "ok": True,
        "provider": "brave_local",
        "provider_label": "Brave Local",
        "query": query,
        "original_query": query,
        "name_corrections_applied": name_corrections_applied,
        "corrected_query": corrected_query if name_corrections_applied else None,
        "rewrite_applied": rewrite_applied,
        "rewritten_query": effective_query if rewrite_applied else None,
        "effective_query": effective_query,
        "start": 1,
        "count": len(results),
        "num_results": max_results,
        "results": results,
        "site_filter": site_val or None,
        "search_time_sec": search_time_sec,
        "has_more": False,
        "next_start": None,
    }


def integration_web_search(
    query: Any,
    *,
    num_results: Any = 3,
    start: Any = 1,
    site: Any = None,
    safe: Any = "active",
    country: Any = None,
    language: Any = None,
    timeout_sec: Any = BRAVE_LOCAL_DEFAULT_TIMEOUT_SECONDS,
    freshness: Any = None,
) -> Dict[str, Any]:
    del start, safe, language  # The LLM Context endpoint returns a fixed top-N grounding set.
    q = _text(query)
    if not q:
        return _error("query is required.")
    settings = read_brave_local_settings()
    api_key = settings.get("BRAVE_API_KEY")
    if not api_key:
        return _error(
            "Brave Local is not configured.",
            needs=["Set a Brave Search API key in the Brave Local integration settings (the Brave Search key is used automatically if present)."],
        )

    max_results = _bounded_int(num_results, default=3, minimum=1, maximum=10)
    max_snippets_per_url = _bounded_int(settings.get("MAX_SNIPPETS_PER_URL"), default=3, minimum=1, maximum=5)
    max_tokens_per_url = _bounded_int(settings.get("MAX_TOKENS_PER_URL"), default=2048, minimum=128, maximum=4096)
    timeout_val = _bounded_int(timeout_sec, default=BRAVE_LOCAL_DEFAULT_TIMEOUT_SECONDS, minimum=3, maximum=60)
    site_val = _text(site)
    country_val = _text(country or settings.get("COUNTRY_CODE")).upper()
    if not re.fullmatch(r"[a-z]{2}", country_val.casefold()):
        country_val = ""
    latitude = _text(settings.get("LATITUDE"))
    longitude = _text(settings.get("LONGITUDE"))
    timezone_val = _text(settings.get("TIMEZONE"))
    post_code = _text(settings.get("POST_CODE"))

    rewrite_applied = False
    corrections = _parse_name_corrections(settings.get("NAME_CORRECTIONS"))
    corrected_query, corrections_applied = _apply_name_corrections(q, corrections)
    effective_query = corrected_query
    if _boolish(settings.get("REWRITE_QUERY"), default=True):
        rewritten = _rewrite_query_with_llm(corrected_query, timeout_val=BRAVE_LOCAL_REWRITE_TIMEOUT_SECONDS)
        if rewritten:
            effective_query = rewritten
            rewrite_applied = True
    search_query = f"site:{site_val} {effective_query}" if site_val else effective_query

    params: Dict[str, Any] = {
        "q": search_query,
        "count": max_results,
        "maximum_number_of_snippets": max_results * max_snippets_per_url,
        "maximum_number_of_tokens": max_results * max_tokens_per_url,
        "maximum_number_of_tokens_per_url": max_tokens_per_url,
        "maximum_number_of_snippets_per_url": max_snippets_per_url,
        "context_threshold_mode": settings.get("CONTEXT_THRESHOLD_MODE", "balanced"),
    }
    if country_val:
        params["country"] = country_val
    freshness_token = _text(freshness).lower()
    if freshness_token in {"pd", "pw", "pm", "py"}:
        params["freshness"] = freshness_token

    headers = {"X-Subscription-Token": api_key}
    if latitude:
        headers["X-Loc-Lat"] = latitude
    if longitude:
        headers["X-Loc-Long"] = longitude
    if timezone_val:
        headers["X-Loc-Timezone"] = timezone_val
    if country_val:
        headers["X-Loc-Country"] = country_val
    if post_code:
        headers["X-Loc-Postal-Code"] = post_code

    payload, error = _api_request(
        BRAVE_LOCAL_API_URL,
        params,
        headers=headers,
        timeout_val=timeout_val,
    )
    if error:
        return error
    if not payload:
        return _error("Invalid response from Brave Local.")
    error_message = _payload_error_message(payload)
    if error_message:
        return _error(error_message)

    grounding = payload.get("grounding") if isinstance(payload.get("grounding"), dict) else {}
    generic_rows = grounding.get("generic") if isinstance(grounding.get("generic"), list) else []
    results: List[Dict[str, Any]] = []
    seen_urls = set()
    for item in generic_rows:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_result(item, max_snippets_per_url=max_snippets_per_url)
        if not normalized:
            continue
        key = normalized["url"] or normalized["title"]
        if key in seen_urls:
            continue
        seen_urls.add(key)
        results.append(normalized)
        if len(results) >= max_results:
            break

    took_ms = payload.get("took_ms")
    try:
        search_time_sec = round(float(took_ms) / 1000.0, 3) if took_ms is not None else None
    except Exception:
        search_time_sec = None

    return _success(
        query=q,
        max_results=max_results,
        results=results,
        site_val=site_val,
        effective_query=effective_query,
        rewrite_applied=rewrite_applied,
        corrected_query=corrected_query,
        name_corrections_applied=corrections_applied,
        search_time_sec=search_time_sec,
    )


def run_integration_action(action_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if _text(action_id) != "test":
        raise KeyError(f"Unsupported Brave Local action: {action_id}")
    query = _text((payload or {}).get("query")) or "tater search test"
    result = integration_web_search(query, num_results=1, timeout_sec=BRAVE_LOCAL_DEFAULT_TIMEOUT_SECONDS)
    if not result.get("ok"):
        raise RuntimeError(_text(result.get("error")) or "Brave Local test failed.")
    parts = []
    if result.get("name_corrections_applied"):
        parts.append(f"name corrected to: {result.get('corrected_query')}")
    if result.get("rewrite_applied"):
        parts.append(f"query rewritten to: {result.get('rewritten_query')}")
    message = "Brave Local returned a result."
    if parts:
        message = f"Brave Local returned a result ({', '.join(parts)})."
    return {"ok": True, "message": message, "count": result.get("count", 0)}
