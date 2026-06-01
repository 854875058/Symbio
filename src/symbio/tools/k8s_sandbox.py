"""K8s / Docker 临时沙箱配置生成器。

核心职责：
1. 生成 K8s Pod Spec、NetworkPolicy、ResourceQuota YAML 字典
2. 生成 Dockerfile 和 docker-compose.yml 内容
3. 提供常见沙箱类型的预置模板（代码执行、数据处理、网页抓取）
4. 将配置导出为 YAML / Docker 文件

使用方式：
    from symbio.tools.k8s_sandbox import (
        SandboxConfig,
        K8sSandboxGenerator,
        DockerSandboxGenerator,
        SANDBOX_TEMPLATES,
    )

    config = SANDBOX_TEMPLATES["code_execution"]
    k8s_gen = K8sSandboxGenerator()
    pod_spec = k8s_gen.generate_pod_spec(config)
    k8s_gen.export_yaml(config, Path("./output"))

    docker_gen = DockerSandboxGenerator()
    dockerfile = docker_gen.generate_dockerfile(config)
    docker_gen.export_files(config, Path("./output"))
"""

from __future__ import annotations

import copy
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("k8s_sandbox")


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class NetworkPolicyMode(str, Enum):
    """网络策略模式。

    DENY_ALL: 拒绝所有流量（仅允许 DNS 和指定端点）
    ALLOW_SPECIFIC: 仅允许指定的外部端点
    """

    DENY_ALL = "deny_all"
    ALLOW_SPECIFIC = "allow_specific"


class SandboxType(str, Enum):
    """沙箱类型枚举。"""

    CODE_EXECUTION = "code_execution"
    DATA_PROCESSING = "data_processing"
    WEB_SCRAPING = "web_scraping"
    CUSTOM = "custom"


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------


class SandboxConfig(BaseModel):
    """沙箱配置。

    Attributes:
        name: 沙箱实例名称。
        image: 容器镜像，如 python:3.11-slim。
        cpu_limit: CPU 限制，如 "500m" 或 "1"。
        memory_limit: 内存限制，如 "256Mi" 或 "1Gi"。
        timeout_seconds: 最大运行时长（秒），超时后 Pod 将被清理。
        network_policy: 网络策略模式。
        allowed_endpoints: network_policy=ALLOW_SPECIFIC 时允许的外部端点列表。
        read_only_root: 是否将根文件系统设为只读。
        sandbox_type: 沙箱类型，用于选择预置模板。
        env: 额外环境变量。
        volumes: 额外卷挂载 {volume_name: mount_path}。
        command: 容器启动命令。
        args: 容器启动参数。
        labels: 附加标签。
        namespace: K8s 命名空间。
        service_account: K8s ServiceAccount 名称。
    """

    name: str = "sandbox"
    image: str = "python:3.11-slim"
    cpu_limit: str = "500m"
    memory_limit: str = "256Mi"
    timeout_seconds: int = 300
    network_policy: NetworkPolicyMode = NetworkPolicyMode.DENY_ALL
    allowed_endpoints: list[str] = Field(default_factory=list)
    read_only_root: bool = True
    sandbox_type: SandboxType = SandboxType.CUSTOM
    env: dict[str, str] = Field(default_factory=dict)
    volumes: dict[str, str] = Field(default_factory=dict)
    command: Optional[list[str]] = None
    args: Optional[list[str]] = None
    labels: dict[str, str] = Field(default_factory=dict)
    namespace: str = "default"
    service_account: str = "default"


# ---------------------------------------------------------------------------
# K8s 生成器
# ---------------------------------------------------------------------------


