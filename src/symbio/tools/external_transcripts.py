"""Import local Codex and Claude Code transcripts into Symbio chat sessions."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


ExternalTranscriptProvider = Literal["codex", "claude-code"]
SUPPORTED_TRANSCRIPT_PROVIDERS = {"codex", "claude-code"}


class ExternalTranscriptMessage(BaseModel):
    """Normalized chat message extracted from an external-agent transcript."""

    role: str
    content: str
    timestamp: str = ""


class ExternalTranscriptSummary(BaseModel):
    """Metadata shown in discovery lists without exposing full transcript text."""

    provider: str
    path: str
    external_session_id: str = ""
    title: str = ""
    message_count: int = 0
    file_size: int = 0
    updated_at: str = ""
    created_at: str = ""


class ExternalTranscript(BaseModel):
    """Parsed external-agent transcript."""

    provider: str
    path: str
    external_session_id: str = ""
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    file_size: int = 0
    messages: list[ExternalTranscriptMessage] = Field(default_factory=list)

    def summary(self) -> ExternalTranscriptSummary:
        return ExternalTranscriptSummary(
            provider=self.provider,
            path=self.path,
            external_session_id=self.external_session_id,
            title=self.title,
            message_count=len(self.messages),
            file_size=self.file_size,
            updated_at=self.updated_at,
            created_at=self.created_at,
        )


def default_external_transcript_roots() -> dict[str, Path]:
    """Return default local roots used by Codex and Claude Code."""
    home = Path.home()
    return {
        "codex": home / ".codex",
        "claude-code": home / ".claude",
    }


def discover_external_transcripts(
    *,
    codex_root: str | Path | None = None,
    claude_root: str | Path | None = None,
    limit: int = 50,
) -> list[ExternalTranscriptSummary]:
    """Discover importable Codex and Claude Code JSONL transcripts."""
    roots = default_external_transcript_roots()
    if codex_root is not None:
        roots["codex"] = Path(codex_root)
    if claude_root is not None:
        roots["claude-code"] = Path(claude_root)

    candidates: list[tuple[str, Path]] = []
    codex_sessions = roots["codex"] / "sessions"
    if codex_sessions.exists():
        candidates.extend(("codex", path) for path in codex_sessions.rglob("*.jsonl"))

    claude_projects = roots["claude-code"] / "projects"
    if claude_projects.exists():
        candidates.extend(("claude-code", path) for path in claude_projects.rglob("*.jsonl"))

    candidates.sort(key=lambda item: _safe_mtime(item[1]), reverse=True)
    summaries: list[ExternalTranscriptSummary] = []
    for provider, path in candidates[: max(limit, 1)]:
        try:
            transcript = parse_external_transcript(path, provider=provider, max_messages=5000)
        except Exception:
            continue
        if transcript.messages:
            summaries.append(transcript.summary())
    return summaries


def parse_external_transcript(
    path: str | Path,
    *,
    provider: str | None = None,
    max_messages: int = 5000,
) -> ExternalTranscript:
    """Parse a Codex or Claude Code JSONL transcript into normalized messages."""
    transcript_path = Path(path).resolve()
    if not transcript_path.exists():
        raise FileNotFoundError(str(transcript_path))
    normalized_provider = _normalize_provider(provider) or _infer_provider(transcript_path)
    if normalized_provider not in SUPPORTED_TRANSCRIPT_PROVIDERS:
        raise ValueError(f"Unsupported transcript provider: {provider or transcript_path}")

    messages: list[ExternalTranscriptMessage] = []
    external_session_id = ""
    created_at = ""
    updated_at = _iso_from_timestamp(_safe_mtime(transcript_path))

    with transcript_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            if len(messages) >= max_messages:
                break
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            external_session_id = external_session_id or _extract_session_id(record, normalized_provider)
            timestamp = _extract_timestamp(record)
            if timestamp:
                created_at = created_at or timestamp
                updated_at = timestamp

            message = _extract_message(record, normalized_provider, timestamp)
            if message is not None:
                messages.append(message)

    title = _title_from_messages(messages) or transcript_path.stem
    return ExternalTranscript(
        provider=normalized_provider,
        path=str(transcript_path),
        external_session_id=external_session_id or transcript_path.stem,
        title=title,
        created_at=created_at,
        updated_at=updated_at,
        file_size=transcript_path.stat().st_size,
        messages=messages,
    )


def _extract_message(
    record: dict[str, Any],
    provider: str,
    timestamp: str,
) -> ExternalTranscriptMessage | None:
    if provider == "claude-code":
        source = record.get("message") if isinstance(record.get("message"), dict) else record
    else:
        payload = record.get("payload")
        source = payload if isinstance(payload, dict) else record
        if source.get("type") not in {None, "message"} and "role" not in source:
            return None

    role = _normalize_role(source.get("role") or record.get("role") or record.get("type"))
    if role is None:
        return None
    content = _content_to_text(source.get("content"))
    if not content:
        return None
    if provider == "codex" and _is_codex_internal_content(content):
        return None
    return ExternalTranscriptMessage(role=role, content=content, timestamp=timestamp)


def _content_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                block_type = item.get("type", "")
                if block_type in {"tool_use", "tool_result", "image", "thinking"}:
                    continue
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    if isinstance(value, dict):
        text = value.get("text") or value.get("content")
        return text.strip() if isinstance(text, str) else ""
    return ""


def _extract_session_id(record: dict[str, Any], provider: str) -> str:
    if provider == "claude-code":
        return str(record.get("sessionId") or record.get("session_id") or "").strip()
    payload = record.get("payload")
    if isinstance(payload, dict):
        if record.get("type") == "session_meta" and isinstance(payload.get("id"), str):
            return payload["id"].strip()
        if isinstance(payload.get("session_id"), str):
            return payload["session_id"].strip()
    return str(record.get("session_id") or record.get("sessionId") or "").strip()


def _extract_timestamp(record: dict[str, Any]) -> str:
    for key in ("timestamp", "created_at", "createdAt"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    payload = record.get("payload")
    if isinstance(payload, dict):
        value = payload.get("timestamp")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_role(value: Any) -> str | None:
    role = str(value or "").strip().lower()
    if role in {"user", "human"}:
        return "user"
    if role in {"assistant", "ai"}:
        return "assistant"
    if role in {"system", "developer"}:
        return "system"
    return None


def _is_codex_internal_content(content: str) -> bool:
    text = content.strip()
    return (
        text.startswith("<environment_context>")
        or text.startswith("# AGENTS.md instructions")
        or text.startswith("<permissions instructions>")
        or text.startswith("<collaboration_mode>")
        or text.startswith("<skills_instructions>")
    )


def _normalize_provider(value: str | None) -> str:
    provider = (value or "").strip().lower().replace("_", "-")
    if provider in {"claude", "claude-code", "cc"}:
        return "claude-code"
    if provider in {"codex", "openai-codex"}:
        return "codex"
    return provider


def _infer_provider(path: Path) -> str:
    parts = {part.lower() for part in path.parts}
    if ".claude" in parts:
        return "claude-code"
    if ".codex" in parts:
        return "codex"
    return ""


def _title_from_messages(messages: list[ExternalTranscriptMessage]) -> str:
    for message in messages:
        if message.role == "user":
            title = message.content.strip().splitlines()[0]
            return title[:80]
    return ""


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _iso_from_timestamp(value: float) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(value).isoformat(timespec="seconds")
