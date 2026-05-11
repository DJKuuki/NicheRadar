"""Shared LLM file-exchange helpers.

The bot uses a "write prompt → human/LLM processes → read response from file"
pattern in two places:

* ``debate_orchestrator``      — multi-round debate via ``ai_inbox/`` + ``ai_outbox/``
* ``evidence_source_finder``   — single-shot via ``source_inbox/`` + ``source_outbox/``

The two flows have small differences (filename conventions, blocking vs.
non-blocking waits, optional JSON sidecar) but share the bulk of the I/O
plumbing, markdown stripping, and terminal-paste loop. This module centralises
that plumbing; the callers keep their own prompt builders and parsers.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "strip_markdown_code_blocks",
    "read_pasted_response",
    "LlmFileExchange",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

_FENCE_BLOCK_RE = re.compile(r"```[\w-]*\n(.*?)\n```", flags=re.DOTALL)
_FENCE_INLINE_RE = re.compile(r"```[\w-]*\s*(.*?)\s*```", flags=re.DOTALL)


def strip_markdown_code_blocks(text: str) -> str:
    """Remove ``` ... ``` fences (with optional language tag) from a string.

    Tries the block form first (``` ... \\n ... \\n ```), then the inline
    form. Whitespace is stripped from the result. Used to normalise LLM
    responses that wrap their JSON / structured output in a code fence.
    """
    stripped = _FENCE_BLOCK_RE.sub(r"\1", text)
    stripped = _FENCE_INLINE_RE.sub(r"\1", stripped)
    return stripped.strip()


# ---------------------------------------------------------------------------
# Terminal interaction
# ---------------------------------------------------------------------------

def read_pasted_response(
    *,
    end_marker: str = "END_OF_AI_RESPONSE",
    on_eof: str = "ignore",
) -> str:
    """Read a multi-line response from stdin.

    Reads lines until either:

    * the user types ``end_marker`` on its own line (case-sensitive after strip), or
    * stdin reports EOF (Ctrl+D on Unix, Ctrl+Z+Enter on Windows).

    Whitespace around the marker is ignored. Returns the joined lines with the
    marker line excluded. ``on_eof`` controls what happens on EOF: ``"ignore"``
    (default) returns what has been read; any other value re-raises ``EOFError``.
    """
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            if on_eof == "ignore":
                break
            raise
        if line.strip() == end_marker:
            break
        lines.append(line)
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# File exchange
# ---------------------------------------------------------------------------

class LlmFileExchange:
    """Two-directory file exchange for human-in-the-loop LLM calls.

    Layout::

        <inbox_dir>/<request_id><prompt_suffix>      # written by the bot
        <inbox_dir>/<request_id><sidecar_suffix>     # optional metadata sidecar
        <outbox_dir>/<request_id><result_suffix>     # written by the human/LLM

    When ``session_id`` is provided, both directories are nested under it so
    concurrent runs do not collide.

    Two ways to retrieve the response:

    * :meth:`read_result` — non-blocking; returns ``None`` if the result file
      is not yet present. Suitable for batch flows that re-run the CLI after
      the human drops the file in.
    * :meth:`wait_for_result` — interactive blocking loop that re-checks each
      time the user presses Enter; honours a ``SKIP`` word so the operator can
      bail out for one request without killing the run.
    """

    def __init__(
        self,
        inbox_dir: str | Path,
        outbox_dir: str | Path,
        *,
        prompt_suffix: str = ".txt",
        result_suffix: str = "_result.txt",
        sidecar_suffix: str = ".json",
        session_id: str | None = None,
    ) -> None:
        inbox = Path(inbox_dir)
        outbox = Path(outbox_dir)
        if session_id:
            inbox = inbox / session_id
            outbox = outbox / session_id
        self.inbox_dir = inbox
        self.outbox_dir = outbox
        self.prompt_suffix = prompt_suffix
        self.result_suffix = result_suffix
        self.sidecar_suffix = sidecar_suffix
        self.session_id = session_id

    # ----- paths ----------------------------------------------------------

    def inbox_path(self, request_id: str) -> Path:
        return self.inbox_dir / f"{request_id}{self.prompt_suffix}"

    def sidecar_path(self, request_id: str) -> Path:
        return self.inbox_dir / f"{request_id}{self.sidecar_suffix}"

    def outbox_path(self, request_id: str) -> Path:
        return self.outbox_dir / f"{request_id}{self.result_suffix}"

    # ----- writers --------------------------------------------------------

    def write_prompt(
        self,
        request_id: str,
        prompt_text: str,
        *,
        sidecar: dict[str, Any] | None = None,
    ) -> Path:
        """Write the prompt (and optional JSON sidecar) into the inbox.

        Creates the inbox directory if missing. The optional ``sidecar`` is
        serialised as JSON next to the prompt so downstream tooling can read
        structured context without having to parse the prompt body.
        Returns the path of the prompt file.
        """
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = self.inbox_path(request_id)
        prompt_path.write_text(prompt_text, encoding="utf-8")
        if sidecar is not None:
            sidecar_path = self.sidecar_path(request_id)
            sidecar_path.write_text(
                json.dumps(sidecar, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        logger.info(
            "llm_exchange_prompt_written request_id=%s path=%s",
            request_id,
            prompt_path,
        )
        return prompt_path

    # ----- readers --------------------------------------------------------

    def read_result(self, request_id: str) -> str | None:
        """Return the stripped contents of the outbox result file, or ``None``.

        Non-blocking. Used by callers that want to surface "result not yet
        available" as a recoverable state.
        """
        path = self.outbox_path(request_id)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").strip()

    def wait_for_result(
        self,
        request_id: str,
        *,
        on_missing: Callable[[Path, Path], None] | None = None,
        skip_word: str = "SKIP",
    ) -> str | None:
        """Block on stdin until the outbox result appears or the user skips.

        Each retry prompts the operator with ``> `` and rechecks the file.
        Entering ``skip_word`` (case-insensitive) abandons this request and
        returns ``None`` so the caller can move on. ``on_missing`` is invoked
        once with ``(inbox_path, outbox_path)`` so the caller can print a
        context-appropriate "waiting for ..." banner before the loop starts.
        """
        inbox_path = self.inbox_path(request_id)
        outbox_path = self.outbox_path(request_id)
        result = self.read_result(request_id)
        if result is not None:
            return result
        if on_missing is not None:
            on_missing(inbox_path, outbox_path)
        while True:
            try:
                cmd = input("> ").strip()
            except EOFError:
                return None
            if cmd.upper() == skip_word:
                return None
            result = self.read_result(request_id)
            if result is not None:
                return result
            print("Result file still missing — paste it into the outbox and press Enter to retry.")
