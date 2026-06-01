"""多模态记忆支持 - 处理文本、图片、PDF、代码等不同内容类型"""

from __future__ import annotations

import ast
import re
import struct
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from symbio.utils.logger import get_logger

logger = get_logger("multimodal_memory")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class ContentModality(str, Enum):
    """内容模态类型"""
    TEXT = "text"
    IMAGE = "image"
    PDF = "pdf"
    CODE = "code"


@dataclass
class CodeStructure:
    """代码结构信息（由 AST 解析提取）"""
    functions: list[dict[str, Any]] = field(default_factory=list)
    classes: list[dict[str, Any]] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    exports: list[str] = field(default_factory=list)
    language: str = ""
    total_lines: int = 0

    @property
    def summary(self) -> str:
        """生成代码结构摘要"""
        parts: list[str] = []
        if self.language:
            parts.append(f"语言: {self.language}")
        parts.append(f"总行数: {self.total_lines}")
        if self.imports:
            parts.append(f"导入: {', '.join(self.imports[:10])}")
        if self.functions:
            func_names = [f.get("name", "?") for f in self.functions[:10]]
            parts.append(f"函数: {', '.join(func_names)}")
        if self.classes:
            class_names = [c.get("name", "?") for c in self.classes[:10]]
            parts.append(f"类: {', '.join(class_names)}")
        if self.exports:
            parts.append(f"导出: {', '.join(self.exports[:10])}")
        return " | ".join(parts)


@dataclass
class ProcessedContent:
    """经过处理的多模态内容"""
    modality: ContentModality = ContentModality.TEXT
    original_content: str = ""
    text_representation: str = ""         # 用于嵌入/检索的文本表示
    metadata: dict[str, Any] = field(default_factory=dict)
    code_structure: Optional[CodeStructure] = None
    error: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        return self.error is None and bool(self.text_representation)


# ---------------------------------------------------------------------------
# 代码解析器
# ---------------------------------------------------------------------------

