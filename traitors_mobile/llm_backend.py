"""
traitors-mobile-llm-backend (Module 3)

One interface for all model calls: DeepSeek (primary, default), Claude (legacy),
Ollama (opportunistic secondary, LAN), and a deterministic Mock (tests). Owns
retries with exponential backoff, timeouts, provider selection, and backend
availability probes.

Contract: specs/contracts/llm-backend.md revision 2 (SWA-176/SWA-177).

Constraints honored:
- No prompt building -- this module takes a ready ``messages`` list and
  returns text; prompts belong to the Player module.
- No game/transcript concepts; no writes to disk; no state beyond config.
- Never fabricates a response: when the retry budget is exhausted it
  raises ``BackendUnavailableError`` -- never a placeholder string.
- Never return reasoning_content: DeepSeek v4-flash is a reasoning model;
  only ``content`` field is returned as dialogue, never CoT.
- Empty content is a failure: DeepSeek may exhaust max_tokens on reasoning
  before producing an answer (content: ""). This raises, never returns "".

Pinned stack (tech design sec 6): requests==2.32.3 (DeepSeek + Ollama HTTP),
anthropic==1.2.0 (Claude SDK, legacy path only, no longer default).
No SDK packages for deepseek/ollama/openai.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, TypedDict, Union

import requests

try:  # anthropic is a pinned dependency (anthropic==1.2.0)
    import anthropic
except ImportError:  # pragma: no cover - only reachable if deps not installed
    anthropic = None

# Defaults mirror tech design sec 8 (config schema).
DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_OLLAMA_MODEL = "qwen3:8b"
DEFAULT_OLLAMA_BASE_URL = "http://192.168.0.38:11434"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_BASE_SECONDS = 2.0
DEFAULT_PROBE_TIMEOUT_SECONDS = 5.0


class BackendConfig(TypedDict, total=False):
    """Configuration dict accepted by ``create_backend`` (tech design sec 8)."""

    provider: str
    model: str
    timeout_seconds: float
    max_retries: int
    retry_backoff_base_seconds: float
    ollama_base_url: str
    scripted: List[str]


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class BackendError(Exception):
    """Base class for all LLM backend errors."""

    def __init__(
        self,
        message: str,
        *,
        model: Optional[str] = None,
        retries: Optional[int] = None,
        provider: Optional[str] = None,
        attempts: Optional[int] = None,
        last_error: Optional[Any] = None,
    ) -> None:
        self.message = message
        self.model = model
        self.retries = retries
        self.provider = provider
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(message)

    def __str__(self) -> str:
        details = []
        for name in ("provider", "model", "attempts", "retries", "last_error"):
            value = getattr(self, name, None)
            if value is not None:
                details.append(f"{name}={value!r}")
        suffix = f" ({', '.join(details)})" if details else ""
        return f"{type(self).__name__}: {self.message}{suffix}"


class BackendTimeoutError(BackendError):
    """A request exceeded its timeout. Retryable."""


class RateLimitError(BackendError):
    """The provider rate-limited the request. Retryable."""


class BackendUnreachableError(BackendError):
    """Network/connection failure or server error. Retryable."""


class BackendUnavailableError(BackendError):
    """The retry budget was exhausted; the backend is unavailable.

    Raised instead of ever returning a partial/fabricated response.
    """


class ConfigError(Exception):
    """Invalid backend configuration (bad provider, missing API key)."""


# --------------------------------------------------------------------------
# Data models
# --------------------------------------------------------------------------


@dataclass
class LLMResponse:
    """A completed model call: the text plus provenance for debugging."""

    text: str
    model: str
    raw: dict = field(default_factory=dict)


@dataclass
class ProbeResult:
    """Outcome of a backend availability probe (probe() never raises)."""

    available: bool
    models: List[str]
    error: Optional[str] = None


# --------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------


class LLMBackend:
    """Protocol base: every backend implements ``complete`` and ``probe``."""

    model: str

    def complete(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 256,
        temperature: float = 0.7,
        timeout: Optional[float] = None,
    ) -> LLMResponse:
        """Send ``messages`` and return the model's text response."""
        raise NotImplementedError

    def probe(self) -> ProbeResult:
        """Report backend availability; never raises."""
        raise NotImplementedError


