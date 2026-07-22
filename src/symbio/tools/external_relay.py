"""Relay orchestration —— make Codex and Claude Code talk to each other.

This is the "互相调用" half: Symbio drives a turn-by-turn conversation between
two external coding agents, piping one provider's output into the other's next
prompt. Continuity is carried *in the prompt* (the accumulated transcript),
not in CLI session memory, so it is robust regardless of whether a given run
remembers prior turns. Each turn goes through the same
``ExternalAgentController`` as everything else, so it lands in the audit trail.

Typical use: Codex writes code, Claude Code reviews it, Codex revises — all
orchestrated and observable inside Symbio.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("tools.external_relay")


class RelayTurn(BaseModel):
    """One agent's turn in a relay conversation."""

    index: int
    provider: str
    prompt: str
    output: str = ""
    success: bool = False
    run_id: str = ""
    exit_code: int = -1
    error: str = ""


class RelayConfig(BaseModel):
    """Configuration for a Codex↔Claude Code relay."""

    seed_prompt: str
    provider_a: str = "codex"
    provider_b: str = "claude-code"
    rounds: int = 2  # total alternating turns (A, B, A, ...)
    workspace: str = "."
    model_a: str = ""
    model_b: str = ""
    role_a: str = ""  # e.g. "你负责编写代码"
    role_b: str = ""  # e.g. "你负责审查并提出改进意见"
    timeout: int = 300
    dry_run: bool = False


class RelayResult(BaseModel):
    """Full transcript and outcome of a relay run."""

    config: RelayConfig
    turns: list[RelayTurn] = Field(default_factory=list)
    success: bool = False


class ExternalRelayOrchestrator:
    """Run an alternating conversation between two external coding agents."""

    def __init__(self, *, controller: Any = None, workspace_root: str = ".") -> None:
        self._controller = controller
        self._workspace_root = workspace_root

    def _get_controller(self) -> Any:
        if self._controller is None:
            from symbio.tools.external_agents import ExternalAgentController

            self._controller = ExternalAgentController(workspace_root=self._workspace_root)
        return self._controller

    def _ensure_session(
        self, controller: Any, provider: str, workspace: str, model: str, label: str
    ) -> str:
        from symbio.tools.external_agents import ExternalAgentSessionCreate

        for ws in (workspace, "."):
            try:
                created = controller.create_session(
                    ExternalAgentSessionCreate(
                        provider=provider, workspace=ws, model=model, label=label
                    )
                )
                return created.session_id
            except ValueError:
                continue
        raise ValueError(f"Could not create relay session for provider {provider}")

    @staticmethod
    def _build_turn_prompt(role: str, history: list[RelayTurn], seed: str, is_first: bool) -> str:
        lines: list[str] = []
        if role:
            lines.append(role)
        if is_first or not history:
            lines.append(seed)
            return "\n\n".join(lines)
        lines.append("以下是到目前为止你和另一个编码 Agent 的对话记录：")
        lines.append("")
        lines.append(f"最初的任务：{seed}")
        for turn in history:
            lines.append(f"[{turn.provider}]:\n{turn.output}")
        lines.append("")
        lines.append("请针对上面最新的内容，给出你的下一步回应。")
        return "\n".join(lines)

    async def run(self, config: RelayConfig) -> RelayResult:
        """Execute the relay and return every turn."""
        from symbio.tools.external_agents import (
            ExternalAgentRunRequest,
            ExternalAgentSessionCreate,
        )

        controller = self._get_controller()
        provider_a = ExternalAgentSessionCreate(provider=config.provider_a).provider
        provider_b = ExternalAgentSessionCreate(provider=config.provider_b).provider

        session_a = self._ensure_session(
            controller, provider_a, config.workspace, config.model_a, "relay-A"
        )
        session_b = self._ensure_session(
            controller, provider_b, config.workspace, config.model_b, "relay-B"
        )

        turns: list[RelayTurn] = []
        overall_success = True
        for index in range(max(config.rounds, 1)):
            if index % 2 == 0:
                provider, session_id, role, model = (
                    provider_a,
                    session_a,
                    config.role_a,
                    config.model_a,
                )
            else:
                provider, session_id, role, model = (
                    provider_b,
                    session_b,
                    config.role_b,
                    config.model_b,
                )

            prompt = self._build_turn_prompt(role, turns, config.seed_prompt, index == 0)
            run = await controller.run_session(
                session_id,
                ExternalAgentRunRequest(
                    prompt=prompt,
                    approved=True,
                    dry_run=config.dry_run,
                    model=model,
                    timeout=config.timeout,
                ),
            )
            output = (getattr(run, "stdout", "") or getattr(run, "error", "") or "").strip()
            turn = RelayTurn(
                index=index,
                provider=provider,
                prompt=prompt,
                output=output,
                success=bool(getattr(run, "success", False)),
                run_id=getattr(run, "run_id", ""),
                exit_code=getattr(run, "exit_code", -1),
                error=getattr(run, "error", ""),
            )
            turns.append(turn)
            if not turn.success:
                overall_success = False
                logger.info("relay stopped at turn %s (%s) due to failure", index, provider)
                break

        return RelayResult(config=config, turns=turns, success=overall_success)
