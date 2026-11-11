"""Real LLM clients (contracts v0.8): OpenAI (Responses API) and Anthropic (Messages API).

Stdlib only (urllib): the two endpoints are single JSON POSTs, so an SDK would add a dependency
without buying anything we use. Keys come only from OPENAI_API_KEY / ANTHROPIC_API_KEY and are
never logged, printed, or put in exceptions (every outgoing text passes `redact`).

Dry run (RPG_LLM_DRY_RUN=1 or --dry-run): build the exact request, print it with the key
redacted, make no network call, and answer with the MockLLM so the game stays playable. Works
without a key.
"""
from __future__ import annotations

import json
import logging
import os
import random
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import prompts
from .llm import LLMConfigError, MockLLM, check_input_length
from .proposal_schema import SCHEMA_NAME, proposal_schema, voice_schema

log = logging.getLogger("rpg.llm")

ROLES = ("interpret", "narrate", "voice")
TIMEOUT_S = 60.0
MAX_RETRIES = 3            # after the first try: 4 attempts at most
BACKOFF_BASE_S = 0.5       # 0.5, 1, 2 (+ jitter), capped
BACKOFF_CAP_S = 8.0
RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504, 529}
MAX_TOKENS = {"interpret": 2000, "narrate": 2000, "voice": 800}  # reasoning/thinking tokens count too
REDACTED = "***redacted***"


class LLMError(RuntimeError):
    """A provider call failed (after retries) or returned nothing usable."""


def truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

Transport = Callable[[str, Dict[str, str], bytes, float], Tuple[int, Dict[str, str], bytes]]


def urllib_transport(url: str, headers: Dict[str, str], body: bytes, timeout: float):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() or b""