# --------------------------------------------------------------------------
# Mock backend (deterministic tests / dry runs)
# --------------------------------------------------------------------------


class MockBackend(LLMBackend):
    """Deterministic test backend.

    If ``scripted`` is a list, ``complete`` pops responses from the front,
    raising ``BackendUnavailableError`` when exhausted (never fabricates).
    If callable, ``complete`` calls ``scripted(messages)`` and uses its
    return value.
    """

    def __init__(self, scripted: Union[List[str], Callable[[List[Dict[str, str]]], str]], model: str = "mock") -> None:
        self.model = model
        if callable(scripted):
            self._scripted: Optional[List[str]] = None
            self._script_callable: Optional[Callable[[List[Dict[str, str]]], str]] = scripted
        else:
            self._scripted = list(scripted)
            self._script_callable = None

    def complete(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 256,
        temperature: float = 0.7,
        timeout: Optional[float] = None,
    ) -> LLMResponse:
        if self._script_callable is not None:
            text = self._script_callable(messages)
            return LLMResponse(text=text, model=self.model, raw={})
        if not self._scripted:
            raise BackendUnavailableError(
                "MockBackend script exhausted: no scripted responses remain "
                "and no response will be fabricated",
                provider="mock",
                model=self.model,
                attempts=0,
                last_error="script exhausted",
            )
        text = self._scripted.pop(0)
        return LLMResponse(text=text, model=self.model, raw={})

    def probe(self) -> ProbeResult:
        return ProbeResult(available=True, models=["mock"], error=None)


# --------------------------------------------------------------------------
# Claude backend (legacy, non-default -- retained per SWA-176; no key provisioned)
# --------------------------------------------------------------------------


