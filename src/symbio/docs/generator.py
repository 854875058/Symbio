"""自动文档生成器 - 从代码生成 API 文档"""

from __future__ import annotations

import ast
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("docs.generator")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class DocFormat(str, Enum):
    """文档输出格式"""

    MARKDOWN = "markdown"
    HTML = "html"
    RST = "rst"


class ParameterInfo(BaseModel):
    """参数信息"""

    name: str
    type_hint: str = ""
    default: str = ""
    description: str = ""
    required: bool = True


class ReturnInfo(BaseModel):
    """返回值信息"""

    type_hint: str = ""
    description: str = ""


class FunctionDoc(BaseModel):
    """函数/方法文档"""

    name: str
    qualified_name: str = ""
    docstring: str = ""
    parameters: list[ParameterInfo] = Field(default_factory=list)
    return_info: ReturnInfo = Field(default_factory=ReturnInfo)
    decorators: list[str] = Field(default_factory=list)
    is_async: bool = False
    is_classmethod: bool = False
    is_staticmethod: bool = False
    is_property: bool = False
    line_number: int = 0
    source_file: str = ""


class ClassDoc(BaseModel):
    """类文档"""

    name: str
    qualified_name: str = ""
    docstring: str = ""
    base_classes: list[str] = Field(default_factory=list)
    methods: list[FunctionDoc] = Field(default_factory=list)
    class_variables: list[ParameterInfo] = Field(default_factory=list)
    decorators: list[str] = Field(default_factory=list)
    line_number: int = 0
    source_file: str = ""


class ModuleDoc(BaseModel):
    """模块文档"""

    name: str
    file_path: str = ""
    docstring: str = ""
    classes: list[ClassDoc] = Field(default_factory=list)
    functions: list[FunctionDoc] = Field(default_factory=list)
    constants: list[dict[str, Any]] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)


class PackageDoc(BaseModel):
    """包文档"""

    package_name: str
    description: str = ""
    version: str = ""
    modules: list[ModuleDoc] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# 代码解析器
# ---------------------------------------------------------------------------


