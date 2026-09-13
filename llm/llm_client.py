"""
llm_client.py
=============
Reusable LLM client.

Every agent in this project (Investigation / Conversation / Report)
performs its AI inference through THIS single class. Centralising the
LLM plumbing here gives us:

* One place to configure the provider (OpenRouter).
* Automatic JSON parsing / cleanup for structured agent output.
* Exponential-backoff retries for transient network errors.

Supported features
------------------
- OpenRouter (OpenAI-compatible API)
- JSON output mode (``json_output=True``)
- Markdown-fence stripping (`````json ... `````)
- Automatic retries (default 3 attempts)
- Provider agnostic design
"""

from __future__ import annotations

import json
import time
from typing import Any

from openai import OpenAI

from config import settings


class LLMNotConfiguredError(RuntimeError):
    """
    Raised when an LLM call is attempted without a server-side
    OpenRouter key. Kept separate from provider errors so the API can
    answer HTTP 503 ("server not configured") rather than 502
    ("upstream failed").
    """


class LLMClient:
    """
    Thin, reusable wrapper around the OpenAI SDK pointing at OpenRouter.

    Example
    -------
    >>> client = LLMClient()
    >>> data = client.generate("Return {\"ok\": true}", json_output=True)
    >>> data
    {'ok': True}
    """

    def __init__(self):
        """
        Build the underlying OpenAI client.

        By default we talk to OpenRouter's gateway
        (``https://openrouter.ai/api/v1``), never to OpenAI directly.
        ``OPENROUTER_BASE_URL`` overrides the gateway for self-hosted
        OpenAI-compatible servers or test doubles.

        Raises
        ------
        LLMNotConfiguredError
            ``OPENROUTER_API_KEY`` is not set on the server. The API
            layer maps this to HTTP 503 with a configuration message
            instead of a confusing provider-side failure.
        """

        if not settings.llm_configured:
            raise LLMNotConfiguredError(
                "OPENROUTER_API_KEY is not configured on the server. "
                "Add it to the server-side .env (see .env.example) and "
                "restart the backend to enable the AI agents."
            )

        # ``max_retries=0`` is deliberate: this method owns the
        # exponential-backoff retry policy, so stacking the SDK's
        # retries on top would multiply slow provider outages.
        self.client = OpenAI(
            api_key=settings.OPENROUTER_API_KEY,
            base_url=settings.OPENROUTER_BASE_URL,
            timeout=90.0,
            max_retries=0,
        )

        self.model = settings.LLM_MODEL

    def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        json_output: bool = False,
        retries: int = 3,
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
        retries : int, default 3
            Number of attempts before giving up on transient errors.
            Wait time backs off exponentially (2 ** attempt seconds).

        Returns
        -------
        str or dict[str, Any]
            The generated text or the parsed JSON object.

        Raises
        ------
        ValueError
            If the model returns empty content or invalid JSON.
        RuntimeError
            If every attempt failed (network / API errors).
        """

        last_error = None
        request_json_mode = json_output

        # ----------------------------------------------------------
        # Retry loop with exponential backoff
        # ----------------------------------------------------------

        for attempt in range(retries):

            try:

                # 1. Send the single-user-turn request. Most prompts
                # embed their own system instructions for provider
                # compatibility. When structured output is requested,
                # also ask OpenAI-compatible gateways that support it
                # to constrain the response to JSON.
                request_kwargs: dict[str, Any] = {
                    "model": self.model,
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

                response = self.client.chat.completions.create(
                    **request_kwargs
                )

                # 2. Read the generated text.
                text = response.choices[0].message.content

                if text is None:
                    raise ValueError("Model returned empty response.")

                # 3. Strip any ```json ... ``` fences some models add.
                text = self._clean_response(text)

                # 4. Optionally parse the text as JSON and return it.
                if not json_output:
                    return text

                return self._parse_json(text)

            # A JSONDecodeError is a *content* bug (bad model output),
            # not a network blip -> fail fast with a helpful message.
            except json.JSONDecodeError as e:

                raise ValueError(
                    f"LLM returned invalid JSON.\n\n{text}"
                ) from e

            # Any other exception (timeouts, 429s, 5xx ...) may be
            # transient -> retry with exponential backoff.
            except Exception as e:

                last_error = e

                # Some self-hosted OpenAI-compatible gateways do not
                # implement response_format. Retry immediately without it
                # once instead of failing the whole structured agent.
                error_text = str(e).lower()
                unsupported_json_mode = (
                    "response_format" in error_text
                    or "response format" in error_text
                )

                if (
                    json_output
                    and request_json_mode
                    and unsupported_json_mode
                ):
                    request_json_mode = False
                    continue

                if attempt == retries - 1:
                    break

                wait = 2 ** attempt

                print(
                    f"\nRetry {attempt + 1}/{retries} "
                    f"after {wait}s..."
                )

                time.sleep(wait)

        # All retries exhausted.
        raise RuntimeError(
            f"LLM request failed.\n\n{last_error}"
        )

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