class ClaudeBackend(LLMBackend):
    """Claude via the official ``anthropic`` SDK (Messages API)."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_CLAUDE_MODEL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_base_seconds: float = DEFAULT_RETRY_BACKOFF_BASE_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if anthropic is None:  # pragma: no cover - pinned dep, defensive
            raise ConfigError("anthropic SDK not importable; install anthropic==1.2.0")
        self.api_key = api_key
        self.model = model
        self.max_retries = max_retries
        self.retry_backoff_base_seconds = retry_backoff_base_seconds
        self.timeout_seconds = timeout_seconds
        self._client = anthropic.Anthropic(api_key=api_key)

    def complete(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 256,
        temperature: float = 0.7,
        timeout: Optional[float] = None,
    ) -> LLMResponse:
        request_timeout = timeout if timeout is not None else self.timeout_seconds
        last_error: Optional[BackendError] = None
        for attempt in range(self.max_retries + 1):
            try:
                # Extract system message from messages list (Anthropic SDK expects
                # system as a separate parameter, not in the messages list).
                system_message = None
                non_system_messages = []
                for msg in messages:
                    if msg.get("role") == "system":
                        system_message = msg.get("content")
                    else:
                        non_system_messages.append(msg)
                
                # Build kwargs for the API call.
                # Note: anthropic 1.2.0 does not expose 'temperature' as a direct parameter;
                # it must be passed via extra_body for forward compatibility.
                create_kwargs = {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "messages": non_system_messages,
                    "timeout": request_timeout,
                    "extra_body": {"temperature": temperature},
                }
                if system_message:
                    create_kwargs["system"] = system_message
                
                response = self._client.messages.create(**create_kwargs)
                text = response.content[0].text
                return LLMResponse(text=text, model=self.model, raw=self._raw_dict(response))
            except anthropic.RateLimitError as exc:
                last_error = RateLimitError(str(exc), model=self.model, retries=self.max_retries - attempt)
            except anthropic.APITimeoutError as exc:
                last_error = BackendTimeoutError(str(exc), model=self.model, retries=self.max_retries - attempt)
            except anthropic.APIConnectionError as exc:
                last_error = BackendUnreachableError(str(exc), model=self.model, retries=self.max_retries - attempt)
            except anthropic.APIStatusError as exc:
                if exc.status_code == 429:
                    last_error = RateLimitError(str(exc), model=self.model, retries=self.max_retries - attempt)
                elif exc.status_code >= 500:
                    last_error = BackendUnreachableError(str(exc), model=self.model, retries=self.max_retries - attempt)
                else:
                    # Non-retryable 4xx (auth/validation) -- surface as a
                    # BackendError, never retry, never fabricate.
                    raise BackendError(
                        f"Claude API error (HTTP {exc.status_code}): {exc}",
                        model=self.model,
                    ) from exc
            if attempt < self.max_retries:
                time.sleep(self.retry_backoff_base_seconds * (2 ** attempt))
        raise BackendUnavailableError(
            f"Claude backend unavailable after {self.max_retries + 1} attempts",
            provider="claude",
            model=self.model,
            attempts=self.max_retries + 1,
            last_error=str(last_error),
        )

    @staticmethod
    def _raw_dict(response: Any) -> dict:
        dump = getattr(response, "model_dump", None)
        if callable(dump):
            return dump()
        return {"content": getattr(response, "content", None)}

    def probe(self) -> ProbeResult:
        # Claude liveness is only determinable by a real call (handled by the
        # retry policy); the probe documents configuration, not connectivity.
        return ProbeResult(available=True, models=[self.model], error=None)


# --------------------------------------------------------------------------
# Ollama backend (opportunistic secondary, LAN)
# --------------------------------------------------------------------------


class OllamaBackend(LLMBackend):
    """Ollama via plain HTTP against the OpenAI-compatible /v1 endpoint."""

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        model: str = DEFAULT_OLLAMA_MODEL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_base_seconds: float = DEFAULT_RETRY_BACKOFF_BASE_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_retries = max_retries
        self.retry_backoff_base_seconds = retry_backoff_base_seconds
        self.timeout_seconds = timeout_seconds

    def complete(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 256,
        temperature: float = 0.7,
        timeout: Optional[float] = None,
    ) -> LLMResponse:
        request_timeout = timeout if timeout is not None else self.timeout_seconds
        url = f"{self.base_url}/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        last_error: Optional[BackendError] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = requests.post(url, json=payload, timeout=request_timeout)
                if resp.status_code == 429:
                    raise RateLimitError(
                        f"Ollama returned HTTP 429", model=self.model, retries=self.max_retries - attempt
                    )
                if resp.status_code >= 500:
                    raise BackendUnreachableError(
                        f"Ollama returned HTTP {resp.status_code}", model=self.model,
                        retries=self.max_retries - attempt,
                    )
                resp.raise_for_status()
                data = resp.json()
                text = data["choices"][0]["message"]["content"]
                return LLMResponse(text=text, model=self.model, raw=data)
            except RateLimitError as exc:
                last_error = exc
            except BackendUnreachableError as exc:
                last_error = exc
            except requests.exceptions.Timeout as exc:
                last_error = BackendTimeoutError(str(exc), model=self.model, retries=self.max_retries - attempt)
            except requests.exceptions.ConnectionError as exc:
                last_error = BackendUnreachableError(str(exc), model=self.model, retries=self.max_retries - attempt)
            except requests.exceptions.HTTPError as exc:
                # Non-retryable 4xx (validation/auth) -- surface, don't fabricate.
                raise BackendError(str(exc), model=self.model) from exc
            if attempt < self.max_retries:
                time.sleep(self.retry_backoff_base_seconds * (2 ** attempt))
        raise BackendUnavailableError(
            f"Ollama backend unavailable after {self.max_retries + 1} attempts",
            provider="ollama",
            model=self.model,
            attempts=self.max_retries + 1,
            last_error=str(last_error),
        )

    def probe(self) -> ProbeResult:
        """GET /api/tags with a short timeout; returns a result, never raises."""
        try:
            resp = requests.get(
                f"{self.base_url}/api/tags",
                timeout=min(self.timeout_seconds, DEFAULT_PROBE_TIMEOUT_SECONDS),
            )
            resp.raise_for_status()
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            return ProbeResult(available=True, models=models, error=None)
        except Exception as exc:  # noqa: BLE001 - probe must never raise
            return ProbeResult(available=False, models=[], error=str(exc))


# --------------------------------------------------------------------------
# DeepSeek backend (primary, default in revision 2)
# --------------------------------------------------------------------------


class DeepSeekBackend(LLMBackend):
    """DeepSeek v4-flash via plain HTTP against the OpenAI-compatible /v1 endpoint.

    Primary, default real-API backend (revision 2, SWA-176). DeepSeek is a reasoning
    model: responses carry both 'content' (the answer) and 'reasoning_content' (CoT).
    Only content is returned as the response; reasoning is never returned as dialogue.
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        base_url: str = DEFAULT_DEEPSEEK_BASE_URL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_base_seconds: float = DEFAULT_RETRY_BACKOFF_BASE_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.retry_backoff_base_seconds = retry_backoff_base_seconds
        self.timeout_seconds = timeout_seconds

    def complete(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 256,
        temperature: float = 0.7,
        timeout: Optional[float] = None,
    ) -> LLMResponse:
        request_timeout = timeout if timeout is not None else self.timeout_seconds
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        last_error: Optional[BackendError] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=request_timeout)
                if resp.status_code == 429:
                    raise RateLimitError(
                        f"DeepSeek returned HTTP 429", model=self.model, retries=self.max_retries - attempt
                    )
                if resp.status_code >= 500:
                    raise BackendUnreachableError(
                        f"DeepSeek returned HTTP {resp.status_code}", model=self.model,
                        retries=self.max_retries - attempt,
                    )
                resp.raise_for_status()
                data = resp.json()
                try:
                    text = data["choices"][0]["message"].get("content")
                except (KeyError, IndexError, TypeError) as exc:
                    raise BackendError(
                        "DeepSeek returned a malformed response (missing "
                        "choices[0].message.content); not a valid exchange",
                        model=self.model,
                    ) from exc
                # Never return empty content (reasoning model edge case):
                # DeepSeek can exhaust max_tokens on chain-of-thought before
                # producing an answer, yielding content: "" with
                # finish_reason: "length". Empty/missing content is a failure,
                # not a response -- never return "" and never substitute
                # reasoning_content.
                if not text:
                    raise BackendError(
                        "DeepSeek returned empty content (max_tokens budget "
                        "likely consumed by reasoning before an answer was "
                        "produced); not a valid response and it will not be "
                        "recorded as an exchange",
                        model=self.model,
                    )
                return LLMResponse(text=text, model=self.model, raw=data)
            except RateLimitError as exc:
                last_error = exc
            except BackendUnreachableError as exc:
                last_error = exc
            except requests.exceptions.Timeout as exc:
                last_error = BackendTimeoutError(str(exc), model=self.model, retries=self.max_retries - attempt)
            except requests.exceptions.ConnectionError as exc:
                last_error = BackendUnreachableError(str(exc), model=self.model, retries=self.max_retries - attempt)
            except requests.exceptions.HTTPError as exc:
                # Non-retryable 4xx (validation/auth) -- surface, don't fabricate.
                raise BackendError(str(exc), model=self.model) from exc
            except BackendError:
                # Empty/missing content (reasoning budget consumed) is
                # non-retryable: retrying the same request will not produce an
                # answer, so surface it immediately instead of burning the
                # retry budget. (RateLimit/Unreachable/Timeout errors are
                # retryable and are handled by the clauses above.)
                raise
            if attempt < self.max_retries:
                time.sleep(self.retry_backoff_base_seconds * (2 ** attempt))
        raise BackendUnavailableError(
            f"DeepSeek backend unavailable after {self.max_retries + 1} attempts",
            provider="deepseek",
            model=self.model,
            attempts=self.max_retries + 1,
            last_error=str(last_error),
        )

    def probe(self) -> ProbeResult:
        """GET /models with a short timeout; returns a result, never raises."""
        try:
            url = f"{self.base_url}/models"
            headers = {"Authorization": f"Bearer {self.api_key}"}
            resp = requests.get(
                url,
                headers=headers,
                timeout=min(self.timeout_seconds, DEFAULT_PROBE_TIMEOUT_SECONDS),
            )
            resp.raise_for_status()
            data = resp.json()
            # DeepSeek returns models in data.data[].id format (OpenAI-compatible)
            model_ids = [m.get("id", "") for m in data.get("data", [])]
            # Only report available=True if our configured model is in the list
            if self.model in model_ids:
                return ProbeResult(available=True, models=model_ids, error=None)
            else:
                return ProbeResult(
                    available=False,
                    models=model_ids,
                    error=f"Configured model {self.model} not in available models"
                )
        except Exception as exc:  # noqa: BLE001 - probe must never raise
            return ProbeResult(available=False, models=[], error=str(exc))


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------


