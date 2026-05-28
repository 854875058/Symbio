"""Skill 标准格式 - 定义 Skill 的 JSON Schema 规范"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Skill JSON Schema 定义
# ---------------------------------------------------------------------------

SKILL_MANIFEST_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "https://symbio.ai/schemas/skill-manifest.json",
    "title": "Symbio Skill Manifest",
    "description": "Symbio Skill 清单文件的标准格式定义",
    "type": "object",
    "required": ["name", "version", "description", "skill_type"],
    "properties": {
        "name": {
            "type": "string",
            "description": "Skill 唯一标识名称 (小写字母、数字、下划线)",
            "pattern": "^[a-z][a-z0-9_]{2,63}$",
            "examples": ["code_review", "data_analysis", "web_search"],
        },
        "display_name": {
            "type": "string",
            "description": "Skill 显示名称",
            "maxLength": 128,
        },
        "version": {
            "type": "string",
            "description": "语义化版本号",
            "pattern": "^\\d+\\.\\d+\\.\\d+(-[a-zA-Z0-9.]+)?$",
            "examples": ["1.0.0", "2.1.0-beta.1"],
        },
        "description": {
            "type": "string",
            "description": "Skill 功能描述",
            "maxLength": 1024,
        },
        "skill_type": {
            "type": "string",
            "enum": ["tool", "agent", "workflow", "integration", "custom"],
            "description": "Skill 类型",
        },
        "author": {
            "type": "string",
            "description": "作者/组织",
        },
        "license": {
            "type": "string",
            "description": "许可证",
            "examples": ["MIT", "Apache-2.0", "proprietary"],
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "标签列表",
            "maxItems": 20,
        },
        "capabilities": {
            "type": "array",
            "items": {"type": "string"},
            "description": "能力声明列表",
        },
        "entry_point": {
            "type": "string",
            "description": "入口点 (Python 模块路径或可执行文件路径)",
            "examples": ["symbio.skills.builtin.code_review:CodeReviewSkill"],
        },
        "input_schema": {
            "$ref": "#/definitions/IOSchema",
            "description": "输入参数 JSON Schema",
        },
        "output_schema": {
            "$ref": "#/definitions/IOSchema",
            "description": "输出结果 JSON Schema",
        },
        "config_schema": {
            "$ref": "#/definitions/IOSchema",
            "description": "配置项 JSON Schema",
        },
        "dependencies": {
            "type": "array",
            "items": {"$ref": "#/definitions/Dependency"},
            "description": "依赖列表",
        },
        "permissions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "所需权限列表",
            "examples": [["file:read", "network:http", "shell:execute"]],
        },
        "config": {
            "type": "object",
            "description": "默认配置值",
        },
        "environment": {
            "type": "object",
            "properties": {
                "required_env_vars": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "必需的环境变量",
                },
                "min_python_version": {
                    "type": "string",
                    "description": "最低 Python 版本",
                    "examples": ["3.10"],
                },
                "platforms": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["linux", "macos", "windows"],
                    },
                    "description": "支持的操作系统平台",
                },
            },
        },
        "metadata": {
            "type": "object",
            "description": "附加元数据",
        },
    },
    "definitions": {
        "IOSchema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["object", "string", "number", "integer", "boolean", "array"],
                },
                "properties": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "description": {"type": "string"},
                            "default": {},
                            "enum": {"type": "array"},
                        },
                    },
                },
                "required": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
        "Dependency": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {
                    "type": "string",
                    "description": "依赖 Skill 名称",
                },
                "version": {
                    "type": "string",
                    "description": "版本约束",
                    "examples": [">=1.0.0", "^2.0.0", "~1.2.0"],
                },
                "optional": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否为可选依赖",
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Pydantic 数据模型
# ---------------------------------------------------------------------------

class SkillIOSchema(BaseModel):
    """Skill 输入/输出 Schema"""
    type: str = "object"
    properties: dict[str, dict[str, Any]] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)
    description: str = ""


class SkillDependencySpec(BaseModel):
    """Skill 依赖规格"""
    name: str
    version: str = ""
    optional: bool = False


class SkillManifest(BaseModel):
    """Skill 清单文件

    对应 skill.json 的标准格式。
    """
    name: str
    display_name: str = ""
    version: str = "1.0.0"
    description: str = ""
    skill_type: str = "custom"
    author: str = ""
    license: str = ""
    tags: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    entry_point: str = ""
    input_schema: SkillIOSchema = Field(default_factory=SkillIOSchema)
    output_schema: SkillIOSchema = Field(default_factory=SkillIOSchema)
    config_schema: SkillIOSchema = Field(default_factory=SkillIOSchema)
    dependencies: list[SkillDependencySpec] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_json_schema(self) -> dict[str, Any]:
        """导出为 JSON Schema 兼容格式"""
        return self.model_dump(exclude_none=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillManifest":
        """从字典创建"""
        return cls(**data)

    def validate_against_schema(self) -> list[str]:
        """验证清单是否符合标准 Schema

        Returns:
            验证错误列表 (空列表表示通过)
        """
        errors: list[str] = []

        # 名称格式验证
        import re
        if not re.match(r"^[a-z][a-z0-9_]{2,63}$", self.name):
            errors.append(f"Skill 名称格式无效: {self.name} (需小写字母开头, 仅含小写字母/数字/下划线, 3-64字符)")

        # 版本号格式验证
        if not re.match(r"^\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$", self.version):
            errors.append(f"版本号格式无效: {self.version} (需语义化版本号)")

        # 类型验证
        valid_types = {"tool", "agent", "workflow", "integration", "custom"}
        if self.skill_type not in valid_types:
            errors.append(f"无效的 Skill 类型: {self.skill_type}")

        # 描述长度
        if len(self.description) > 1024:
            errors.append(f"描述过长: {len(self.description)} > 1024")

        # 标签数量
        if len(self.tags) > 20:
            errors.append(f"标签过多: {len(self.tags)} > 20")

        return errors


# ---------------------------------------------------------------------------
# Skill 模板生成器
# ---------------------------------------------------------------------------

class SkillTemplateGenerator:
    """Skill 模板生成器"""

    @staticmethod
    def generate_manifest(
        name: str,
        description: str,
        skill_type: str = "tool",
        **kwargs: Any,
    ) -> SkillManifest:
        """生成 Skill 清单模板

        Args:
            name: Skill 名称
            description: 描述
            skill_type: 类型
            **kwargs: 其他参数

        Returns:
            Skill 清单对象
        """
        return SkillManifest(
            name=name,
            display_name=kwargs.get("display_name", name.replace("_", " ").title()),
            version=kwargs.get("version", "0.1.0"),
            description=description,
            skill_type=skill_type,
            author=kwargs.get("author", ""),
            tags=kwargs.get("tags", []),
            capabilities=kwargs.get("capabilities", []),
            entry_point=kwargs.get("entry_point", ""),
        )

    @staticmethod
    def generate_skill_code(manifest: SkillManifest) -> str:
        """生成 Skill 基础代码模板

        Args:
            manifest: Skill 清单

        Returns:
            Python 代码字符串
        """
        class_name = "".join(word.capitalize() for word in manifest.name.split("_"))

        code = f'''"""Skill: {manifest.display_name or manifest.name}

