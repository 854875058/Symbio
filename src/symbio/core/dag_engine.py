"""动态 DAG 引擎 — 运行时拓扑演化引擎。

核心职责：
1. 动态创建、修改、删除 DAG 节点
2. 运行时拓扑重构（根据中间观测结果动态增删节点）
3. 并行执行多个就绪节点
4. 节点依赖管理与环检测
5. 节点状态追踪（pending / running / success / failed / cancelled）
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine, Optional
from uuid import uuid4

import networkx as nx
from pydantic import BaseModel, Field

from symbio.core.event_bus import Event, EventBus, EventType
from symbio.core.state_manager import StateManager
from symbio.utils.logger import get_logger

logger = get_logger("dag_engine")

# ---------------------------------------------------------------------------
# 类型定义
# ---------------------------------------------------------------------------

# 节点可调用对象签名：接收上下文 dict，返回 NodeObservation
NodeCallable = Callable[[dict[str, Any]], Coroutine[Any, Any, "NodeObservation"]]


class NodeStatus(str, Enum):
    """DAG 节点生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TopologyAction(str, Enum):
    """拓扑变更动作类型。"""

    ADD_NODE = "add_node"
    REMOVE_NODE = "remove_node"
    ADD_EDGE = "add_edge"
    REMOVE_EDGE = "remove_edge"


# ---------------------------------------------------------------------------
# Pydantic 数据模型
# ---------------------------------------------------------------------------


class NodeObservation(BaseModel):
    """节点执行后的观测结果。

    Attributes:
        node_id: 产生该观测的节点 ID。
        output: 观测输出数据。
        expected: 是否符合预期。若为 False，引擎将触发拓扑重构。
        metadata: 附加元数据。
    """

    node_id: str
    output: Any = None
    expected: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class DAGNode(BaseModel):
    """DAG 节点定义。

    Attributes:
        node_id: 节点唯一标识。
        name: 节点可读名称。
        callable_ref: 节点对应的异步可调用对象（运行时不序列化）。
        status: 当前状态。
        dependencies: 依赖的上游节点 ID 列表。
        result: 执行完成后的观测结果。
        error: 执行失败时的错误信息。
        metadata: 附加元数据。
        created_at: 创建时间。
        started_at: 开始执行时间。
        completed_at: 完成时间。
    """

    model_config = {"arbitrary_types_allowed": True}

    node_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = ""
    callable_ref: Optional[NodeCallable] = Field(default=None, exclude=True)
    status: NodeStatus = NodeStatus.PENDING
    dependencies: list[str] = Field(default_factory=list)
    result: Optional[NodeObservation] = None
    error: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class TopologyChange(BaseModel):
    """描述一次拓扑变更操作。

    Attributes:
        action: 变更动作类型。
        node_id: 目标节点 ID。
        name: （仅 ADD_NODE）节点名称。
        callable_ref: （仅 ADD_NODE）节点可调用对象。
        dependencies: （仅 ADD_NODE）节点依赖。
        metadata: （仅 ADD_NODE）附加元数据。
        source: （仅 ADD_EDGE / REMOVE_EDGE）边的起始节点。
        target: （仅 ADD_EDGE / REMOVE_EDGE）边的目标节点。
    """

    model_config = {"arbitrary_types_allowed": True}

    action: TopologyAction
    node_id: str = ""
    name: str = ""
    callable_ref: Optional[NodeCallable] = Field(default=None, exclude=True)
    dependencies: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: str = ""
    target: str = ""


class DAGStats(BaseModel):
    """DAG 执行统计快照。"""

    total_nodes: int = 0
    pending: int = 0
    running: int = 0
    success: int = 0
    failed: int = 0
    cancelled: int = 0


class NodeResult(BaseModel):
    """节点执行结果的结构化表示，用于 MapReduce 合并。

    Attributes:
        node_id: 节点唯一标识。
        output: 节点输出数据。
        status: 执行状态（success / failed）。
        metadata: 附加元数据。
    """

    node_id: str
    output: Any = None
    status: str = "success"
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# DAG 引擎
# ---------------------------------------------------------------------------


