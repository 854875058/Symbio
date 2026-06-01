"""Phase 2 功能集成测试

覆盖模块：
1. TaskDecomposer - 任务分解器
2. SubAgentManager - 子任务调度管理器
3. DebateEngine - 共识辩论引擎（含 LLM 策略）
4. Orchestrator - 调度中枢集成流程

Mock 策略：
- Anthropic API 调用使用 httpx mock
- Agent 执行使用 unittest.mock.AsyncMock
- 使用 pytest 类组织，与 test_integration.py 保持一致
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 确保 src 在 Python 路径中
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.agents.debate import (
    Argument,
    ConsensusDetector,
    ConsensusResult,
    CriticStrategy,
    DebateEngine,
    DebateRole,
    DebateRound,
    DebateSession,
    DebateStatus,
    LLMCriticStrategy,
    LLMProposerStrategy,
    LLMRefinerStrategy,
    ProposerStrategy,
    RefinerStrategy,
)
from symbio.agents.registry import AgentRegistry
from symbio.core.decomposer import (
    DecompositionResult,
    SubTask,
    TaskDecomposer,
    _DEBATE_KEYWORDS,
)
from symbio.core.event_bus import EventBus
from symbio.core.orchestrator import Orchestrator
from symbio.core.rate_limiter import RateLimiter
from symbio.agents.subagent import (
    AggregatedResult,
    SubAgentManager,
    SubAgentResult,
)
from symbio.utils.types import (
    AgentState,
    Intent,
    Message,
    MessageSource,
    Result,
    Task,
    TaskComplexity,
    TokenUsage,
)


# ================================================================
# 共用辅助函数
# ================================================================


def _make_mock_settings(api_key="test-api-key"):
    """创建一个 mock Settings 对象，避免读取 symbio.yaml 文件。"""
    mock_settings = MagicMock()
    mock_settings.model = MagicMock()
    mock_settings.model.anthropic_api_key = api_key
    mock_settings.model.anthropic_base_url = "https://api.anthropic.com"
    mock_settings.model.model_low = "claude-3-5-haiku-20241022"
    mock_settings.model.model_medium = "claude-sonnet-4-20250514"
    mock_settings.model.model_high = "claude-opus-4-20250514"
    mock_settings.model.openai_api_key = ""
    mock_settings.model.openai_base_url = "https://api.openai.com/v1"
    return mock_settings


def _make_mock_httpx_response(response_data: dict):
    """创建一个 mock httpx.Response 对象。"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = response_data
    mock_response.raise_for_status = MagicMock()
    return mock_response


def _make_mock_httpx_client(mock_response):
    """创建一个 mock httpx.AsyncClient，可用作 async context manager。"""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def _make_subtask(
    name="test_subtask",
    description="test description",
    action="chat",
    dependencies=None,
    suggested_agent="general",
    subtask_id=None,
):
    """创建一个 SubTask 实例。"""
    kwargs = {
        "name": name,
        "description": description,
        "action": action,
        "dependencies": dependencies or [],
        "suggested_agent": suggested_agent,
    }
    if subtask_id:
        kwargs["subtask_id"] = subtask_id
    return SubTask(**kwargs)


def _make_parent_task(task_id="parent-task-001"):
    """创建一个父 Task 实例。"""
    return Task(
        task_id=task_id,
        intent=Intent(raw_text="test task", action="chat"),
    )


def _make_mock_agent(name="general", execute_result=None):
    """创建一个 mock Agent。"""
    agent = MagicMock()
    agent.name = name
    agent.state = AgentState.IDLE

    if execute_result is None:
        execute_result = Result(
            task_id="mock-task",
            success=True,
            content=f"Agent {name} 执行完成",
            token_usage=TokenUsage(
                input_tokens=10, output_tokens=20, total_tokens=30
            ),
        )
    agent.execute = AsyncMock(return_value=execute_result)
    agent.can_handle = MagicMock(return_value=True)
    return agent


# ================================================================
# 1. TaskDecomposer 测试
# ================================================================


