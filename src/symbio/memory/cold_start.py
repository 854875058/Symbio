"""冷启动代码仓库扫描器 - 扫描项目仓库并自动填充记忆系统"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from symbio.memory.manager import MemoryManager, MemoryType, MemoryPriority
from symbio.utils.logger import get_logger

logger = get_logger("cold_start")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class Dependency(BaseModel):
    """依赖信息"""

    name: str
    version: str = ""
    source: str = ""  # "package.json", "requirements.txt", "pyproject.toml"
    is_dev: bool = False


class DockerService(BaseModel):
    """Docker Compose 服务"""

    name: str
    image: str = ""
    ports: list[str] = Field(default_factory=list)
    volumes: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class DockerInfo(BaseModel):
    """Docker 配置信息"""

    has_dockerfile: bool = False
    has_compose: bool = False
    services: list[DockerService] = Field(default_factory=list)
    base_images: list[str] = Field(default_factory=list)
    exposed_ports: list[str] = Field(default_factory=list)
    volumes: list[str] = Field(default_factory=list)


class FileTypeInfo(BaseModel):
    """文件类型统计"""

    extension: str
    count: int
    total_size_kb: float = 0.0


class ProjectStructure(BaseModel):
    """项目结构信息"""

    root_name: str = ""
    total_files: int = 0
    total_dirs: int = 0
    file_types: list[FileTypeInfo] = Field(default_factory=list)
    top_level_dirs: list[str] = Field(default_factory=list)
    top_level_files: list[str] = Field(default_factory=list)
    has_src_dir: bool = False
    has_tests_dir: bool = False
    has_docs_dir: bool = False
    has_ci_config: bool = False
    languages: list[str] = Field(default_factory=list)


class ScanResult(BaseModel):
    """扫描结果"""

    repo_path: str
    repo_name: str = ""
    scan_time: datetime = Field(default_factory=datetime.now)
    project_description: str = ""
    dependencies: list[Dependency] = Field(default_factory=list)
    structure: Optional[ProjectStructure] = None
    docker_info: Optional[DockerInfo] = None
    env_example_content: str = ""
    readme_content: str = ""
    k8s_manifests: list[str] = Field(default_factory=list)
    summary: str = ""

    def build_summary(self) -> str:
        """生成人类可读的扫描摘要"""
        parts = [f"项目: {self.repo_name}"]

        if self.project_description:
            parts.append(f"描述: {self.project_description}")

        if self.structure:
            parts.append(
                f"文件: {self.structure.total_files} 个, "
                f"{self.structure.total_dirs} 个目录"
            )
            if self.structure.languages:
                parts.append(f"语言: {', '.join(self.structure.languages)}")

        if self.dependencies:
            dep_names = [d.name for d in self.dependencies[:10]]
            parts.append(f"依赖: {', '.join(dep_names)}")

        if self.docker_info and self.docker_info.has_compose:
            svc_names = [s.name for s in self.docker_info.services]
            parts.append(f"Docker 服务: {', '.join(svc_names)}")

        if self.k8s_manifests:
            parts.append(f"K8s 清单: {len(self.k8s_manifests)} 个")

        self.summary = "\n".join(parts)
        return self.summary


# ---------------------------------------------------------------------------
# 冷启动扫描器
# ---------------------------------------------------------------------------


class ColdStartScanner:
    """冷启动代码仓库扫描器

    扫描项目仓库结构、依赖、Docker 配置、README 等，
    并将发现的知识自动填充到记忆系统中。

    用法::

        scanner = ColdStartScanner()
        result = scanner.scan_repository("/path/to/project")
        await scanner.populate_memory(result, memory_manager)
    """

    # 最大递归深度
    MAX_SCAN_DEPTH = 5
    # 忽略的目录
    IGNORE_DIRS = {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".eggs",
        ".idea",
        ".vscode",
    }
    # 忽略的文件
    IGNORE_FILES = {".DS_Store", "Thumbs.db"}

    def scan_repository(self, repo_path: str) -> ScanResult:
        """扫描代码仓库

        Args:
            repo_path: 仓库根路径

        Returns:
            扫描结果
        """
        path = Path(repo_path).resolve()
        if not path.is_dir():
            raise ValueError(f"仓库路径不存在或不是目录: {repo_path}")

        logger.info(f"开始扫描仓库: {path}")

        result = ScanResult(
            repo_path=str(path),
            repo_name=path.name,
        )

        # 1. 扫描项目结构
        result.structure = self._scan_structure(path)
        logger.info(
            f"结构扫描完成: {result.structure.total_files} 文件, "
            f"{result.structure.total_dirs} 目录"
        )

        # 2. 扫描依赖
        result.dependencies = self._scan_dependencies(path)
        logger.info(f"依赖扫描完成: {len(result.dependencies)} 个依赖")

        # 3. 扫描 README
        result.readme_content = self._scan_readme(path)
        result.project_description = self._extract_description(result.readme_content)
        logger.info(
            f"README 扫描完成: "
            f"description='{result.project_description[:60]}'"
        )

        # 4. 扫描 Docker 配置
        result.docker_info = self._scan_docker(path)
        logger.info(
            f"Docker 扫描完成: "
            f"dockerfile={result.docker_info.has_dockerfile}, "
            f"compose={result.docker_info.has_compose}, "
            f"services={len(result.docker_info.services)}"
        )

        # 5. 扫描 .env.example
        result.env_example_content = self._scan_env_example(path)

        # 6. 扫描 K8s 配置
        result.k8s_manifests = self._scan_k8s(path)

        # 7. 构建摘要
        result.build_summary()

        logger.info(f"仓库扫描完成: {result.repo_name}")
        return result

    # ------------------------------------------------------------------
    # 依赖扫描
    # ------------------------------------------------------------------

    def _scan_dependencies(self, path: Path) -> list[Dependency]:
        """扫描依赖文件，解析 package.json / requirements.txt / pyproject.toml

        Args:
            path: 项目根路径

        Returns:
            依赖列表
        """
        deps: list[Dependency] = []

        # requirements.txt / requirements-*.txt
        for req_file in sorted(path.glob("requirements*.txt")):
            deps.extend(self._parse_requirements_txt(req_file))

        # pyproject.toml
        pyproject = path / "pyproject.toml"
        if pyproject.is_file():
            deps.extend(self._parse_pyproject_toml(pyproject))

        # package.json
        pkg_json = path / "package.json"
        if pkg_json.is_file():
            deps.extend(self._parse_package_json(pkg_json))

        # 去重（取第一次出现的版本）
        seen: dict[str, Dependency] = {}
        for dep in deps:
            key = dep.name.lower()
            if key not in seen:
                seen[key] = dep

        return list(seen.values())

    def _parse_requirements_txt(self, file_path: Path) -> list[Dependency]:
        """解析 requirements.txt"""
        deps: list[Dependency] = []
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                # 解析 name>=version, name==version, name~=version 等
                match = re.match(r"^([A-Za-z0-9_.-]+)\s*([>=<~!]=?\s*\S+)?", line)
                if match:
                    deps.append(Dependency(
                        name=match.group(1),
                        version=(match.group(2) or "").strip(),
                        source=file_path.name,
                    ))
        except Exception as e:
            logger.warning(f"解析 {file_path} 失败: {e}")
        return deps

    def _parse_pyproject_toml(self, file_path: Path) -> list[Dependency]:
        """解析 pyproject.toml 的依赖"""
        deps: list[Dependency] = []
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")

            # 简易 TOML 解析：提取 [project.dependencies] 和
            # [project.optional-dependencies] 中的依赖
            in_deps = False
            in_optional = False

            for line in content.splitlines():
                stripped = line.strip()

                # 检测节标题
                if stripped == "[project]":
                    in_deps = False
                    in_optional = False
                    continue
                if stripped == "[project.dependencies]":
                    in_deps = True
                    in_optional = False
                    continue
                if stripped.startswith("[project.optional-dependencies"):
                    in_deps = False
                    in_optional = True
                    continue
                if stripped.startswith("["):
                    in_deps = False
                    in_optional = False
                    continue

                if in_deps or in_optional:
                    # 格式: "name>=version" 或 "name"
                    match = re.match(
                        r'^["\']?([A-Za-z0-9_.-]+)["\']?\s*(?:,\s*["\']?([^"\']*))?',
                        stripped,
                    )
                    if match and match.group(1):
                        version_str = match.group(2) or ""
                        # 尝试提取内联版本
                        ver_match = re.search(r'[>=<~!]=?\s*\S+', version_str)
                        if not ver_match:
                            ver_match = re.search(r'[>=<~!]=?\s*\S+', stripped)
                        version = ver_match.group(0).strip() if ver_match else ""
                        deps.append(Dependency(
                            name=match.group(1),
                            version=version,
                            source="pyproject.toml",
                        ))

            # 解析 [tool.poetry.dependencies] (Poetry 格式)
            in_poetry_deps = False
            for line in content.splitlines():
                stripped = line.strip()
                if stripped == "[tool.poetry.dependencies]":
                    in_poetry_deps = True
                    continue
                if stripped.startswith("["):
                    in_poetry_deps = False
                    continue
                if in_poetry_deps:
                    match = re.match(r'^([A-Za-z0-9_.-]+)\s*=\s*["\']?([^"\']+)', stripped)
                    if match and match.group(1) != "python":
                        deps.append(Dependency(
                            name=match.group(1),
                            version=match.group(2).strip(),
                            source="pyproject.toml",
                        ))
        except Exception as e:
            logger.warning(f"解析 {file_path} 失败: {e}")
        return deps

    def _parse_package_json(self, file_path: Path) -> list[Dependency]:
        """解析 package.json"""
        deps: list[Dependency] = []
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            for name, version in data.get("dependencies", {}).items():
                deps.append(Dependency(
                    name=name,
                    version=version,
                    source="package.json",
                    is_dev=False,
                ))
            for name, version in data.get("devDependencies", {}).items():
                deps.append(Dependency(
                    name=name,
                    version=version,
                    source="package.json",
                    is_dev=True,
                ))
        except Exception as e:
            logger.warning(f"解析 {file_path} 失败: {e}")
        return deps

    # ------------------------------------------------------------------
    # 项目结构扫描
    # ------------------------------------------------------------------

    def _scan_structure(self, path: Path) -> ProjectStructure:
        """扫描项目目录结构

        Args:
            path: 项目根路径

        Returns:
            项目结构信息
        """
        structure = ProjectStructure(root_name=path.name)

        ext_counts: dict[str, int] = {}
        ext_sizes: dict[str, float] = {}
        total_files = 0
        total_dirs = 0

        for item in self._walk_directory(path, depth=0):
            if item.is_file():
                total_files += 1
                ext = item.suffix.lower() or "(无扩展名)"
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
                try:
                    size_kb = item.stat().st_size / 1024
                except OSError:
                    size_kb = 0.0
                ext_sizes[ext] = ext_sizes.get(ext, 0.0) + size_kb
            elif item.is_dir():
                total_dirs += 1

        structure.total_files = total_files
        structure.total_dirs = total_dirs

        # 文件类型统计（按数量降序）
        structure.file_types = sorted(
            [
                FileTypeInfo(
                    extension=ext,
                    count=count,
                    total_size_kb=round(ext_sizes.get(ext, 0.0), 2),
                )
                for ext, count in ext_counts.items()
            ],
            key=lambda ft: ft.count,
            reverse=True,
        )

        # 顶层目录和文件
        try:
            for item in sorted(path.iterdir()):
                if item.name in self.IGNORE_DIRS:
                    continue
                if item.is_dir():
                    structure.top_level_dirs.append(item.name)
                elif item.is_file():
                    structure.top_level_files.append(item.name)
        except PermissionError:
            pass

        # 特殊目录检测
        structure.has_src_dir = (path / "src").is_dir()
        structure.has_tests_dir = any(
            (path / d).is_dir() for d in ("tests", "test", "spec")
        )
        structure.has_docs_dir = any(
            (path / d).is_dir() for d in ("docs", "doc", "documentation")
        )
        structure.has_ci_config = any(
            (path / f).is_file()
            for f in (
                ".github/workflows",
                ".gitlab-ci.yml",
                "Jenkinsfile",
                ".circleci/config.yml",
            )
        ) or (path / ".github" / "workflows").is_dir()

        # 推断编程语言
        structure.languages = self._detect_languages(ext_counts, path)

        return structure

    def _walk_directory(self, path: Path, depth: int) -> list[Path]:
        """递归遍历目录（限制深度）"""
        results: list[Path] = []
        if depth > self.MAX_SCAN_DEPTH:
            return results

        try:
            for item in sorted(path.iterdir()):
                if item.name in self.IGNORE_DIRS:
                    continue
                if item.name in self.IGNORE_FILES:
                    continue

                results.append(item)

                if item.is_dir():
                    results.extend(self._walk_directory(item, depth + 1))
        except PermissionError:
            pass

        return results

    @staticmethod
    def _detect_languages(ext_counts: dict[str, int], path: Path) -> list[str]:
        """根据文件扩展名推断编程语言"""
        ext_lang_map: dict[str, str] = {
            ".py": "Python",
            ".js": "JavaScript",
            ".ts": "TypeScript",
            ".jsx": "React/JSX",
            ".tsx": "React/TSX",
            ".go": "Go",
            ".rs": "Rust",
            ".java": "Java",
            ".kt": "Kotlin",
            ".swift": "Swift",
            ".rb": "Ruby",
            ".php": "PHP",
            ".c": "C",
            ".cpp": "C++",
            ".h": "C/C++ Header",
            ".cs": "C#",
            ".scala": "Scala",
            ".r": "R",
            ".m": "Objective-C",
            ".dart": "Dart",
            ".lua": "Lua",
            ".sh": "Shell",
            ".sql": "SQL",
            ".vue": "Vue",
            ".svelte": "Svelte",
        }

        detected: list[tuple[str, int]] = []
        for ext, count in ext_counts.items():
            lang = ext_lang_map.get(ext)
            if lang:
                detected.append((lang, count))

        # 按文件数降序
        detected.sort(key=lambda x: x[1], reverse=True)
        return [lang for lang, _ in detected]

    # ------------------------------------------------------------------
    # README 扫描
    # ------------------------------------------------------------------

    def _scan_readme(self, path: Path) -> str:
        """读取 README 文件内容

        Args:
            path: 项目根路径

        Returns:
            README 内容（截取前 5000 字符）
        """
        for name in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
            readme_path = path / name
            if readme_path.is_file():
                try:
                    content = readme_path.read_text(encoding="utf-8", errors="ignore")
                    logger.debug(f"读取 README: {readme_path}")
                    return content[:5000]
                except Exception as e:
                    logger.warning(f"读取 {readme_path} 失败: {e}")
        return ""

    @staticmethod
    def _extract_description(readme_content: str) -> str:
        """从 README 内容中提取项目描述"""
        if not readme_content:
            return ""

        lines = readme_content.splitlines()

        # 跳过标题行（以 # 开头），取第一个非空的描述段落
        found_title = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if found_title:
                    break
                continue
            if stripped.startswith("#"):
                found_title = True
                continue
            if found_title and not stripped.startswith(("#", "!", ">", "---", "===")):
                # 取第一段描述文本
                return stripped[:200]

        # 如果没找到标题后的段落，返回前 200 字符
        clean = re.sub(r"[#*_`\[\]()]", "", readme_content).strip()
        return clean[:200] if clean else ""

    # ------------------------------------------------------------------
    # Docker 扫描
    # ------------------------------------------------------------------

    def _scan_docker(self, path: Path) -> DockerInfo:
        """扫描 Docker 配置

        Args:
            path: 项目根路径

        Returns:
            Docker 配置信息
        """
        info = DockerInfo()

        # Dockerfile
        dockerfile = path / "Dockerfile"
        if dockerfile.is_file():
            info.has_dockerfile = True
            info.base_images = self._parse_dockerfile(dockerfile)

        # docker-compose.yml / docker-compose.yaml
        for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
            compose_path = path / name
            if compose_path.is_file():
                info.has_compose = True
                info.services = self._parse_docker_compose(compose_path)
                break

        # 汇总端口和卷
        for svc in info.services:
            info.exposed_ports.extend(svc.ports)
            info.volumes.extend(svc.volumes)
        info.exposed_ports = list(set(info.exposed_ports))
        info.volumes = list(set(info.volumes))

        return info

    @staticmethod
    def _parse_dockerfile(file_path: Path) -> list[str]:
        """从 Dockerfile 中提取基础镜像"""
        base_images: list[str] = []
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            for match in re.finditer(r"^\s*FROM\s+(\S+)", content, re.MULTILINE):
                base_images.append(match.group(1))
        except Exception as e:
            logger.warning(f"解析 Dockerfile 失败: {e}")
        return base_images

    @staticmethod
    def _parse_docker_compose(file_path: Path) -> list[DockerService]:
        """简易解析 docker-compose.yml 的服务信息（不依赖 yaml 库）"""
        services: list[DockerService] = []
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")

            # 尝试用 json 方式解析（如果是 json 格式）
            try:
                data = json.loads(content)
                if "services" in data:
                    for name, cfg in data["services"].items():
                        if not isinstance(cfg, dict):
                            continue
                        svc = DockerService(name=name)
                        svc.image = cfg.get("image", "")
                        svc.ports = [str(p) for p in cfg.get("ports", [])]
                        svc.volumes = [str(v) for v in cfg.get("volumes", [])]
                        if isinstance(cfg.get("environment"), dict):
                            svc.environment = {
                                str(k): str(v)
                                for k, v in cfg["environment"].items()
                            }
                        deps = cfg.get("depends_on", [])
                        if isinstance(deps, list):
                            svc.depends_on = [str(d) for d in deps]
                        elif isinstance(deps, dict):
                            svc.depends_on = list(deps.keys())
                        services.append(svc)
                    return services
            except json.JSONDecodeError:
                pass

            # 简易 YAML 行解析（不依赖 PyYAML）
            # 使用状态机：跟踪当前所处的 YAML 块
            current_service: Optional[str] = None
            current_block: str = ""  # 当前正在解析的属性块 (ports/volumes/environment)
            services_indent = -1
            svc_indent = -1
            block_indent = -1

            for line in content.splitlines():
                if not line.strip() or line.strip().startswith("#"):
                    continue

                current_indent = len(line) - len(line.lstrip())
                stripped = line.strip()

                # 检测 "services:" 块
                if re.match(r"^services\s*:", line):
                    services_indent = current_indent
                    current_block = ""
                    continue

                if services_indent < 0:
                    continue

                # 服务名（比 services 低一级缩进，以冒号结尾）
                if current_indent == services_indent + 2 and not stripped.startswith("-"):
                    svc_match = re.match(r"^(\s+)(\w[\w-]*)\s*:", line)
                    if svc_match:
                        current_service = svc_match.group(2)
                        svc_indent = current_indent
                        current_block = ""
                        services.append(DockerService(name=current_service))
                        continue

                # 服务属性（比服务名再低一级缩进）
                if current_service and services and current_indent > svc_indent >= 0:
                    svc = services[-1]

                    # 检测属性 key
                    key_match = re.match(r"^\s+(\w[\w-]*)\s*:", line)
                    if key_match:
                        attr_name = key_match.group(1)
                        # 检查是否是新的顶层属性（非列表项）
                        if current_indent == svc_indent + 2:
                            current_block = attr_name
                            block_indent = current_indent

                            # image 是标量值，直接提取
                            if attr_name == "image":
                                img_val = re.match(r"^\s+image\s*:\s*(\S+)", line)
                                if img_val:
                                    svc.image = img_val.group(1)
                                current_block = ""
                            continue

                    # 列表项（以 "- " 开头）
                    if stripped.startswith("- ") and current_block:
                        item_val = stripped[2:].strip().strip("\"'")

                        if current_block == "ports":
                            # 端口映射: "8080:80" 或 "8080:80/tcp"
                            if re.match(r"\d+", item_val):
                                svc.ports.append(item_val)
                        elif current_block == "volumes":
                            svc.volumes.append(item_val)
                        elif current_block == "depends_on":
                            svc.depends_on.append(item_val)
                        continue

                    # environment 键值对
                    if current_block == "environment":
                        env_match = re.match(r"^\s+(\w[\w-]*)\s*[:=]\s*(.+)", line)
                        if env_match:
                            key = env_match.group(1)
                            val = env_match.group(2).strip().strip("\"'")
                            svc.environment[key] = val

        except Exception as e:
            logger.warning(f"解析 docker-compose 失败: {e}")

        return services

    # ------------------------------------------------------------------
    # .env.example 扫描
    # ------------------------------------------------------------------

    @staticmethod
    def _scan_env_example(path: Path) -> str:
        """读取 .env.example 内容"""
        for name in (".env.example", ".env.sample", ".env.template"):
            env_path = path / name
            if env_path.is_file():
                try:
                    return env_path.read_text(encoding="utf-8", errors="ignore")[:3000]
                except Exception as e:
                    logger.warning(f"读取 {env_path} 失败: {e}")
        return ""

    # ------------------------------------------------------------------
    # K8s 扫描
    # ------------------------------------------------------------------

    @staticmethod
    def _scan_k8s(path: Path) -> list[str]:
        """扫描 Kubernetes 配置文件"""
        manifests: list[str] = []
        k8s_dirs = ["k8s", "kubernetes", "deploy", "deployments", "manifests", "helm"]

        for dir_name in k8s_dirs:
            k8s_path = path / dir_name
            if k8s_path.is_dir():
                for yaml_file in sorted(k8s_path.rglob("*.yaml")):
                    manifests.append(str(yaml_file.relative_to(path)))
                for yml_file in sorted(k8s_path.rglob("*.yml")):
                    rel = str(yml_file.relative_to(path))
                    if rel not in manifests:
                        manifests.append(rel)

        return manifests

    # ------------------------------------------------------------------
    # 记忆填充
    # ------------------------------------------------------------------

    async def populate_memory(
        self,
        scan_result: ScanResult,
        memory_manager: MemoryManager,
    ) -> int:
        """将扫描结果填充到记忆系统

        Args:
            scan_result: 扫描结果
            memory_manager: 记忆管理器实例

        Returns:
            存储的记忆条数
        """
        stored_count = 0

        # 1. 项目概述
        if scan_result.summary:
            await memory_manager.add_memory(
                content=scan_result.summary,
                memory_type=MemoryType.SEMANTIC,
                priority=MemoryPriority.HIGH,
                source="cold_start:overview",
                tags=["project", "overview", scan_result.repo_name],
                importance=0.9,
                metadata={"scan_time": scan_result.scan_time.isoformat()},
            )
            stored_count += 1

        # 2. 项目描述
        if scan_result.project_description:
            await memory_manager.add_memory(
                content=f"项目 {scan_result.repo_name} 的描述: {scan_result.project_description}",
                memory_type=MemoryType.SEMANTIC,
                priority=MemoryPriority.HIGH,
                source="cold_start:description",
                tags=["project", "description", scan_result.repo_name],
                importance=0.85,
            )
            stored_count += 1

        # 3. 依赖信息（按类型分组存储）
        if scan_result.dependencies:
            dep_groups: dict[str, list[Dependency]] = {}
            for dep in scan_result.dependencies:
                source = dep.source or "unknown"
                dep_groups.setdefault(source, []).append(dep)

            for source, deps in dep_groups.items():
                dep_list = ", ".join(
                    f"{d.name}({d.version})" if d.version else d.name
                    for d in deps[:50]
                )
                await memory_manager.add_memory(
                    content=f"项目 {scan_result.repo_name} 的 {source} 依赖: {dep_list}",
                    memory_type=MemoryType.SEMANTIC,
                    priority=MemoryPriority.NORMAL,
                    source="cold_start:dependencies",
                    tags=["dependencies", source, scan_result.repo_name],
                    importance=0.7,
                    metadata={"dep_count": len(deps), "source_file": source},
                )
                stored_count += 1

        # 4. 项目结构
        if scan_result.structure:
            struct = scan_result.structure
            structure_desc = (
                f"项目 {scan_result.repo_name} 结构: "
                f"{struct.total_files} 文件, {struct.total_dirs} 目录. "
                f"语言: {', '.join(struct.languages) if struct.languages else '未检测到'}. "
                f"顶层目录: {', '.join(struct.top_level_dirs[:15])}. "
                f"顶层文件: {', '.join(struct.top_level_files[:10])}."
            )
            if struct.has_src_dir:
                structure_desc += " 包含 src/ 目录."
            if struct.has_tests_dir:
                structure_desc += " 包含测试目录."
            if struct.has_ci_config:
                structure_desc += " 包含 CI 配置."

            await memory_manager.add_memory(
                content=structure_desc,
                memory_type=MemoryType.SEMANTIC,
                priority=MemoryPriority.NORMAL,
                source="cold_start:structure",
                tags=["structure", scan_result.repo_name],
                importance=0.7,
            )
            stored_count += 1

            # 文件类型统计
            if struct.file_types:
                top_types = struct.file_types[:10]
                types_desc = ", ".join(
                    f"{ft.extension}({ft.count}个)" for ft in top_types
                )
                await memory_manager.add_memory(
                    content=f"项目 {scan_result.repo_name} 的主要文件类型: {types_desc}",
                    memory_type=MemoryType.SEMANTIC,
                    source="cold_start:file_types",
                    tags=["structure", "file_types", scan_result.repo_name],
                    importance=0.5,
                )
                stored_count += 1

        # 5. Docker 信息
        if scan_result.docker_info:
            docker = scan_result.docker_info
            if docker.has_compose and docker.services:
                svc_desc = "; ".join(
                    f"{s.name}(image={s.image}, ports={s.ports})"
                    for s in docker.services
                )
                await memory_manager.add_memory(
                    content=f"项目 {scan_result.repo_name} 的 Docker 服务: {svc_desc}",
                    memory_type=MemoryType.SEMANTIC,
                    priority=MemoryPriority.NORMAL,
                    source="cold_start:docker",
                    tags=["docker", "services", scan_result.repo_name],
                    importance=0.75,
                )
                stored_count += 1

            if docker.base_images:
                await memory_manager.add_memory(
                    content=f"项目 {scan_result.repo_name} 的 Docker 基础镜像: {', '.join(docker.base_images)}",
                    memory_type=MemoryType.SEMANTIC,
                    source="cold_start:docker",
                    tags=["docker", "base_images", scan_result.repo_name],
                    importance=0.6,
                )
                stored_count += 1

        # 6. .env.example
        if scan_result.env_example_content:
            env_keys = self._extract_env_keys(scan_result.env_example_content)
            if env_keys:
                await memory_manager.add_memory(
                    content=f"项目 {scan_result.repo_name} 的环境变量: {', '.join(env_keys)}",
                    memory_type=MemoryType.SEMANTIC,
                    priority=MemoryPriority.NORMAL,
                    source="cold_start:env",
                    tags=["env", "config", scan_result.repo_name],
                    importance=0.65,
                )
                stored_count += 1

        # 7. K8s 配置
        if scan_result.k8s_manifests:
            await memory_manager.add_memory(
                content=f"项目 {scan_result.repo_name} 的 K8s 清单: {', '.join(scan_result.k8s_manifests)}",
                memory_type=MemoryType.SEMANTIC,
                source="cold_start:k8s",
                tags=["k8s", "deployment", scan_result.repo_name],
                importance=0.6,
            )
            stored_count += 1

        # 8. README 关键内容
        if scan_result.readme_content:
            await memory_manager.add_memory(
                content=f"项目 {scan_result.repo_name} 的 README 摘要: {scan_result.readme_content[:1000]}",
                memory_type=MemoryType.SEMANTIC,
                priority=MemoryPriority.NORMAL,
                source="cold_start:readme",
                tags=["readme", "documentation", scan_result.repo_name],
                importance=0.7,
            )
            stored_count += 1

        logger.info(
            f"记忆填充完成: 项目 {scan_result.repo_name}, "
            f"存储 {stored_count} 条记忆"
        )
        return stored_count

    @staticmethod
    def _extract_env_keys(env_content: str) -> list[str]:
        """从 .env.example 中提取环境变量名"""
        keys: list[str] = []
        for line in env_content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if match:
                keys.append(match.group(1))
        return keys


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------


async def scan_and_populate(
    repo_path: str,
    memory_manager: Optional[MemoryManager] = None,
) -> ScanResult:
    """扫描仓库并填充记忆（便捷入口）

    Args:
        repo_path: 仓库路径
        memory_manager: 记忆管理器实例（为 None 则创建默认实例）

    Returns:
        扫描结果
    """
    scanner = ColdStartScanner()
    result = scanner.scan_repository(repo_path)

    if memory_manager is None:
        memory_manager = MemoryManager.create_default()
        await memory_manager.initialize()

    await scanner.populate_memory(result, memory_manager)
    return result