{manifest.description}
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("skills.{manifest.name}")


class {class_name}Input(BaseModel):
    """输入参数"""
    query: str = Field(description="输入查询")
    params: dict[str, Any] = Field(default_factory=dict, description="附加参数")


class {class_name}Output(BaseModel):
    """输出结果"""
    result: str = Field(description="执行结果")
    data: dict[str, Any] = Field(default_factory=dict, description="结果数据")


class {class_name}Skill:
    """{manifest.description}"""

    name = "{manifest.name}"
    version = "{manifest.version}"

    def __init__(self, config: dict[str, Any] | None = None):
        self._config = config or {{}}

    async def execute(self, input_data: {class_name}Input) -> {class_name}Output:
        """执行 Skill

        Args:
            input_data: 输入参数

        Returns:
            执行结果
        """
        logger.info(f"执行 {{self.name}}: query={{input_data.query}}")

        # TODO: 实现具体逻辑
        result = f"处理完成: {{input_data.query}}"
        return {class_name}Output(result=result)

    def get_manifest(self) -> dict[str, Any]:
        """获取 Skill 清单"""
        return {{
            "name": self.name,
            "version": self.version,
            "description": "{manifest.description}",
        }}
'''
        return code


def get_skill_manifest_schema() -> dict[str, Any]:
    """获取 Skill 清单的 JSON Schema"""
    return SKILL_MANIFEST_SCHEMA