class TestTaskDecomposer:
    """任务分解器测试"""

    async def test_simple_task_returns_single_subtask(self):
        """简单任务应返回单个子任务（LLM 不可用时回退）"""
        decomposer = TaskDecomposer()

        mock_settings = _make_mock_settings(api_key="")  # 无 API key
        with patch("symbio.core.decomposer.get_settings", return_value=mock_settings):
            intent = Intent(raw_text="你好", action="chat")
            result = await decomposer.decompose(intent, task_id="task-001")

        assert result.task_id == "task-001"
        assert len(result.subtasks) == 1
        assert result.subtasks[0].name == "execute_task"
        assert result.subtasks[0].action == "chat"
        assert result.needs_debate is False

    async def test_complex_task_with_mock_llm(self):
        """复杂任务：mock LLM 返回多个子任务"""
        llm_response_data = {
            "content": [
                {
                    "text": json.dumps({
                        "reasoning": "这是一个多步骤任务",
                        "needs_debate": False,
                        "subtasks": [
                            {
                                "name": "分析代码",
                                "description": "分析现有代码结构",
                                "action": "code_review",
                                "dependencies": [],
                                "estimated_complexity": "medium",
                                "suggested_agent": "code_reviewer",
                            },
                            {
                                "name": "编写测试",
                                "description": "为代码编写单元测试",
                                "action": "write_code",
                                "dependencies": ["分析代码"],
                                "estimated_complexity": "medium",
                                "suggested_agent": "coder",
                            },
                        ],
                    })
                }
            ],
            "usage": {"input_tokens": 50, "output_tokens": 100},
        }

        mock_response = _make_mock_httpx_response(llm_response_data)
        mock_client = _make_mock_httpx_client(mock_response)
        mock_settings = _make_mock_settings(api_key="test-key")

        with (
            patch("symbio.core.decomposer.get_settings", return_value=mock_settings),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            decomposer = TaskDecomposer()
            intent = Intent(raw_text="分析代码并编写测试", action="code_review")
            result = await decomposer.decompose(intent, task_id="task-002")

        assert result.task_id == "task-002"
        assert len(result.subtasks) == 2
        assert result.subtasks[0].name == "分析代码"
        assert result.subtasks[1].name == "编写测试"
        # 验证依赖关系已正确解析
        assert result.subtasks[1].dependencies == [result.subtasks[0].subtask_id]
        assert len(result.execution_order) == 2  # 两组：先分析，后测试

    async def test_fallback_when_llm_unavailable(self):
        """LLM 不可用时应回退到单子任务"""
        mock_settings = _make_mock_settings(api_key="")
        with patch("symbio.core.decomposer.get_settings", return_value=mock_settings):
            decomposer = TaskDecomposer()
            intent = Intent(
                raw_text="帮我设计一个微服务架构",
                action="chat",
                estimated_complexity=TaskComplexity.HIGH,
            )
            result = await decomposer.decompose(intent, task_id="task-003")

        assert len(result.subtasks) == 1
        assert result.subtasks[0].name == "execute_task"
        assert "回退" in result.reasoning

    async def test_fallback_when_llm_returns_invalid_json(self):
        """LLM 返回无效 JSON 时应安全回退"""
        mock_response = _make_mock_httpx_response({
            "content": [{"text": "这不是 JSON 内容"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })
        mock_client = _make_mock_httpx_client(mock_response)
        mock_settings = _make_mock_settings(api_key="test-key")

        with (
            patch("symbio.core.decomposer.get_settings", return_value=mock_settings),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            decomposer = TaskDecomposer()
            intent = Intent(raw_text="测试任务", action="chat")
            result = await decomposer.decompose(intent, task_id="task-004")

        assert len(result.subtasks) == 1
        assert result.subtasks[0].name == "execute_task"

    async def test_execution_order_topological_sort(self):
        """验证拓扑排序：无依赖的子任务并行，有依赖的串行"""
        # 构造 3 个子任务：A 无依赖，B 依赖 A，C 无依赖
        st_a = _make_subtask(name="A", subtask_id="id-a")
        st_b = _make_subtask(name="B", subtask_id="id-b", dependencies=["id-a"])
        st_c = _make_subtask(name="C", subtask_id="id-c")

        execution_order = TaskDecomposer._calculate_execution_order([st_a, st_b, st_c])

        assert len(execution_order) == 2
        # 第一组：A 和 C 并行
        assert set(execution_order[0]) == {"id-a", "id-c"}
        # 第二组：B 串行
        assert execution_order[1] == ["id-b"]

    async def test_execution_order_all_independent(self):
        """所有子任务无依赖时应全部并行"""
        st_a = _make_subtask(name="A", subtask_id="id-a")
        st_b = _make_subtask(name="B", subtask_id="id-b")
        st_c = _make_subtask(name="C", subtask_id="id-c")

        execution_order = TaskDecomposer._calculate_execution_order([st_a, st_b, st_c])

        assert len(execution_order) == 1
        assert set(execution_order[0]) == {"id-a", "id-b", "id-c"}

    async def test_execution_order_chain(self):
        """链式依赖 A -> B -> C 应产生 3 个串行组"""
        st_a = _make_subtask(name="A", subtask_id="id-a")
        st_b = _make_subtask(name="B", subtask_id="id-b", dependencies=["id-a"])
        st_c = _make_subtask(name="C", subtask_id="id-c", dependencies=["id-b"])

        execution_order = TaskDecomposer._calculate_execution_order([st_a, st_b, st_c])

        assert len(execution_order) == 3
        assert execution_order[0] == ["id-a"]
        assert execution_order[1] == ["id-b"]
        assert execution_order[2] == ["id-c"]

    async def test_execution_order_empty(self):
        """空子任务列表应返回空执行顺序"""
        execution_order = TaskDecomposer._calculate_execution_order([])
        assert execution_order == []

    async def test_debate_detection_keywords(self):
        """包含辩论关键词的输入应触发辩论检测"""
        for keyword in ["对比", "评估", "选择", "比较", "权衡", "compare", "evaluate"]:
            intent = Intent(
                raw_text=f"请{keyword}这两种方案的优劣",
                action="chat",
            )
            result = DecompositionResult(
                task_id="test",
                original_intent=intent.raw_text,
                subtasks=[],
                needs_debate=False,
            )
            assert TaskDecomposer._should_debate(intent, result) is True, (
                f"关键词 '{keyword}' 应触发辩论"
            )

    async def test_debate_detection_high_complexity(self):
        """高复杂度任务应触发辩论"""
        intent = Intent(
            raw_text="普通任务描述",
            action="chat",
            estimated_complexity=TaskComplexity.HIGH,
        )
        result = DecompositionResult(
            task_id="test",
            original_intent=intent.raw_text,
            subtasks=[],
            needs_debate=False,
        )
        assert TaskDecomposer._should_debate(intent, result) is True

    async def test_debate_detection_llm_flag(self):
        """LLM 标记 needs_debate=True 时应触发辩论"""
        intent = Intent(raw_text="普通任务", action="chat")
        result = DecompositionResult(
            task_id="test",
            original_intent=intent.raw_text,
            subtasks=[],
            needs_debate=True,
        )
        assert TaskDecomposer._should_debate(intent, result) is True

    async def test_no_debate_for_simple_task(self):
        """简单任务不应触发辩论"""
        intent = Intent(raw_text="你好", action="chat")
        result = DecompositionResult(
            task_id="test",
            original_intent=intent.raw_text,
            subtasks=[],
            needs_debate=False,
        )
        assert TaskDecomposer._should_debate(intent, result) is False

    async def test_strip_code_fences(self):
        """验证代码围栏去除"""
        text_with_fences = '```json\n{"key": "value"}\n```'
        stripped = TaskDecomposer._strip_code_fences(text_with_fences)
        assert stripped == '{"key": "value"}'

        text_without_fences = '{"key": "value"}'
        stripped = TaskDecomposer._strip_code_fences(text_without_fences)
        assert stripped == '{"key": "value"}'

    async def test_llm_response_with_code_fences(self):
        """LLM 返回带代码围栏的 JSON 时应正确解析"""
        fenced_json = '```json\n{"reasoning": "test", "needs_debate": false, "subtasks": [{"name": "task1", "description": "desc", "action": "chat", "dependencies": [], "estimated_complexity": "low", "suggested_agent": "general"}]}\n```'
        mock_response = _make_mock_httpx_response({
            "content": [{"text": fenced_json}],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        })
        mock_client = _make_mock_httpx_client(mock_response)
        mock_settings = _make_mock_settings(api_key="test-key")

        with (
            patch("symbio.core.decomposer.get_settings", return_value=mock_settings),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            decomposer = TaskDecomposer()
            intent = Intent(raw_text="测试", action="chat")
            result = await decomposer.decompose(intent, task_id="task-fence")

        assert len(result.subtasks) == 1
        assert result.subtasks[0].name == "task1"


# ================================================================
# 2. SubAgentManager 测试
# ================================================================


class TestSubAgentManager:
    """子任务调度管理器测试"""

    def setup_method(self):
        """每个测试前重置管理器。"""
        self.registry = MagicMock(spec=AgentRegistry)
        self.event_bus = EventBus()
        self.rate_limiter = RateLimiter()

    def _create_manager(self):
        """创建 SubAgentManager 实例。"""
        return SubAgentManager(
            registry=self.registry,
            event_bus=self.event_bus,
            rate_limiter=self.rate_limiter,
        )

    async def test_execute_single_subtask(self):
        """执行单个子任务"""
        manager = self._create_manager()

        subtask = _make_subtask(name="single_task", subtask_id="st-001")
        parent_task = _make_parent_task()
        mock_agent = _make_mock_agent("general")

        self.registry.get.return_value = mock_agent
        self.registry.find_best.return_value = mock_agent

        result = await manager.execute_subtasks(
            subtasks=[subtask],
            parent_task=parent_task,
            execution_order=[["st-001"]],
        )

        assert result.success is True
        assert result.total_subtasks == 1
        assert result.completed_subtasks == 1
        assert result.failed_subtasks == 0
        mock_agent.execute.assert_called_once()

    async def test_execute_multiple_parallel_subtasks(self):
        """执行多个并行子任务（无依赖）"""
        manager = self._create_manager()

        subtask_a = _make_subtask(name="task_a", subtask_id="st-a")
        subtask_b = _make_subtask(name="task_b", subtask_id="st-b")
        subtask_c = _make_subtask(name="task_c", subtask_id="st-c")

        mock_agent = _make_mock_agent("general")
        self.registry.get.return_value = mock_agent
        self.registry.find_best.return_value = mock_agent

        parent_task = _make_parent_task()

        result = await manager.execute_subtasks(
            subtasks=[subtask_a, subtask_b, subtask_c],
            parent_task=parent_task,
            execution_order=[["st-a", "st-b", "st-c"]],  # 全部并行
        )

        assert result.success is True
        assert result.total_subtasks == 3
        assert result.completed_subtasks == 3
        assert mock_agent.execute.call_count == 3

    async def test_execute_subtasks_with_dependencies(self):
        """执行有依赖关系的子任务（串行）"""
        manager = self._create_manager()

        subtask_a = _make_subtask(name="step1", subtask_id="st-a")
        subtask_b = _make_subtask(
            name="step2", subtask_id="st-b", dependencies=["st-a"]
        )

        mock_agent = _make_mock_agent("general")
        self.registry.get.return_value = mock_agent
        self.registry.find_best.return_value = mock_agent

        parent_task = _make_parent_task()

        result = await manager.execute_subtasks(
            subtasks=[subtask_a, subtask_b],
            parent_task=parent_task,
            execution_order=[["st-a"], ["st-b"]],  # 串行
        )

        assert result.success is True
        assert result.total_subtasks == 2
        assert result.completed_subtasks == 2
        assert mock_agent.execute.call_count == 2

    async def test_fault_isolation(self):
        """故障隔离：一个子任务失败，其他子任务继续执行"""
        manager = self._create_manager()

        subtask_ok = _make_subtask(name="ok_task", subtask_id="st-ok", suggested_agent="general")
        subtask_fail = _make_subtask(name="fail_task", subtask_id="st-fail", suggested_agent="failing_agent")

        # 第一个 agent 成功，第二个 agent 失败
        agent_ok = _make_mock_agent("general")
        agent_ok.execute = AsyncMock(
            return_value=Result(
                task_id="ok", success=True, content="成功"
            )
        )

        agent_fail = _make_mock_agent("failing_agent")
        agent_fail.execute = AsyncMock(side_effect=RuntimeError("执行失败"))

        def get_agent(name):
            if name == "general":
                return agent_ok
            elif name == "failing_agent":
                return agent_fail
            return agent_ok

        self.registry.get.side_effect = get_agent
        self.registry.find_best.return_value = agent_ok

        parent_task = _make_parent_task()

        result = await manager.execute_subtasks(
            subtasks=[subtask_ok, subtask_fail],
            parent_task=parent_task,
            execution_order=[["st-ok", "st-fail"]],
        )

        # 总体应标记为失败（有子任务失败）
        assert result.success is False
        assert result.total_subtasks == 2
        assert result.completed_subtasks == 1
        assert result.failed_subtasks == 1

    async def test_result_aggregation(self):
        """结果聚合：验证 combined_content 和 token 汇总"""
        manager = self._create_manager()

        subtask_a = _make_subtask(name="task_a", subtask_id="st-a", suggested_agent="agent_a")
        subtask_b = _make_subtask(name="task_b", subtask_id="st-b", suggested_agent="agent_b")

        agent_a = _make_mock_agent("agent_a")
        agent_a.execute = AsyncMock(
            return_value=Result(
                task_id="a",
                success=True,
                content="结果A",
                token_usage=TokenUsage(
                    input_tokens=100, output_tokens=50, total_tokens=150
                ),
            )
        )

        agent_b = _make_mock_agent("agent_b")
        agent_b.execute = AsyncMock(
            return_value=Result(
                task_id="b",
                success=True,
                content="结果B",
                token_usage=TokenUsage(
                    input_tokens=200, output_tokens=80, total_tokens=280
                ),
            )
        )

        def get_agent(name):
            if name == "agent_a":
                return agent_a
            elif name == "agent_b":
                return agent_b
            return agent_a

        self.registry.get.side_effect = get_agent
        self.registry.find_best.return_value = agent_a

        parent_task = _make_parent_task()

        result = await manager.execute_subtasks(
            subtasks=[subtask_a, subtask_b],
            parent_task=parent_task,
            execution_order=[["st-a", "st-b"]],
        )

        assert result.success is True
        assert result.completed_subtasks == 2
        assert "结果A" in result.combined_content
        assert "结果B" in result.combined_content
        assert result.total_token_usage["input_tokens"] == 300
        assert result.total_token_usage["output_tokens"] == 130
        assert result.total_token_usage["total_tokens"] == 430

    async def test_no_agent_available(self):
        """没有可用 Agent 时子任务应标记为失败"""
        manager = self._create_manager()

        subtask = _make_subtask(name="orphan_task", subtask_id="st-orphan")
        parent_task = _make_parent_task()

        self.registry.get.return_value = None
        self.registry.find_best.return_value = None

        result = await manager.execute_subtasks(
            subtasks=[subtask],
            parent_task=parent_task,
            execution_order=[["st-orphan"]],
        )

        assert result.success is False
        assert result.failed_subtasks == 1
        assert result.results[0].error is not None

    async def test_mixed_success_failure_aggregation(self):
        """混合成功/失败的结果聚合"""
        manager = self._create_manager()

        subtask_ok = _make_subtask(name="ok", subtask_id="st-ok")
        subtask_fail = _make_subtask(name="fail", subtask_id="st-fail")

        agent = _make_mock_agent("general")
        call_count = 0

        async def mock_execute(task):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return Result(task_id="ok", success=True, content="成功")
            raise RuntimeError("失败")

        agent.execute = mock_execute
        self.registry.get.return_value = agent
        self.registry.find_best.return_value = agent

        parent_task = _make_parent_task()

        result = await manager.execute_subtasks(
            subtasks=[subtask_ok, subtask_fail],
            parent_task=parent_task,
            execution_order=[["st-ok", "st-fail"]],
        )

        assert result.success is False
        assert result.completed_subtasks == 1
        assert result.failed_subtasks == 1
        assert len(result.results) == 2


# ================================================================
# 3. DebateEngine 测试
# ================================================================


class TestDebateEngine:
    """共识辩论引擎测试"""

    async def test_debate_with_template_strategies(self):
        """使用模板策略（非 LLM）运行辩论，三角色均应产生内容"""
        engine = DebateEngine(use_llm=False, max_rounds=2, consensus_threshold=0.5)

        session = await engine.run_debate(topic="选择最佳编程语言")

        assert session.status in (DebateStatus.CONSENSUS, DebateStatus.MAX_ROUNDS_REACHED)
        assert len(session.rounds) >= 1

        for debate_round in session.rounds:
            assert debate_round.proposal is not None
            assert debate_round.proposal.role == DebateRole.PROPOSER
            assert debate_round.proposal.content  # 非空

            assert debate_round.critique is not None
            assert debate_round.critique.role == DebateRole.CRITIC
            assert debate_round.critique.content

            assert debate_round.refinement is not None
            assert debate_round.refinement.role == DebateRole.REFINER
            assert debate_round.refinement.content

    async def test_consensus_detection_achieved(self):
        """共识检测：当分数超过阈值时应达成共识"""
        # 使用很低的阈值确保达成共识
        engine = DebateEngine(use_llm=False, max_rounds=5, consensus_threshold=0.1)

        session = await engine.run_debate(topic="简单测试")

        # 阈值极低，应该在第一轮就达成共识
        assert session.status == DebateStatus.CONSENSUS
        assert session.final_proposal != ""
        assert session.final_consensus is not None
        assert session.final_consensus.is_consensus is True

    async def test_max_rounds_reached(self):
        """达到最大轮次仍未达成共识"""
        # 使用极高的阈值确保无法达成共识
        engine = DebateEngine(use_llm=False, max_rounds=2, consensus_threshold=0.99)

        session = await engine.run_debate(topic="困难的决策")

        assert session.status == DebateStatus.MAX_ROUNDS_REACHED
        assert len(session.rounds) == 2
        assert session.final_consensus is not None
        assert session.final_consensus.is_consensus is False

    async def test_fallback_to_template_when_llm_unavailable(self):
        """LLM 不可用时应降级到模板策略"""
        # 使用 LLM 策略但无 API key，应自动降级
        mock_settings = _make_mock_settings(api_key="")

        proposer = LLMProposerStrategy(settings=mock_settings)
        critic = LLMCriticStrategy(settings=mock_settings)
        refiner = LLMRefinerStrategy(settings=mock_settings)

        engine = DebateEngine(
            proposer=proposer,
            critic=critic,
            refiner=refiner,
            max_rounds=1,
            consensus_threshold=0.1,
        )

        session = await engine.run_debate(topic="测试降级")

        # 应该成功完成（降级到模板）
        assert len(session.rounds) == 1
        assert session.rounds[0].proposal.content
        assert session.rounds[0].critique.content
        assert session.rounds[0].refinement.content

    async def test_custom_generator_strategies(self):
        """自定义 generator 的策略应正确调用"""
        proposer_content = "自定义提案内容"
        critic_content = "自定义批评内容"
        refiner_content = "自定义精炼内容"

        proposer = ProposerStrategy(
            generator=lambda **kwargs: proposer_content
        )
        critic = CriticStrategy(
            generator=lambda **kwargs: critic_content
        )
        refiner = RefinerStrategy(
            generator=lambda **kwargs: refiner_content
        )

        engine = DebateEngine(
            proposer=proposer,
            critic=critic,
            refiner=refiner,
            max_rounds=1,
            consensus_threshold=0.1,
        )

        session = await engine.run_debate(topic="自定义测试")

        assert session.rounds[0].proposal.content == proposer_content
        assert session.rounds[0].critique.content == critic_content
        assert session.rounds[0].refinement.content == refiner_content

    async def test_debate_session_stored(self):
        """辩论会话应被存储并可检索"""
        engine = DebateEngine(use_llm=False, max_rounds=1, consensus_threshold=0.1)

        session = await engine.run_debate(
            topic="存储测试",
            session_id="test-session-001",
        )

        retrieved = engine.get_session("test-session-001")
        assert retrieved is not None
        assert retrieved.session_id == "test-session-001"
        assert retrieved.topic == "存储测试"

        sessions = engine.list_sessions()
        assert len(sessions) >= 1

    async def test_debate_history_format(self):
        """辩论历史应返回正确格式"""
        engine = DebateEngine(use_llm=False, max_rounds=1, consensus_threshold=0.1)

        session = await engine.run_debate(
            topic="历史测试",
            session_id="history-001",
        )

        history = engine.get_debate_history("history-001")
        assert len(history) >= 1

        first_round = history[0]
        assert "round" in first_round
        assert "consensus_score" in first_round
        assert "status" in first_round
        assert "proposal" in first_round
        assert "critique" in first_round
        assert "refinement" in first_round

    async def test_consensus_detector(self):
        """共识检测器单独测试"""
        detector = ConsensusDetector(threshold=0.5)

        proposal = Argument(
            role=DebateRole.PROPOSER,
            round_number=1,
            content="这是一个详细的方案，包含所有必要的实现细节和具体步骤",
            confidence=0.8,
        )
        critique = Argument(
            role=DebateRole.CRITIC,
            round_number=1,
            content="方案整体可行，但某些细节需要改进",
            confidence=0.7,
        )
        refinement = Argument(
            role=DebateRole.REFINER,
            round_number=1,
            content="这是一个详细的方案，包含所有必要的实现细节和具体步骤，并已改进",
            confidence=0.85,
        )

        result = detector.detect(
            proposal=proposal,
            critique=critique,
            refinement=refinement,
            history=[],
        )

        assert isinstance(result, ConsensusResult)
        assert 0.0 <= result.score <= 1.0
        assert isinstance(result.is_consensus, bool)

    async def test_debate_with_initial_proposal(self):
        """带初始提案的辩论"""
        engine = DebateEngine(use_llm=False, max_rounds=1, consensus_threshold=0.1)

        session = await engine.run_debate(
            topic="优化方案",
            initial_proposal="初始方案：使用缓存",
        )

        assert len(session.rounds) == 1
        # 第一轮提案应基于初始提案生成
        assert session.rounds[0].proposal.content

    async def test_llm_strategy_with_mock_api(self):
        """LLM 策略使用 mock API 调用"""
        mock_settings = _make_mock_settings(api_key="test-key")

        llm_response = {
            "content": [
                {
                    "text": json.dumps({
                        "content": "LLM 生成的提案",
                        "reasoning": "基于深入分析",
                        "confidence": 0.85,
                    })
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

        mock_response = _make_mock_httpx_response(llm_response)
        mock_client = _make_mock_httpx_client(mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            proposer = LLMProposerStrategy(settings=mock_settings)
            arg = await proposer.generate_argument(
                topic="测试主题",
                history=[],
                current_proposal="初始提案",
            )

        assert arg.content == "LLM 生成的提案"
        assert arg.reasoning == "基于深入分析"
        assert arg.confidence == 0.85
        assert arg.role == DebateRole.PROPOSER

    async def test_metadata_recorded(self):
        """辩论完成后 metadata 应包含摘要信息"""
        engine = DebateEngine(use_llm=False, max_rounds=2, consensus_threshold=0.1)

        session = await engine.run_debate(
            topic="元数据测试",
            metadata={"custom_key": "custom_value"},
        )

        assert session.metadata.get("custom_key") == "custom_value"
        assert "debate_summary" in session.metadata
        summary = session.metadata["debate_summary"]
        assert "total_llm_calls" in summary
        assert "round_durations_sec" in summary


# ================================================================
# 4. Orchestrator 集成测试
# ================================================================


class TestOrchestratorIntegration:
    """调度中枢集成测试"""

    async def test_single_task_flow(self):
        """单任务流程：简单消息 -> 单 Agent 执行"""
        orchestrator = Orchestrator()

        mock_settings = _make_mock_settings(api_key="")
        mock_agent = _make_mock_agent("general")
        mock_agent.execute = AsyncMock(
            return_value=Result(
                task_id="test", success=True, content="你好！"
            )
        )

        with (
            patch("symbio.core.orchestrator.get_settings", return_value=mock_settings),
            patch.object(
                orchestrator.registry, "find_best", return_value=mock_agent
            ),
            patch.object(
                orchestrator.registry, "get", return_value=mock_agent
            ),
        ):
            message = Message(
                source=MessageSource.CLI,
                user_id="test-user",
                content="你好",
                session_id="test-session",
            )
            result = await orchestrator.process(message)

        assert result.success is True
        assert result.content == "你好！"

    async def test_multi_subtask_flow(self):
        """多子任务流程：mock decomposer 返回多个子任务，SubAgentManager 执行"""
        # 这个测试验证 decomposer + subagent manager 的集成
        llm_response_data = {
            "content": [
                {
                    "text": json.dumps({
                        "reasoning": "多步骤任务",
                        "needs_debate": False,
                        "subtasks": [
                            {
                                "name": "步骤1",
                                "description": "第一步",
                                "action": "chat",
                                "dependencies": [],
                                "estimated_complexity": "low",
                                "suggested_agent": "general",
                            },
                            {
                                "name": "步骤2",
                                "description": "第二步",
                                "action": "chat",
                                "dependencies": ["步骤1"],
                                "estimated_complexity": "medium",
                                "suggested_agent": "general",
                            },
                        ],
                    })
                }
            ],
            "usage": {"input_tokens": 50, "output_tokens": 100},
        }

        mock_response = _make_mock_httpx_response(llm_response_data)
        mock_client = _make_mock_httpx_client(mock_response)
        mock_settings = _make_mock_settings(api_key="test-key")

        with (
            patch("symbio.core.decomposer.get_settings", return_value=mock_settings),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            decomposer = TaskDecomposer()
            intent = Intent(raw_text="多步骤任务", action="chat")
            decomposition = await decomposer.decompose(intent, task_id="multi-001")

        # 验证分解结果
        assert len(decomposition.subtasks) == 2

        # 使用 SubAgentManager 执行
        registry = MagicMock(spec=AgentRegistry)
        event_bus = EventBus()
        rate_limiter = RateLimiter()
        manager = SubAgentManager(registry, event_bus, rate_limiter)

        mock_agent = _make_mock_agent("general")
        registry.get.return_value = mock_agent
        registry.find_best.return_value = mock_agent

        parent_task = _make_parent_task(task_id="multi-001")

        aggregated = await manager.execute_subtasks(
            subtasks=decomposition.subtasks,
            parent_task=parent_task,
            execution_order=decomposition.execution_order,
        )

        assert aggregated.success is True
        assert aggregated.total_subtasks == 2
        assert aggregated.completed_subtasks == 2

    async def test_debate_flow(self):
        """辩论流程：mock decomposer 标记 needs_debate=True，DebateEngine 执行"""
        llm_response_data = {
            "content": [
                {
                    "text": json.dumps({
                        "reasoning": "需要多方评估的决策",
                        "needs_debate": True,
                        "subtasks": [
                            {
                                "name": "评估方案",
                                "description": "对比评估多个方案",
                                "action": "analyze_data",
                                "dependencies": [],
                                "estimated_complexity": "high",
                                "suggested_agent": "general",
                            },
                        ],
                    })
                }
            ],
            "usage": {"input_tokens": 50, "output_tokens": 100},
        }

        mock_response = _make_mock_httpx_response(llm_response_data)
        mock_client = _make_mock_httpx_client(mock_response)
        mock_settings = _make_mock_settings(api_key="test-key")

        with (
            patch("symbio.core.decomposer.get_settings", return_value=mock_settings),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            decomposer = TaskDecomposer()
            intent = Intent(
                raw_text="对比评估三种数据库方案的优劣",
                action="chat",
            )
            decomposition = await decomposer.decompose(intent, task_id="debate-001")

        # 验证辩论标记
        assert decomposition.needs_debate is True

        # 使用 DebateEngine 进行辩论
        engine = DebateEngine(use_llm=False, max_rounds=2, consensus_threshold=0.1)
        session = await engine.run_debate(
            topic=decomposition.original_intent,
            metadata={"task_id": decomposition.task_id},
        )

        assert len(session.rounds) >= 1
        assert session.final_proposal != ""

    async def test_orchestrator_with_llm_intent_parsing(self):
        """Orchestrator 使用 LLM 解析意图的完整流程"""
        orchestrator = Orchestrator()

        mock_settings = _make_mock_settings(api_key="test-key")

        # Mock LLM 意图解析响应
        intent_response = _make_mock_httpx_response({
            "content": [
                {
                    "text": json.dumps({
                        "action": "code_review",
                        "parameters": {"language": "python"},
                        "requires_tools": [],
                        "requires_memory": False,
                    })
                }
            ],
            "usage": {"input_tokens": 30, "output_tokens": 20},
        })
        mock_client = _make_mock_httpx_client(intent_response)

        mock_agent = _make_mock_agent("code_reviewer")
        mock_agent.execute = AsyncMock(
            return_value=Result(
                task_id="test", success=True, content="代码审查完成"
            )
        )

        with (
            patch("symbio.core.orchestrator.get_settings", return_value=mock_settings),
            patch("httpx.AsyncClient", return_value=mock_client),
            patch.object(
                orchestrator.registry, "find_best", return_value=mock_agent
            ),
            patch.object(
                orchestrator.registry, "get", return_value=mock_agent
            ),
        ):
            message = Message(
                source=MessageSource.CLI,
                user_id="test-user",
                content="请审查这段 Python 代码",
                session_id="test-session",
            )
            result = await orchestrator.process(message)

        assert result.success is True
        assert "代码审查完成" in result.content

    async def test_orchestrator_fallback_intent(self):
        """Orchestrator 意图解析回退：LLM 不可用时降级到 chat"""
        orchestrator = Orchestrator()

        mock_settings = _make_mock_settings(api_key="")
        mock_agent = _make_mock_agent("general")
        mock_agent.execute = AsyncMock(
            return_value=Result(
                task_id="test", success=True, content="默认回复"
            )
        )

        with (
            patch("symbio.core.orchestrator.get_settings", return_value=mock_settings),
            patch.object(
                orchestrator.registry, "find_best", return_value=mock_agent
            ),
            patch.object(
                orchestrator.registry, "get", return_value=mock_agent
            ),
        ):
            message = Message(
                source=MessageSource.CLI,
                user_id="test-user",
                content="随便聊聊天",
                session_id="test-session",
            )
            result = await orchestrator.process(message)

        assert result.success is True

    async def test_orchestrator_no_agent_available(self):
        """Orchestrator 没有可用 Agent 时应返回失败"""
        orchestrator = Orchestrator()

        mock_settings = _make_mock_settings(api_key="")

        with (
            patch("symbio.core.orchestrator.get_settings", return_value=mock_settings),
            patch.object(
                orchestrator.registry, "find_best", return_value=None
            ),
            patch.object(
                orchestrator.registry, "get", return_value=None
            ),
        ):
            message = Message(
                source=MessageSource.CLI,
                user_id="test-user",
                content="测试消息",
                session_id="test-session",
            )
            result = await orchestrator.process(message)

        assert result.success is False
        assert "没有可用的 Agent" in result.content

    async def test_end_to_end_decompose_and_execute(self):
        """端到端：分解任务 -> 拓扑排序 -> SubAgentManager 执行"""
        # Mock LLM 返回 3 个子任务，其中 2 个并行，1 个依赖它们
        llm_response_data = {
            "content": [
                {
                    "text": json.dumps({
                        "reasoning": "三步任务",
                        "needs_debate": False,
                        "subtasks": [
                            {
                                "name": "收集数据",
                                "description": "收集所需数据",
                                "action": "search",
                                "dependencies": [],
                                "estimated_complexity": "low",
                                "suggested_agent": "general",
                            },
                            {
                                "name": "分析数据",
                                "description": "分析收集的数据",
                                "action": "analyze_data",
                                "dependencies": [],
                                "estimated_complexity": "medium",
                                "suggested_agent": "general",
                            },
                            {
                                "name": "生成报告",
                                "description": "基于分析结果生成报告",
                                "action": "write_code",
                                "dependencies": ["收集数据", "分析数据"],
                                "estimated_complexity": "medium",
                                "suggested_agent": "general",
                            },
                        ],
                    })
                }
            ],
            "usage": {"input_tokens": 50, "output_tokens": 100},
        }

        mock_response = _make_mock_httpx_response(llm_response_data)
        mock_client = _make_mock_httpx_client(mock_response)
        mock_settings = _make_mock_settings(api_key="test-key")

        # Step 1: 分解
        with (
            patch("symbio.core.decomposer.get_settings", return_value=mock_settings),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            decomposer = TaskDecomposer()
            intent = Intent(raw_text="收集数据，分析后生成报告", action="analyze_data")
            decomposition = await decomposer.decompose(intent, task_id="e2e-001")

        assert len(decomposition.subtasks) == 3
        assert len(decomposition.execution_order) == 2  # 2 个并行组

        # Step 2: 执行
        registry = MagicMock(spec=AgentRegistry)
        event_bus = EventBus()
        rate_limiter = RateLimiter()
        manager = SubAgentManager(registry, event_bus, rate_limiter)

        mock_agent = _make_mock_agent("general")
        registry.get.return_value = mock_agent
        registry.find_best.return_value = mock_agent

        parent_task = _make_parent_task(task_id="e2e-001")

        aggregated = await manager.execute_subtasks(
            subtasks=decomposition.subtasks,
            parent_task=parent_task,
            execution_order=decomposition.execution_order,
        )

        assert aggregated.success is True
        assert aggregated.total_subtasks == 3
        assert aggregated.completed_subtasks == 3
        assert mock_agent.execute.call_count == 3
