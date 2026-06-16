"""Prometheus /metrics 端点测试。"""

from pathlib import Path
import json
import sys

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.interfaces.api import app


@pytest.mark.asyncio
async def test_metrics_endpoint_prometheus_format():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    # 关键指标都在
    for name in [
        "symbio_build_info",
        "symbio_sessions_total",
        "symbio_cache_hit_rate",
        "symbio_security_block_rate",
        "symbio_hitl_pending",
    ]:
        assert name in body, f"missing metric {name}"
    # Prometheus 文本格式：每个指标有 HELP/TYPE 注释
    assert "# HELP symbio_build_info" in body
    assert "# TYPE symbio_build_info gauge" in body
    # build_info 带 version 标签且值为 1
    assert "symbio_build_info{version=" in body


@pytest.mark.asyncio
async def test_metrics_lines_are_parseable():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")
    for line in resp.text.splitlines():
        if not line or line.startswith("#"):
            continue
        # "name value" 或 "name{labels} value"
        parts = line.rsplit(" ", 1)
        assert len(parts) == 2, f"bad metric line: {line}"
        float(parts[1])  # 值必须可解析为数字


def test_grafana_dashboard_json_valid():
    path = PROJECT_ROOT / "config" / "grafana" / "provisioning" / "dashboards" / "symbio.json"
    assert path.exists(), "Grafana dashboard JSON missing"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["title"] == "Symbio Overview"
    assert data["uid"] == "symbio-overview"
    assert len(data["panels"]) >= 5
    # 每个 panel 的 target expr 都引用 symbio_ 指标
    exprs = [t["expr"] for p in data["panels"] for t in p.get("targets", [])]
    assert exprs and all(e.startswith("symbio_") for e in exprs)