class DAGEngine:
    """动态 DAG 执行引擎。

    Features:
        - 运行时增删节点与边
        - 基于 networkx 的环检测与拓扑排序
        - asyncio 并行执行就绪节点
        - 节点观测驱动的拓扑重构
        - 完整状态追踪与事件广播
    """

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        state_manager: Optional[StateManager] = None,
    ) -> None:
        self._graph = nx.DiGraph()
        self._nodes: dict[str, DAGNode] = {}
        self._event_bus = event_bus
        self._state_manager = state_manager
        self._lock = asyncio.Lock()
        self._cancelled = False
        logger.info("DAGEngine initialized")

    # ------------------------------------------------------------------
    # 节点管理
    # ------------------------------------------------------------------

    def add_node(
        self,
        name: str,
        func: NodeCallable,
        node_id: Optional[str] = None,
        dependencies: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> DAGNode:
        """向 DAG 添加一个新节点。

        Args:
            name: 节点可读名称。
            func: 节点执行的异步可调用对象。
            node_id: 可选的节点 ID，不提供则自动生成。
            dependencies: 依赖的上游节点 ID 列表。
            metadata: 附加元数据。

        Returns:
            创建的 DAGNode 实例。

        Raises:
            ValueError: 如果 node_id 重复或依赖的节点不存在。
        """
        node_id = node_id or str(uuid4())

        if node_id in self._nodes:
            raise ValueError(f"Node '{node_id}' already exists")

        dependencies = dependencies or []
        for dep_id in dependencies:
            if dep_id not in self._nodes:
                raise ValueError(f"Dependency node '{dep_id}' does not exist")

        node = DAGNode(
            node_id=node_id,
            name=name,
            callable_ref=func,
            dependencies=dependencies,
            metadata=metadata or {},
        )

        self._nodes[node_id] = node
        self._graph.add_node(node_id)

        for dep_id in dependencies:
            self._graph.add_edge(dep_id, node_id)

        # 环检测 — 如果新增边导致环，回滚
        if not nx.is_directed_acyclic_graph(self._graph):
            # 回滚
            for dep_id in dependencies:
                if self._graph.has_edge(dep_id, node_id):
                    self._graph.remove_edge(dep_id, node_id)
            self._graph.remove_node(node_id)
            del self._nodes[node_id]
            raise ValueError(f"Adding node '{node_id}' would create a cycle; operation rolled back")

        logger.info(f"Added node '{name}' (id={node_id}, deps={dependencies})")
        return node

    def remove_node(self, node_id: str) -> None:
        """从 DAG 中移除一个节点及其所有关联边。

        正在运行中的节点会被标记为 cancelled，但不会立即中断。

        Args:
            node_id: 要移除的节点 ID。

        Raises:
            KeyError: 节点不存在。
        """
        if node_id not in self._nodes:
            raise KeyError(f"Node '{node_id}' does not exist")

        node = self._nodes[node_id]

        # 正在运行中的节点标记取消
        if node.status == NodeStatus.RUNNING:
            node.status = NodeStatus.CANCELLED
            node.completed_at = datetime.now()
            logger.warning(f"Cancelling running node '{node.name}' (id={node_id}) due to removal")

        # 将依赖此节点的下游节点也标记为 cancelled
        for downstream_id in list(self._graph.successors(node_id)):
            downstream = self._nodes.get(downstream_id)
            if downstream and downstream.status in (NodeStatus.PENDING, NodeStatus.RUNNING):
                downstream.status = NodeStatus.CANCELLED
                downstream.completed_at = datetime.now()
                downstream.error = f"Cancelled: upstream node '{node_id}' was removed"
                logger.info(
                    f"Cascading cancel: node '{downstream.name}' (id={downstream_id}) "
                    f"cancelled due to removal of '{node_id}'"
                )

        self._graph.remove_node(node_id)
        del self._nodes[node_id]
        logger.info(f"Removed node '{node.name}' (id={node_id})")

    def add_edge(self, source_id: str, target_id: str) -> None:
        """添加一条依赖边（source -> target 表示 target 依赖 source）。

        Args:
            source_id: 上游节点 ID。
            target_id: 下游节点 ID。

        Raises:
            KeyError: 节点不存在。
            ValueError: 添加边会形成环。
        """
        if source_id not in self._nodes:
            raise KeyError(f"Source node '{source_id}' does not exist")
        if target_id not in self._nodes:
            raise KeyError(f"Target node '{target_id}' does not exist")

        if self._graph.has_edge(source_id, target_id):
            logger.debug(f"Edge '{source_id}' -> '{target_id}' already exists, skipping")
            return

        self._graph.add_edge(source_id, target_id)
        self._nodes[target_id].dependencies.append(source_id)

        if not nx.is_directed_acyclic_graph(self._graph):
            self._graph.remove_edge(source_id, target_id)
            self._nodes[target_id].dependencies.remove(source_id)
            raise ValueError(f"Adding edge '{source_id}' -> '{target_id}' would create a cycle")

        logger.info(f"Added edge: {source_id} -> {target_id}")

    def remove_edge(self, source_id: str, target_id: str) -> None:
        """移除一条依赖边。

        Args:
            source_id: 上游节点 ID。
            target_id: 下游节点 ID。

        Raises:
            KeyError: 边不存在。
        """
        if not self._graph.has_edge(source_id, target_id):
            raise KeyError(f"Edge '{source_id}' -> '{target_id}' does not exist")

        self._graph.remove_edge(source_id, target_id)
        target_node = self._nodes[target_id]
        if source_id in target_node.dependencies:
            target_node.dependencies.remove(source_id)

        logger.info(f"Removed edge: {source_id} -> {target_id}")

    def get_node(self, node_id: str) -> DAGNode:
        """获取节点。

        Args:
            node_id: 节点 ID。

        Returns:
            DAGNode 实例。

        Raises:
            KeyError: 节点不存在。
        """
        if node_id not in self._nodes:
            raise KeyError(f"Node '{node_id}' does not exist")
        return self._nodes[node_id]

    def list_nodes(self, status: Optional[NodeStatus] = None) -> list[DAGNode]:
        """列出所有节点，可按状态过滤。

        Args:
            status: 过滤的状态，None 表示全部。

        Returns:
            节点列表。
        """
        nodes = list(self._nodes.values())
        if status is not None:
            nodes = [n for n in nodes if n.status == status]
        return nodes

    def get_stats(self) -> DAGStats:
        """获取 DAG 执行统计快照。"""
        nodes = list(self._nodes.values())
        return DAGStats(
            total_nodes=len(nodes),
            pending=sum(1 for n in nodes if n.status == NodeStatus.PENDING),
            running=sum(1 for n in nodes if n.status == NodeStatus.RUNNING),
            success=sum(1 for n in nodes if n.status == NodeStatus.SUCCESS),
            failed=sum(1 for n in nodes if n.status == NodeStatus.FAILED),
            cancelled=sum(1 for n in nodes if n.status == NodeStatus.CANCELLED),
        )

    def get_execution_order(self) -> list[str]:
        """返回当前图的拓扑排序（不会包含已取消的节点）。"""
        active = [nid for nid, node in self._nodes.items() if node.status != NodeStatus.CANCELLED]
        subgraph = self._graph.subgraph(active)
        return list(nx.topological_sort(subgraph))

    # ------------------------------------------------------------------
    # 就绪节点发现
    # ------------------------------------------------------------------

    def _get_ready_nodes(self) -> list[DAGNode]:
        """找出所有满足执行条件的 pending 节点。

        条件：状态为 PENDING，且所有上游依赖均为 SUCCESS。
        """
        ready: list[DAGNode] = []
        for node in self._nodes.values():
            if node.status != NodeStatus.PENDING:
                continue
            all_deps_done = all(
                self._nodes[dep_id].status == NodeStatus.SUCCESS
                for dep_id in node.dependencies
                if dep_id in self._nodes
            )
            if all_deps_done:
                ready.append(node)
        return ready

    # ------------------------------------------------------------------
    # 单节点执行
    # ------------------------------------------------------------------

    async def _execute_node(self, node: DAGNode, context: dict[str, Any]) -> None:
        """执行单个节点并处理其观测结果。

        当配置了 StateManager 时，使用 CAS 乐观锁进行并发安全的状态更新：
        1. 执行前读取当前状态版本号
        2. 节点执行完成后，通过 CAS 更新状态
        3. 若 CAS 失败（版本冲突），读取最新版本后重试

        Args:
            node: 要执行的节点。
            context: 传递给节点可调用对象的上下文。
        """
        if self._cancelled:
            node.status = NodeStatus.CANCELLED
            node.completed_at = datetime.now()
            return

        if node.callable_ref is None:
            node.status = NodeStatus.FAILED
            node.error = "Node has no callable_ref"
            node.completed_at = datetime.now()
            logger.error(f"Node '{node.name}' (id={node.node_id}) has no callable_ref")
            return

        node.status = NodeStatus.RUNNING
        node.started_at = datetime.now()
        logger.info(f"Executing node '{node.name}' (id={node.node_id})")

        await self._emit_event(
            EventType.AGENT_SPAWNED,
            {"node_id": node.node_id, "node_name": node.name},
        )

        # CAS 乐观锁：执行前读取当前版本号
        expected_version: Optional[int] = None
        if self._state_manager is not None:
            try:
                state_snapshot = await self._state_manager.read()
                expected_version = state_snapshot.version
            except RuntimeError:
                # StateManager 未初始化，退化为无 CAS 模式
                expected_version = None

        try:
            observation = await node.callable_ref(context)
            node.result = observation
            node.status = NodeStatus.SUCCESS
            node.completed_at = datetime.now()

            logger.info(
                f"Node '{node.name}' (id={node.node_id}) completed successfully "
                f"(expected={observation.expected})"
            )

            # CAS 更新：将节点结果写入全局状态，冲突时重试
            if self._state_manager is not None and expected_version is not None:
                await self._cas_update_state(node, observation, expected_version)

            await self._emit_event(
                EventType.AGENT_COMPLETED,
                {
                    "node_id": node.node_id,
                    "node_name": node.name,
                    "expected": observation.expected,
                },
            )

            # 如果观测不符合预期，触发拓扑重构
            if not observation.expected:
                logger.warning(
                    f"Node '{node.name}' returned unexpected observation; "
                    f"triggering topology reconfiguration"
                )
                await self._handle_unexpected_observation(observation, context)

        except Exception as exc:
            node.status = NodeStatus.FAILED
            node.error = str(exc)
            node.completed_at = datetime.now()

            logger.error(f"Node '{node.name}' (id={node.node_id}) failed: {exc}")

            await self._emit_event(
                EventType.AGENT_FAILED,
                {"node_id": node.node_id, "node_name": node.name, "error": str(exc)},
            )

    async def _cas_update_state(
        self,
        node: DAGNode,
        observation: NodeObservation,
        expected_version: int,
        max_retries: int = 5,
    ) -> None:
        """通过 CAS 乐观锁将节点结果写入全局状态。

        若版本冲突（其他并行节点已更新状态），则读取最新版本后重试。

        Args:
            node: 已完成的节点。
            observation: 节点观测结果。
            expected_version: 执行开始时读取的版本号。
            max_retries: 最大重试次数。
        """

        def _make_updater(obs: NodeObservation, nid: str) -> Callable:  # type: ignore[type-arg]
            def updater(state):  # type: ignore[no-untyped-def]
                state.metadata[f"node_result:{nid}"] = {
                    "output": obs.output,
                    "expected": obs.expected,
                    "node_meta": obs.metadata,
                }
                return state

            return updater

        updater_fn = _make_updater(observation, node.node_id)

        for attempt in range(1, max_retries + 1):
            success = await self._state_manager.compare_and_swap(  # type: ignore[union-attr]
                expected_version=expected_version,
                updater=updater_fn,
            )

            if success:
                logger.debug(
                    f"CAS update succeeded for node '{node.name}' "
                    f"(version {expected_version} -> {expected_version + 1})"
                )
                return

            # CAS 失败：读取最新版本并重试
            logger.warning(
                f"CAS conflict for node '{node.name}' "
                f"(attempt {attempt}/{max_retries}), retrying..."
            )
            latest_state = await self._state_manager.read()  # type: ignore[union-attr]
            expected_version = latest_state.version

        logger.error(
            f"CAS update exhausted {max_retries} retries for node '{node.name}'; "
            f"result stored locally only"
        )

    # ------------------------------------------------------------------
    # 拓扑重构
    # ------------------------------------------------------------------

    async def _handle_unexpected_observation(
        self,
        observation: NodeObservation,
        context: dict[str, Any],
    ) -> None:
        """处理不符合预期的节点观测结果。

        默认实现调用 reconfigure_topology 钩子，子类可覆盖此方法以实现
        更复杂的重构逻辑。
        """
        changes = await self.reconfigure_topology(observation, context)
        if changes:
            await self.apply_topology_changes(changes)

    async def reconfigure_topology(
        self,
        observation: NodeObservation,
        context: dict[str, Any],
    ) -> list[TopologyChange]:
        """拓扑重构钩子 — 根据不符合预期的观测结果决定拓扑变更。

        子类或外部调用者应覆盖此方法以实现具体的重构策略。

        Args:
            observation: 不符合预期的观测结果。
            context: 当前执行上下文。

        Returns:
            待执行的拓扑变更列表。
        """
        logger.debug(
            f"Default reconfigure_topology called for node '{observation.node_id}'; "
            f"no changes applied (override this method to implement custom logic)"
        )
        return []

    async def apply_topology_changes(self, changes: list[TopologyChange]) -> None:
        """原子性地应用一批拓扑变更。

        如果任何变更导致非法状态（如产生环），整批变更回滚。

        Args:
            changes: 要应用的拓扑变更列表。

        Raises:
            ValueError: 变更导致环或其他非法状态。
        """
        applied: list[str] = []  # 已应用的变更描述，用于回滚
        # 保存快照用于回滚
        # 修复：需要单独保存 callable_ref，因为 model_dump(exclude={"callable_ref"}) 会丢失它
        snapshot_nodes = {
            nid: node.model_dump(exclude={"callable_ref"}) for nid, node in self._nodes.items()
        }
        # 单独保存 callable_ref
        snapshot_callable_refs = {
            nid: node.callable_ref for nid, node in self._nodes.items()
        }
        snapshot_edges = list(self._graph.edges())
        snapshot_graph_nodes = list(self._graph.nodes())

        try:
            for change in changes:
                if change.action == TopologyAction.ADD_NODE:
                    self.add_node(
                        name=change.name,
                        func=change.callable_ref,
                        node_id=change.node_id,
                        dependencies=change.dependencies,
                        metadata=change.metadata,
                    )
                    applied.append(f"add_node:{change.node_id}")

                elif change.action == TopologyAction.REMOVE_NODE:
                    self.remove_node(change.node_id)
                    applied.append(f"remove_node:{change.node_id}")

                elif change.action == TopologyAction.ADD_EDGE:
                    self.add_edge(change.source, change.target)
                    applied.append(f"add_edge:{change.source}->{change.target}")

                elif change.action == TopologyAction.REMOVE_EDGE:
                    self.remove_edge(change.source, change.target)
                    applied.append(f"remove_edge:{change.source}->{change.target}")

            logger.info(f"Applied {len(changes)} topology change(s): {applied}")

        except Exception as exc:
            logger.error(
                f"Topology change failed at step '{applied[-1] if applied else 'N/A'}': {exc}. "
                f"Rolling back {len(applied)} applied change(s)."
            )
            # 回滚：恢复到快照
            self._rollback(snapshot_nodes, snapshot_edges, snapshot_graph_nodes, snapshot_callable_refs)
            raise ValueError(f"Topology change failed and was rolled back: {exc}") from exc

    def _rollback(
        self,
        snapshot_nodes: dict[str, dict],
        snapshot_edges: list[tuple[str, str]],
        snapshot_graph_nodes: list[str],
        snapshot_callable_refs: dict[str, Optional[NodeCallable]] = None,
    ) -> None:
        """将 DAG 恢复到快照状态。

        Args:
            snapshot_nodes: 节点数据快照（不含 callable_ref）
            snapshot_edges: 边列表快照
            snapshot_graph_nodes: 图节点列表快照
            snapshot_callable_refs: callable_ref 备份（修复回滚丢失问题）
        """
        self._graph.clear()
        for nid in snapshot_graph_nodes:
            self._graph.add_node(nid)
        for src, tgt in snapshot_edges:
            self._graph.add_edge(src, tgt)

        self._nodes.clear()
        for nid, ndata in snapshot_nodes.items():
            # 修复：从备份中恢复 callable_ref
            # 如果备份中没有（新增的节点），则设为 None
            callable_ref = None
            if snapshot_callable_refs and nid in snapshot_callable_refs:
                callable_ref = snapshot_callable_refs[nid]
            self._nodes[nid] = DAGNode(**ndata, callable_ref=callable_ref)

        logger.warning("DAG state rolled back to snapshot")

    # ------------------------------------------------------------------
    # 报告观测
    # ------------------------------------------------------------------

    async def report_observation(
        self,
        observation: NodeObservation,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        """外部向 DAG 引擎汇报节点观测结果。

        如果观测不符合预期，将触发拓扑重构。

        Args:
            observation: 节点观测结果。
            context: 可选的执行上下文。
        """
        node_id = observation.node_id
        if node_id in self._nodes:
            self._nodes[node_id].result = observation

        if not observation.expected:
            logger.warning(
                f"Unexpected observation reported for node '{node_id}'; "
                f"triggering topology reconfiguration"
            )
            await self._handle_unexpected_observation(observation, context or {})

    # ------------------------------------------------------------------
    # DAG 执行主循环
    # ------------------------------------------------------------------

    async def execute(
        self,
        context: Optional[dict[str, Any]] = None,
        max_parallel: int = 0,
    ) -> dict[str, NodeObservation]:
        """执行整个 DAG。

        按拓扑顺序逐步执行节点，同一层级的节点并行执行。
        执行过程中支持动态拓扑变更。

        Args:
            context: 传递给每个节点的上下文。
            max_parallel: 最大并行节点数，0 表示不限制。

        Returns:
            所有成功节点的观测结果字典 {node_id: NodeObservation}。
        """
        context = context or {}
        self._cancelled = False

        stats = self.get_stats()
        logger.info(
            f"Starting DAG execution: {stats.total_nodes} node(s) (pending={stats.pending})"
        )

        await self._emit_event(
            EventType.TASK_STARTED,
            {"total_nodes": stats.total_nodes},
        )

        iteration = 0
        while not self._cancelled:
            iteration += 1
            ready = self._get_ready_nodes()

            if not ready:
                # 检查是否还有正在运行的节点
                running = [n for n in self._nodes.values() if n.status == NodeStatus.RUNNING]
                if not running:
                    # 没有就绪节点也没有运行中节点 — 执行结束
                    break
                # 等待运行中的节点完成
                await asyncio.sleep(0.05)
                continue

            logger.debug(
                f"Iteration {iteration}: {len(ready)} ready node(s) — {[n.name for n in ready]}"
            )

            # 并行执行就绪节点
            if max_parallel > 0:
                # 分批执行
                for i in range(0, len(ready), max_parallel):
                    batch = ready[i : i + max_parallel]
                    await asyncio.gather(*(self._execute_node(n, context) for n in batch))
            else:
                await asyncio.gather(*(self._execute_node(n, context) for n in ready))

        # 收集结果
        results: dict[str, NodeObservation] = {}
        for node in self._nodes.values():
            if node.result is not None:
                results[node.node_id] = node.result

        final_stats = self.get_stats()
        logger.info(
            f"DAG execution finished: "
            f"success={final_stats.success}, failed={final_stats.failed}, "
            f"cancelled={final_stats.cancelled}"
        )

        await self._emit_event(
            EventType.TASK_COMPLETED,
            {
                "success": final_stats.success,
                "failed": final_stats.failed,
                "cancelled": final_stats.cancelled,
            },
        )

        return results

    def cancel(self) -> None:
        """取消 DAG 所有待执行和正在执行的节点。"""
        self._cancelled = True
        for node in self._nodes.values():
            if node.status in (NodeStatus.PENDING, NodeStatus.RUNNING):
                node.status = NodeStatus.CANCELLED
                node.completed_at = datetime.now()
        logger.info("DAG execution cancelled")

    def reset(self) -> None:
        """重置所有节点状态为 PENDING，不清除图结构。"""
        self._cancelled = False
        for node in self._nodes.values():
            if node.status != NodeStatus.SUCCESS:
                node.status = NodeStatus.PENDING
                node.result = None
                node.error = None
                node.started_at = None
                node.completed_at = None
        logger.info("DAG state reset")

    def clear(self) -> None:
        """清空整个 DAG。"""
        self._graph.clear()
        self._nodes.clear()
        self._cancelled = False
        logger.info("DAG cleared")

    # ------------------------------------------------------------------
    # MapReduce 结果合并
    # ------------------------------------------------------------------

    @staticmethod
    def merge_results(results: list[NodeResult]) -> dict[str, Any]:
        """合并多个节点执行结果（MapReduce 风格）。

        将一组 NodeResult 聚合为单个字典：
        - outputs: {node_id: output} 所有节点输出的映射
        - succeeded: 成功节点的 node_id 列表
        - failed: 失败节点的 node_id 列表
        - all_metadata: 所有节点元数据的合并（后者覆盖前者）
        - merged_output: 所有成功节点 output 的列表（便于下游 reduce）

        Args:
            results: 节点执行结果列表。

        Returns:
            聚合后的字典。
        """
        outputs: dict[str, Any] = {}
        succeeded: list[str] = []
        failed: list[str] = []
        all_metadata: dict[str, Any] = {}
        merged_output: list[Any] = []

        for r in results:
            outputs[r.node_id] = r.output
            all_metadata[r.node_id] = r.metadata
            if r.status == "success":
                succeeded.append(r.node_id)
                if r.output is not None:
                    merged_output.append(r.output)
            else:
                failed.append(r.node_id)

        return {
            "outputs": outputs,
            "succeeded": succeeded,
            "failed": failed,
            "all_metadata": all_metadata,
            "merged_output": merged_output,
        }

    # ------------------------------------------------------------------
    # 可视化 / 调试
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """导出 DAG 为可序列化字典（用于调试 / 序列化检查点）。"""
        return {
            "nodes": {
                nid: node.model_dump(exclude={"callable_ref"}) for nid, node in self._nodes.items()
            },
            "edges": list(self._graph.edges()),
            "stats": self.get_stats().model_dump(),
        }

    def __repr__(self) -> str:
        stats = self.get_stats()
        return (
            f"DAGEngine(nodes={stats.total_nodes}, "
            f"pending={stats.pending}, running={stats.running}, "
            f"success={stats.success}, failed={stats.failed}, "
            f"cancelled={stats.cancelled})"
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    async def _emit_event(self, event_type: EventType, data: dict[str, Any]) -> None:
        """向事件总线发送事件（如果已配置）。"""
        if self._event_bus:
            await self._event_bus.emit(
                Event(
                    type=event_type,
                    data=data,
                    source="dag_engine",
                )
            )
