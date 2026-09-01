---
id: traitors-mobile-llm-backend
type: interface-contract
project: traitors-mobile
parents: [SWA-146]
status: draft
version: 1
paperclip_issue: SWA-146
owner_role: Architect
created: 2026-09-01
updated: 2026-09-01
---

# Module: LLM Backend (traitors-mobile-llm-backend)

## Purpose
One interface for all model calls: Claude (primary, default), Ollama (opportunistic secondary, LAN), and a deterministic Mock (tests). Owns retries with exponential backoff, timeouts, provider selection, and the Ollama availability probe. Satisfies spec §7: "Claude API is the primary, reliable backend and the default in configuration… Local Ollama is a secondary, opportunistic backend only… Failure handling: timeouts, rate limits, and unreachable backends must surface as explicit, retryable errors… Automated tests that don't need a real model call must use a mock backend."

## Depends on
- None (standalone). Built second, right after Scenario.

## Constraints (non-goals)
- **No prompt building** — this module takes a ready `messages` list and returns text. Prompts belong to the Player module.
- No game/transcript concepts (no "exchanges", no logging of turns) — the Orchestrator owns recording.
- Must **never fabricate a response**: on any failure after retries are exhausted, raise, never return a placeholder string that could be recorded as a real exchange.
- No writes to disk; no state beyond configuration.
- Ollama access is plain HTTP — the `ollama` Python package is NOT installed and must NOT be added (verified absent).

## External dependencies
- `anthropic==1.2.0` (Claude SDK). **Verified:** `pip index versions anthropic` → 1.2.0 is LATEST; also `python3 -c "import anthropic"` → 0.87.0 importable in base env (fallback).
- `requests==2.32.3` (Ollama HTTP). **Verified:** `python3 -c "import requests"` → 2.32.3 importable.
- `ANTHROPIC_API_KEY` env var. **Verified:** key name present in `~/.hermes/.env` (value not printed); live SDK call `messages.create(model="claude-haiku-4-5", max_tokens=8)` returned "OK" on 2026-09-01. Note: NOT set in paperclip agent shells by default — real runs must source `~/.hermes/.env` or export it.
- Ollama server `http://192.168.0.38:11434`. **Verified:** `curl /api/tags` and `curl /v1/models` both responded on 2026-09-01; 10 models listed incl. `qwen3:8b`. Opportunistic — may be offline at run time; `probe()` must detect this, never assume.

## Interface

### `class LLMBackend` (protocol)
All backends implement `complete` and `probe`.

### `complete(messages: list[dict], max_tokens: int = 256, temperature: float = 0.7, timeout: float | None = None) -> LLMResponse`
- Behavior: sends `messages` (list of `{"role": "system"|"user"|"assistant", "content": str}`) to the configured provider and returns `LLMResponse(text: str, model: str, raw: dict)` — `raw` is the provider's parsed JSON response (for debugging/tests; must not be relied on for gameplay). Claude: `anthropic.Anthropic(...).messages.create(model=..., max_tokens=..., messages=...)` (content text extracted from `content[0].text`). Ollama: `POST {ollama_base_url}/v1/chat/completions` with `{"model": ..., "messages": messages, "max_tokens": max_tokens, "temperature": temperature}` (OpenAI-compatible shape, verified live). Retry policy: retry on `RateLimitError`, HTTP 429/5xx, and network errors, with exponential backoff `retry_backoff_base_seconds * 2**attempt`, up to `max_retries`.
- Raises: `BackendTimeoutError` (timeout exceeded, retryable); `RateLimitError` (provider rate limit, retryable); `BackendUnreachableError` (network/connection failure, retryable); after the retry budget is exhausted on any of these, `BackendUnavailableError` (message includes provider, model, attempts, last error). All are subclasses of `BackendError`. Never returns a partial/fabricated response.
- Side effects: network calls only; no file or game-state changes.

### `probe() -> ProbeResult`
- Behavior: for Ollama: `GET {ollama_base_url}/api/tags` with a short timeout (5s); returns `ProbeResult(available: bool, models: list[str], error: str | None)`. For Claude: returns `ProbeResult(available=True, models=[configured_model], error=None)` — Claude liveness is only determinable by a real call, which the retry policy handles; the probe documents configuration, not connectivity. For Mock: `available=True, models=["mock"]`.
- Raises: never (returns a result instead).
- Side effects: for Ollama, one network call.

### `class MockBackend(scripted: list[str] | callable, model: str = "mock")`
- Behavior: deterministic test backend. If `scripted` is a list, `complete` pops responses from the front (raising `BackendUnavailableError` when exhausted — so tests can prove the orchestrator aborts rather than fabricates); if callable, calls `scripted(messages)` and uses its return. Responses must be plain text strings (the caller parses them).
- Raises: `BackendUnavailableError` when scripted responses are exhausted.
- Side effects: none.

### `create_backend(config: BackendConfig) -> LLMBackend`
- Behavior: factory. `provider == "claude"` → `ClaudeBackend(api_key=os.environ["ANTHROPIC_API_KEY"], model=config.model, ...)`; `provider == "ollama"` → `OllamaBackend(base_url=config.ollama_base_url, model=config.model, ...)`; `provider == "mock"` → `MockBackend(...)`. Raises `ConfigError` for any other provider string, and a clear `ConfigError` ("ANTHROPIC_API_KEY not set… source ~/.hermes/.env") when `provider == "claude"` and the env var is missing.
- Raises: `ConfigError` as above.
- Side effects: none.

## Reuse check
Searched existing `specs/` contracts (this repo and other repos under `~/sdd-projects/`) for: `llm backend`, `anthropic`, `ollama`, `retry`. Found: none with interface contracts (prior projects' backends were build-time inventions, not contracted). **Strong reuse candidate** — a messages-in/text-out LLM abstraction with retries, mock, and a provider factory is generic and likely to be reused by future experiments and other projects. Its interface must stay game-free (no imports from `scenario`, `player`, etc.).

## QA acceptance highlights (behavioral)
- With a scripted MockBackend, `complete` returns the scripted texts in order; exhausting the script raises `BackendUnavailableError`.
- A backend configured to always raise (mock that raises on every call) does NOT return text after retries — it raises `BackendUnavailableError` (never-fabricate rule).
- `create_backend({"provider": "claude"})` without `ANTHROPIC_API_KEY` in env raises `ConfigError` naming the missing key.
- `OllamaBackend.probe()` returns `available=False` gracefully when the server is unreachable (test with an invalid URL), and `available=True` with the real models list when run against `http://192.168.0.38:11434` (integration check only, not in unit tests).
