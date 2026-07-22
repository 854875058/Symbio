"""Focused HITL persistence and payload tests."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.core.hitl_gateway import ApprovalGateway, ApprovalRequest, RiskLevel
from symbio.core.hitl_notifier import HITLNotifier, HITLNotificationTarget, approval_short_code
from symbio.core.orchestrator import Orchestrator
from symbio.interfaces.api import _hitl_request_payload
from symbio.utils.types import Message, MessageSource, Result


def _make_mock_settings(api_key: str = ""):
    class _Model:
        anthropic_api_key = api_key
        anthropic_base_url = "https://api.anthropic.com"
        openai_api_key = ""
        openai_base_url = "https://api.openai.com/v1"
        model_low = "claude-3-5-haiku-20241022"
        model_medium = "claude-sonnet-4-20250514"
        model_high = "claude-opus-4-20250514"

    class _Settings:
        model = _Model()

    return _Settings()


class _FakeDAGOrchestrator:
    def __init__(self, content: str):
        self.execute = AsyncMock(
            return_value=Result(
                task_id="resumed-task",
                success=True,
                content=content,
            )
        )


async def test_hitl_gateway_persists_pending_request_and_task_context(tmp_path):
    persist_path = tmp_path / "hitl.db"
    gateway = ApprovalGateway(persist_path=str(persist_path))
    request = ApprovalRequest(
        task_id="task-hitl-persist",
        action="Run risky task",
        impact_scope="workspace",
        reason="Need durable approval state",
        risk_level=RiskLevel.MEDIUM,
        metadata={"workflow_policy": {"require_plan": True}},
    )

    request_id = await gateway.submit_request(request)
    await gateway.attach_task_context(
        request_id,
        {"task_id": "task-hitl-persist", "metadata": {"risk_level": "medium"}},
    )
    await gateway.close()

    restored_gateway = ApprovalGateway(persist_path=str(persist_path))
    restored_request = await restored_gateway.get_request(request_id)
    restored_context = await restored_gateway.get_task_context(request_id)

    assert restored_request is not None
    assert restored_request.status.value == "pending"
    assert restored_request.metadata["workflow_policy"]["require_plan"] is True
    assert restored_context is not None
    assert restored_context["task_id"] == "task-hitl-persist"

    await restored_gateway.close()


async def test_orchestrator_resume_after_restart_uses_persisted_hitl_task_context(tmp_path):
    persist_path = tmp_path / "hitl.db"

    orchestrator = Orchestrator()
    orchestrator.hitl_gateway = ApprovalGateway(persist_path=str(persist_path))
    orchestrator.initialize_memory = AsyncMock()
    orchestrator.memory_bridge.enhance_context = AsyncMock(return_value="")
    orchestrator.dag_orchestrator = _FakeDAGOrchestrator("resumed after restart")

    message = Message(
        source=MessageSource.CLI,
        user_id="test-user",
        content="Run a risky operation",
        session_id="test-session",
        metadata={"risk_level": "medium"},
    )

    with patch(
        "symbio.core.orchestrator.get_settings", return_value=_make_mock_settings(api_key="")
    ):
        pending_result = await orchestrator.process(message)

    request_id = pending_result.data["hitl_request_id"]
    await orchestrator.hitl_gateway.approve(request_id, approver_id="alice")
    await orchestrator.hitl_gateway.close()

    restarted = Orchestrator()
    restarted.hitl_gateway = ApprovalGateway(persist_path=str(persist_path))
    restarted.dag_orchestrator = _FakeDAGOrchestrator("resumed after restart")

    resumed = await restarted.resume_after_approval(request_id)

    assert resumed is not None
    assert resumed.content == "resumed after restart"
    assert resumed.data["hitl_approved"] is True
    assert resumed.data["hitl_request_id"] == request_id
    restarted.dag_orchestrator.execute.assert_awaited_once()

    assert await restarted.hitl_gateway.get_task_context(request_id) is None
    await restarted.hitl_gateway.close()


async def test_orchestrator_hitl_notification_audit_payload_persists(tmp_path):
    persist_path = tmp_path / "hitl.db"
    orchestrator = Orchestrator()
    orchestrator.hitl_gateway = ApprovalGateway(persist_path=str(persist_path))
    orchestrator.hitl_notifier = HITLNotifier(
        targets=[
            HITLNotificationTarget(
                platform="qq",
                chat_id="symbio-approvers",
                chat_type="group",
            )
        ],
        callback_base_url="https://symbio.example",
    )
    orchestrator.initialize_memory = AsyncMock()
    orchestrator.memory_bridge.enhance_context = AsyncMock(return_value="")
    orchestrator.dag_orchestrator = _FakeDAGOrchestrator("should not run before approval")

    message = Message(
        source=MessageSource.CLI,
        user_id="test-user",
        content="Run a risky operation",
        session_id="test-session",
        metadata={"risk_level": "medium"},
    )

    with patch(
        "symbio.core.orchestrator.get_settings", return_value=_make_mock_settings(api_key="")
    ):
        pending_result = await orchestrator.process(message)

    request_id = pending_result.data["hitl_request_id"]
    stored = await orchestrator.hitl_gateway.get_request(request_id)
    payload = _hitl_request_payload(stored)

    assert stored is not None
    assert stored.metadata["notification_status"] == "prepared"
    assert stored.metadata["notifications"][0]["platform"] == "qq"
    assert stored.metadata["notifications"][0]["channel"] == "symbio-approvers"
    assert payload["notification_status"] == "prepared"
    assert payload["notification_count"] == 1
    assert payload["latest_notification"]["request_id"] == request_id

    await orchestrator.hitl_gateway.close()


def test_hitl_request_payload_exposes_metadata_and_approval_counters():
    request = ApprovalRequest(
        task_id="task-hitl-payload",
        action="Delete generated files",
        impact_scope="workspace",
        reason="Needs review",
        risk_level=RiskLevel.MEDIUM,
        metadata={"workflow_policy": {"require_plan": True}},
    )

    payload = _hitl_request_payload(request)

    assert payload["metadata"]["workflow_policy"]["require_plan"] is True
    assert payload["approval_count"] == 0
    assert payload["pending_approvals"] == 1


async def test_hitl_notification_audit_payload_persists_with_request(tmp_path):
    persist_path = tmp_path / "hitl.db"
    gateway = ApprovalGateway(persist_path=str(persist_path))
    notifier = HITLNotifier(
        targets=[
            HITLNotificationTarget(
                platform="wechat",
                chat_id="ops-mobile",
                chat_type="group",
            )
        ],
        callback_base_url="https://symbio.example",
    )
    request = ApprovalRequest(
        task_id="task-hitl-notify-persist",
        action="Restart payment service",
        impact_scope="payment-service",
        reason="High impact mobile approval required",
        risk_level=RiskLevel.MEDIUM,
    )

    request_id = await gateway.submit_request(request)
    stored = await gateway.get_request(request_id)
    assert stored is not None

    notifications = await notifier.prepare_notifications(stored)
    stored.metadata["notifications"] = notifications
    await gateway.update_request(stored)
    await gateway.close()

    restored_gateway = ApprovalGateway(persist_path=str(persist_path))
    restored = await restored_gateway.get_request(request_id)

    assert restored is not None
    notification = restored.metadata["notifications"][0]
    assert notification["platform"] == "wechat"
    assert notification["recipient"] == "ops-mobile"
    assert notification["request_id"] == request_id
    assert notification["short_code"] == approval_short_code(request_id)
    assert notification["approve_command"].startswith("approve ")
    assert notification["reject_command"].startswith("reject ")
    assert notification["api_path"] == "/api/hitl/im-callback"
    assert notification["callback_url"] == "https://symbio.example/api/hitl/im-callback"

    await restored_gateway.close()
