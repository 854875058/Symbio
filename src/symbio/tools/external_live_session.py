"""Live two-way bridge for external coding-agent conversations.

Symbio already does two half-measures for Codex / Claude Code:

* one-shot invocation (``external_agents.ExternalAgentController``), and
* read-only snapshot import of past transcripts (``external_transcripts``).

This module joins both halves into a *live, bidirectional* link to a single
ongoing conversation, without trying to inject keystrokes into a running TUI
(which is brittle and non-portable). The mechanism both CLIs already give us:

* **Inbound sync** — every turn is persisted to a JSONL transcript on disk. We
  ``tail`` that file from a byte cursor, so new turns appear in Symbio in near
  real time, *including turns the user typed in their own terminal* against the
  same session id.
* **Outbound sync** — both CLIs continue a conversation with ``--resume <id>``.
  Resuming appends to the *same* transcript, so a prompt sent from Symbio lands
  in the same thread the user is watching.

A :class:`LiveSession` ties ``external_session_id`` + transcript path +
workspace together so the one conversation is both watched and writable.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from symbio.tools.external_transcripts import (
    ExternalTranscriptMessage,
    default_external_transcript_roots,
    parse_transcript_lines,
)
from symbio.utils.logger import get_logger

logger = get_logger("tools.external_live_session")


class LiveSession(BaseModel):
    """A live, bidirectional handle onto one external-agent conversation."""

    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    provider: str
    external_session_id: str
    transcript_path: str
    workspace: str = "."
    label: str = ""
    byte_offset: int = 0
    controller_session_id: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    last_poll_at: datetime | None = None
    last_send_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LiveSendResult(BaseModel):
    """Outcome of injecting a prompt into a live external-agent session."""

    session_id: str
    provider: str
    success: bool = False
    dry_run: bool = False
    run_id: str = ""
    exit_code: int = -1
    command: list[str] = Field(default_factory=list)
    stdout: str = ""
    error: str = ""
    new_messages: list[ExternalTranscriptMessage] = Field(default_factory=list)


def _read_new_lines(path: Path, offset: int) -> tuple[list[str], int]:
    """Read only the complete lines appended after ``offset``.

    Returns the new lines and the advanced byte offset. A trailing partial line
    (no newline yet) is left unconsumed so we never parse a half-written record.
    If the file shrank (rotated/truncated) the cursor resets to the start.
    """
    if not path.exists():
        return [], offset
    size = path.stat().st_size
    if size < offset:  # file was rotated or truncated
        offset = 0
    if size <= offset:
        return [], offset
    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read()
    last_newline = chunk.rfind(b"\n")
    if last_newline == -1:
        return [], offset  # only a partial line so far; wait for it to finish
    complete = chunk[: last_newline + 1]
    new_offset = offset + len(complete)
    text = complete.decode("utf-8", errors="replace")
    return text.splitlines(), new_offset


def resolve_transcript_path(
    provider: str,
    external_session_id: str,
    *,
    roots: dict[str, Path] | None = None,
) -> Path | None:
    """Locate the JSONL transcript file for a given external session id."""
    roots = roots or default_external_transcript_roots()
    normalized = "claude-code" if provider in {"claude", "claude-code", "cc"} else "codex"
    if normalized == "claude-code":
        base = roots.get("claude-code", Path()) / "projects"
        patterns = [f"{external_session_id}.jsonl", f"*{external_session_id}*.jsonl"]
    else:
        base = roots.get("codex", Path()) / "sessions"
        patterns = [f"*{external_session_id}*.jsonl", f"{external_session_id}.jsonl"]
    if not base.exists():
        return None
    for pattern in patterns:
        matches = sorted(base.rglob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if matches:
            return matches[0]
    return None


class ExternalLiveSessionManager:
    """Attach to, tail, and write back into external-agent conversations."""

    def __init__(
        self,
        *,
        controller: Any = None,
        state_path: str | Path = Path("data") / "external_live_sessions.json",
        workspace_root: str | Path = ".",
    ) -> None:
        self._controller = controller
        self.state_path = Path(state_path)
        self.workspace_root = Path(workspace_root).resolve()
        self.sessions: dict[str, LiveSession] = {}
        self._load_state()

    # -- discovery / controller -------------------------------------------
    def _get_controller(self) -> Any:
        if self._controller is None:
            from symbio.tools.external_agents import ExternalAgentController

            self._controller = ExternalAgentController(workspace_root=self.workspace_root)
        return self._controller

    # -- attach ------------------------------------------------------------
    def attach(
        self,
        *,
        provider: str,
        transcript_path: str | Path | None = None,
        external_session_id: str = "",
        workspace: str = ".",
        label: str = "",
        from_start: bool = True,
    ) -> LiveSession:
        """Start tracking a conversation for two-way sync.

        Provide either an explicit ``transcript_path`` or an
        ``external_session_id`` to resolve from the default CLI roots. When
        ``from_start`` is False the cursor jumps to end-of-file so the first
        poll only returns turns produced after attaching.
        """
        from symbio.tools.external_agents import ExternalAgentSessionCreate

        normalized = ExternalAgentSessionCreate(provider=provider).provider

        path: Path | None
        if transcript_path:
            path = Path(transcript_path).resolve()
        else:
            if not external_session_id:
                raise ValueError("Either transcript_path or external_session_id is required")
            path = resolve_transcript_path(normalized, external_session_id)
        if path is None or not path.exists():
            raise FileNotFoundError(
                f"Transcript not found for {normalized} session {external_session_id or transcript_path}"
            )

        resolved_external_id = external_session_id or path.stem

        controller_session_id = self._register_controller_session(
            normalized, resolved_external_id, workspace, label
        )

        offset = 0 if from_start else path.stat().st_size
        session = LiveSession(
            provider=normalized,
            external_session_id=resolved_external_id,
            transcript_path=str(path),
            workspace=workspace,
            label=label or f"{normalized} live",
            byte_offset=offset,
            controller_session_id=controller_session_id,
        )
        self.sessions[session.session_id] = session
        self._save_state()
        logger.info(
            "attached live session %s -> %s (%s)",
            session.session_id,
            resolved_external_id,
            normalized,
        )
        return session

    def _register_controller_session(
        self, provider: str, external_session_id: str, workspace: str, label: str
    ) -> str:
        """Register a controller session for resume; tolerate workspace bounds."""
        from symbio.tools.external_agents import ExternalAgentSessionCreate

        controller = self._get_controller()
        for ws in (workspace, "."):
            try:
                created = controller.create_session(
                    ExternalAgentSessionCreate(
                        provider=provider,
                        workspace=ws,
                        external_session_id=external_session_id,
                        label=label or f"{provider} live",
                    )
                )
                return created.session_id
            except ValueError:
                continue  # workspace outside root -> retry with default
        return ""

    # -- inbound sync ------------------------------------------------------
    def poll(self, session_id: str) -> list[ExternalTranscriptMessage]:
        """Return chat messages appended since the last poll (inbound sync)."""
        session = self._require(session_id)
        path = Path(session.transcript_path)
        lines, new_offset = _read_new_lines(path, session.byte_offset)
        messages = parse_transcript_lines(lines, provider=session.provider) if lines else []
        if new_offset != session.byte_offset:
            session.byte_offset = new_offset
            session.last_poll_at = datetime.now()
            session.updated_at = session.last_poll_at
            self._save_state()
        return messages

    # -- outbound sync -----------------------------------------------------
    async def send(
        self,
        session_id: str,
        prompt: str,
        *,
        dry_run: bool = False,
        model: str = "",
        timeout: int = 300,
    ) -> LiveSendResult:
        """Inject a prompt into the conversation via ``--resume`` (outbound sync).

        The CLI appends the new turns to the same transcript, after which we
        tail to capture them and return them alongside the run outcome.
        """
        from symbio.tools.external_agents import ExternalAgentRunRequest

        session = self._require(session_id)
        if not session.controller_session_id:
            raise RuntimeError(
                "Live session has no controller session; cannot resume. Re-attach."
            )

        controller = self._get_controller()
        run = await controller.run_session(
            session.controller_session_id,
            ExternalAgentRunRequest(
                prompt=prompt,
                approved=True,
                dry_run=dry_run,
                model=model,
                timeout=timeout,
            ),
        )
        session.last_send_at = datetime.now()
        session.updated_at = session.last_send_at
        self._save_state()

        new_messages = self.poll(session_id) if not dry_run else []
        return LiveSendResult(
            session_id=session.session_id,
            provider=session.provider,
            success=bool(getattr(run, "success", False)),
            dry_run=bool(getattr(run, "dry_run", dry_run)),
            run_id=getattr(run, "run_id", ""),
            exit_code=getattr(run, "exit_code", -1),
            command=list(getattr(run, "command", []) or []),
            stdout=getattr(run, "stdout", ""),
            error=getattr(run, "error", ""),
            new_messages=new_messages,
        )

    # -- queries -----------------------------------------------------------
    def list_sessions(self) -> list[LiveSession]:
        return sorted(self.sessions.values(), key=lambda s: s.updated_at, reverse=True)

    def get_session(self, session_id: str) -> LiveSession | None:
        return self.sessions.get(session_id)

    def detach(self, session_id: str) -> bool:
        if session_id in self.sessions:
            del self.sessions[session_id]
            self._save_state()
            return True
        return False

    def _require(self, session_id: str) -> LiveSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(f"Live session not found: {session_id}")
        return session

    # -- persistence -------------------------------------------------------
    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.sessions = {
                item["session_id"]: LiveSession(**item)
                for item in data.get("sessions", [])
            }
        except Exception:
            self.sessions = {}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessions": [session.model_dump(mode="json") for session in self.sessions.values()],
        }
        self.state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
