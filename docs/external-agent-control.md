# External Agent Control

Status: implemented as of 2026-06-09.

## Implemented

- Discover local Codex and Claude Code CLIs from `PATH`.
- Register an existing external CLI session under Symbio control.
- Keep a persisted Symbio session handle in `data/external_agents.json`.
- Build resumable command previews for Codex and Claude Code.
- Send prompts through the registered session when the CLI is installed.
- Discover local Codex and Claude Code JSONL transcripts and import them into Symbio chat sessions.
- Expose provider, session, run, and audit APIs under `/api/external-agents/*`.
- Show providers, session registration, transcript import, run results, and audit records in the Web UI.

## Evidence

- `src/symbio/tools/external_agents.py`
- `src/symbio/tools/external_transcripts.py`
- `src/symbio/interfaces/api.py`
- `web/index.html`
- `web/js/33-external-agents.js`
- `web/style.css`
- `tests/test_external_agents.py`
- `tests/test_external_transcript_import.py`

## Remaining

- Stream live CLI output into the UI.
- Add cancellation for long-running external agent runs.
- Sync more provider-specific permission modes into the UI.
- Inject MCP servers and environment profiles per external session.
- Support a cross-machine daemon deployment instead of local-only control.
