import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.interfaces.api import app


@pytest.fixture(autouse=True)
def reset_marketplace_state(tmp_path):
    previous = getattr(app.state, "skill_marketplace", None)
    previous_dir = getattr(app.state, "skill_marketplace_dir", None)
    app.state.skill_marketplace = None
    app.state.skill_marketplace_dir = str(tmp_path / "marketplace")
    try:
        yield
    finally:
        if previous is not None:
            app.state.skill_marketplace = previous
        elif hasattr(app.state, "skill_marketplace"):
            delattr(app.state, "skill_marketplace")
        if previous_dir is not None:
            app.state.skill_marketplace_dir = previous_dir
        elif hasattr(app.state, "skill_marketplace_dir"):
            delattr(app.state, "skill_marketplace_dir")


@pytest.mark.asyncio
async def test_skill_marketplace_lists_seed_packages():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/skills/marketplace")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 3
    assert data["stats"]["total_packages"] >= 3
    names = {package["name"] for package in data["packages"]}
    assert {"code_review_plus", "dataset_exporter", "hitl_connector"}.issubset(names)
    assert all(package["package_id"] for package in data["packages"])


@pytest.mark.asyncio
async def test_skill_marketplace_search_filters_seed_packages():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/skills/marketplace", params={"q": "dataset"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert all("dataset" in package["name"] or "dataset" in package["description"].lower() for package in data["packages"])


@pytest.mark.asyncio
async def test_skill_marketplace_install_returns_installed_record():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        list_resp = await client.get("/api/skills/marketplace", params={"q": "hitl"})
        package_id = list_resp.json()["packages"][0]["package_id"]
        install_resp = await client.post(f"/api/skills/marketplace/{package_id}/install")

    assert install_resp.status_code == 200
    data = install_resp.json()
    assert data["success"] is True
    assert data["record"]["package_id"] == package_id
    assert data["record"]["status"] == "installed"
    assert data["record"]["install_path"]
