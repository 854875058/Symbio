"""远程 Skills 源 —— 从 GitHub 搜索/拉取真实的 Agent Skills 接入本地市场。

Agent Skill 的事实标准（anthropics/skills 及 topic:agent-skills 生态）：一个技能 =
仓库里一个目录，含 ``SKILL.md``（YAML frontmatter: name/description/license）+ 配套
文件。本模块用 GitHub API：
- git tree 一次列出某仓库下所有 ``<skills_path>/<name>/SKILL.md`` → 技能清单
- contents + raw 拉取某技能目录的全部文件到本地
- 解析 SKILL.md frontmatter → 本地 SkillManifest（名字按本地 schema 规整）

HTTP 走代理（本机海外访问需 127.0.0.1:7897），session 可注入以便测试。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from symbio.skills.schema import SkillManifest
from symbio.utils.logger import get_logger

logger = get_logger("skills.remote_source")

GITHUB_API = "https://api.github.com"
DEFAULT_REPO = "anthropics/skills"
DEFAULT_SKILLS_PATH = "skills"


class RemoteSkill(BaseModel):
    """远程源里的一个技能候选。"""

    name: str
    description: str = ""
    repo: str
    path: str            # 仓库内目录，如 "skills/algorithmic-art"
    ref: str = "main"
    html_url: str = ""


class _HttpSession:
    """最小 HTTP 会话：GET JSON / 文本，走代理 + 可选 token。"""

    def __init__(self, proxy: Optional[str] = None, token: str = "", timeout: float = 20.0):
        import os

        self._proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
        self._token = token or os.environ.get("GITHUB_TOKEN", "")
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "symbio-skill-marketplace"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def get_json(self, url: str) -> Any:
        import httpx

        with httpx.Client(timeout=self._timeout, proxy=self._proxy or None) as client:
            resp = client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    def get_text(self, url: str) -> str:
        import httpx

        with httpx.Client(timeout=self._timeout, proxy=self._proxy or None) as client:
            resp = client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.text


class GitHubSkillSource:
    """从 GitHub 仓库列出并拉取 Agent Skills。"""

    def __init__(
        self,
        repo: str = DEFAULT_REPO,
        skills_path: str = DEFAULT_SKILLS_PATH,
        ref: str = "main",
        session: Any = None,
        proxy: Optional[str] = None,
    ) -> None:
        self.repo = repo
        self.skills_path = skills_path.strip("/")
        self.ref = ref
        self._session = session or _HttpSession(proxy=proxy)

    def list_skills(self, query: str = "", limit: int = 100) -> list[RemoteSkill]:
        """列出仓库下的技能（一次 git tree 调用）。query 为名字关键词过滤。"""
        url = f"{GITHUB_API}/repos/{self.repo}/git/trees/{self.ref}?recursive=1"
        data = self._session.get_json(url)
        tree = data.get("tree", []) if isinstance(data, dict) else []
        prefix = f"{self.skills_path}/" if self.skills_path else ""
        skills: list[RemoteSkill] = []
        seen: set[str] = set()
        for node in tree:
            path = str(node.get("path", ""))
            if not path.endswith("/SKILL.md"):
                continue
            skill_dir = path[: -len("/SKILL.md")]
            if prefix and not skill_dir.startswith(prefix):
                continue
            name = skill_dir.split("/")[-1]
            if name in seen:
                continue
            if query and query.lower() not in name.lower():
                continue
            seen.add(name)
            skills.append(RemoteSkill(
                name=name, repo=self.repo, path=skill_dir, ref=self.ref,
                html_url=f"https://github.com/{self.repo}/tree/{self.ref}/{skill_dir}",
            ))
            if len(skills) >= limit:
                break
        return skills

    def fetch_skill(self, remote: RemoteSkill, dest: str | Path) -> SkillManifest:
        """把某技能目录的所有文件拉到 dest，并解析 SKILL.md → SkillManifest。"""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        self._download_dir(remote.repo, remote.path, remote.ref, dest)
        skill_md = dest / "SKILL.md"
        front = _parse_frontmatter(skill_md.read_text(encoding="utf-8")) if skill_md.exists() else {}
        raw_name = str(front.get("name") or remote.name)
        description = str(front.get("description") or "")[:1024]
        return SkillManifest(
            name=_sanitize_name(raw_name),
            display_name=raw_name,
            version="1.0.0",
            description=description or f"Imported from {remote.repo}/{remote.path}",
            skill_type="custom",
            author=remote.repo.split("/")[0],
            license=str(front.get("license") or "")[:120],
            tags=["remote", "github"],
        )

    def _download_dir(self, repo: str, path: str, ref: str, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        url = f"{GITHUB_API}/repos/{repo}/contents/{path}?ref={ref}"
        entries = self._session.get_json(url)
        if not isinstance(entries, list):
            return
        for entry in entries:
            etype = entry.get("type")
            name = entry.get("name", "")
            if etype == "file" and entry.get("download_url"):
                content = self._session.get_text(entry["download_url"])
                (dest / name).write_text(content, encoding="utf-8")
            elif etype == "dir":
                self._download_dir(repo, entry.get("path", f"{path}/{name}"), ref, dest / name)


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """解析 SKILL.md 顶部的 YAML frontmatter（--- 包裹）。"""
    match = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _sanitize_name(raw: str) -> str:
    """把任意名字规整成本地 schema 允许的 ^[a-z][a-z0-9_]{2,63}$。"""
    s = re.sub(r"[^a-z0-9_]", "_", (raw or "").lower())
    s = re.sub(r"_+", "_", s).strip("_")
    if not s or not s[0].isalpha():
        s = "skill_" + s
    if len(s) < 3:
        s = (s + "_skill")
    return s[:64]
