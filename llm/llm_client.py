"""
llm_client.py
=============
Reusable multi-provider LLM client.

Every agent in this project (Investigation / Conversation / Report)
performs its AI inference through THIS single class. Centralising the
LLM plumbing here gives us:

* One place to configure the providers (OpenRouter + NVIDIA NIM).
* Automatic JSON parsing / cleanup for structured agent output.
* A resilient fallback chain: primary model -> provider spares ->
  the OTHER provider's models - so one slow/dead/rate-limited model
  never fails the whole investigation.

Supported features
------------------
- OpenRouter (OpenAI-compatible API)
- NVIDIA NIM (OpenAI-compatible API, https://build.nvidia.com)
- Per-request provider/model override + runtime-active selection
- JSON output mode (``json_output=True``)
- Markdown-fence stripping (`````json ... `````)
- Automatic retries with exponential backoff (per model)
- Automatic model + cross-provider fallback (no code changes needed
  when a model is retired - the next spare answers instead)

Example
-------
>>> client = LLMClient()  # uses the runtime-active provider/model
>>> data = client.generate("Return {\\"ok\\": true}", json_output=True)
>>> data
{'ok': True}
>>> fast = LLMClient(provider="nvidia")  # lightning-fast replies
>>> fast.generate("Say hi!")
'...'
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from openai import OpenAI

from config import (
    PROVIDER_NVIDIA,
    PROVIDER_OPENROUTER,
    settings,
)

logger = logging.getLogger("TraceAI-LLM")


class LLMNotConfiguredError(RuntimeError):
    """
    Raised when an LLM call is attempted without a usable server-side
    provider key. Kept separate from provider errors so the API can
    answer HTTP 503 ("server not configured") rather than 502
    ("upstream failed").
    """


def _looks_like_unknown_model(error: Exception) -> bool:
    """
    True when the provider says "this model id does not exist".

    Retrying the SAME id would be pointless - the fallback chain must
    move on to the next spare immediately.
    """

    text = str(error).lower()

    markers = (
        "model_not_found",
        "model not found",
        "unknown model",
        "invalid model",
        "model does not exist",
        "does not exist",
        "not supported",
        "unsupported model",
        "no such model",
        "404",
    )

    return any(marker in text for marker in markers)


def _looks_like_auth_error(error: Exception) -> bool:
    """
    True when the provider rejected the credentials (dead key).

    Every model of THAT provider would fail the same way, so the chain
    skips the rest of the provider and (when enabled) hops to the other
    provider instead of hammering a dead key.
    """

    status = getattr(error, "status_code", None)

    if status in (401, 403):
        return True

    text = str(error).lower()

    return (
        "unauthorized" in text
        or "invalid api key" in text
        or "invalid_api_key" in text
        or "authentication" in text
        or "forbidden" in text
    )


def _looks_like_json_mode_unsupported(error: Exception) -> bool:
    """True when the gateway rejected the ``response_format`` field."""

    text = str(error).lower()

    return "response_format" in text or "response format" in text


class LLMClient:
    """
    Thin, reusable wrapper around the OpenAI SDK pointing at the
    configured providers (OpenRouter and/or NVIDIA NIM).

    Parameters
    ----------
    provider : str | None
        ``"openrouter"`` or ``"nvidia"`` (aliases like ``"nim"`` are
        accepted). ``None`` uses the runtime-active provider, which the
        dashboard can switch via ``POST /api/llm/select``.
    model : str | None
        Explicit model id. ``None`` uses the active model (or the
        provider's default when the provider was switched).
    timeout : float | None
        Per-request timeout override in seconds. ``None`` uses the
        provider's configured timeout (30 s for NVIDIA NIM, 90 s for
        OpenRouter by default).

    Example
    -------
    >>> client = LLMClient()
    >>> data = client.generate("Return {\\"ok\\": true}", json_output=True)
    >>> data
    {'ok': True}
    """

    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ):
        """
        Build the underlying provider client(s).

        Raises
        ------
        LLMNotConfiguredError
            No usable provider key is configured on the server. The API
            layer maps this to HTTP 503 with a configuration message
            instead of a confusing provider-side failure.
        """

        if not settings.llm_configured:
            raise LLMNotConfiguredError(
                "Neither OPENROUTER_API_KEY nor NVIDIA_NIM_API_KEY is "
                "configured on the server. Add at least one of them to "
                "the server-side .env (see .env.example) and restart "
                "the backend to enable the AI agents."
            )

        resolved_provider, resolved_model = settings.resolve_provider_model(
            provider, model
        )

        # The requested provider may have no key while the OTHER one
        # does (e.g. the dashboard asked for NVIDIA before its key was
        # added). Rather than failing, hop to the configured provider
        # automatically - the fallback philosophy is "never fail when a
        # working provider exists".
        if not settings.is_provider_configured(resolved_provider):
            other = (
                PROVIDER_NVIDIA
                if resolved_provider == PROVIDER_OPENROUTER
                else PROVIDER_OPENROUTER
            )

            if settings.is_provider_configured(other):
                logger.warning(
                    "Provider '%s' has no API key - automatically using "
                    "'%s' instead. Add %s to the server .env to enable "
                    "'%s'.",
                    resolved_provider,
                    other,
                    settings.provider_key_name(resolved_provider),
                    resolved_provider,
                )
                resolved_provider = other
                resolved_model = (
                    (model or "").strip()
                    or settings.get_default_model(other)
                )
            else:
                raise LLMNotConfiguredError(
                    f"{settings.provider_key_name(resolved_provider)} is "
                    "not configured on the server. Add it to the "
                    "server-side .env (see .env.example) and restart the "
                    "backend to enable the AI agents."
                )

        self.provider = resolved_provider
        self.model = resolved_model
        self.timeout_override = timeout

        # Lazily-built OpenAI SDK clients, one per provider.
        self._clients: dict = {}

        # Backwards-compatible handle to the PRIMARY provider client
        # (kept so older code/tests touching ``client.client`` work).
        self.client = self._client_for(self.provider)

        # Observability of the LAST generate() call: which provider/model
        # actually answered, and which spares were tried before it.
        self.last_provider: str | None = None
        self.last_model: str | None = None
        self.fallbacks_used: list = []
        self.last_error: Exception | None = None

    # ----------------------------------------------------------
    # Provider plumbing
    # ----------------------------------------------------------

    def _client_for(self, provider: str) -> OpenAI:
        """Return (building + caching) the SDK client for a provider."""

        if provider not in self._clients:
            api_key = settings.get_api_key(provider)

            if not api_key:
                raise LLMNotConfiguredError(
                    f"{settings.provider_key_name(provider)} is not "
                    "configured on the server."
                )

            # ``max_retries=0`` is deliberate: generate() owns the
            # retry + fallback policy, so stacking the SDK's retries on
            # top would multiply slow provider outages.
            self._clients[provider] = OpenAI(
                api_key=api_key,
                base_url=settings.get_base_url(provider),
                timeout=(
                    self.timeout_override
                    if self.timeout_override
                    else settings.get_timeout(provider)
                ),
                max_retries=0,
            )

        return self._clients[provider]

    def attempt_chain(self) -> list:
        """
        Return the ordered ``[(provider, model), ...]`` fallback chain.

        1. The requested (primary) model.
        2. That provider's configured spare models (fastest first).
        3. When cross-provider fallback is enabled and the other
           provider's key exists: the other provider's default model +
           its spares.
        """

        chain: list = [(self.provider, self.model)]

        for spare in settings.get_fallback_models(self.provider):
            candidate = (self.provider, spare)

            if candidate not in chain:
                chain.append(candidate)

        if settings.LLM_CROSS_PROVIDER_FALLBACK:
            other = (
                PROVIDER_NVIDIA
                if self.provider == PROVIDER_OPENROUTER
                else PROVIDER_OPENROUTER
            )

            if settings.is_provider_configured(other):
                candidate = (other, settings.get_default_model(other))

                if candidate not in chain:
                    chain.append(candidate)

                for spare in settings.get_fallback_models(other):
                    candidate = (other, spare)

                    if candidate not in chain:
                        chain.append(candidate)

        return chain

    # ----------------------------------------------------------
    # Generation with retries + fallback
    # ----------------------------------------------------------

    def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        json_output: bool = False,
        retries: int = 2,
        max_tokens: int | None = None,
    ) -> str | dict[str, Any]:
        """
        Run one chat-completion call and return the model output.

        Parameters
        ----------
        prompt : str
            The full user prompt (agents embed their system rules in it).
        temperature : float, default 0.2
            Low temperature keeps structured agent output deterministic.
        json_output : bool, default False
            When True the response is parsed with ``json.loads`` and a
            dict is returned; otherwise raw text is returned.
        retries : int, default 2
            Attempts PER MODEL before moving to the next fallback model.
            Wait time backs off exponentially (1 s, 2 s, ...) capped at
            5 s so fail-over stays quick.
        max_tokens : int | None, default None
            Hard cap on the generated completion. Every agent passes a
            budget matched to its output shape (small JSON verdicts need
            far fewer tokens than the markdown report) so a slow or
            verbose model cannot stall ``POST /analyze`` - or the
            per-token bill - without bound. Omit (None) for uncapped.

        Returns
        -------
        str or dict[str, Any]
            The generated text or the parsed JSON object.

        Raises
        ------
        ValueError
            Every fallback model returned empty content or invalid JSON.
        RuntimeError
            Every fallback model failed (network / API errors). The
            message lists each tried provider/model and its error.
        """

        chain = self.attempt_chain()
        attempts_per_model = max(1, int(retries))

        # Reset observability for this call.
        self.last_provider = None
        self.last_model = None
        self.fallbacks_used = []
        self.last_error = None

        # Providers whose key was rejected - skip their remaining models.
        dead_providers: set = set()

        for chain_index, (provider, model) in enumerate(chain):
            is_last_model = chain_index == len(chain) - 1

            if provider in dead_providers:
                continue

            if not settings.is_provider_configured(provider):
                continue

            request_json_mode = json_output

            for attempt in range(attempts_per_model):
                try:
                    request_kwargs: dict[str, Any] = {
                        "model": model,
                        "temperature": temperature,
                        "messages": [
                            {
                                "role": "user",
                                "content": prompt,
                            }
                        ],
                    }

                    if request_json_mode:
                        request_kwargs["response_format"] = {
                            "type": "json_object"
                        }

                    if max_tokens is not None:
                        request_kwargs["max_tokens"] = max_tokens

                    response = self._client_for(provider).chat.completions.create(
                        **request_kwargs
                    )

                    text = response.choices[0].message.content

                    if text is None or not str(text).strip():
                        raise ValueError("Model returned empty response.")

                    text = self._clean_response(str(text))

                    if not json_output:
                        self.last_provider = provider
                        self.last_model = model
                        return text

                    try:
                        parsed = self._parse_json(text)
                    except (json.JSONDecodeError, ValueError) as json_error:
                        # A JSON failure is a *content* bug (bad model
                        # output), not a network blip: retrying the same
                        # model rarely helps, so record it and move to
                        # the next fallback model instead.
                        self._record_fallback(
                            provider, model, json_error
                        )

                        if is_last_model:
                            raise ValueError(
                                "LLM returned invalid JSON on every "
                                f"fallback model.\n\n{text}"
                            ) from json_error

                        logger.warning(
                            "Model '%s' (%s) returned invalid JSON - "
                            "trying the next fallback model.",
                            model,
                            provider,
                        )
                        break

                    self.last_provider = provider
                    self.last_model = model
                    return parsed

                except LLMNotConfiguredError as e:
                    # Key disappeared mid-flight (operator edited .env):
                    # skip this provider's remaining models.
                    dead_providers.add(provider)
                    self._record_fallback(provider, model, e)
                    break

                except ValueError as e:
                    # Empty-content ValueError raised above (JSON errors
                    # are handled in the inner try). Fail over to the
                    # next model; re-raise only on the last one.
                    if "empty response" not in str(e).lower() or is_last_model:
                        if is_last_model and not json_output:
                            raise
                        if "empty response" in str(e).lower() and not is_last_model:
                            self._record_fallback(provider, model, e)
                            logger.warning(
                                "Model '%s' (%s) returned an empty "
                                "response - trying the next fallback.",
                                model,
                                provider,
                            )
                            break
                        raise

                except Exception as e:  # noqa: BLE001 - provider errors
                    self.last_error = e

                    # Unknown model id: NEVER retry the same id - the
                    # provider will reject it identically every time.
                    if _looks_like_unknown_model(e):
                        self._record_fallback(provider, model, e)

                        if is_last_model:
                            break

                        logger.warning(
                            "Model '%s' (%s) was rejected by the provider "
                            "(%s) - trying the next fallback model.",
                            model,
                            provider,
                            e,
                        )
                        break

                    # Dead credentials: skip the whole provider.
                    if _looks_like_auth_error(e):
                        dead_providers.add(provider)
                        self._record_fallback(provider, model, e)
                        logger.warning(
                            "Provider '%s' rejected its API key - skipping "
                            "its remaining models.",
                            provider,
                        )
                        break

                    # Some OpenAI-compatible gateways do not implement
                    # response_format: retry immediately without it once
                    # instead of failing the whole structured agent.
                    if (
                        json_output
                        and request_json_mode
                        and _looks_like_json_mode_unsupported(e)
                    ):
                        request_json_mode = False
                        continue

                    # Last attempt for THIS model -> record and move on.
                    if attempt == attempts_per_model - 1:
                        self._record_fallback(provider, model, e)

                        if not is_last_model:
                            logger.warning(
                                "Model '%s' (%s) failed (%s) - trying the "
                                "next fallback model.",
                                model,
                                provider,
                                e,
                            )
                        break

                    wait = min(2 ** attempt, 5)

                    print(
                        f"\nRetry {attempt + 1}/{attempts_per_model} for "
                        f"model '{model}' ({provider}) after {wait}s..."
                    )

                    time.sleep(wait)

        # All fallback models exhausted.
        tried = "; ".join(
            f"{entry['provider']}/{entry['model']}: {entry['error']}"
            for entry in self.fallbacks_used
        ) or "no model could be attempted"

        raise RuntimeError(
            "LLM request failed on every fallback model. "
            f"Tried [{tried}]. "
            "Check the provider API keys, the model ids "
            "(GET /api/llm/models), provider status and server network "
            "access."
        )

    def _record_fallback(
        self,
        provider: str,
        model: str,
        error: BaseException,
    ) -> None:
        """Append one failed attempt to ``fallbacks_used``."""

        self.last_error = (
            error if isinstance(error, Exception) else self.last_error
        )
        self.fallbacks_used.append({
            "provider": provider,
            "model": model,
            "error": str(error)[:300],
        })

    @classmethod
    def _parse_json(cls, text: str) -> dict[str, Any]:
        """
        Parse a model response that is supposed to be a JSON object.

        Models are explicitly told to return JSON, but chat models
        occasionally add a short note or code fence despite that
        instruction. The parser first tries the complete cleaned text,
        then the outermost ``{...}`` span, while still rejecting a
        response that does not contain one valid JSON object.
        """

        cleaned = cls._clean_response(text)

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")

            if start == -1 or end <= start:
                raise

            parsed = json.loads(cleaned[start:end + 1])

        if not isinstance(parsed, dict):
            raise ValueError("LLM returned JSON that is not an object.")

        return parsed

    @staticmethod
    def _clean_response(text: str) -> str:
        """
        Remove markdown code-fence wrappers if the model returns::

            ```json
            {...}
            ```

        without touching the JSON payload itself.
        """

        text = text.strip()

        # Strip opening fence ("```json" = 7 chars, plain "```" = 3).
        if text.startswith("```json"):
            text = text[7:]

        if text.startswith("```"):
            text = text[3:]

        # Strip closing fence.
        if text.endswith("```"):
            text = text[:-3]

        return text.strip()