class K8sSandboxGenerator:
    """Kubernetes 临时沙箱配置生成器。

    生成符合安全最佳实践的 K8s 资源 YAML 字典：
    - Pod Spec：资源限制、只读根文件系统、非 root 用户、临时卷
    - NetworkPolicy：默认拒绝所有流量，可选允许指定端点
    - ResourceQuota：限制命名空间资源用量
    """

    # Pod 内部临时目录
    _TMPFS_PATHS = ["/tmp", "/var/tmp"]
    # 非 root 用户
    _DEFAULT_UID = 65534
    _DEFAULT_GID = 65534

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def generate_pod_spec(self, config: SandboxConfig) -> dict[str, Any]:
        """生成 K8s Pod YAML 字典。

        包含：
        - 资源请求与限制
        - 只读根文件系统 + tmpfs 临时卷
        - 非 root 安全上下文
        - 自动清理注解（timeout_seconds）

        Args:
            config: 沙箱配置。

        Returns:
            Pod YAML 字典。
        """
        labels = {
            "app": "symbio-sandbox",
            "sandbox-type": config.sandbox_type.value,
            **config.labels,
        }

        # 容器安全上下文
        security_context: dict[str, Any] = {
            "runAsNonRoot": True,
            "runAsUser": self._DEFAULT_UID,
            "runAsGroup": self._DEFAULT_GID,
            "readOnlyRootFilesystem": config.read_only_root,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        }

        # 卷与卷挂载
        volumes: list[dict[str, Any]] = []
        volume_mounts: list[dict[str, Any]] = []

        # 临时目录 tmpfs 卷
        if config.read_only_root:
            volumes.append(
                {
                    "name": "tmp-volumes",
                    "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"},
                }
            )
            for path in self._TMPFS_PATHS:
                volume_mounts.append(
                    {"name": "tmp-volumes", "mountPath": path}
                )

        # 用户自定义卷
        for vol_name, mount_path in config.volumes.items():
            volumes.append({"name": vol_name, "emptyDir": {}})
            volume_mounts.append(
                {"name": vol_name, "mountPath": mount_path}
            )

        # 环境变量
        env_list = [{"name": k, "value": v} for k, v in config.env.items()]

        # 容器定义
        container: dict[str, Any] = {
            "name": config.name,
            "image": config.image,
            "imagePullPolicy": "IfNotPresent",
            "resources": {
                "requests": {
                    "cpu": config.cpu_limit,
                    "memory": config.memory_limit,
                },
                "limits": {
                    "cpu": config.cpu_limit,
                    "memory": config.memory_limit,
                },
            },
            "securityContext": security_context,
        }

        if env_list:
            container["env"] = env_list
        if volume_mounts:
            container["volumeMounts"] = volume_mounts
        if config.command:
            container["command"] = config.command
        if config.args:
            container["args"] = config.args

        pod: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": config.name,
                "namespace": config.namespace,
                "labels": labels,
                "annotations": {
                    "symbio/timeout-seconds": str(config.timeout_seconds),
                    "symbio/sandbox-type": config.sandbox_type.value,
                },
            },
            "spec": {
                "restartPolicy": "Never",
                "terminationGracePeriodSeconds": 10,
                "serviceAccountName": config.service_account,
                "automountServiceAccountToken": False,
                "containers": [container],
                "volumes": volumes if volumes else [],
                "securityContext": {
                    "runAsNonRoot": True,
                    "fsGroup": self._DEFAULT_GID,
                },
            },
        }

        logger.info(f"Generated Pod spec for sandbox '{config.name}'")
        return pod

    def generate_network_policy(
        self, config: SandboxConfig
    ) -> dict[str, Any]:
        """生成 NetworkPolicy YAML 字典。

        默认行为：拒绝所有入站和出站流量。
        可选例外：
        - kube-dns（UDP 53）出站始终放行
        - config.allowed_endpoints 中指定的 CIDR / 端口

        Args:
            config: 沙箱配置。

        Returns:
            NetworkPolicy YAML 字典。
        """
        # 允许 DNS 出站
        egress_rules: list[dict[str, Any]] = [
            {
                "to": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {
                                "kubernetes.io/metadata.name": "kube-system"
                            }
                        }
                    }
                ],
                "ports": [
                    {"protocol": "UDP", "port": 53},
                    {"protocol": "TCP", "port": 53},
                ],
            }
        ]

        # 允许指定端点
        if config.network_policy == NetworkPolicyMode.ALLOW_SPECIFIC:
            for endpoint in config.allowed_endpoints:
                egress_rules.append(
                    {
                        "to": [{"ipBlock": {"cidr": endpoint}}],
                        "ports": [
                            {"protocol": "TCP", "port": 443},
                            {"protocol": "TCP", "port": 80},
                        ],
                    }
                )

        policy: dict[str, Any] = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": f"{config.name}-netpol",
                "namespace": config.namespace,
                "labels": {
                    "app": "symbio-sandbox",
                    "sandbox-type": config.sandbox_type.value,
                },
            },
            "spec": {
                "podSelector": {
                    "matchLabels": {
                        "app": "symbio-sandbox",
                        "sandbox-type": config.sandbox_type.value,
                    }
                },
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],  # 拒绝所有入站
                "egress": egress_rules,
            },
        }

        logger.info(f"Generated NetworkPolicy for sandbox '{config.name}'")
        return policy

    def generate_resource_quota(
        self, config: SandboxConfig
    ) -> dict[str, Any]:
        """生成 ResourceQuota YAML 字典。

        限制命名空间内的总资源用量，防止沙箱实例过度消耗。

        Args:
            config: 沙箱配置。

        Returns:
            ResourceQuota YAML 字典。
        """
        quota: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {
                "name": f"{config.name}-quota",
                "namespace": config.namespace,
                "labels": {
                    "app": "symbio-sandbox",
                    "sandbox-type": config.sandbox_type.value,
                },
            },
            "spec": {
                "hard": {
                    "requests.cpu": config.cpu_limit,
                    "requests.memory": config.memory_limit,
                    "limits.cpu": config.cpu_limit,
                    "limits.memory": config.memory_limit,
                    "pods": "1",
                }
            },
        }

        logger.info(f"Generated ResourceQuota for sandbox '{config.name}'")
        return quota

    def export_yaml(self, config: SandboxConfig, output_dir: Path) -> list[Path]:
        """将 K8s 资源导出为 YAML 文件。

        生成文件：
        - {name}-pod.yaml
        - {name}-network-policy.yaml
        - {name}-resource-quota.yaml

        Args:
            config: 沙箱配置。
            output_dir: 输出目录路径。

        Returns:
            生成的文件路径列表。
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []

        files = [
            (f"{config.name}-pod.yaml", self.generate_pod_spec(config)),
            (
                f"{config.name}-network-policy.yaml",
                self.generate_network_policy(config),
            ),
            (
                f"{config.name}-resource-quota.yaml",
                self.generate_resource_quota(config),
            ),
        ]

        for filename, data in files:
            filepath = output_dir / filename
            with open(filepath, "w", encoding="utf-8") as f:
                yaml.dump(
                    data,
                    f,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )
            created.append(filepath)
            logger.info(f"Exported K8s YAML: {filepath}")

        return created


# ---------------------------------------------------------------------------
# Docker 生成器
# ---------------------------------------------------------------------------


class DockerSandboxGenerator:
    """Docker 沙箱配置生成器。

    生成：
    - Dockerfile：基于配置的容器镜像定义
    - docker-compose.yml：带资源限制、网络隔离、只读文件系统的编排文件
    """

    def generate_dockerfile(self, config: SandboxConfig) -> str:
        """生成 Dockerfile 内容。

        Args:
            config: 沙箱配置。

        Returns:
            Dockerfile 字符串。
        """
        lines = [
            f"FROM {config.image}",
            "",
            "# 安全：非 root 用户",
            f"RUN groupadd -g {K8sSandboxGenerator._DEFAULT_GID} sandbox "
            f"&& useradd -u {K8sSandboxGenerator._DEFAULT_UID} "
            f"-g sandbox -s /bin/sh sandbox",
            "",
            "# 临时目录",
            "RUN mkdir -p /tmp /var/tmp && chmod 1777 /tmp /var/tmp",
        ]

        # 工作卷目录
        for vol_name, mount_path in config.volumes.items():
            lines.append(f"RUN mkdir -p {mount_path} && chown sandbox:sandbox {mount_path}")

        lines += [
            "",
            "USER sandbox",
            "WORKDIR /workspace",
        ]

        if config.command:
            cmd_str = " ".join(f'"{c}"' for c in config.command)
            lines.append(f"CMD [{cmd_str}]")
        elif config.args:
            args_str = " ".join(f'"{a}"' for a in config.args)
            lines.append(f"CMD [{args_str}]")
        else:
            lines.append('CMD ["/bin/sh"]')

        logger.info(f"Generated Dockerfile for sandbox '{config.name}'")
        return "\n".join(lines) + "\n"

    def generate_docker_compose(self, config: SandboxConfig) -> str:
        """生成 docker-compose.yml 内容。

        Args:
            config: 沙箱配置。

        Returns:
            docker-compose.yml 字符串。
        """
        # CPU 转换：K8s 毫核 -> Docker nano_cpus
        cpu_nano = self._cpu_k8s_to_nano(config.cpu_limit)

        # 内存转换：K8s 单位 -> bytes
        memory_bytes = self._memory_k8s_to_bytes(config.memory_limit)

        # 环境变量
        env_list = [f"{k}={v}" for k, v in config.env.items()]

        # 服务定义
        service: dict[str, Any] = {
            "image": config.image,
            "read_only": config.read_only_root,
            "mem_limit": memory_bytes,
            "cpus": round(cpu_nano / 1_000_000_000, 2),
            "network_mode": "none"
            if config.network_policy == NetworkPolicyMode.DENY_ALL
            else "bridge",
            "security_opt": ["no-new-privileges"],
                "tmpfs": [
                    "/tmp:rw,noexec,nosuid,size=64m",
                    "/var/tmp:rw,noexec,nosuid,size=64m",
                ],
        }

        if config.command:
            service["command"] = config.command
        if config.args:
            service["entrypoint"] = config.command or []
            service["command"] = config.args
        if env_list:
            service["environment"] = env_list

        compose: dict[str, Any] = {
            "version": "3.8",
            "services": {config.name: service},
        }

        logger.info(f"Generated docker-compose.yml for sandbox '{config.name}'")
        return yaml.dump(
            compose,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )

    def export_files(self, config: SandboxConfig, output_dir: Path) -> list[Path]:
        """导出 Docker 文件到指定目录。

        生成文件：
        - Dockerfile
        - docker-compose.yml

        Args:
            config: 沙箱配置。
            output_dir: 输出目录路径。

        Returns:
            生成的文件路径列表。
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []

        dockerfile_path = output_dir / "Dockerfile"
        with open(dockerfile_path, "w", encoding="utf-8") as f:
            f.write(self.generate_dockerfile(config))
        created.append(dockerfile_path)
        logger.info(f"Exported Dockerfile: {dockerfile_path}")

        compose_path = output_dir / "docker-compose.yml"
        with open(compose_path, "w", encoding="utf-8") as f:
            f.write(self.generate_docker_compose(config))
        created.append(compose_path)
        logger.info(f"Exported docker-compose.yml: {compose_path}")

        return created

    # ------------------------------------------------------------------
    # 内部转换工具
    # ------------------------------------------------------------------

    @staticmethod
    def _cpu_k8s_to_nano(cpu_str: str) -> int:
        """将 K8s CPU 字符串转换为 Docker nano_cpus。

        支持格式： "500m" -> 500_000_000, "1" -> 1_000_000_000, "1.5" -> 1_500_000_000
        """
        cpu_str = cpu_str.strip()
        if cpu_str.endswith("m"):
            return int(cpu_str[:-1]) * 1_000_000
        return int(float(cpu_str) * 1_000_000_000)

    @staticmethod
    def _memory_k8s_to_bytes(mem_str: str) -> int:
        """将 K8s 内存字符串转换为字节数。

        支持格式： "256Mi", "1Gi", "512M", "1G", "1048576"
        """
        mem_str = mem_str.strip()
        units = {
            "Ki": 1024,
            "Mi": 1024**2,
            "Gi": 1024**3,
            "Ti": 1024**4,
            "K": 1000,
            "M": 1000**2,
            "G": 1000**3,
            "T": 1000**4,
        }
        for suffix, multiplier in units.items():
            if mem_str.endswith(suffix):
                return int(float(mem_str[: -len(suffix)]) * multiplier)
        return int(mem_str)


