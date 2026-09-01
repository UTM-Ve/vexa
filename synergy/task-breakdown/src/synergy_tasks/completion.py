"""completion.py — the model client: one small, provider-agnostic HTTP call.

Deliberately NOT an import of core's ``llm`` package: this stage must be able to run beside any
core revision, so it speaks the two wire dialects itself (OpenAI-compatible ``/chat/completions``
and the Anthropic Messages API) over raw httpx. It reads the SAME environment a Vexa deployment
already sets, so an operator configures nothing twice:

``VEXA_LLM_PROVIDER`` (``openai-compat`` default · ``anthropic``) · ``VEXA_LLM_BASE_URL`` ·
``VEXA_LLM_API_KEY`` (falling back to ``ANTHROPIC_AUTH_TOKEN`` → ``ANTHROPIC_API_KEY``) ·
``VEXA_LLM_MODEL`` · ``VEXA_LLM_MAX_TOKENS`` (Anthropic dialect only).

No vendor name is hard-coded as a default model: a model is a free string the endpoint interprets.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Protocol

import httpx

_ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"
_ANTHROPIC_API_VERSION = "2023-06-01"


class CompletionError(RuntimeError):
    """The call failed (transport, 4xx/5xx, or an unreadable payload)."""


class AuthError(CompletionError):
    """The endpoint rejected the credential (401/403) — a distinct case: the fix is configuration."""


class ConfigError(CompletionError):
    """The client is not configured enough to make a call (no endpoint, no model)."""


@dataclass(frozen=True)
class CompletionResult:
    text: str
    model: str


class CompletionPort(Protocol):
    def complete(self, prompt: str, *, system: Optional[str] = None,
                 model: Optional[str] = None) -> CompletionResult: ...


def _key_from_env(explicit: Optional[str]) -> str:
    return (explicit or os.environ.get("VEXA_LLM_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_API_KEY") or "")


def _raise_for_status(response: httpx.Response, base: str) -> None:
    if response.status_code in (401, 403):
        raise AuthError(f"{response.status_code} from {base}: {response.text[:300]}")
    if response.status_code >= 400:
        raise CompletionError(f"{response.status_code} from {base}: {response.text[:300]}")


class OpenAICompatCompletion:
    """Any endpoint speaking ``POST {base}/chat/completions`` — OpenRouter, vLLM, Ollama, LM Studio,
    OpenAI itself, most gateways."""

    name = "openai-compat"

    def __init__(self, *, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 model: Optional[str] = None, timeout: float = 120.0,
                 transport: Optional[httpx.BaseTransport] = None) -> None:
        self._base = (base_url or os.environ.get("VEXA_LLM_BASE_URL")
                      or os.environ.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
        self._key = _key_from_env(api_key)
        self._model = model or os.environ.get("VEXA_LLM_MODEL") or ""
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def complete(self, prompt: str, *, system: Optional[str] = None,
                 model: Optional[str] = None) -> CompletionResult:
        target = (model or "").strip() or self._model
        if not self._base:
            raise ConfigError("no completion endpoint: set VEXA_LLM_BASE_URL "
                              "(e.g. https://openrouter.ai/api/v1, http://ollama:11434/v1)")
        if not target:
            raise ConfigError("no model: set VEXA_LLM_MODEL, or `model:` in the workspace's agents/tasks.md")
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        try:
            response = self._client.post(f"{self._base}/chat/completions",
                                         json={"model": target, "messages": messages}, headers=headers)
        except httpx.HTTPError as exc:
            raise CompletionError(f"transport failure against {self._base}: {exc}") from exc
        _raise_for_status(response, self._base)
        try:
            choice = (response.json().get("choices") or [{}])[0]
            text = (choice.get("message") or {}).get("content") or ""
        except (ValueError, AttributeError, IndexError, TypeError) as exc:
            raise CompletionError(f"malformed payload from {self._base}: {exc}") from exc
        return CompletionResult(text=str(text), model=target)


class AnthropicCompletion:
    """The Anthropic Messages dialect (``POST {base}/v1/messages``) — api.anthropic.com and the
    proxies that speak it."""

    name = "anthropic"

    def __init__(self, *, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 model: Optional[str] = None, timeout: float = 120.0,
                 transport: Optional[httpx.BaseTransport] = None) -> None:
        self._base = (base_url or os.environ.get("VEXA_LLM_BASE_URL")
                      or _ANTHROPIC_DEFAULT_BASE).rstrip("/")
        self._key = _key_from_env(api_key)
        self._model = model or os.environ.get("VEXA_LLM_MODEL") or ""
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def complete(self, prompt: str, *, system: Optional[str] = None,
                 model: Optional[str] = None) -> CompletionResult:
        target = (model or "").strip() or self._model
        if not target:
            raise ConfigError("no model: set VEXA_LLM_MODEL, or `model:` in the workspace's agents/tasks.md")
        payload: dict = {"model": target, "max_tokens": _max_tokens(),
                         "messages": [{"role": "user", "content": prompt}]}
        if system:
            payload["system"] = system
        headers = {"x-api-key": self._key, "anthropic-version": _ANTHROPIC_API_VERSION}
        try:
            response = self._client.post(f"{self._base}/v1/messages", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise CompletionError(f"transport failure against {self._base}: {exc}") from exc
        _raise_for_status(response, self._base)
        try:
            blocks = response.json().get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        except (ValueError, AttributeError, TypeError) as exc:
            raise CompletionError(f"malformed payload from {self._base}: {exc}") from exc
        return CompletionResult(text=text, model=target)


def _max_tokens() -> int:
    try:
        return int(os.environ.get("VEXA_LLM_MAX_TOKENS", "4096"))
    except ValueError:
        return 4096


PROVIDERS = {"openai-compat": OpenAICompatCompletion, "anthropic": AnthropicCompletion}


def completion_from_env() -> CompletionPort:
    """The adapter named by ``VEXA_LLM_PROVIDER`` (default ``openai-compat``). An unknown name fails
    LOUD with the known set — a typo must never limp on into a confusing downstream error."""
    key = (os.environ.get("VEXA_LLM_PROVIDER") or "").strip() or "openai-compat"
    cls = PROVIDERS.get(key)
    if cls is None:
        raise ConfigError(f"unknown VEXA_LLM_PROVIDER {key!r} — known providers: {sorted(PROVIDERS)}")
    return cls()