def create_backend(config: BackendConfig) -> LLMBackend:
    """Factory: build the backend selected by ``config["provider"]``.

    Raises ``ConfigError`` for unknown providers, missing API keys (deepseek/claude),
    or other invalid config.

    Valid providers (revision 2): "deepseek" (primary, default), "claude" (legacy),
    "ollama" (opportunistic secondary), "mock" (deterministic tests).
    """
    provider = (config or {}).get("provider")
    if provider == "mock":
        return MockBackend(
            scripted=config.get("scripted", []),
            model=config.get("model", "mock"),
        )
    if provider == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ConfigError(
                "DEEPSEEK_API_KEY not set in environment; source ~/.hermes/.env "
                "or export it before creating a DeepSeek backend"
            )
        return DeepSeekBackend(
            api_key=api_key,
            model=config.get("model", DEFAULT_DEEPSEEK_MODEL),
            base_url=config.get("base_url", DEFAULT_DEEPSEEK_BASE_URL),
            max_retries=config.get("max_retries", DEFAULT_MAX_RETRIES),
            retry_backoff_base_seconds=config.get(
                "retry_backoff_base_seconds", DEFAULT_RETRY_BACKOFF_BASE_SECONDS
            ),
            timeout_seconds=config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        )
    if provider == "claude":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ConfigError(
                "ANTHROPIC_API_KEY not set in environment; source ~/.hermes/.env "
                "or export it before creating a Claude backend"
            )
        return ClaudeBackend(
            api_key=api_key,
            model=config.get("model", DEFAULT_CLAUDE_MODEL),
            max_retries=config.get("max_retries", DEFAULT_MAX_RETRIES),
            retry_backoff_base_seconds=config.get(
                "retry_backoff_base_seconds", DEFAULT_RETRY_BACKOFF_BASE_SECONDS
            ),
            timeout_seconds=config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        )
    if provider == "ollama":
        return OllamaBackend(
            base_url=config.get("ollama_base_url", DEFAULT_OLLAMA_BASE_URL),
            model=config.get("model", DEFAULT_OLLAMA_MODEL),
            max_retries=config.get("max_retries", DEFAULT_MAX_RETRIES),
            retry_backoff_base_seconds=config.get(
                "retry_backoff_base_seconds", DEFAULT_RETRY_BACKOFF_BASE_SECONDS
            ),
            timeout_seconds=config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        )
    raise ConfigError(
        f"Invalid backend provider {provider!r}; "
        f"valid providers are 'deepseek', 'claude', 'ollama', 'mock'"
    )


__all__ = [
    "LLMBackend",
    "LLMResponse",
    "MockBackend",
    "ClaudeBackend",
    "DeepSeekBackend",
    "OllamaBackend",
    "ProbeResult",
    "BackendError",
    "BackendTimeoutError",
    "RateLimitError",
    "BackendUnreachableError",
    "BackendUnavailableError",
    "ConfigError",
    "BackendConfig",
    "create_backend",
]
