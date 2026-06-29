"""Skills 市场 - Skill 浏览、搜索与安装"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger
from symbio.skills.registry import SkillRegistry, SkillRegistration, SkillStatus, SkillType
from symbio.skills.schema import SkillManifest

logger = get_logger("skills.marketplace")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class PackageStatus(str, Enum):
    """包状态"""
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"
    YANKED = "yanked"


class InstallStatus(str, Enum):
    """安装状态"""
    PENDING = "pending"
    DOWNLOADING = "downloading"
    INSTALLING = "installing"
    INSTALLED = "installed"
    FAILED = "failed"
    UNINSTALLED = "uninstalled"


class SkillPackage(BaseModel):
    """Skill 包 (市场中的发布单元)"""
    package_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    display_name: str = ""
    description: str = ""
    version: str = "1.0.0"
    author: str = ""
    license: str = "MIT"
    tags: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    homepage: str = ""
    repository: str = ""
    icon_url: str = ""
    downloads: int = 0
    rating: float = Field(default=0.0, ge=0.0, le=5.0)
    review_count: int = 0
    status: PackageStatus = PackageStatus.PUBLISHED
    manifest: dict[str, Any] = Field(default_factory=dict)
    checksum: str = ""
    file_size_bytes: int = 0
    published_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PackageVersion(BaseModel):
    """包版本信息"""
    version: str
    changelog: str = ""
    published_at: datetime = Field(default_factory=datetime.now)
    yanked: bool = False
    yanked_reason: str = ""


class InstallRecord(BaseModel):
    """安装记录"""
    record_id: str = Field(default_factory=lambda: str(uuid4()))
    package_id: str
    package_name: str
    version: str
    status: InstallStatus = InstallStatus.PENDING
    installed_at: datetime = Field(default_factory=datetime.now)
    install_path: str = ""
    error: Optional[str] = None


class Review(BaseModel):
    """用户评价"""
    review_id: str = Field(default_factory=lambda: str(uuid4()))
    package_id: str
    user_id: str
    rating: int = Field(ge=1, le=5)
    title: str = ""
    content: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class SearchResult(BaseModel):
    """搜索结果"""
    packages: list[SkillPackage] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    has_more: bool = False


class MarketplaceStats(BaseModel):
    """市场统计"""
    total_packages: int = 0
    total_downloads: int = 0
    total_reviews: int = 0
    packages_by_category: dict[str, int] = Field(default_factory=dict)
    top_packages: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Skill 市场
# ---------------------------------------------------------------------------

class SkillMarketplace:
    """Skills 市场

    提供 Skill 的浏览、搜索、安装和管理功能。

    用法:
        marketplace = SkillMarketplace(registry=SkillRegistry())
        marketplace.publish_package(manifest, source_dir="./my_skill")
        results = marketplace.search("代码审查")
        marketplace.install(results.packages[0].package_id)
    """

    def __init__(
        self,
        registry: SkillRegistry | None = None,
        storage_dir: str | Path = "./marketplace",
    ):
        self._registry = registry or SkillRegistry()
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)

        self._packages_dir = self._storage_dir / "packages"   # 已发布包的源码存放处
        self._installed_file = self._storage_dir / "installed.json"

        self._packages: dict[str, SkillPackage] = {}
        self._versions: dict[str, list[PackageVersion]] = {}  # package_id -> versions
        self._install_records: list[InstallRecord] = []
        self._reviews: dict[str, list[Review]] = {}  # package_id -> reviews
        self._lock = threading.Lock()

        self._load_packages()
        self._load_install_records()

    def _load_packages(self) -> None:
        """从磁盘加载包信息"""
        index_file = self._storage_dir / "index.json"
        if index_file.exists():
            try:
                with open(index_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for pkg_data in data.get("packages", []):
                    pkg = SkillPackage(**pkg_data)
                    self._packages[pkg.package_id] = pkg
                logger.info(f"加载 {len(self._packages)} 个市场包")
            except Exception as exc:
                logger.error(f"加载市场索引失败: {exc}")

    def _save_index(self) -> None:
        """保存索引到磁盘"""
        index_file = self._storage_dir / "index.json"
        data = {
            "packages": [pkg.model_dump() for pkg in self._packages.values()],
            "updated_at": datetime.now().isoformat(),
        }
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    def publish_package(
        self,
        manifest: SkillManifest,
        source_path: str | Path | None = None,
        categories: list[str] | None = None,
        homepage: str = "",
        repository: str = "",
    ) -> SkillPackage:
        """发布 Skill 包到市场

        Args:
            manifest: Skill 清单
            source_path: 源码路径
            categories: 分类列表
            homepage: 主页 URL
            repository: 仓库 URL

        Returns:
            发布的包信息
        """
        # 验证清单
        errors = manifest.validate_against_schema()
        if errors:
            raise ValueError(f"清单验证失败: {errors}")

        package = SkillPackage(
            name=manifest.name,
            display_name=manifest.display_name or manifest.name.replace("_", " ").title(),
            description=manifest.description,
            version=manifest.version,
            author=manifest.author,
            license=manifest.license,
            tags=manifest.tags,
            categories=categories or [],
            homepage=homepage,
            repository=repository,
            manifest=manifest.model_dump(),
        )

        # 有源码就存进 packages/<id>/，安装时拷出，真正分发技能内容
        if source_path:
            src = Path(source_path)
            if src.exists():
                store = self._packages_dir / package.package_id
                store.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, store, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, store / src.name)
                package.file_size_bytes = _dir_size(store)
                package.checksum = _dir_checksum(store)

        with self._lock:
            self._packages[package.package_id] = package
            if package.package_id not in self._versions:
                self._versions[package.package_id] = []
            self._versions[package.package_id].append(
                PackageVersion(version=manifest.version)
            )

        self._save_index()
        logger.info(f"发布 Skill 包: {package.name} v{package.version}")
        return package

    def search(
        self,
        query: str = "",
        tags: list[str] | None = None,
        categories: list[str] | None = None,
        skill_type: str | None = None,
        sort_by: str = "downloads",
        page: int = 1,
        page_size: int = 20,
    ) -> SearchResult:
        """搜索 Skill 包

        Args:
            query: 搜索关键词
            tags: 按标签过滤
            categories: 按分类过滤
            skill_type: 按 Skill 类型过滤
            sort_by: 排序方式 (downloads, rating, newest)
            page: 页码
            page_size: 每页数量

        Returns:
            搜索结果
        """
        with self._lock:
            packages = [
                p for p in self._packages.values()
                if p.status == PackageStatus.PUBLISHED
            ]

        # 关键词搜索
        if query:
            query_lower = query.lower()
            packages = [
                p for p in packages
                if query_lower in p.name.lower()
                or query_lower in p.description.lower()
                or query_lower in p.display_name.lower()
                or any(query_lower in tag for tag in p.tags)
            ]

        # 标签过滤
        if tags:
            packages = [p for p in packages if any(t in p.tags for t in tags)]

        # 分类过滤
        if categories:
            packages = [p for p in packages if any(c in p.categories for c in categories)]

        # 排序
        if sort_by == "downloads":
            packages.sort(key=lambda p: p.downloads, reverse=True)
        elif sort_by == "rating":
            packages.sort(key=lambda p: p.rating, reverse=True)
        elif sort_by == "newest":
            packages.sort(key=lambda p: p.published_at, reverse=True)

        total = len(packages)
        start = (page - 1) * page_size
        end = start + page_size
        paginated = packages[start:end]

        return SearchResult(
            packages=paginated,
            total=total,
            page=page,
            page_size=page_size,
            has_more=end < total,
        )

    def get_package(self, package_id: str) -> SkillPackage | None:
        """获取包详情"""
        return self._packages.get(package_id)

    def get_package_versions(self, package_id: str) -> list[PackageVersion]:
        """获取包版本历史"""
        return self._versions.get(package_id, [])

    def install(
        self,
        package_id: str,
        install_dir: str | Path | None = None,
    ) -> InstallRecord:
        """安装 Skill 包

        Args:
            package_id: 包 ID
            install_dir: 安装目录

        Returns:
            安装记录
        """
        package = self._packages.get(package_id)
        if not package:
            raise ValueError(f"包不存在: {package_id}")

        record = InstallRecord(
            package_id=package_id,
            package_name=package.name,
            version=package.version,
            status=InstallStatus.INSTALLING,
        )

        try:
            # 确定安装路径并把技能内容真正写到磁盘（不再是空目录）
            target_dir = Path(install_dir) if install_dir else self._storage_dir / "installed" / package.name
            file_count = self._materialize_skill(package, target_dir)
            record.install_path = str(target_dir)

            # 创建注册信息
            registration = SkillRegistration(
                name=package.name,
                display_name=package.display_name,
                description=package.description,
                version=package.version,
                skill_type=SkillType(package.manifest.get("skill_type", "custom")),
                author=package.author,
                tags=package.tags,
                entry_point=package.manifest.get("entry_point", ""),
            )
            self._registry.register(registration)

            record.status = InstallStatus.INSTALLED

            # 更新下载量
            package.downloads += 1
            self._save_index()

            logger.info(f"安装 Skill: {package.name} v{package.version} -> {target_dir}（{file_count} 个文件）")

        except Exception as exc:
            record.status = InstallStatus.FAILED
            record.error = str(exc)
            logger.error(f"安装失败: {package.name} - {exc}")

        self._install_records.append(record)
        self._save_install_records()
        return record

    def _materialize_skill(self, package: SkillPackage, target_dir: Path) -> int:
        """把技能内容真正写到 target_dir：有源码就拷源码，否则按 manifest 生成
        manifest.json + SKILL.md，保证装完目录里有真实可看可用的内容。返回文件数。"""
        target_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        store = self._packages_dir / package.package_id
        if store.exists():
            for item in store.iterdir():
                dest = target_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)
                copied += 1
        if copied == 0:
            (target_dir / "manifest.json").write_text(
                json.dumps(package.manifest or {}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (target_dir / "SKILL.md").write_text(_render_skill_md(package), encoding="utf-8")
            copied = 2
        return copied

    def _load_install_records(self) -> None:
        if not self._installed_file.exists():
            return
        try:
            data = json.loads(self._installed_file.read_text(encoding="utf-8"))
            self._install_records = [InstallRecord(**item) for item in data]
        except Exception as exc:
            logger.error(f"加载安装记录失败: {exc}")
            self._install_records = []

    def _save_install_records(self) -> None:
        try:
            self._installed_file.write_text(
                json.dumps([r.model_dump() for r in self._install_records],
                           ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:  # pragma: no cover
            logger.error(f"保存安装记录失败: {exc}")

    def uninstall(self, package_name: str) -> bool:
        """卸载 Skill 包

        Args:
            package_name: 包名称

        Returns:
            是否成功
        """
        # 删除安装目录 + 标记记录卸载
        removed_dir = False
        for record in self._install_records:
            if record.package_name == package_name and record.status == InstallStatus.INSTALLED:
                record.status = InstallStatus.UNINSTALLED
                path = Path(record.install_path)
                if record.install_path and path.exists():
                    shutil.rmtree(path, ignore_errors=True)
                    removed_dir = True
        self._save_install_records()

        skill = self._registry.get_by_name(package_name)
        if skill:
            self._registry.unregister(skill.skill_id)

        if not skill and not removed_dir:
            logger.warning(f"未找到已安装的 Skill: {package_name}")
            return False
        logger.info(f"卸载 Skill: {package_name}")
        return True

    def list_installed(self) -> list[InstallRecord]:
        """列出已安装的包"""
        return [r for r in self._install_records if r.status == InstallStatus.INSTALLED]

    def add_review(
        self,
        package_id: str,
        user_id: str,
        rating: int,
        title: str = "",
        content: str = "",
    ) -> Review:
        """添加评价"""
        package = self._packages.get(package_id)
        if not package:
            raise ValueError(f"包不存在: {package_id}")

        review = Review(
            package_id=package_id,
            user_id=user_id,
            rating=rating,
            title=title,
            content=content,
        )

        with self._lock:
            if package_id not in self._reviews:
                self._reviews[package_id] = []
            self._reviews[package_id].append(review)

            # 更新包的评分
            reviews = self._reviews[package_id]
            package.rating = sum(r.rating for r in reviews) / len(reviews)
            package.review_count = len(reviews)

        self._save_index()
        logger.info(f"添加评价: {package.name} - {rating}星")
        return review

    def get_reviews(self, package_id: str) -> list[Review]:
        """获取包的评价"""
        return self._reviews.get(package_id, [])

    def get_categories(self) -> list[str]:
        """获取所有分类"""
        categories: set[str] = set()
        for pkg in self._packages.values():
            categories.update(pkg.categories)
        return sorted(categories)

    def get_popular_tags(self, limit: int = 20) -> list[dict[str, Any]]:
        """获取热门标签"""
        tag_counts: dict[str, int] = {}
        for pkg in self._packages.values():
            for tag in pkg.tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

        sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
        return [{"tag": tag, "count": count} for tag, count in sorted_tags[:limit]]

    def get_statistics(self) -> MarketplaceStats:
        """获取市场统计"""
        published = [p for p in self._packages.values() if p.status == PackageStatus.PUBLISHED]
        total_downloads = sum(p.downloads for p in published)
        total_reviews = sum(len(r) for r in self._reviews.values())

        categories: dict[str, int] = {}
        for pkg in published:
            for cat in pkg.categories:
                categories[cat] = categories.get(cat, 0) + 1

        top = sorted(published, key=lambda p: p.downloads, reverse=True)[:10]
        top_info = [
            {"name": p.name, "version": p.version, "downloads": p.downloads, "rating": p.rating}
            for p in top
        ]

        return MarketplaceStats(
            total_packages=len(published),
            total_downloads=total_downloads,
            total_reviews=total_reviews,
            packages_by_category=categories,
            top_packages=top_info,
        )


def _render_skill_md(package: SkillPackage) -> str:
    """从 manifest 生成一份可读的 SKILL.md（manifest-only 包安装时落地）。"""
    manifest = package.manifest or {}
    caps = manifest.get("capabilities") or []
    entry = manifest.get("entry_point", "")
    lines = [
        f"# {package.display_name or package.name}",
        "",
        package.description or "（无描述）",
        "",
        f"- 版本：{package.version}",
        f"- 作者：{package.author or '未知'}",
        f"- 许可证：{package.license}",
    ]
    if entry:
        lines.append(f"- 入口：`{entry}`")
    if package.tags:
        lines.append(f"- 标签：{', '.join(package.tags)}")
    lines += ["", "## 能力", ""]
    lines += [f"- {c}" for c in caps] if caps else ["（未声明）"]
    lines.append("")
    return "\n".join(lines)


def _dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _dir_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(path.rglob("*")):
        if file.is_file():
            digest.update(file.relative_to(path).as_posix().encode("utf-8"))
            digest.update(file.read_bytes())
    return digest.hexdigest()[:16]
