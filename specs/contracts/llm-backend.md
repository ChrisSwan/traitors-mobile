---
id: traitors-mobile-llm-backend
type: interface-contract
project: traitors-mobile
parents: [SWA-146, SWA-176]
status: draft
version: 2
paperclip_issue: SWA-177
owner_role: Architect
created: 2026-09-01
updated: 2026-09-03
---

# Module: LLM Backend (traitors-mobile-llm-backend)

> Revision 2 (2026-09-03, SWA-176/SWA-177): DeepSeek becomes the primary,
> default real-API backend. Claude is demoted to a legacy, non-default path
> (code retained, no longer expected to work — no Anthropic key is
> provisioned, intentional). Ollama stays the opportunistic/free secondary,
> unchanged. config.example.json's default `backend.provider` becomes
> `"deepseek"`. Revision 1 (SWA-146) specified Claude primary.

## Purpose
One interface for all model calls: DeepSeek (primary, default — OpenAI-compatible
chat completions), Claude (legacy path, retained but non-default), Ollama
(opportunistic secondary, LAN), and a deterministic Mock (tests). Owns retries
with exponential backoff, timeouts, provider selection, and the Ollama
availability probe. Satisfies spec §7 as amended by SWA-176 ("DeepSeek API is the
primary, reliable backend and the default in configuration… Claude is legacy and
not expected to work without a provisioned key… Local Ollama is a secondary,
opportunistic backend only… Failure handling: timeouts, rate limits, and
unreachable backends must surface as explicit, retryable errors… Automated tests
that don't need a real model call must use a mock backend.").

## Depends on
- None (standalone). Built second, right after Scenario.

## Constraints (non-goals)
- **No prompt building** — this module takes a ready `messages` list and returns text. Prompts belong to the Player module.
- No game/transcript concepts (no "exchanges", no logging of turns) — the Orchestrator owns recording.
- Must **never fabricate a response**: on any failure after retries are exhausted, raise, never return a placeholder string that could be recorded as a real exchange.
- **Never return `reasoning_content` as text.** DeepSeek v4-flash is a reasoning model: every chat completion response carries a `message.reasoning_content` field (chain-of-thought) alongside `message.content`. Only `content` is the model's answer. CoT must never be recorded as a player's dialogue, logged, or surfaced to the caller as `LLMResponse.text`.
- **Empty `content` is a failure, not a response.** DeepSeek returns `content: ""` with `finish_reason: "length"` when the `max_tokens` budget is exhausted by reasoning before an answer is produced (verified live). `complete` must never return `""` as if it were a real exchange and never substitute `reasoning_content`. **Revision (SWA-180, verified live 2026-09-03): empty content is RETRYABLE** — the reasoning length for an identical prompt is stochastic (the same real mid-game prompt produced 1.7k–6.5k completion tokens across repeated calls; attempts that returned content succeeded), so a fresh attempt within the retry budget has a real chance of answering. `complete` retries empty content with the normal exponential backoff and raises `BackendUnavailableError` when the budget is exhausted. A structurally malformed 2xx (no `content` field at all) remains an immediate, non-retryable `BackendError`.
- No writes to disk; no state beyond configuration.
- Ollama and DeepSeek access are plain HTTP — the `ollama`, `openai`, and `deepseek` Python packages are NOT installed and must NOT be added (all three verified absent in `.venv`).

## External dependencies
- `anthropic==1.2.0` (Claude SDK — **legacy path only**, retained per SWA-176, not the default). **Verified:** installed in `.venv` (`pip list` → 1.2.0, 2026-09-03).
- `requests==2.32.3` (DeepSeek + Ollama HTTP). **Verified:** installed in `.venv` (`pip list` → 2.32.3, 2026-09-03).
- `DEEPSEEK_API_KEY` env var. **Verified:** `grep -c '^DEEPSEEK_API_KEY=' ~/.hermes/.env` → 1 (key present, real value, not printed); live authenticated calls to the DeepSeek API returned HTTP 200 on 2026-09-03 (see endpoint row). Note: NOT set in paperclip agent shells by default — real runs must `source ~/.hermes/.env` or export it.
- DeepSeek endpoint `https://api.deepseek.com/v1` (OpenAI-compatible chat completions). **Verified live 2026-09-03:**
  - `GET https://api.deepseek.com/v1/models` (Bearer key) → HTTP 200, JSON list with ids `deepseek-v4-flash`, `deepseek-v4-pro`, `deepseek-v4-flash-vision-exp`. So model `deepseek-v4-flash` exists on the real API.
  - `POST https://api.deepseek.com/v1/chat/completions` with `{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":256,"temperature":0.0}` → HTTP 200, OpenAI-compatible `chat.completion` object; `choices[0].message` has keys `content`, `reasoning_content`, `role`; `content == "OK"`, `finish_reason == "stop"`; `usage.completion_tokens_details.reasoning_tokens == 22`.
  - Same call with `max_tokens: 16` → HTTP 200 but `content == ""`, `reasoning_content` non-empty, `finish_reason == "length"` (the whole budget was consumed by reasoning). This is the empty-content case the contract must handle.
- `ANTHROPIC_API_KEY` env var — **no longer provisioned** (intentional, per SWA-176; `grep -c '^ANTHROPIC_API_KEY=' ~/.hermes/.env` → 0 on 2026-09-03). The claude provider code path stays in place and still reads this var, but with no key set it will raise `ConfigError` at `create_backend` time. This is expected, not a bug — do not "fix" it by inventing a key.
- Ollama server `http://192.168.0.38:11434`. **Verified:** re-probed 2026-09-03 — `curl /api/tags` responds; models incl. `phi4:latest`, `qwen3:8b`. Opportunistic — may be offline at run time; `probe()` must detect this, never assume.

## Interface

### `class LLMBackend` (protocol)
All backends implement `complete` and `probe`.

### `complete(messages: list[dict], max_tokens: int = 256, temperature: float = 0.7, timeout: float | None = None) -> LLMResponse`
- Behavior: sends `messages` (list of `{"role": "system"|"user"|"assistant", "content": str}`) to the configured provider and returns `LLMResponse(text: str, model: str, raw: dict)` — `raw` is the provider's parsed JSON response (for debugging/tests; must not be relied on for gameplay).
  - DeepSeek: `POST {base_url}/chat/completions` with `{"model": ..., "messages": messages, "max_tokens": max_tokens, "temperature": temperature}` and header `Authorization: Bearer <api_key>` (OpenAI-compatible shape, verified live). Text extracted from `choices[0].message.content` only. If `content` is empty (`finish_reason == "length"` — reasoning consumed the budget) retry within the retry budget, then raise `BackendUnavailableError` (SWA-180 revision; reasoning length is stochastic, verified live 2026-09-03); if the `content` field is missing entirely (malformed 2xx) raise `BackendError` immediately. Never use `reasoning_content`.
  - Claude (legacy): `anthropic.Anthropic(...).messages.create(...)` as in revision 1 (content text from `content[0].text`). Unchanged code path.
  - Ollama: `POST {ollama_base_url}/v1/chat/completions` with the same OpenAI-compatible payload as DeepSeek (verified live). Unchanged.
  - Retry policy (all HTTP providers): retry on HTTP 429/5xx and network errors with exponential backoff `retry_backoff_base_seconds * 2**attempt`, up to `max_retries`.
- Raises: `BackendTimeoutError` (timeout exceeded, retryable); `RateLimitError` (provider rate limit, retryable); `BackendUnreachableError` (network/connection failure, retryable); non-retryable 4xx (auth/validation) → `BackendError` immediately; empty/missing content on a 2xx → `BackendError` (message should say content was empty and reasoning likely consumed the `max_tokens` budget); after the retry budget is exhausted on retryable errors, `BackendUnavailableError` (message includes provider, model, attempts, last error). All are subclasses of `BackendError`. Never returns a partial/fabricated response.
- Side effects: network calls only; no file or game-state changes.

### `probe() -> ProbeResult`
- Behavior:
  - DeepSeek: `GET {base_url}/models` with auth header and a short timeout (5s); returns `ProbeResult(available=True, models=[ids...], error=None)` iff HTTP 200 **and** the configured model id appears in the returned list; otherwise `available=False` with the error string. (The `/v1/models` endpoint is verified live.)
  - Ollama: unchanged from revision 1 — `GET {ollama_base_url}/api/tags` with a short timeout (5s).
  - Claude (legacy): unchanged from revision 1 — config-only `ProbeResult(available=True, models=[configured_model])` (no key provisioned; liveness would require a real call).
  - Mock: `available=True, models=["mock"]`.
- Raises: never (returns a result instead).
- Side effects: for DeepSeek/Ollama, one network call.

### `class MockBackend(scripted: list[str] | callable, model: str = "mock")`
- Behavior: unchanged from revision 1 — deterministic test backend. If `scripted` is a list, `complete` pops responses from the front (raising `BackendUnavailableError` when exhausted); if callable, calls `scripted(messages)` and uses its return. Responses must be plain text strings (the caller parses them).
- Raises: `BackendUnavailableError` when scripted responses are exhausted.
- Side effects: none.

### `class DeepSeekBackend(LLMBackend)` (new)
- Constructor: `DeepSeekBackend(api_key: str, model: str = DEFAULT_DEEPSEEK_MODEL, base_url: str = DEFAULT_DEEPSEEK_BASE_URL, max_retries: int = DEFAULT_MAX_RETRIES, retry_backoff_base_seconds: float = DEFAULT_RETRY_BACKOFF_BASE_SECONDS, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS)`.
- New module constants: `DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"`, `DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"`.
- `complete`/`probe` as specified in the shared signatures above (DeepSeek bullets).

### `create_backend(config: BackendConfig) -> LLMBackend`
- Behavior: factory. `provider == "deepseek"` → `DeepSeekBackend(api_key=os.environ["DEEPSEEK_API_KEY"], model=config.get("model", DEFAULT_DEEPSEEK_MODEL), ...)`; `provider == "claude"` (legacy) → `ClaudeBackend(api_key=os.environ["ANTHROPIC_API_KEY"], ...)`; `provider == "ollama"` → `OllamaBackend(...)`; `provider == "mock"` → `MockBackend(...)`. Raises `ConfigError` for any other provider string.
- ConfigError messages: when `provider == "deepseek"` and `DEEPSEEK_API_KEY` is missing → `"DEEPSEEK_API_KEY not set… source ~/.hermes/.env"`. When `provider == "claude"` and `ANTHROPIC_API_KEY` is missing → the existing revision-1 message (kept; expected to fire until a key is provisioned).
- Raises: `ConfigError` as above.
- Side effects: none.

### Config defaults (integration schema, tech design §8)
- `backend.provider` default flips `"claude"` → `"deepseek"`; `backend.model` default flips `"claude-haiku-4-5"` → `"deepseek-v4-flash"`; `config.example.json` must ship `"provider": "deepseek"` / `"model": "deepseek-v4-flash"`.
- Valid provider strings: `"deepseek" | "claude" | "ollama" | "mock"` (the integration module's `VALID_PROVIDERS` must include `"deepseek"`).
- The claude model default `claude-haiku-4-5` remains valid if a user explicitly selects `"provider": "claude"` and supplies a key.

## Reuse check
Searched existing `specs/` contracts (this repo, and other repos under `~/sdd-projects/`) for: `llm backend`, `anthropic`, `ollama`, `deepseek`, `openai`, `chat completions`. Found: none with interface contracts (prior projects' backends were build-time inventions, not contracted). This module was already flagged a **strong reuse candidate** in revision 1; revision 2 does not change that — DeepSeek is deliberately added through the same plain-HTTP OpenAI-compatible pattern as Ollama (one code path shape for both), keeping the module game-free (no imports from `scenario`, `player`, etc.).

## QA acceptance highlights (behavioral)
- With a scripted MockBackend, `complete` returns the scripted texts in order; exhausting the script raises `BackendUnavailableError` (unchanged from revision 1).
- `create_backend({"provider": "deepseek"})` with `DEEPSEEK_API_KEY` in env → returns a `DeepSeekBackend` whose `model` defaults to `deepseek-v4-flash`.
- `create_backend({"provider": "deepseek"})` without `DEEPSEEK_API_KEY` in env raises `ConfigError` naming `DEEPSEEK_API_KEY` and the `source ~/.hermes/.env` fix.
- `create_backend({"provider": "claude"})` without `ANTHROPIC_API_KEY` still raises `ConfigError` naming the missing key (existing test, no regression).
- A mocked DeepSeek 2xx response `{"choices":[{"message":{"role":"assistant","content":"<text>","reasoning_content":"<secret>"}}]}` → `complete` returns `LLMResponse(text="<text>", ...)`; **`text` never contains `<secret>`** (CoT never leaks into dialogue).
- A mocked DeepSeek 2xx response with `content: ""` and non-empty `reasoning_content` → `complete` raises (a `BackendError` subclass) once the retry budget is exhausted — never returns `""`, never returns the reasoning text. (SWA-180: empty content is retryable within the budget; a mocked empty-then-success sequence returns the second attempt's text with backoff between attempts.)
- DeepSeek HTTP 429 → `RateLimitError`; HTTP 500 → `BackendUnreachableError`; HTTP 401 → non-retryable `BackendError`; both retryable classes raise `BackendUnavailableError` once the retry budget is exhausted (never-fabricate rule).
- `DeepSeekBackend.probe()` with an unreachable/invalid URL returns `available=False` gracefully (unit test, no network); against the real `https://api.deepseek.com/v1` with the key it returns `available=True` including `deepseek-v4-flash` (integration check only, not in unit tests).
- `OllamaBackend.probe()` behavior unchanged: `available=False` on unreachable server; `available=True` with the real models list against `http://192.168.0.38:11434` (integration check only).
- Integration defaults: `load_config({})` yields `backend.provider == "deepseek"` and `backend.model == "deepseek-v4-flash"`; `config.example.json` `backend.provider == "deepseek"`.
- The full existing suite (240 tests) stays green — claude/ollama/mock backends are unaffected (no regressions).