class CodeParser:
    """代码解析器 - AST 感知的代码结构提取

    支持 Python（ast 模块）和 JavaScript（正则表达式）。
    """

    @staticmethod
    def _extract_python_ast(code: str) -> CodeStructure:
        """使用 Python ast 模块提取代码结构

        Args:
            code: Python 源代码

        Returns:
            CodeStructure 包含函数、类、导入信息
        """
        structure = CodeStructure(language="python")
        structure.total_lines = code.count("\n") + 1

        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            logger.warning(f"Python AST 解析失败: {e}")
            # 降级：尝试正则提取基本结构
            return CodeParser._fallback_python_parse(code)

        for node in ast.iter_child_nodes(tree):
            # 函数定义
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_info: dict[str, Any] = {
                    "name": node.name,
                    "line": node.lineno,
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                    "args": [],
                    "decorators": [],
                    "docstring": ast.get_docstring(node) or "",
                }
                # 提取参数
                for arg in node.args.args:
                    func_info["args"].append(arg.arg)
                # 提取装饰器
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Name):
                        func_info["decorators"].append(dec.id)
                    elif isinstance(dec, ast.Attribute):
                        func_info["decorators"].append(ast.dump(dec))
                structure.functions.append(func_info)

            # 类定义
            elif isinstance(node, ast.ClassDef):
                class_info: dict[str, Any] = {
                    "name": node.name,
                    "line": node.lineno,
                    "bases": [],
                    "methods": [],
                    "decorators": [],
                    "docstring": ast.get_docstring(node) or "",
                }
                # 基类
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        class_info["bases"].append(base.id)
                    elif isinstance(base, ast.Attribute):
                        class_info["bases"].append(ast.dump(base))
                # 方法
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        class_info["methods"].append(item.name)
                # 装饰器
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Name):
                        class_info["decorators"].append(dec.id)
                structure.classes.append(class_info)

            # import 语句
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name
                    structure.imports.append(name)

            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    full = f"{module}.{alias.name}" if module else alias.name
                    structure.imports.append(full)

        logger.debug(
            f"Python AST 解析: {len(structure.functions)} 函数, "
            f"{len(structure.classes)} 类, {len(structure.imports)} 导入"
        )
        return structure

    @staticmethod
    def _fallback_python_parse(code: str) -> CodeStructure:
        """Python 正则降级解析（AST 失败时使用）"""
        structure = CodeStructure(language="python")
        structure.total_lines = code.count("\n") + 1

        # 函数定义
        for match in re.finditer(
            r"^(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)", code, re.MULTILINE
        ):
            structure.functions.append({
                "name": match.group(1),
                "line": code[:match.start()].count("\n") + 1,
                "is_async": "async" in match.group(0),
                "args": [a.strip().split(":")[0].strip() for a in match.group(2).split(",") if a.strip()],
            })

        # 类定义
        for match in re.finditer(r"^class\s+(\w+)", code, re.MULTILINE):
            structure.classes.append({
                "name": match.group(1),
                "line": code[:match.start()].count("\n") + 1,
            })

        # import 语句
        for match in re.finditer(r"^import\s+(.+)$", code, re.MULTILINE):
            structure.imports.append(match.group(1).strip())
        for match in re.finditer(r"^from\s+\S+\s+import\s+(.+)$", code, re.MULTILINE):
            structure.imports.append(match.group(1).strip())

        return structure

    @staticmethod
    def _extract_js_structure(code: str) -> CodeStructure:
        """使用正则表达式提取 JavaScript/TypeScript 代码结构

        Args:
            code: JavaScript/TypeScript 源代码

        Returns:
            CodeStructure 包含函数、类、导入/导出信息
        """
        structure = CodeStructure(language="javascript")
        structure.total_lines = code.count("\n") + 1

        # 函数声明: function name(...) { ... }
        for match in re.finditer(
            r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)",
            code, re.MULTILINE,
        ):
            structure.functions.append({
                "name": match.group(1),
                "line": code[:match.start()].count("\n") + 1,
                "is_async": "async" in match.group(0),
                "args": [a.strip().split(":")[0].strip().split("=")[0].strip()
                         for a in match.group(2).split(",") if a.strip()],
            })

        # 箭头函数: const name = (...) => { ... }
        for match in re.finditer(
            r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(([^)]*)\)\s*=>",
            code, re.MULTILINE,
        ):
            structure.functions.append({
                "name": match.group(1),
                "line": code[:match.start()].count("\n") + 1,
                "is_async": "async" in match.group(0),
                "args": [a.strip().split(":")[0].strip().split("=")[0].strip()
                         for a in match.group(2).split(",") if a.strip()],
            })

        # 类声明
        for match in re.finditer(
            r"(?:export\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?",
            code, re.MULTILINE,
        ):
            class_info: dict[str, Any] = {
                "name": match.group(1),
                "line": code[:match.start()].count("\n") + 1,
                "bases": [match.group(2)] if match.group(2) else [],
            }
            structure.classes.append(class_info)

        # import 语句
        for match in re.finditer(
            r'import\s+.*?from\s+["\']([^"\']+)["\']', code, re.MULTILINE
        ):
            structure.imports.append(match.group(1))

        for match in re.finditer(
            r'(?:const|let|var)\s+(\w+)\s*=\s*require\s*\(', code, re.MULTILINE
        ):
            structure.imports.append(match.group(1))

        # export 语句
        for match in re.finditer(
            r"export\s+(?:default\s+)?(?:function|class|const|let|var)\s+(\w+)",
            code, re.MULTILINE,
        ):
            structure.exports.append(match.group(1))

        # export { ... }
        for match in re.finditer(r"export\s*\{([^}]+)\}", code, re.MULTILINE):
            for name in match.group(1).split(","):
                name = name.strip().split(" as ")[0].strip()
                if name:
                    structure.exports.append(name)

        logger.debug(
            f"JS/TS 正则解析: {len(structure.functions)} 函数, "
            f"{len(structure.classes)} 类, {len(structure.exports)} 导出"
        )
        return structure


# ---------------------------------------------------------------------------
# 多模态记忆
# ---------------------------------------------------------------------------

