"""
HITL (Human-in-the-Loop) Async Approval Gateway

非阻塞式审批网关，支持风险分级、自动审批、防疲劳、Webhook 回调。
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    """风险等级"""
    LOW = "low"               # auto-approve
    MEDIUM = "medium"         # 1 approver, 1 hour timeout
    HIGH = "high"             # 2 approvers, 2 hours
    CRITICAL = "critical"     # 3 approvers, 24 hours


class ApprovalStatus(str, Enum):
    """审批状态"""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    AUTO_APPROVED = "auto_approved"


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

class ApprovalRecord(BaseModel):
    """审批记录"""
    approver_id: str
    decision: ApprovalStatus
    comment: str = ""
    timestamp: datetime = Field(default_factory=datetime.now)


class ApprovalRequest(BaseModel):
    """审批请求 - 六要素

    六要素:
        1. action         -- 做什么
        2. impact_scope   -- 影响范围
        3. reason         -- 为什么
        4. alternatives   -- 替代方案
        5. risk_level     -- 风险等级
        6. timeout_seconds -- 超时时间
    """
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    node_id: str = ""  # DAG node id

    # 六要素
    action: str = ""
    impact_scope: str = ""
    reason: str = ""
    alternatives: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    timeout_seconds: int = 3600
    auto_approve_on_timeout: bool = False

    # 状态
    status: ApprovalStatus = ApprovalStatus.PENDING
    required_approvers: int = 1
    approvals: list[ApprovalRecord] = Field(default_factory=list)

    # 时间
    created_at: datetime = Field(default_factory=datetime.now)
    resolved_at: Optional[datetime] = None

    # 回调
    callback_url: str = ""
    jwt_token: str = ""


class WebhookPayload(BaseModel):
    """Webhook 回调载荷"""
    request_id: str
    status: ApprovalStatus
    approver_id: str = ""
    comment: str = ""
    token: str = ""


# ---------------------------------------------------------------------------
# Risk Configuration
# ---------------------------------------------------------------------------

_RISK_APPROVERS: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}

_RISK_TIMEOUT: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 3600,      # 1 hour
    RiskLevel.HIGH: 7200,        # 2 hours
    RiskLevel.CRITICAL: 86400,   # 24 hours
}


# ---------------------------------------------------------------------------
# JWT-like Token Helpers
# ---------------------------------------------------------------------------

def generate_approval_token(
    request_id: str,
    secret: str = "symbio-secret",
    ttl_seconds: int = 86400,
) -> str:
    """Generate a simple HMAC-based approval token.

    The token is ``base64(json_payload.signature)`` so that the caller can
    decode and verify without an external JWT library.
    """
    payload = json.dumps({
        "request_id": request_id,
        "exp": time.time() + ttl_seconds,
    })
    signature = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256,
    ).hexdigest()
    token = base64.b64encode(f"{payload}.{signature}".encode()).decode()
    return token


def verify_approval_token(
    token: str,
    secret: str = "symbio-secret",
) -> Optional[str]:
    """Verify an approval token.

    Returns the ``request_id`` if valid and not expired, otherwise ``None``.
    """
    try:
        decoded = base64.b64decode(token.encode()).decode()
        payload_str, signature = decoded.rsplit(".", 1)
        expected_sig = hmac.new(
            secret.encode(), payload_str.encode(), hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            return None
        payload = json.loads(payload_str)
        if payload.get("exp", 0) < time.time():
            return None
        return payload.get("request_id")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Approval Gateway
# ---------------------------------------------------------------------------

class ApprovalGateway:
    """Async approval gateway -- non-blocking HITL.

    Usage::

        gateway = ApprovalGateway()
        request_id = await gateway.submit_request(request)
        # ... later ...
        await gateway.approve(request_id, approver_id="alice")
    """

    def __init__(self) -> None:
        self._pending: dict[str, ApprovalRequest] = {}
        self._history: list[ApprovalRequest] = []
        self._lock = asyncio.Lock()
        self._callbacks: dict[str, list[Callable]] = {}

    # -- public API ---------------------------------------------------------

    async def submit_request(self, request: ApprovalRequest) -> str:
        """Submit an approval request. Returns ``request_id``.

        LOW-risk requests are auto-approved immediately.
        """
        # Auto-approve LOW risk
        if request.risk_level == RiskLevel.LOW:
            request.status = ApprovalStatus.AUTO_APPROVED
            request.resolved_at = datetime.now()
            self._history.append(request)
            # Fire callback if registered
            await self._fire_callbacks(request)
            return request.request_id

        # Set required approvers based on risk
        request.required_approvers = _RISK_APPROVERS[request.risk_level]

        # Set timeout based on risk (caller can still override beforehand)
        if request.timeout_seconds == 3600:  # only override if still default
            request.timeout_seconds = _RISK_TIMEOUT[request.risk_level]

        # Generate token for webhook
        if request.callback_url and not request.jwt_token:
            request.jwt_token = generate_approval_token(request.request_id)

        async with self._lock:
            self._pending[request.request_id] = request

        # Start timeout timer
        asyncio.create_task(self._timeout_handler(request))

        return request.request_id

    async def approve(
        self,
        request_id: str,
        approver_id: str,
        comment: str = "",
    ) -> ApprovalRequest:
        """Record an approval. Resolves the request when enough approvals accumulate."""
        async with self._lock:
            request = self._pending.get(request_id)
            if request is None:
                raise KeyError(f"Request {request_id} not found or already resolved")

            # Prevent duplicate approval from same approver
            if any(a.approver_id == approver_id for a in request.approvals):
                return request

            record = ApprovalRecord(
                approver_id=approver_id,
                decision=ApprovalStatus.APPROVED,
                comment=comment,
            )
            request.approvals.append(record)

            # Check quorum
            approved_count = sum(
                1 for a in request.approvals
                if a.decision == ApprovalStatus.APPROVED
            )
            if approved_count >= request.required_approvers:
                request.status = ApprovalStatus.APPROVED
                request.resolved_at = datetime.now()
                del self._pending[request_id]
                self._history.append(request)
                await self._fire_callbacks(request)

            return request

    async def reject(
        self,
        request_id: str,
        approver_id: str,
        comment: str = "",
    ) -> ApprovalRequest:
        """Reject a request (immediate resolution)."""
        async with self._lock:
            request = self._pending.get(request_id)
            if request is None:
                raise KeyError(f"Request {request_id} not found or already resolved")

            record = ApprovalRecord(
                approver_id=approver_id,
                decision=ApprovalStatus.REJECTED,
                comment=comment,
            )
            request.approvals.append(record)
            request.status = ApprovalStatus.REJECTED
            request.resolved_at = datetime.now()

            del self._pending[request_id]
            self._history.append(request)

        await self._fire_callbacks(request)
        return request

    async def get_pending(self) -> list[ApprovalRequest]:
        """Return all pending approval requests."""
        async with self._lock:
            return list(self._pending.values())

    async def get_request(self, request_id: str) -> Optional[ApprovalRequest]:
        """Fetch a request by id (pending or history)."""
        async with self._lock:
            if request_id in self._pending:
                return self._pending[request_id]
        for req in self._history:
            if req.request_id == request_id:
                return req
        return None

    async def get_history(self) -> list[ApprovalRequest]:
        """Return all resolved requests."""
        return list(self._history)

    def on_resolved(self, request_id: str, callback: Callable) -> None:
        """Register a callback to be invoked when a request is resolved."""
        self._callbacks.setdefault(request_id, []).append(callback)

    # -- internal -----------------------------------------------------------

    async def _timeout_handler(self, request: ApprovalRequest) -> None:
        """Sleep until timeout, then resolve if still pending."""
        await asyncio.sleep(request.timeout_seconds)
        async with self._lock:
            if request.request_id not in self._pending:
                return  # already resolved
            if request.auto_approve_on_timeout:
                # Treat as system approval
                record = ApprovalRecord(
                    approver_id="system",
                    decision=ApprovalStatus.APPROVED,
                    comment="Auto-approved on timeout",
                )
                request.approvals.append(record)
                request.status = ApprovalStatus.APPROVED
            else:
                request.status = ApprovalStatus.TIMEOUT
            request.resolved_at = datetime.now()
            del self._pending[request.request_id]
            self._history.append(request)
        await self._fire_callbacks(request)

    async def _fire_callbacks(self, request: ApprovalRequest) -> None:
        """Invoke registered callbacks for a resolved request."""
        cbs = self._callbacks.pop(request.request_id, [])
        for cb in cbs:
            try:
                result = cb(request)
                # Support both sync and async callbacks
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass  # callbacks should not crash the gateway


# ---------------------------------------------------------------------------
# Fatigue Prevention
# ---------------------------------------------------------------------------

class FatiguePreventer:
    """Prevent approval fatigue by merging similar requests.

    Within a configurable time window, if a new request has a similar
    ``action`` and ``impact_scope`` to an existing pending request, it
    should be merged rather than creating a separate approval ticket.
    """

    def __init__(self, merge_window_seconds: int = 300) -> None:
        self._merge_window = merge_window_seconds
        self._recent_requests: list[ApprovalRequest] = []

    def should_merge(self, new_request: ApprovalRequest) -> Optional[str]:
        """Check whether *new_request* should be merged into a recent one.

        Returns the existing ``request_id`` to merge into, or ``None``.
        """
        now = datetime.now()
        window = timedelta(seconds=self._merge_window)

        # Prune old entries
        self._recent_requests = [
            r for r in self._recent_requests
            if (now - r.created_at) < window
        ]

        for existing in self._recent_requests:
            if (
                existing.status == ApprovalStatus.PENDING
                and existing.action == new_request.action
                and existing.impact_scope == new_request.impact_scope
            ):
                return existing.request_id

        # Record for future comparisons
        self._recent_requests.append(new_request)
        return None

    def should_auto_approve(self, request: ApprovalRequest) -> bool:
        """Decide whether a request can be auto-approved based on heuristics.

        Current policy: auto-approve if risk is LOW *or* if the exact same
        action+impact pair has been approved at least 3 times in recent history
        without any rejection.
        """
        if request.risk_level == RiskLevel.LOW:
            return True

        # Count recent approvals for the same action+impact
        approval_count = sum(
            1 for r in self._recent_requests
            if (
                r.action == request.action
                and r.impact_scope == request.impact_scope
                and r.status in (ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED)
            )
        )
        rejection_count = sum(
            1 for r in self._recent_requests
            if (
                r.action == request.action
                and r.impact_scope == request.impact_scope
                and r.status == ApprovalStatus.REJECTED
            )
        )
        return approval_count >= 3 and rejection_count == 0
