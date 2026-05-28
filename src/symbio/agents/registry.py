"""Agent 注册中心 - 管理所有可用 Agent"""

from __future__ import annotations

from typing import Optional

from symbio.agents.base import BaseAgent
from symbio.utils.logger import get_logger
from symbio.utils.types import Intent, TaskComplexity

logger = get_logger("registry")


class AgentRegistry:
    """Agent 注册中心

    管理所有注册的 Agent，支持动态发现和匹配。
    """

    def __init__(self):
        self._agents: dict[str, BaseAgent] = {}
        self._agent_classes: dict[str, type[BaseAgent]] = {}

    def register(self, agent_class: type[BaseAgent]) -> type[BaseAgent]:
        """注册 Agent 类（装饰器）

        Usage:
            @registry.register
            class MyAgent(BaseAgent):
                name = "my_agent"
                ...
        """
        instance = agent_class()
        self._agents[instance.name] = instance
        self._agent_classes[instance.name] = agent_class
        logger.info(f"注册 Agent: {instance.name}")
        return agent_class

    def register_instance(self, agent: BaseAgent) -> None:
        """注册 Agent 实例"""
        self._agents[agent.name] = agent
        logger.info(f"注册 Agent 实例: {agent.name}")

    def unregister(self, name: str) -> bool:
        """注销 Agent"""
        if name in self._agents:
            del self._agents[name]
            self._agent_classes.pop(name, None)
            logger.info(f"注销 Agent: {name}")
            return True
        return False

    def get(self, name: str) -> Optional[BaseAgent]:
        """获取 Agent"""
        return self._agents.get(name)

    def find_best(self, intent: Intent) -> Optional[BaseAgent]:
        """查找最适合的 Agent

        Args:
            intent: 用户意图

        Returns:
            最适合的 Agent，如果没有找到返回 None
        """
        candidates = []

        for agent in self._agents.values():
            # 跳过不可用的 Agent
            if agent.state not in [AgentState.IDLE, AgentState.COMPLETED]:
                continue

            # 检查是否能处理
            if agent.can_handle(intent):
                candidates.append(agent)

        if not candidates:
            logger.warning(f"未找到能处理意图的 Agent: {intent.raw_text[:50]}...")
            return None

        # 选择第一个可用的（后续可以加评分机制）
        selected = candidates[0]
        logger.debug(f"选择 Agent: {selected.name}")
        return selected

    def list_agents(self) -> list[dict]:
        """列出所有 Agent"""
        return [agent.get_info() for agent in self._agents.values()]

    def get_available_agents(self) -> list[BaseAgent]:
        """获取所有可用的 Agent"""
        return [
            agent for agent in self._agents.values()
            if agent.state in [AgentState.IDLE, AgentState.COMPLETED]
        ]


# 全局注册中心实例
_registry = AgentRegistry()


def get_registry() -> AgentRegistry:
    """获取全局注册中心"""
    return _registry


def register_agent(name: str = None):
    """注册 Agent 装饰器

    Usage:
        @register_agent("my_agent")
        class MyAgent(BaseAgent):
            ...
    """
    def decorator(agent_class: type[BaseAgent]) -> type[BaseAgent]:
        if name:
            agent_class.name = name
        _registry.register(agent_class)
        return agent_class
    return decorator


# 导入 AgentState 用于类型检查
from symbio.utils.types import AgentState
