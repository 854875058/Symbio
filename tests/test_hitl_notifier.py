"""Tests for HITL external notification bridge."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.core.hitl_gateway import ApprovalRequest, RiskLevel
from symbio.core.hitl_notifier import (
    HITLNotificationTarget,
    HITLNotifier,
    approval_short_code,
    parse_im_approval_command,
)


def test_parse_im_approval_command_chinese_approve():
    request_id = "123e4567-e89b-12d3-a456-426614174000"
    command = parse_im_approval_command(f"同意 {request_id} 可以执行")

    assert command is not None
    assert command.action == "approve"
    assert command.request_id == request_id
    assert command.comment == "可以执行"


def test_parse_im_approval_command_reject():
    request_id = "123e4567-e89b-12d3-a456-426614174000"
    command = parse_im_approval_command(f"reject {request_id} too risky")

    assert command is not None
    assert command.action == "reject"
    assert command.comment == "too risky"


def test_parse_im_approval_command_short_code():
    command = parse_im_approval_command("approve A1B2C3D4 ok")

    assert command is not None
    assert command.action == "approve"
    assert command.request_id == "A1B2C3D4"
    assert command.comment == "ok"


def test_onebot_payload_uses_group_message_shape():
    request = ApprovalRequest(
        task_id="task-1",
        action="Delete files",
        impact_scope="workspace",
        reason="Risky operation",
        risk_level=RiskLevel.MEDIUM,
    )
    target = HITLNotificationTarget(
        platform="qq",
        endpoint="http://127.0.0.1:3000",
        chat_id="10001",
        chat_type="group",
    )
    notifier = HITLNotifier([target])

    url = notifier._url_for("qq", target)
    message = notifier.render_message(request)
    payload = notifier._payload_for("qq", target, request, message)

    assert url == "http://127.0.0.1:3000/send_group_msg"
    assert payload["group_id"] == "10001"
    code = approval_short_code(request.request_id)
    assert f"Code: {code}" in payload["message"]
    assert f" {code}" in payload["message"]
    assert f" {request.request_id}" not in payload["message"]
    assert "同意" in payload["message"]


def test_wechaty_payload_uses_text_message_shape():
    request = ApprovalRequest(task_id="task-2", action="Deploy", risk_level=RiskLevel.HIGH)
    target = HITLNotificationTarget(
        platform="wechaty",
        endpoint="http://127.0.0.1:9091/send",
        chat_id="ops-room",
    )
    notifier = HITLNotifier([target])

    message = notifier.render_message(request)
    payload = notifier._payload_for("wechaty", target, request, message)

    assert payload["to"] == "ops-room"
    assert payload["type"] == "text"
    assert "Deploy" in payload["text"]


def test_render_message_uses_short_code_for_im_commands():
    request = ApprovalRequest(
        request_id="123e4567-e89b-12d3-a456-426614174000",
        task_id="task-short-code",
        action="Restart service",
        risk_level=RiskLevel.MEDIUM,
    )
    notifier = HITLNotifier([])

    message = notifier.render_message(request)

    assert "Code: 123E4567" in message
    assert " 123E4567" in message
    assert " 123e4567-e89b-12d3-a456-426614174000" not in message
