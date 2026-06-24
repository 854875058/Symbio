"""Tests for the external-agent live two-way sync bridge."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from symbio.tools.external_live_session import (
    ExternalLiveSessionManager,
    _read_new_lines,
    resolve_transcript_path,
)
from symbio.tools.external_transcripts import parse_transcript_lines


def _claude_line(role: str, content: str) -> str:
    return json.dumps(
        {"type": role, "message": {"role": role, "content": content}},
        ensure_ascii=False,
    )


def _write_lines(path: Path, lines: list[str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


class StubController:
    """Minimal stand-in for ExternalAgentController used by the live manager."""

    def __init__(self, transcript_path: Path):
        self.transcript_path = Path(transcript_path)
        self.created: list[SimpleNamespace] = []
        self.runs: list[tuple[str, object]] = []

    def create_session(self, request):
        session = SimpleNamespace(
            session_id=f"ctrl-{len(self.created)}",
            external_session_id=request.external_session_id,
            workspace=request.workspace,
        )
        self.created.append(session)
        return session

    async def run_session(self, session_id, request):
        self.runs.append((session_id, request))
        command = ["claude", "-p", "--resume", "ext-123", request.prompt]
        if not request.dry_run:
            # Simulate the CLI appending the assistant turn to the same transcript.
            _write_lines(self.transcript_path, [_claude_line("assistant", "resumed reply")])
        return SimpleNamespace(
            success=True,
            dry_run=request.dry_run,
            run_id="run-1",
            exit_code=0,
            command=command,
            stdout="done",
            error="",
        )


# --------------------------------------------------------------------------
# parse_transcript_lines
# --------------------------------------------------------------------------

def test_parse_transcript_lines_extracts_chat():
    lines = [
        _claude_line("user", "你好"),
        "   ",  # blank
        "{not json",  # garbage tolerated
        _claude_line("assistant", "在的"),
    ]
    messages = parse_transcript_lines(lines, provider="claude-code")
    assert [m.role for m in messages] == ["user", "assistant"]
    assert [m.content for m in messages] == ["你好", "在的"]


# --------------------------------------------------------------------------
# _read_new_lines (tail cursor)
# --------------------------------------------------------------------------

def test_read_new_lines_only_returns_new(tmp_path: Path):
    f = tmp_path / "t.jsonl"
    _write_lines(f, [_claude_line("user", "a")])
    lines, off = _read_new_lines(f, 0)
    assert len(lines) == 1 and off == f.stat().st_size

    # nothing new yet
    lines2, off2 = _read_new_lines(f, off)
    assert lines2 == [] and off2 == off

    # append two more, only those come back
    _write_lines(f, [_claude_line("assistant", "b"), _claude_line("user", "c")])
    lines3, off3 = _read_new_lines(f, off2)
    assert len(lines3) == 2 and off3 == f.stat().st_size


def test_read_new_lines_holds_partial_line(tmp_path: Path):
    f = tmp_path / "t.jsonl"
    _write_lines(f, [_claude_line("user", "a")])
    _, off = _read_new_lines(f, 0)
    # write a fragment without a trailing newline
    with f.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant",')
    lines, off2 = _read_new_lines(f, off)
    assert lines == [] and off2 == off  # not consumed until the line completes


def test_read_new_lines_resets_on_truncation(tmp_path: Path):
    f = tmp_path / "t.jsonl"
    _write_lines(f, [_claude_line("user", "a"), _claude_line("user", "b")])
    _, off = _read_new_lines(f, 0)
    # rotate: shrink the file below the previous offset
    f.write_text(_claude_line("user", "fresh") + "\n", encoding="utf-8")
    lines, _ = _read_new_lines(f, off)
    assert len(lines) == 1 and lines[0]


# --------------------------------------------------------------------------
# manager: attach + poll (inbound)
# --------------------------------------------------------------------------

def test_attach_from_start_then_poll_incremental(tmp_path: Path):
    transcript = tmp_path / "ext-123.jsonl"
    _write_lines(transcript, [_claude_line("user", "first")])
    mgr = ExternalLiveSessionManager(
        controller=StubController(transcript),
        state_path=tmp_path / "state.json",
        workspace_root=tmp_path,
    )
    sess = mgr.attach(provider="claude-code", transcript_path=transcript, from_start=True)
    assert sess.external_session_id == "ext-123"

    first = mgr.poll(sess.session_id)
    assert [m.content for m in first] == ["first"]

    _write_lines(transcript, [_claude_line("assistant", "second")])
    second = mgr.poll(sess.session_id)
    assert [m.content for m in second] == ["second"]

    # nothing new -> empty
    assert mgr.poll(sess.session_id) == []


def test_attach_from_end_skips_history(tmp_path: Path):
    transcript = tmp_path / "ext-9.jsonl"
    _write_lines(transcript, [_claude_line("user", "old")])
    mgr = ExternalLiveSessionManager(
        controller=StubController(transcript),
        state_path=tmp_path / "state.json",
        workspace_root=tmp_path,
    )
    sess = mgr.attach(provider="claude", transcript_path=transcript, from_start=False)
    assert mgr.poll(sess.session_id) == []  # history skipped
    _write_lines(transcript, [_claude_line("assistant", "new")])
    assert [m.content for m in mgr.poll(sess.session_id)] == ["new"]


# --------------------------------------------------------------------------
# manager: send (outbound resume + capture reply)
# --------------------------------------------------------------------------

async def test_send_resumes_and_captures_reply(tmp_path: Path):
    transcript = tmp_path / "ext-123.jsonl"
    _write_lines(transcript, [_claude_line("user", "hi")])
    stub = StubController(transcript)
    mgr = ExternalLiveSessionManager(
        controller=stub,
        state_path=tmp_path / "state.json",
        workspace_root=tmp_path,
    )
    sess = mgr.attach(provider="claude-code", transcript_path=transcript)
    mgr.poll(sess.session_id)  # drain history

    result = await mgr.send(sess.session_id, "继续干活")
    # controller was resumed with the registered controller session id
    assert stub.runs and stub.runs[0][0] == sess.controller_session_id
    assert "--resume" in result.command
    assert result.success is True
    # the CLI's appended reply was tailed back automatically
    assert [m.content for m in result.new_messages] == ["resumed reply"]


async def test_send_dry_run_does_not_tail(tmp_path: Path):
    transcript = tmp_path / "ext-123.jsonl"
    _write_lines(transcript, [_claude_line("user", "hi")])
    stub = StubController(transcript)
    mgr = ExternalLiveSessionManager(
        controller=stub,
        state_path=tmp_path / "state.json",
        workspace_root=tmp_path,
    )
    sess = mgr.attach(provider="claude-code", transcript_path=transcript)
    result = await mgr.send(sess.session_id, "试运行", dry_run=True)
    assert result.dry_run is True
    assert result.new_messages == []


# --------------------------------------------------------------------------
# manager: validation + persistence
# --------------------------------------------------------------------------

def test_attach_requires_path_or_id(tmp_path: Path):
    mgr = ExternalLiveSessionManager(
        controller=StubController(tmp_path / "x.jsonl"),
        state_path=tmp_path / "state.json",
        workspace_root=tmp_path,
    )
    with pytest.raises(ValueError):
        mgr.attach(provider="codex")


def test_attach_missing_file_raises(tmp_path: Path):
    mgr = ExternalLiveSessionManager(
        controller=StubController(tmp_path / "x.jsonl"),
        state_path=tmp_path / "state.json",
        workspace_root=tmp_path,
    )
    with pytest.raises(FileNotFoundError):
        mgr.attach(provider="claude-code", transcript_path=tmp_path / "missing.jsonl")


def test_state_persists_and_reloads(tmp_path: Path):
    transcript = tmp_path / "ext-7.jsonl"
    _write_lines(transcript, [_claude_line("user", "x")])
    state = tmp_path / "state.json"
    mgr = ExternalLiveSessionManager(
        controller=StubController(transcript), state_path=state, workspace_root=tmp_path
    )
    sess = mgr.attach(provider="claude-code", transcript_path=transcript)
    mgr.poll(sess.session_id)

    reloaded = ExternalLiveSessionManager(
        controller=StubController(transcript), state_path=state, workspace_root=tmp_path
    )
    again = reloaded.get_session(sess.session_id)
    assert again is not None
    assert again.external_session_id == "ext-7"
    assert again.byte_offset == sess.byte_offset  # cursor survived restart


def test_resolve_transcript_path_finds_claude_session(tmp_path: Path):
    roots = {"claude-code": tmp_path / ".claude", "codex": tmp_path / ".codex"}
    proj = roots["claude-code"] / "projects" / "demo"
    proj.mkdir(parents=True)
    target = proj / "abc-123.jsonl"
    target.write_text(_claude_line("user", "hi") + "\n", encoding="utf-8")
    found = resolve_transcript_path("claude-code", "abc-123", roots=roots)
    assert found == target
