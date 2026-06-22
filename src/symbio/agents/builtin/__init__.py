"""内置 Agent —— 导入即注册到全局 AgentRegistry。"""

from symbio.agents.builtin.general_agent import GeneralAgent
from symbio.agents.builtin.external_backed_agent import (
    ClaudeCodeAgent,
    CodexAgent,
    ExternalBackedAgent,
)

__all__ = [
    "GeneralAgent",
    "ExternalBackedAgent",
    "ClaudeCodeAgent",
    "CodexAgent",
]