class _Provider:
    name = "base"
    env_key = ""
    url = ""
    default_models: Dict[str, str] = {}

    def __init__(self, *, dry_run: Optional[bool] = None, transport: Optional[Transport] = None,
                 sleep: Callable[[float], None] = time.sleep, out: Optional[Callable[[str], None]] = None,
                 rng: Optional[random.Random] = None):
        self.dry_run = truthy(os.environ.get("RPG_LLM_DRY_RUN")) if dry_run is None else dry_run
        key = os.environ.get(self.env_key) or ""
        if not key and not self.dry_run:
            raise LLMConfigError(
                f"RPG_LLM={self.name} needs the {self.env_key} environment variable. "
                f"Set it (export {self.env_key}=...), use --dry-run to see the requests without one, "
                f"or use RPG_LLM=mock.")
        self._api_key = key  # never logged, printed or repr'd
        self.models = {role: self._model_for(role) for role in ROLES}
        self.model = self.models["interpret"]
        self._transport = transport or urllib_transport
        self._sleep = sleep
        self._out = out or (lambda s: print(s, file=sys.stderr))
        self._rng = rng or random.Random()
        self._mock = MockLLM()
        self._last_player_text = ""
        self._last_interpret_raw: Optional[str] = None

    def _model_for(self, role: str) -> str:
        p = self.name.upper()
        return (os.environ.get(f"RPG_{p}_MODEL_{role.upper()}") or os.environ.get(f"RPG_{p}_MODEL")
                or os.environ.get("RPG_LLM_MODEL") or self.default_models[role])

    def __repr__(self):
        return f"<{type(self).__name__} models={self.models!r} dry_run={self.dry_run}>"

    # -- secrets -------------------------------------------------------------------
    def redact(self, text: str) -> str:
        return text.replace(self._api_key, REDACTED) if self._api_key else text

    def redacted_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        hidden = REDACTED if self._api_key else f"<unset: {self.env_key}>"
        return {k: (hidden if k.lower() in ("authorization", "x-api-key") else v) for k, v in headers.items()}

    # -- the call --------------------------------------------------------------------
    def headers(self) -> Dict[str, str]:
        raise NotImplementedError

    def post(self, role: str, body: Dict) -> Optional[Dict]:
        """POST with retries; None in dry-run (after printing the request)."""
        if self.dry_run:
            self._out(self.redact(json.dumps({"dry_run": True, "provider": self.name, "role": role,
                                              "url": self.url, "headers": self.redacted_headers(self.headers()),
                                              "body": body}, indent=2, ensure_ascii=False)))
            return None
        data = json.dumps(body).encode("utf-8")
        last = "no attempt"
        for attempt in range(MAX_RETRIES + 1):
            retry_after = None
            try:
                status, hdrs, raw = self._transport(self.url, self.headers(), data, TIMEOUT_S)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                status, hdrs, raw, last = None, {}, b"", f"network error: {type(e).__name__}"
            if status is not None:
                if 200 <= status < 300:
                    try:
                        parsed = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        raise LLMError(f"{self.name} {role}: response body was not JSON") from None
                    self._log_usage(role, parsed)
                    return parsed
                last = f"HTTP {status}: {self.redact(raw[:300].decode('utf-8', 'replace'))}"
                if status not in RETRY_STATUSES:
                    raise LLMError(f"{self.name} {role} failed: {last}")
                retry_after = {k.lower(): v for k, v in hdrs.items()}.get("retry-after")
            if attempt == MAX_RETRIES:
                break
            delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** attempt) * (1 + 0.25 * self._rng.random())
            try:
                delay = max(delay, min(BACKOFF_CAP_S, float(retry_after))) if retry_after else delay
            except ValueError:
                pass
            log.debug("%s %s: %s; retry %d in %.1fs", self.name, role, last, attempt + 1, delay)
            self._sleep(delay)
        raise LLMError(f"{self.name} {role} failed after {MAX_RETRIES + 1} attempts: {last}")

    def _log_usage(self, role: str, resp: Dict) -> None:
        """Record token/cache usage from a provider response on the existing llm logger."""
        usage = resp.get("usage") or {}
        details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens")
        if cached is None:
            cached = usage.get("cache_read_input_tokens")
        written = details.get("cache_write_tokens")
        if written is None:
            written = usage.get("cache_creation_input_tokens")
        info = {
            "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens"),
            "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens"),
            "cached_tokens": cached,
            "cache_write_tokens": written,
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        }
        self.last_usage = info
        log.info("%s %s usage input=%s output=%s cached_tokens=%s cache_write=%s cache_read_input_tokens=%s",
                 self.name, role, info["input_tokens"], info["output_tokens"],
                 info["cached_tokens"], info["cache_write_tokens"], info["cache_read_input_tokens"])

    # -- LLMClient ---------------------------------------------------------------------
    def interpret(self, player_text: str, context: Dict, reprompt: Optional[str] = None):
        """Returns the parsed proposal dict, or the raw text when it isn't JSON (the engine's
        validate_proposal then answers with the re-prompt, once)."""
        check_input_length(player_text)
        self._last_player_text = player_text
        system, user = prompts.interpret_messages(context, player_text)
        turns = [("user", user)]
        if reprompt:
            if self._last_interpret_raw:
                turns.append(("assistant", self._last_interpret_raw))
            turns.append(("user", reprompt))
        body = self.build("interpret", system, turns, schema=(SCHEMA_NAME, proposal_schema()))
        resp = self.post("interpret", body)
        if resp is None:
            return self._mock.interpret(player_text, context, reprompt)
        text = self.parse_text(resp, "interpret")
        self._last_interpret_raw = text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def narrate(self, result: Dict, context: Dict) -> str:
        system, user = prompts.narrate_messages(result, context, context.get("player_text") or
                                                ("" if result.get("opening") else self._last_player_text))
        resp = self.post("narrate", self.build("narrate", system, [("user", user)]))
        if resp is None:
            return self._mock.narrate(result, context)
        return self.parse_text(resp, "narrate")  # the caller's post-processor checks and closes it

    def voice_npc(self, npc_persona_view: Dict, allowed_topics: List[Dict], result: Dict, context: Dict) -> Dict:
        system, user = prompts.voice_messages(npc_persona_view, result)
        resp = self.post("voice", self.build("voice", system, [("user", user)], schema=("npc_lines", voice_schema())))
        if resp is None:
            return self._mock.voice_npc(npc_persona_view, allowed_topics, result, context)
        return self.parse_text(resp, "voice")  # JSON text; talk.voice parses it and retries once if bad

    # provider specifics
    def build(self, role: str, system: str, turns: List[Tuple[str, str]], schema=None) -> Dict:
        raise NotImplementedError

    def parse_text(self, resp: Dict, role: str) -> str:
        raise NotImplementedError