class CodeParser:
    """Python 代码 AST 解析器"""

    def parse_file(self, file_path: str | Path) -> ModuleDoc:
        """解析 Python 文件

        Args:
            file_path: 文件路径

        Returns:
            模块文档
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        with open(path, "r", encoding="utf-8") as f:
            source = f.read()

        tree = ast.parse(source, filename=str(path))
        module_name = path.stem

        module_doc = ModuleDoc(
            name=module_name,
            file_path=str(path),
        )

        # 提取模块 docstring
        module_doc.docstring = ast.get_docstring(tree) or ""

        # 解析顶层节点
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                class_doc = self._parse_class(node, str(path))
                module_doc.classes.append(class_doc)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_doc = self._parse_function(node, str(path))
                module_doc.functions.append(func_doc)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                module_doc.imports.extend(self._extract_imports(node))
            elif isinstance(node, ast.Assign):
                constants = self._extract_constants(node)
                module_doc.constants.extend(constants)

        return module_doc

    def _parse_class(self, node: ast.ClassDef, source_file: str) -> ClassDoc:
        """解析类定义"""
        class_doc = ClassDoc(
            name=node.name,
            docstring=ast.get_docstring(node) or "",
            base_classes=[self._get_name(base) for base in node.bases],
            line_number=node.lineno,
            source_file=source_file,
        )

        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method_doc = self._parse_function(item, source_file, parent_class=node.name)
                # 检查装饰器
                for dec in item.decorator_list:
                    dec_name = self._get_name(dec)
                    if dec_name == "classmethod":
                        method_doc.is_classmethod = True
                    elif dec_name == "staticmethod":
                        method_doc.is_staticmethod = True
                    elif dec_name == "property":
                        method_doc.is_property = True
                    method_doc.decorators.append(dec_name)
                class_doc.methods.append(method_doc)
            elif isinstance(item, ast.Assign):
                constants = self._extract_constants(item)
                for c in constants:
                    class_doc.class_variables.append(
                        ParameterInfo(
                            name=c.get("name", ""),
                            type_hint=c.get("type", ""),
                            default=c.get("value", ""),
                        )
                    )

        # 类装饰器
        for dec in node.decorator_list:
            class_doc.decorators.append(self._get_name(dec))

        return class_doc

    def _parse_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        source_file: str,
        parent_class: str = "",
    ) -> FunctionDoc:
        """解析函数/方法定义"""
        is_async = isinstance(node, ast.AsyncFunctionDef)
        qualified_name = f"{parent_class}.{node.name}" if parent_class else node.name

        func_doc = FunctionDoc(
            name=node.name,
            qualified_name=qualified_name,
            docstring=ast.get_docstring(node) or "",
            is_async=is_async,
            line_number=node.lineno,
            source_file=source_file,
        )

        # 解析参数
        func_doc.parameters = self._parse_arguments(node.args)

        # 解析返回值类型
        if node.returns:
            func_doc.return_info = ReturnInfo(type_hint=self._get_annotation(node.returns))

        # 从 docstring 提取参数描述
        self._enrich_from_docstring(func_doc)

        return func_doc

    def _parse_arguments(self, args: ast.arguments) -> list[ParameterInfo]:
        """解析函数参数"""
        params: list[ParameterInfo] = []
        num_defaults = len(args.defaults)

        for i, arg in enumerate(args.args):
            if arg.arg == "self" or arg.arg == "cls":
                continue

            param = ParameterInfo(name=arg.arg)
            if arg.annotation:
                param.type_hint = self._get_annotation(arg.annotation)

            # 默认值
            default_idx = i - (len(args.args) - num_defaults)
            if default_idx >= 0 and default_idx < len(args.defaults):
                param.default = self._get_value(args.defaults[default_idx])
                param.required = False

            params.append(param)

        # *args
        if args.vararg:
            params.append(
                ParameterInfo(
                    name=f"*{args.vararg.arg}",
                    type_hint=self._get_annotation(args.vararg.annotation)
                    if args.vararg.annotation
                    else "",
                )
            )

        # **kwargs
        if args.kwarg:
            params.append(
                ParameterInfo(
                    name=f"**{args.kwarg.arg}",
                    type_hint=self._get_annotation(args.kwarg.annotation)
                    if args.kwarg.annotation
                    else "",
                )
            )

        return params

    def _get_annotation(self, node: ast.expr) -> str:
        """获取类型注解字符串"""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Constant):
            return repr(node.value)
        elif isinstance(node, ast.Attribute):
            return f"{self._get_annotation(node.value)}.{node.attr}"
        elif isinstance(node, ast.Subscript):
            return f"{self._get_annotation(node.value)}[{self._get_annotation(node.slice)}]"
        elif isinstance(node, ast.Tuple):
            return ", ".join(self._get_annotation(e) for e in node.elts)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return f"{self._get_annotation(node.left)} | {self._get_annotation(node.right)}"
        return ast.dump(node)

    def _get_name(self, node: ast.expr) -> str:
        """获取名称"""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return f"{self._get_name(node.value)}.{node.attr}"
        return ""

    def _get_value(self, node: ast.expr) -> str:
        """获取默认值字符串"""
        if isinstance(node, ast.Constant):
            return repr(node.value)
        elif isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.List):
            return "[...]"
        elif isinstance(node, ast.Dict):
            return "{...}"
        elif isinstance(node, ast.Call):
            return f"{self._get_name(node.func)}(...)"
        return "..."

    def _extract_imports(self, node: ast.stmt) -> list[str]:
        """提取导入信息"""
        if isinstance(node, ast.Import):
            return [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            return [f"{module}.{alias.name}" for alias in node.names]
        return []

    def _extract_constants(self, node: ast.Assign) -> list[dict[str, Any]]:
        """提取常量定义"""
        constants: list[dict[str, Any]] = []
        for target in node.targets:
            if isinstance(target, ast.Name):
                name = target.id
                if name.isupper():
                    constants.append(
                        {
                            "name": name,
                            "value": self._get_value(node.value),
                            "type": type(node.value).__name__,
                        }
                    )
        return constants

    def _enrich_from_docstring(self, func_doc: FunctionDoc) -> None:
        """从 docstring 中补充参数描述"""
        if not func_doc.docstring:
            return

        lines = func_doc.docstring.split("\n")
        in_args_section = False

        for line in lines:
            stripped = line.strip()
            if stripped.lower().startswith(("args:", "parameters:", "params:")):
                in_args_section = True
                continue
            if stripped.lower().startswith(("returns:", "return:", "raises:", "yields:")):
                in_args_section = False
                continue

            if in_args_section and ":" in stripped:
                parts = stripped.split(":", 1)
                param_name = parts[0].strip()
                param_desc = parts[1].strip()
                for param in func_doc.parameters:
                    if param.name == param_name or param.name.startswith(param_name):
                        param.description = param_desc
                        break


# ---------------------------------------------------------------------------
# 文档渲染器
# ---------------------------------------------------------------------------


class MarkdownRenderer:
    """Markdown 文档渲染器"""

    def render_module(self, module_doc: ModuleDoc) -> str:
        """渲染模块文档"""
        lines: list[str] = []
        lines.append(f"# Module: `{module_doc.name}`")
        lines.append("")

        if module_doc.file_path:
            lines.append(f"**File:** `{module_doc.file_path}`")
            lines.append("")

        if module_doc.docstring:
            lines.append(module_doc.docstring)
            lines.append("")

        # 常量
        if module_doc.constants:
            lines.append("## Constants")
            lines.append("")
            for const in module_doc.constants:
                lines.append(f"- **{const['name']}** = `{const['value']}`")
            lines.append("")

        # 类
        for cls in module_doc.classes:
            lines.append(self._render_class(cls))

        # 函数
        if module_doc.functions:
            lines.append("## Functions")
            lines.append("")
            for func in module_doc.functions:
                lines.append(self._render_function(func))

        return "\n".join(lines)

    def _render_class(self, class_doc: ClassDoc) -> str:
        """渲染类文档"""
        lines: list[str] = []
        bases = f"({', '.join(class_doc.base_classes)})" if class_doc.base_classes else ""
        lines.append(f"## Class: `{class_doc.name}{bases}`")
        lines.append("")

        if class_doc.docstring:
            lines.append(class_doc.docstring)
            lines.append("")

        if class_doc.class_variables:
            lines.append("### Class Variables")
            lines.append("")
            lines.append("| Name | Type | Default |")
            lines.append("|------|------|---------|")
            for var in class_doc.class_variables:
                lines.append(f"| `{var.name}` | `{var.type_hint}` | `{var.default}` |")
            lines.append("")

        if class_doc.methods:
            lines.append("### Methods")
            lines.append("")
            for method in class_doc.methods:
                lines.append(self._render_function(method))

        return "\n".join(lines)

    def _render_function(self, func_doc: FunctionDoc) -> str:
        """渲染函数文档"""
        lines: list[str] = []

        # 函数签名
        prefix = "async " if func_doc.is_async else ""
        if func_doc.is_classmethod:
            prefix = "@classmethod " + prefix
        elif func_doc.is_staticmethod:
            prefix = "@staticmethod " + prefix
        elif func_doc.is_property:
            prefix = "@property "

        params_str = ", ".join(self._render_param(p) for p in func_doc.parameters)
        ret_str = f" -> {func_doc.return_info.type_hint}" if func_doc.return_info.type_hint else ""

        lines.append(f"#### `{prefix}{func_doc.name}({params_str}){ret_str}`")
        lines.append("")

        if func_doc.docstring:
            lines.append(func_doc.docstring)
            lines.append("")

        # 参数表
        if func_doc.parameters:
            lines.append("| Parameter | Type | Default | Required | Description |")
            lines.append("|-----------|------|---------|----------|-------------|")
            for p in func_doc.parameters:
                required = "Yes" if p.required else "No"
                default = f"`{p.default}`" if p.default else "-"
                type_hint = f"`{p.type_hint}`" if p.type_hint else "-"
                lines.append(
                    f"| `{p.name}` | {type_hint} | {default} | {required} | {p.description} |"
                )
            lines.append("")

        return "\n".join(lines)

    def _render_param(self, param: ParameterInfo) -> str:
        """渲染参数"""
        if param.type_hint and param.default:
            return f"{param.name}: {param.type_hint} = {param.default}"
        elif param.type_hint:
            return f"{param.name}: {param.type_hint}"
        elif param.default:
            return f"{param.name} = {param.default}"
        return param.name

    def render_package(self, package_doc: PackageDoc) -> str:
        """渲染包文档"""
        lines: list[str] = []
        lines.append(f"# {package_doc.package_name}")
        lines.append("")

        if package_doc.description:
            lines.append(package_doc.description)
            lines.append("")

        lines.append(f"Generated: {package_doc.generated_at.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")

        # 目录
        lines.append("## Table of Contents")
        lines.append("")
        for module in package_doc.modules:
            lines.append(f"- [{module.name}](#{module.name})")
        lines.append("")

        # 各模块
        for module in package_doc.modules:
            lines.append(self.render_module(module))
            lines.append("")
            lines.append("---")
            lines.append("")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 文档生成器
# ---------------------------------------------------------------------------


class APIDocGenerator:
    """API 文档生成器

    从 Python 源码自动提取并生成 API 文档。

    用法:
        generator = APIDocGenerator()
        doc = generator.generate_from_file("my_module.py")
        generator.save(doc, output_path="docs/api.md")
    """

    def __init__(self, doc_format: DocFormat = DocFormat.MARKDOWN):
        self._parser = CodeParser()
        self._renderer = MarkdownRenderer()
        self._format = doc_format

    def generate_from_file(self, file_path: str | Path) -> ModuleDoc:
        """从单个文件生成文档

        Args:
            file_path: Python 文件路径

        Returns:
            模块文档
        """
        logger.info(f"解析文件: {file_path}")
        return self._parser.parse_file(file_path)

    def generate_from_directory(
        self,
        directory: str | Path,
        recursive: bool = True,
        exclude_patterns: list[str] | None = None,
    ) -> PackageDoc:
        """从目录生成包文档

        Args:
            directory: 源码目录
            recursive: 是否递归子目录
            exclude_patterns: 排除模式列表

        Returns:
            包文档
        """
        dir_path = Path(directory)
        if not dir_path.exists():
            raise FileNotFoundError(f"目录不存在: {directory}")

        exclude = exclude_patterns or ["__pycache__", "*.pyc", "test_*", "*_test.py"]

        package_doc = PackageDoc(
            package_name=dir_path.name,
        )

        pattern = "**/*.py" if recursive else "*.py"
        for py_file in sorted(dir_path.glob(pattern)):
            # 排除检查
            if any(py_file.match(pat) for pat in exclude):
                continue
            try:
                module_doc = self.generate_from_file(py_file)
                package_doc.modules.append(module_doc)
            except Exception as exc:
                logger.warning(f"解析文件失败: {py_file} - {exc}")

        logger.info(f"生成包文档: {package_doc.package_name}, {len(package_doc.modules)} 个模块")
        return package_doc

    def render(self, module_doc: ModuleDoc) -> str:
        """渲染模块文档为字符串"""
        return self._renderer.render_module(module_doc)

    def render_package(self, package_doc: PackageDoc) -> str:
        """渲染包文档为字符串"""
        return self._renderer.render_package(package_doc)

    def save(
        self,
        module_doc: ModuleDoc,
        output_path: str | Path,
    ) -> None:
        """保存模块文档到文件"""
        content = self.render(module_doc)
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with open(output, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"文档已保存: {output_path}")

    def save_package(
        self,
        package_doc: PackageDoc,
        output_path: str | Path,
    ) -> None:
        """保存包文档到文件"""
        content = self.render_package(package_doc)
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with open(output, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"包文档已保存: {output_path}")