# ---------------------------------------------------------------------------
# 预置模板
# ---------------------------------------------------------------------------


SANDBOX_TEMPLATES: dict[str, SandboxConfig] = {
    # 代码执行沙箱 — 轻量、严格隔离
    "code_execution": SandboxConfig(
        name="code-exec-sandbox",
        image="python:3.11-slim",
        cpu_limit="500m",
        memory_limit="256Mi",
        timeout_seconds=120,
        network_policy=NetworkPolicyMode.DENY_ALL,
        read_only_root=True,
        sandbox_type=SandboxType.CODE_EXECUTION,
        command=["python", "-c"],
        args=["print('sandbox ready')"],
        labels={"purpose": "code-execution"},
    ),
    # 数据处理沙箱 — 较高资源、允许外部数据源
    "data_processing": SandboxConfig(
        name="data-proc-sandbox",
        image="python:3.11-slim",
        cpu_limit="2",
        memory_limit="2Gi",
        timeout_seconds=3600,
        network_policy=NetworkPolicyMode.ALLOW_SPECIFIC,
        allowed_endpoints=["10.0.0.0/8", "172.16.0.0/12"],
        read_only_root=True,
        sandbox_type=SandboxType.DATA_PROCESSING,
        env={"PYTHONUNBUFFERED": "1"},
        volumes={"data": "/data"},
        labels={"purpose": "data-processing"},
    ),
    # 网页抓取沙箱 — 允许外部 HTTP/HTTPS 访问
    "web_scraping": SandboxConfig(
        name="web-scrape-sandbox",
        image="python:3.11-slim",
        cpu_limit="1",
        memory_limit="512Mi",
        timeout_seconds=600,
        network_policy=NetworkPolicyMode.ALLOW_SPECIFIC,
        allowed_endpoints=["0.0.0.0/0"],
        read_only_root=True,
        sandbox_type=SandboxType.WEB_SCRAPING,
        env={
            "PYTHONUNBUFFERED": "1",
            "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
        },
        labels={"purpose": "web-scraping"},
    ),
}


def get_template(name: str) -> SandboxConfig:
    """按名称获取预置模板的深拷贝。

    Args:
        name: 模板名称（code_execution / data_processing / web_scraping）。

    Returns:
        SandboxConfig 深拷贝实例。

    Raises:
        KeyError: 模板名称不存在。
    """
    if name not in SANDBOX_TEMPLATES:
        available = ", ".join(SANDBOX_TEMPLATES.keys())
        raise KeyError(
            f"Unknown sandbox template '{name}'. Available: {available}"
        )
    return SANDBOX_TEMPLATES[name].model_copy(deep=True)


def list_templates() -> list[str]:
    """列出所有可用的预置模板名称。

    Returns:
        模板名称列表。
    """
    return list(SANDBOX_TEMPLATES.keys())
