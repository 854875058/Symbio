from pathlib import Path
import sys

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.capabilities import get_capability_report
from symbio.interfaces.api import app


def test_capability_report_marks_claim_statuses_and_evidence():
    report = get_capability_report()

    assert report["summary"]["total"] >= 10
    assert report["summary"]["implemented"] >= 1
    assert report["summary"]["partial"] >= 1
    assert report["summary"]["missing"] >= 1

    items = {item["id"]: item for item in report["items"]}
    assert items["dynamic_dag"]["status"] == "implemented"
    assert "src/symbio/core/dag_runtime.py" in items["dynamic_dag"]["evidence"]
    assert items["hitl_im_approval"]["status"] == "implemented"
    assert "src/symbio/core/hitl_notifier.py" in items["hitl_im_approval"]["evidence"]
    assert items["a2a_protocol"]["status"] == "partial"
    assert items["computer_use_loop"]["status"] == "missing"

    for item in report["items"]:
        assert item["claim"]
        assert item["module"]
        assert item["next_step"]
        assert item["docs"]


@pytest.mark.asyncio
async def test_capabilities_api_exposes_claim_ledger():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/capabilities")

    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["total"] == len(data["items"])
    assert any(item["id"] == "dynamic_dag" for item in data["items"])
    assert any(item["status"] == "missing" for item in data["items"])


@pytest.mark.asyncio
async def test_observability_summary_reports_uninitialized_tracer():
    previous = getattr(app.state, "tracer", None)
    if hasattr(app.state, "tracer"):
        delattr(app.state, "tracer")
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/observability/summary")

        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["is_started"] is False
        assert data["spans"]["captured"] == 0
        assert data["metrics"]["records"] == 0
    finally:
        if previous is not None:
            app.state.tracer = previous


@pytest.mark.asyncio
async def test_observability_summary_exposes_runtime_counts():
    class FakeTracer:
        config = type("Config", (), {"service_name": "symbio-test", "exporter": "console"})()

        def is_started(self):
            return True

        def get_metric_records(self):
            return [{"name": "requests_total"}, {"name": "latency_ms"}]

        async def get_captured_spans(self):
            return [{"span_id": "s1"}, {"span_id": "s2"}, {"span_id": "s3"}]

        async def get_token_heatmap(self):
            return {"total_tokens": 42, "entries": [{"node_id": "n1"}]}

    previous = getattr(app.state, "tracer", None)
    app.state.tracer = FakeTracer()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/observability/summary")

        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["is_started"] is True
        assert data["service_name"] == "symbio-test"
        assert data["exporter"] == "console"
        assert data["spans"]["captured"] == 3
        assert data["metrics"]["records"] == 2
        assert data["tokens"]["total_tokens"] == 42
        assert data["tokens"]["entries"] == 1
    finally:
        if previous is not None:
            app.state.tracer = previous
        else:
            delattr(app.state, "tracer")