class OpenAIClient(_Provider):
    """POST /v1/responses with text.format json_schema (strict) for interpret and voice."""
    name, env_key = "openai", "OPENAI_API_KEY"
    url = "https://api.openai.com/v1/responses"
    default_models = {"interpret": "gpt-6-luna", "voice": "gpt-6-luna", "narrate": "gpt-6.1-sol"}

    def headers(self):
        return {"Authorization": f"Bearer {self._api_key or 'MISSING_OPENAI_API_KEY'}",
                "Content-Type": "application/json"}

    def build(self, role, system, turns, schema=None):
        body: Dict[str, Any] = {
            "model": self.models[role], "instructions": system,
            "input": [{"role": r, "content": t} for r, t in turns],
            "max_output_tokens": MAX_TOKENS[role], "store": False,
            "prompt_cache_key": f"rpg-{role}",
        }
        # GPT-6 Responses API: prompt_cache_retention is deprecated; ttl is the supported control.
        # 12h suits the static system prefix (incl. Context field notes) reused across turns.
        if getattr(self, "use_prompt_cache_options", True):
            body["prompt_cache_options"] = {"ttl": "12h"}
        effort = os.environ.get("RPG_OPENAI_REASONING_EFFORT")
        if effort:
            body["reasoning"] = {"effort": effort}
        if schema:
            body["text"] = {"format": {"type": "json_schema", "name": schema[0], "schema": schema[1], "strict": True}}
        return body

    def parse_text(self, resp, role):
        parts, refusal = [], None
        for item in resp.get("output") or []:
            if item.get("type") != "message":
                continue
            for c in item.get("content") or []:
                if c.get("type") == "output_text":
                    parts.append(c.get("text") or "")
                elif c.get("type") == "refusal":
                    refusal = c.get("refusal") or "refused"
        if not parts and isinstance(resp.get("output_text"), str):
            parts = [resp["output_text"]]
        if not parts:
            raise LLMError(f"{self.name} {role}: no text in the response"
                           + (" (the model refused)" if refusal else f" (status {resp.get('status')!r})"))
        if resp.get("status") == "incomplete":
            log.debug("%s %s: incomplete response (%s)", self.name, role, resp.get("incomplete_details"))
        return "".join(parts)


class AnthropicClient(_Provider):
    """POST /v1/messages with output_config.format json_schema for interpret and voice."""
    name, env_key = "anthropic", "ANTHROPIC_API_KEY"
    url = "https://api.anthropic.com/v1/messages"
    version = "2026-10-21"  # no temperature/top_p/top_k in this version: we send none
    default_models = {"interpret": "claude-haiku-5-5", "voice": "claude-haiku-5-5", "narrate": "claude-sonnet-5-5"}
    default_effort = {"interpret": "low", "voice": "low", "narrate": None}

    def headers(self):
        return {"x-api-key": self._api_key or "MISSING_ANTHROPIC_API_KEY", "anthropic-version": self.version,
                "content-type": "application/json"}

    def build(self, role, system, turns, schema=None):
        body: Dict[str, Any] = {
            "model": self.models[role], "max_tokens": MAX_TOKENS[role],
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": r, "content": t} for r, t in turns],
        }
        out: Dict[str, Any] = {}
        effort = os.environ.get("RPG_ANTHROPIC_EFFORT") or self.default_effort[role]
        if effort:
            out["effort"] = effort
        if schema:
            out["format"] = {"type": "json_schema", "schema": schema[1]}
        if out:
            body["output_config"] = out
        return body

    def parse_text(self, resp, role):
        text = "".join(b.get("text") or "" for b in resp.get("content") or [] if b.get("type") == "text")
        if not text:
            raise LLMError(f"anthropic {role}: no text in the response (stop_reason {resp.get('stop_reason')!r})")
        if resp.get("stop_reason") == "max_tokens":
            log.debug("anthropic %s: hit max_tokens", role)
        return text


class GrokClient(OpenAIClient):
    """xAI Responses API (docs.x.ai): POST {base}/responses, same shape as OpenAI, including
    text.format json_schema (strict), store, and prompt_cache_key. Default model grok-4.7
    was listed on docs.x.ai this run. prompt_cache_options is omitted (not documented for xAI)."""
    name, env_key = "grok", "XAI_API_KEY"
    default_models = {"interpret": "grok-4.7", "voice": "grok-4.7", "narrate": "grok-4.7"}
    use_prompt_cache_options = False
    default_base_url = "https://api.x.ai/v1"

    def __init__(self, **kw):
        super().__init__(**kw)
        base = (os.environ.get("RPG_GROK_BASE_URL") or self.default_base_url).rstrip("/")
        self.url = base + "/responses"
