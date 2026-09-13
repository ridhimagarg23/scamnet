"""
prompt_loader.py
================
Utility for loading prompt templates from the ``prompts/`` folder.

Prompts are stored as plain ``.txt`` files (not Python strings) so
they can be tuned, diffed and reviewed without touching code.
Each file contains the fixed system behaviour for one agent plus a
JSON output contract:

* ``prompts/investigation_prompt.txt``  -> InvestigationAgent
* ``prompts/conversation_prompt.txt``   -> ConversationAgent
* ``prompts/report_prompt.txt``         -> ReportAgent

The prompt file is prepended to the dynamic, per-request context
(built inside each agent's ``run()``) before calling the LLM.
"""

from pathlib import Path


class PromptLoader:
    """
    Loads prompt templates from the prompts folder.
    """

    PROMPTS_DIR = Path("prompts")

    @classmethod
    def load(cls, filename: str) -> str:
        """
        Read and return the raw prompt template text.

        Parameters
        ----------
        filename : str
            Prompt filename, e.g. ``"investigation_prompt.txt"``.

        Returns
        -------
        str
            The trimmed prompt template text.

        Raises
        ------
        FileNotFoundError
            If the requested prompt file does not exist.

        Example
        -------
        >>> PromptLoader.load("investigation_prompt.txt")[:26]
        'You are TraceAI, an expert'
        """

        path = cls.PROMPTS_DIR / filename

        if not path.exists():
            raise FileNotFoundError(
                f"Prompt not found: {filename}"
            )

        return path.read_text(
            encoding="utf-8"
        ).strip()
