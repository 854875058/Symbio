"""Agent 模块"""

from symbio.agents.base import BaseAgent
from symbio.agents.registry import AgentRegistry, get_registry, register_agent
from symbio.agents.debate import DebateEngine

# 别名
MultiAgentDebate = DebateEngine

__all__ = [
    "BaseAgent",
    "AgentRegistry",
    "get_registry",
    "register_agent",
    "DebateEngine",
    "MultiAgentDebate",
]
