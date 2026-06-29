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
    # 真安装：目录里有真实落地的文件
    install_dir = Path(data["record"]["install_path"])
    assert (install_dir / "SKILL.md").is_file()
    assert (install_dir / "manifest.json").is_file()


# ---------------------------------------------------------------------------
# 批次D1：真安装落地 + 记录持久化 + 源码打包（直连 SkillMarketplace）
# ---------------------------------------------------------------------------

from symbio.skills.marketplace import SkillMarketplace
from symbio.skills.registry import SkillRegistry
from symbio.skills.schema import SkillManifest


def _manifest(name: str) -> SkillManifest:
    return SkillManifest(
        name=name, display_name=name.replace("_", " ").title(), version="1.0.0",
        description="demo skill", skill_type="tool", author="t", license="MIT",
        tags=["x"], capabilities=["c"], entry_point="pkg.mod:Cls",
    )


def test_install_materializes_manifest_only_skill(tmp_path):
    mkt = SkillMarketplace(registry=SkillRegistry(), storage_dir=tmp_path / "mkt")
    pkg = mkt.publish_package(_manifest("demo_skill"))
    record = mkt.install(pkg.package_id)

    assert record.status.value == "installed"
    d = Path(record.install_path)
    assert (d / "manifest.json").is_file()
    assert (d / "SKILL.md").read_text(encoding="utf-8").strip()  # 非空

    # 记录持久化：重新实例化仍能读到已安装
    again = SkillMarketplace(registry=SkillRegistry(), storage_dir=tmp_path / "mkt")
    assert any(r.package_name == "demo_skill" for r in again.list_installed())


def test_publish_with_source_then_install_copies_real_files(tmp_path):
    src = tmp_path / "src_skill"
    src.mkdir()
    (src / "main.py").write_text("print('hi')", encoding="utf-8")
    (src / "SKILL.md").write_text("# Real Skill", encoding="utf-8")

    mkt = SkillMarketplace(registry=SkillRegistry(), storage_dir=tmp_path / "mkt")
    pkg = mkt.publish_package(_manifest("real_skill"), source_path=src)
    assert pkg.checksum and pkg.file_size_bytes > 0  # 打了包

    record = mkt.install(pkg.package_id)
    d = Path(record.install_path)
    assert (d / "main.py").read_text(encoding="utf-8") == "print('hi')"
    assert (d / "SKILL.md").read_text(encoding="utf-8") == "# Real Skill"


def test_uninstall_removes_install_dir(tmp_path):
    mkt = SkillMarketplace(registry=SkillRegistry(), storage_dir=tmp_path / "mkt")
    pkg = mkt.publish_package(_manifest("rm_skill"))
    record = mkt.install(pkg.package_id)
    d = Path(record.install_path)
    assert d.exists()

    assert mkt.uninstall("rm_skill") is True
    assert not d.exists()


# ---------------------------------------------------------------------------
# 批次D2b：远程源 API（注入假 GitHub session，不打真网络）
# ---------------------------------------------------------------------------

from symbio.skills.remote_source import GitHubSkillSource

_REMOTE_SKILL_MD = (
    "---\nname: algorithmic-art\ndescription: Generative art with p5.js\nlicense: MIT\n---\nbody\n"
)


class _FakeGhSession:
    def __init__(self):
        self._json = {
            "git/trees": {"tree": [
                {"path": "skills/algorithmic-art/SKILL.md", "type": "blob"},
                {"path": "skills/docx/SKILL.md", "type": "blob"},
            ]},
            "contents/skills/algorithmic-art": [
                {"name": "SKILL.md", "type": "file", "download_url": "https://raw/skillmd"},
                {"name": "LICENSE.txt", "type": "file", "download_url": "https://raw/license"},
            ],
        }
        self._text = {"skillmd": _REMOTE_SKILL_MD, "license": "MIT text"}

    def _lookup(self, store, url):
        for key in sorted(store, key=len, reverse=True):
            if key in url:
                return store[key]
        raise KeyError(url)

    def get_json(self, url):
        return self._lookup(self._json, url)

    def get_text(self, url):
        return self._lookup(self._text, url)


@pytest.fixture
def fake_remote_source():
    app.state.remote_skill_source_factory = lambda repo, ref="main": GitHubSkillSource(
        repo=repo, ref=ref, session=_FakeGhSession()
    )
    try:
        yield
    finally:
        if hasattr(app.state, "remote_skill_source_factory"):
            delattr(app.state, "remote_skill_source_factory")


@pytest.mark.asyncio
async def test_remote_search_lists_github_skills(fake_remote_source):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/skills/marketplace/remote", params={"q": "algo"})
    assert resp.status_code == 200
    data = resp.json()
    assert any(s["name"] == "algorithmic-art" for s in data["skills"])
    assert data["repo"] == "anthropics/skills"


@pytest.mark.asyncio
async def test_remote_install_materializes_skill(fake_remote_source):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/skills/marketplace/remote/install",
            json={"repo": "anthropics/skills", "path": "skills/algorithmic-art", "name": "algorithmic-art"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success"] is True
    assert data["record"]["status"] == "installed"
    assert (Path(data["record"]["install_path"]) / "SKILL.md").is_file()
