"""批次D2a：从 GitHub 远程源拉取 Agent Skills（注入假 HTTP 会话）。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.skills.marketplace import SkillMarketplace
from symbio.skills.registry import SkillRegistry
from symbio.skills.remote_source import (
    GitHubSkillSource,
    _parse_frontmatter,
    _sanitize_name,
)


class FakeSession:
    """按 URL 子串返回预置响应；优先匹配更长（更具体）的 key。"""

    def __init__(self, json_map: dict, text_map: dict):
        self.json_map = json_map
        self.text_map = text_map

    def _lookup(self, store: dict, url: str):
        for key in sorted(store, key=len, reverse=True):
            if key in url:
                return store[key]
        raise KeyError(url)

    def get_json(self, url: str):
        return self._lookup(self.json_map, url)

    def get_text(self, url: str):
        return self._lookup(self.text_map, url)


_SKILL_MD = (
    "---\n"
    "name: algorithmic-art\n"
    "description: Create generative art with p5.js\n"
    "license: MIT\n"
    "---\n"
    "Body of the skill.\n"
)


def _fake_source() -> GitHubSkillSource:
    json_map = {
        "git/trees": {"tree": [
            {"path": "skills/algorithmic-art/SKILL.md", "type": "blob"},
            {"path": "skills/algorithmic-art/templates/sketch.html", "type": "blob"},
            {"path": "skills/docx/SKILL.md", "type": "blob"},
            {"path": "README.md", "type": "blob"},
        ]},
        "contents/skills/algorithmic-art/templates": [
            {"name": "sketch.html", "type": "file", "download_url": "https://raw/sketch", "path": "skills/algorithmic-art/templates/sketch.html"},
        ],
        "contents/skills/algorithmic-art": [
            {"name": "SKILL.md", "type": "file", "download_url": "https://raw/skillmd", "path": "skills/algorithmic-art/SKILL.md"},
            {"name": "LICENSE.txt", "type": "file", "download_url": "https://raw/license", "path": "skills/algorithmic-art/LICENSE.txt"},
            {"name": "templates", "type": "dir", "path": "skills/algorithmic-art/templates"},
        ],
    }
    text_map = {"skillmd": _SKILL_MD, "license": "MIT text", "sketch": "<html></html>"}
    return GitHubSkillSource(repo="anthropics/skills", session=FakeSession(json_map, text_map))


def test_parse_frontmatter_and_sanitize():
    fm = _parse_frontmatter(_SKILL_MD)
    assert fm["name"] == "algorithmic-art"
    assert "p5.js" in fm["description"]
    assert _sanitize_name("algorithmic-art") == "algorithmic_art"
    assert _sanitize_name("3D-Tool") == "skill_3d_tool"  # 首字符非字母 -> 加前缀
    assert _parse_frontmatter("no frontmatter here") == {}


def test_list_skills_from_tree_with_filter():
    src = _fake_source()
    skills = src.list_skills()
    names = {s.name for s in skills}
    assert names == {"algorithmic-art", "docx"}
    art = next(s for s in skills if s.name == "algorithmic-art")
    assert art.path == "skills/algorithmic-art"
    assert art.repo == "anthropics/skills"
    # 关键词过滤
    assert {s.name for s in src.list_skills(query="docx")} == {"docx"}
    assert src.list_skills(query="zzz") == []


def test_fetch_skill_downloads_files_and_builds_manifest(tmp_path):
    src = _fake_source()
    remote = src.list_skills(query="algorithmic")[0]
    manifest = src.fetch_skill(remote, tmp_path / "dl")

    # 文件真下载（含子目录递归）
    assert (tmp_path / "dl" / "SKILL.md").is_file()
    assert (tmp_path / "dl" / "LICENSE.txt").read_text(encoding="utf-8") == "MIT text"
    assert (tmp_path / "dl" / "templates" / "sketch.html").is_file()
    # manifest 由 frontmatter 构建，名字规整
    assert manifest.name == "algorithmic_art"
    assert manifest.display_name == "algorithmic-art"
    assert "p5.js" in manifest.description
    assert manifest.author == "anthropics"


def test_install_from_remote_end_to_end(tmp_path):
    src = _fake_source()
    remote = src.list_skills(query="algorithmic")[0]
    mkt = SkillMarketplace(registry=SkillRegistry(), storage_dir=tmp_path / "mkt")

    record = mkt.install_from_remote(src, remote)

    assert record.status.value == "installed"
    install_dir = Path(record.install_path)
    # 远程拉下来的真实文件被装到本地
    assert (install_dir / "SKILL.md").is_file()
    assert (install_dir / "templates" / "sketch.html").is_file()
    # 已进入本地市场目录
    assert any(p.name == "algorithmic_art" for p in mkt.search().packages) or \
        any("algorithmic" in p.name for p in mkt.search().packages)
