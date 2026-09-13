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

        Note: we only talk to OpenRouter's gateway
        (``https://openrouter.ai/api/v1``), never to OpenAI directly.
        """

        self.client = OpenAI(
            api_key=settings.OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
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

        # ----------------------------------------------------------
        # Retry loop with exponential backoff
        # ----------------------------------------------------------

        for attempt in range(retries):

            try:

                # 1. Send the single-user-turn request.
                response = self.client.chat.completions.create(
                    model=self.model,
                    temperature=temperature,
                    messages=[
                        {
                            "role": "user",
                            "content": prompt,
                        }
                    ],
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

                return json.loads(text)

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