class MultiModalMemory:
    """多模态记忆处理器

    处理不同内容类型，将其统一转换为可嵌入/检索的文本表示。

    支持的模态:
    - Text: 直接使用（现有功能）
    - Images: 提取元数据（尺寸、格式），生成描述占位符
    - PDF: 提取文本内容（基础解析）
    - Code: AST 感知解析，提取函数/类结构

    Usage:
        mm = MultiModalMemory()

        # 处理文本
        result = mm.process_content("Hello world", ContentModality.TEXT)

        # 处理代码
        result = mm.process_content("def foo(): pass", ContentModality.CODE)

        # 处理图片（传入路径）
        result = mm.process_content("/path/to/image.png", ContentModality.IMAGE)
    """

    # 支持的图片格式（用于魔数检测）
    _IMAGE_SIGNATURES: dict[bytes, str] = {
        b"\x89PNG\r\n\x1a\n": "PNG",
        b"\xff\xd8\xff": "JPEG",
        b"GIF87a": "GIF87a",
        b"GIF89a": "GIF89a",
        b"RIFF": "WEBP",  # 需要进一步验证
        b"BM": "BMP",
    }

    def process_content(
        self,
        content: str,
        content_type: ContentModality,
        **kwargs: Any,
    ) -> ProcessedContent:
        """处理多模态内容，生成统一的文本表示

        Args:
            content: 内容（文本字符串或文件路径）
            content_type: 内容模态类型
            **kwargs: 额外参数（如 language 用于代码）

        Returns:
            ProcessedContent 包含文本表示和元数据
        """
        type_name = content_type.value if isinstance(content_type, ContentModality) else str(content_type)
        logger.info(f"处理内容: type={type_name}, length={len(content)}")

        try:
            if content_type == ContentModality.TEXT:
                return self._process_text(content)
            elif content_type == ContentModality.IMAGE:
                return self._process_image(content)
            elif content_type == ContentModality.PDF:
                return self._process_pdf(content)
            elif content_type == ContentModality.CODE:
                language = kwargs.get("language", "python")
                return self._process_code(content, language)
            else:
                modality = content_type if isinstance(content_type, ContentModality) else ContentModality.TEXT
                return ProcessedContent(
                    modality=modality,
                    original_content=content,
                    error=f"不支持的内容模态: {type_name}",
                )
        except Exception as e:
            logger.error(f"内容处理失败: {e}")
            modality = content_type if isinstance(content_type, ContentModality) else ContentModality.TEXT
            return ProcessedContent(
                modality=modality,
                original_content=content,
                error=str(e),
            )

    def _process_text(self, text: str) -> ProcessedContent:
        """处理纯文本内容

        Args:
            text: 文本内容

        Returns:
            ProcessedContent，text_representation 直接使用原文
        """
        return ProcessedContent(
            modality=ContentModality.TEXT,
            original_content=text,
            text_representation=text,
            metadata={
                "char_count": len(text),
                "line_count": text.count("\n") + 1,
            },
        )

    def _process_image(self, image_path: str) -> ProcessedContent:
        """处理图片内容

        提取图片元数据（格式、尺寸），生成描述占位符。
        纯 stdlib 实现，通过读取文件魔数和二进制头来解析。

        Args:
            image_path: 图片文件路径

        Returns:
            ProcessedContent 包含图片元数据和描述占位符
        """
        path = Path(image_path)

        if not path.exists():
            return ProcessedContent(
                modality=ContentModality.IMAGE,
                original_content=image_path,
                error=f"图片文件不存在: {image_path}",
            )

        file_size = path.stat().st_size
        metadata: dict[str, Any] = {
            "file_path": str(path.resolve()),
            "file_name": path.name,
            "file_size_bytes": file_size,
            "file_extension": path.suffix.lower(),
        }

        # 读取文件头识别格式和尺寸
        try:
            with open(path, "rb") as f:
                header = f.read(64)

            fmt = self._detect_image_format(header)
            metadata["format"] = fmt

            # 尝试从文件头提取尺寸
            dimensions = self._extract_image_dimensions(path, fmt, header)
            if dimensions:
                metadata["width"] = dimensions[0]
                metadata["height"] = dimensions[1]

        except Exception as e:
            logger.warning(f"图片元数据提取失败: {e}")

        # 生成文本描述
        width = metadata.get("width", "未知")
        height = metadata.get("height", "未知")
        fmt_str = metadata.get("format", "未知格式")
        size_kb = file_size / 1024

        text_repr = (
            f"[图片] 文件: {path.name}, 格式: {fmt_str}, "
            f"尺寸: {width}x{height}, 大小: {size_kb:.1f}KB\n"
            f"[图片描述占位] 此图片的内容描述需要通过视觉模型生成。"
        )

        return ProcessedContent(
            modality=ContentModality.IMAGE,
            original_content=image_path,
            text_representation=text_repr,
            metadata=metadata,
        )

    def _detect_image_format(self, header: bytes) -> str:
        """通过文件魔数检测图片格式"""
        for sig, fmt in self._IMAGE_SIGNATURES.items():
            if header[:len(sig)] == sig:
                # WEBP 需要额外验证
                if fmt == "WEBP" and len(header) >= 12:
                    if header[8:12] != b"WEBP":
                        continue
                return fmt
        return "unknown"

    def _extract_image_dimensions(
        self, path: Path, fmt: str, header: bytes
    ) -> Optional[tuple[int, int]]:
        """从文件头提取图片尺寸（纯二进制解析，无需 PIL）"""
        try:
            if fmt == "PNG" and len(header) >= 24:
                # PNG: IHDR chunk 在固定偏移
                width = struct.unpack(">I", header[16:20])[0]
                height = struct.unpack(">I", header[20:24])[0]
                return (width, height)

            elif fmt == "BMP" and len(header) >= 26:
                # BMP: 尺寸在偏移 18
                width = struct.unpack("<I", header[18:22])[0]
                height = abs(struct.unpack("<i", header[22:26])[0])
                return (width, height)

            elif fmt == "GIF87a" or fmt == "GIF89a":
                if len(header) >= 10:
                    width = struct.unpack("<H", header[6:8])[0]
                    height = struct.unpack("<H", header[8:10])[0]
                    return (width, height)

            elif fmt == "JPEG":
                return self._extract_jpeg_dimensions(path)

        except Exception as e:
            logger.debug(f"图片尺寸提取失败 ({fmt}): {e}")

        return None

    @staticmethod
    def _extract_jpeg_dimensions(path: Path) -> Optional[tuple[int, int]]:
        """提取 JPEG 图片尺寸（遍历 SOF 标记）"""
        try:
            with open(path, "rb") as f:
                data = f.read(4096)  # 读取前 4KB 足够找到 SOF

            idx = 0
            while idx < len(data) - 1:
                if data[idx] != 0xFF:
                    break
                marker = data[idx + 1]

                # SOF0-SOF15 (0xC0-0xCF), 排除 0xC4(DHT) 和 0xCC(DAC)
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xCC):
                    if idx + 9 < len(data):
                        height = struct.unpack(">H", data[idx + 5:idx + 7])[0]
                        width = struct.unpack(">H", data[idx + 7:idx + 9])[0]
                        return (width, height)
                    break

                # 跳过当前段
                if idx + 3 < len(data):
                    seg_len = struct.unpack(">H", data[idx + 2:idx + 4])[0]
                    idx += 2 + seg_len
                else:
                    break

        except Exception as e:
            logger.debug(f"JPEG 尺寸提取失败: {e}")

        return None

    def _process_pdf(self, pdf_path: str) -> ProcessedContent:
        """处理 PDF 文件

        使用基础的二进制解析提取文本内容。
        不依赖外部 PDF 库，通过搜索 PDF 流对象提取可读文本。

        Args:
            pdf_path: PDF 文件路径

        Returns:
            ProcessedContent 包含提取的文本和元数据
        """
        path = Path(pdf_path)

        if not path.exists():
            return ProcessedContent(
                modality=ContentModality.PDF,
                original_content=pdf_path,
                error=f"PDF 文件不存在: {pdf_path}",
            )

        file_size = path.stat().st_size
        metadata: dict[str, Any] = {
            "file_path": str(path.resolve()),
            "file_name": path.name,
            "file_size_bytes": file_size,
        }

        extracted_text = ""

        try:
            with open(path, "rb") as f:
                data = f.read()

            # 验证 PDF 魔数
            if not data[:5] == b"%PDF-":
                return ProcessedContent(
                    modality=ContentModality.PDF,
                    original_content=pdf_path,
                    error="不是有效的 PDF 文件",
                )

            # 提取 PDF 版本
            version_match = re.match(rb"%PDF-(\d+\.\d+)", data)
            if version_match:
                metadata["pdf_version"] = version_match.group(1).decode("ascii", errors="ignore")

            # 基础文本提取：搜索文本流中的可打印内容
            # PDF 中的文本通常在 BT...ET 块中，使用 Tj/TJ 操作符
            text_parts: list[str] = []

            # 方法1: 提取 BT...ET 块中的文本
            for match in re.finditer(rb"BT\b(.*?)ET\b", data, re.DOTALL):
                block = match.group(1)
                # 提取 Tj 操作符: (text) Tj
                for tj in re.finditer(rb"\(([^)]*)\)\s*Tj", block):
                    try:
                        text_parts.append(tj.group(1).decode("latin-1", errors="ignore"))
                    except Exception:
                        pass
                # 提取 TJ 操作符: [(text)] TJ
                for tj in re.finditer(rb"\[(.*?)\]\s*TJ", block, re.DOTALL):
                    for seg in re.finditer(rb"\(([^)]*)\)", tj.group(1)):
                        try:
                            text_parts.append(seg.group(1).decode("latin-1", errors="ignore"))
                        except Exception:
                            pass

            # 方法2: 如果上面没提取到，尝试查找流中的文本
            if not text_parts:
                for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.DOTALL):
                    stream_data = match.group(1)
                    # 查找可打印 ASCII 文本段
                    for text_match in re.finditer(rb"[\x20-\x7E]{10,}", stream_data):
                        text_parts.append(
                            text_match.group(0).decode("ascii", errors="ignore")
                        )

            extracted_text = "\n".join(text_parts)

            # 统计页数（通过 /Type /Page 计数）
            page_count = len(re.findall(rb"/Type\s*/Page[^s]", data))
            metadata["page_count"] = page_count

        except Exception as e:
            logger.error(f"PDF 解析失败: {e}")
            return ProcessedContent(
                modality=ContentModality.PDF,
                original_content=pdf_path,
                error=f"PDF 解析失败: {e}",
            )

        if not extracted_text.strip():
            extracted_text = f"[PDF] 文件: {path.name}, 大小: {file_size/1024:.1f}KB, 未能提取到文本内容"

        metadata["char_count"] = len(extracted_text)
        metadata["line_count"] = extracted_text.count("\n") + 1

        logger.info(f"PDF 解析完成: {path.name}, 提取 {len(extracted_text)} 字符")
        return ProcessedContent(
            modality=ContentModality.PDF,
            original_content=pdf_path,
            text_representation=extracted_text,
            metadata=metadata,
        )

    def _process_code(self, code: str, language: str) -> ProcessedContent:
        """处理代码内容

        根据语言选择 AST 解析器，提取代码结构并生成文本表示。

        Args:
            code: 源代码字符串
            language: 编程语言（"python" 或 "javascript"/"typescript"）

        Returns:
            ProcessedContent 包含代码结构和文本表示
        """
        language_lower = language.lower().strip()

        # 解析代码结构
        if language_lower == "python":
            code_structure = CodeParser._extract_python_ast(code)
        elif language_lower in ("javascript", "typescript", "js", "ts"):
            code_structure = CodeParser._extract_js_structure(code)
        else:
            # 不支持的语言，降级为纯文本
            logger.warning(f"不支持的代码语言 '{language}'，降级为纯文本处理")
            return self._process_text(code)

        # 生成文本表示
        text_parts: list[str] = []
        text_parts.append(f"[代码] 语言: {language}, {code_structure.total_lines} 行")

        if code_structure.imports:
            text_parts.append(f"导入: {', '.join(code_structure.imports[:15])}")

        for func in code_structure.functions:
            name = func.get("name", "?")
            args = ", ".join(func.get("args", []))
            prefix = "async " if func.get("is_async") else ""
            text_parts.append(f"函数: {prefix}{name}({args})")
            docstring = func.get("docstring", "")
            if docstring:
                text_parts.append(f"  说明: {docstring[:200]}")

        for cls in code_structure.classes:
            name = cls.get("name", "?")
            bases = cls.get("bases", [])
            bases_str = f"({', '.join(bases)})" if bases else ""
            text_parts.append(f"类: {name}{bases_str}")
            methods = cls.get("methods", [])
            if methods:
                text_parts.append(f"  方法: {', '.join(methods[:10])}")
            docstring = cls.get("docstring", "")
            if docstring:
                text_parts.append(f"  说明: {docstring[:200]}")

        if code_structure.exports:
            text_parts.append(f"导出: {', '.join(code_structure.exports[:15])}")

        text_representation = "\n".join(text_parts)

        metadata: dict[str, Any] = {
            "language": language,
            "total_lines": code_structure.total_lines,
            "function_count": len(code_structure.functions),
            "class_count": len(code_structure.classes),
            "import_count": len(code_structure.imports),
            "export_count": len(code_structure.exports),
        }

        logger.info(
            f"代码解析完成: {language}, "
            f"{len(code_structure.functions)} 函数, "
            f"{len(code_structure.classes)} 类"
        )

        return ProcessedContent(
            modality=ContentModality.CODE,
            original_content=code,
            text_representation=text_representation,
            metadata=metadata,
            code_structure=code_structure,
        )
