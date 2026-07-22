"""Agent 注册中心 - 管理所有可用 Agent"""

from __future__ import annotations

import asyncio
from typing import Optional

from symbio.agents.base import BaseAgent
from symbio.utils.logger import get_logger
from symbio.utils.types import AgentState, Intent

logger = get_logger("registry")


class AgentRegistry:
    """Agent 注册中心

    管理所有注册的 Agent，支持动态发现和匹配。

    设计原则：
    - 保存 Agent 类和原型实例（用于查询）
    - 每次任务请求创建新实例，避免并发状态错乱
    """

    def __init__(self):
        self._prototypes: dict[str, BaseAgent] = {}  # 原型实例（只读，用于查询）
        self._agent_classes: dict[str, type[BaseAgent]] = {}  # Agent 类
        self._creation_lock = asyncio.Lock()  # 保护并发创建

    def register(self, agent_class: type[BaseAgent]) -> type[BaseAgent]:
        """注册 Agent 类（装饰器）

        Usage:
            @registry.register
            class MyAgent(BaseAgent):
                name = "my_agent"
                ...
        """
        # 创建原型实例（只用于查询，不用于执行）
        prototype = agent_class()
        self._prototypes[prototype.name] = prototype
        self._agent_classes[prototype.name] = agent_class
        logger.info(f"注册 Agent: {prototype.name}")
        return agent_class

    def register_instance(self, agent: BaseAgent) -> None:
        """注册 Agent 实例（作为原型）"""
        self._prototypes[agent.name] = agent
        logger.info(f"注册 Agent 实例: {agent.name}")

    def unregister(self, name: str) -> bool:
        """注销 Agent"""
        if name in self._prototypes:
            del self._prototypes[name]
            self._agent_classes.pop(name, None)
            logger.info(f"注销 Agent: {name}")
            return True
        return False

    def get(self, name: str) -> Optional[BaseAgent]:
        """获取 Agent 原型（只读，用于查询状态）

        注意：不要并发调用此实例的 start() 等修改状态的方法
        """
        return self._prototypes.get(name)

    def create_instance(self, name: str) -> Optional[BaseAgent]:
        """创建新的 Agent 实例（用于并发执行）

        每次调用都返回新实例，避免并发状态错乱。
        """
        agent_class = self._agent_classes.get(name)
        if not agent_class:
            logger.warning(f"未找到 Agent 类: {name}")
            return None
        return agent_class()

    async def get_available_instance(self, name: str) -> Optional[BaseAgent]:
        """获取可用的 Agent 实例（线程安全）

        如果原型空闲，返回原型；否则创建新实例。
        """
        async with self._creation_lock:
            prototype = self._prototypes.get(name)
            if not prototype:
                return None

            # 如果原型空闲，返回原型
            if prototype.state in [AgentState.IDLE, AgentState.COMPLETED]:
                return prototype

            # 否则创建新实例
            logger.debug(f"Agent {name} 忙碌，创建新实例")
            return self.create_instance(name)

    async def find_best(self, intent: Intent) -> Optional[BaseAgent]:
        """查找最适合的 Agent（异步并发评分）

        Args:
            intent: 用户意图

        Returns:
            最适合的 Agent，如果没有找到返回 None
        """
        import asyncio

        # 收集可用候选
        available = [
            agent
            for agent in self._agents.values()
            if agent.state in [AgentState.IDLE, AgentState.COMPLETED]
        ]

        if not available:
            logger.warning("无可用 Agent")
            return None

        # 并发检查每个 Agent 能否处理该意图
        async def _check(agent: BaseAgent) -> tuple[BaseAgent, bool]:
            try:
                can = await agent.can_handle(intent)
                return (agent, can)
            except Exception:
                return (agent, False)

        results = await asyncio.gather(*[_check(a) for a in available])
        candidates = [agent for agent, can in results if can]

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
            agent
            for agent in self._agents.values()
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
