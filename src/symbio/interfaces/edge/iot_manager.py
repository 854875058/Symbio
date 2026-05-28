"""IoT 设备管理 - MQTT 协议支持与设备生命周期管理"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("interfaces.edge.iot")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class DeviceStatus(str, Enum):
    """设备状态"""
    OFFLINE = "offline"
    ONLINE = "online"
    PROVISIONING = "provisioning"
    UPDATING = "updating"
    ERROR = "error"
    MAINTENANCE = "maintenance"


class MQTTQoS(int, Enum):
    """MQTT QoS 级别"""
    AT_MOST_ONCE = 0    # 最多一次
    AT_LEAST_ONCE = 1   # 至少一次
    EXACTLY_ONCE = 2    # 恰好一次


class CommandType(str, Enum):
    """设备命令类型"""
    REBOOT = "reboot"
    UPDATE_FIRMWARE = "update_firmware"
    UPDATE_CONFIG = "update_config"
    COLLECT_DATA = "collect_data"
    SET_MODE = "set_mode"
    CUSTOM = "custom"


class CommandStatus(str, Enum):
    """命令执行状态"""
    PENDING = "pending"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class MQTTConfig(BaseModel):
    """MQTT 连接配置"""
    broker_host: str = "localhost"
    broker_port: int = 1883
    username: str = ""
    password: str = ""
    client_id: str = ""
    use_tls: bool = False
    ca_cert_path: str = ""
    keepalive_sec: int = 60
    reconnect_delay_sec: float = 5.0
    max_reconnect_attempts: int = 10
    topic_prefix: str = "symbio"


class DeviceProfile(BaseModel):
    """设备档案"""
    device_id: str
    name: str
    device_type: str = "generic"
    firmware_version: str = ""
    hardware_version: str = ""
    status: DeviceStatus = DeviceStatus.OFFLINE
    capabilities: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    location: dict[str, float] = Field(default_factory=dict)
    last_seen: Optional[datetime] = None
    registered_at: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeviceCommand(BaseModel):
    """设备命令"""
    command_id: str = Field(default_factory=lambda: str(uuid4()))
    device_id: str
    command_type: CommandType
    payload: dict[str, Any] = Field(default_factory=dict)
    status: CommandStatus = CommandStatus.PENDING
    timeout_sec: int = 60
    created_at: datetime = Field(default_factory=datetime.now)
    sent_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class TelemetryData(BaseModel):
    """设备遥测数据"""
    data_id: str = Field(default_factory=lambda: str(uuid4()))
    device_id: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.now)


class MQTTMessage(BaseModel):
    """MQTT 消息"""
    message_id: str = Field(default_factory=lambda: str(uuid4()))
    topic: str
    payload: str
    qos: MQTTQoS = MQTTQoS.AT_LEAST_ONCE
    retain: bool = False
    timestamp: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# MQTT 客户端接口
# ---------------------------------------------------------------------------

class MQTTClientInterface:
    """MQTT 客户端接口

    定义 MQTT 通信的抽象接口, 可对接 paho-mqtt 等实现。
    """

    def __init__(self, config: MQTTConfig):
        self._config = config
        self._connected = False
        self._subscriptions: dict[str, Callable[[str, str], None]] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        """连接 MQTT Broker"""
        self._connected = True
        logger.info(f"MQTT 连接: {self._config.broker_host}:{self._config.broker_port}")
        return True

    def disconnect(self) -> None:
        """断开连接"""
        self._connected = False
        logger.info("MQTT 断开连接")

    def publish(
        self,
        topic: str,
        payload: str,
        qos: MQTTQoS = MQTTQoS.AT_LEAST_ONCE,
        retain: bool = False,
    ) -> bool:
        """发布消息

        Args:
            topic: 主题
            payload: 消息内容
            qos: QoS 级别
            retain: 是否保留消息

        Returns:
            是否成功
        """
        if not self._connected:
            logger.error("MQTT 未连接, 无法发布")
            return False

        full_topic = f"{self._config.topic_prefix}/{topic}" if self._config.topic_prefix else topic
        logger.debug(f"MQTT 发布: {full_topic} (qos={qos.value})")
        return True

    def subscribe(
        self,
        topic: str,
        callback: Callable[[str, str], None],
        qos: MQTTQoS = MQTTQoS.AT_LEAST_ONCE,
    ) -> bool:
        """订阅主题

        Args:
            topic: 主题
            callback: 回调函数 (topic, payload)
            qos: QoS 级别

        Returns:
            是否成功
        """
        full_topic = f"{self._config.topic_prefix}/{topic}" if self._config.topic_prefix else topic
        self._subscriptions[full_topic] = callback
        logger.info(f"MQTT 订阅: {full_topic}")
        return True

    def unsubscribe(self, topic: str) -> None:
        """取消订阅"""
        full_topic = f"{self._config.topic_prefix}/{topic}" if self._config.topic_prefix else topic
        self._subscriptions.pop(full_topic, None)


# ---------------------------------------------------------------------------
# 设备管理器
# ---------------------------------------------------------------------------

class IoTDeviceManager:
    """IoT 设备管理器

    基于 MQTT 协议管理 IoT 设备的全生命周期, 匕括注册、监控、命令下发和遥测采集。

    用法:
        manager = IoTDeviceManager(mqtt_config=MQTTConfig(broker_host="mqtt.example.com"))
        manager.register_device(DeviceProfile(device_id="sensor-001", name="温度传感器"))
        manager.connect()
        manager.send_command("sensor-001", CommandType.COLLECT_DATA, {"interval": 60})
    """

    def __init__(self, mqtt_config: MQTTConfig | None = None):
        self._mqtt_config = mqtt_config or MQTTConfig()
        self._mqtt_client = MQTTClientInterface(self._mqtt_config)
        self._devices: dict[str, DeviceProfile] = {}
        self._commands: dict[str, DeviceCommand] = {}
        self._telemetry_buffer: list[TelemetryData] = []
        self._command_callbacks: dict[str, Callable[[DeviceCommand], None]] = {}
        self._lock = threading.Lock()

    def connect(self) -> bool:
        """连接 MQTT Broker 并订阅设备主题"""
        success = self._mqtt_client.connect()
        if success:
            # 订阅设备上线/离线
            self._mqtt_client.subscribe("devices/+/status", self._on_device_status)
            # 订阅遥测数据
            self._mqtt_client.subscribe("devices/+/telemetry", self._on_telemetry)
            # 订阅命令响应
            self._mqtt_client.subscribe("devices/+/command/response", self._on_command_response)
            logger.info("IoT 设备管理器已连接")
        return success

    def disconnect(self) -> None:
        """断开连接"""
        self._mqtt_client.disconnect()
        logger.info("IoT 设备管理器已断开")

    def register_device(self, profile: DeviceProfile) -> DeviceProfile:
        """注册设备

        Args:
            profile: 设备档案

        Returns:
            注册的设备档案
        """
        with self._lock:
            self._devices[profile.device_id] = profile

        # 发布设备注册事件
        self._mqtt_client.publish(
            f"devices/{profile.device_id}/registered",
            json.dumps({"device_id": profile.device_id, "name": profile.name}),
        )

        logger.info(f"注册设备: {profile.device_id} - {profile.name}")
        return profile

    def unregister_device(self, device_id: str) -> None:
        """注销设备"""
        with self._lock:
            self._devices.pop(device_id, None)
        logger.info(f"注销设备: {device_id}")

    def update_device_status(self, device_id: str, status: DeviceStatus) -> DeviceProfile:
        """更新设备状态"""
        profile = self._get_device(device_id)
        profile.status = status
        profile.last_seen = datetime.now()
        logger.info(f"设备状态更新: {device_id} -> {status.value}")
        return profile

    def get_device(self, device_id: str) -> DeviceProfile | None:
        """获取设备档案"""
        return self._devices.get(device_id)

    def list_devices(
        self,
        status: DeviceStatus | None = None,
        device_type: str | None = None,
        tags: list[str] | None = None,
    ) -> list[DeviceProfile]:
        """列出设备"""
        devices = list(self._devices.values())
        if status:
            devices = [d for d in devices if d.status == status]
        if device_type:
            devices = [d for d in devices if d.device_type == device_type]
        if tags:
            devices = [d for d in devices if any(t in d.tags for t in tags)]
        return devices

    def send_command(
        self,
        device_id: str,
        command_type: CommandType,
        payload: dict[str, Any] | None = None,
        timeout_sec: int = 60,
        callback: Callable[[DeviceCommand], None] | None = None,
    ) -> DeviceCommand:
        """向设备发送命令

        Args:
            device_id: 设备 ID
            command_type: 命令类型
            payload: 命令参数
            timeout_sec: 超时时间
            callback: 命令完成回调

        Returns:
            命令对象
        """
        command = DeviceCommand(
            device_id=device_id,
            command_type=command_type,
            payload=payload or {},
            timeout_sec=timeout_sec,
        )

        with self._lock:
            self._commands[command.command_id] = command
            if callback:
                self._command_callbacks[command.command_id] = callback

        # 通过 MQTT 发送命令
        topic = f"devices/{device_id}/command"
        message = json.dumps({
            "command_id": command.command_id,
            "type": command_type.value,
            "payload": command.payload,
        })

        success = self._mqtt_client.publish(topic, message, qos=MQTTQoS.AT_LEAST_ONCE)
        command.status = CommandStatus.SENT if success else CommandStatus.FAILED
        command.sent_at = datetime.now() if success else None

        logger.info(f"发送命令: {command.command_id} -> {device_id} ({command_type.value})")
        return command

    def report_telemetry(self, device_id: str, metrics: dict[str, Any]) -> TelemetryData:
        """上报遥测数据

        Args:
            device_id: 设备 ID
            metrics: 遥测指标

        Returns:
            遥测数据对象
        """
        telemetry = TelemetryData(device_id=device_id, metrics=metrics)
        self._telemetry_buffer.append(telemetry)

        # 通过 MQTT 发布
        topic = f"devices/{device_id}/telemetry"
        self._mqtt_client.publish(topic, json.dumps(metrics))

        # 更新设备最后在线时间
        profile = self._devices.get(device_id)
        if profile:
            profile.last_seen = datetime.now()
            if profile.status == DeviceStatus.OFFLINE:
                profile.status = DeviceStatus.ONLINE

        return telemetry

    def get_device_telemetry(
        self,
        device_id: str,
        limit: int = 100,
    ) -> list[TelemetryData]:
        """获取设备遥测数据"""
        return [
            t for t in self._telemetry_buffer if t.device_id == device_id
        ][-limit:]

    def get_command_status(self, command_id: str) -> DeviceCommand | None:
        """获取命令状态"""
        return self._commands.get(command_id)

    def get_statistics(self) -> dict[str, Any]:
        """获取设备管理统计信息"""
        devices = list(self._devices.values())
        status_counts: dict[str, int] = {}
        for d in devices:
            key = d.status.value
            status_counts[key] = status_counts.get(key, 0) + 1

        return {
            "total_devices": len(devices),
            "status_counts": status_counts,
            "total_commands": len(self._commands),
            "pending_commands": sum(1 for c in self._commands.values() if c.status == CommandStatus.PENDING),
            "total_telemetry_records": len(self._telemetry_buffer),
        }

    def _get_device(self, device_id: str) -> DeviceProfile:
        """获取设备 (内部)"""
        profile = self._devices.get(device_id)
        if not profile:
            raise ValueError(f"设备不存在: {device_id}")
        return profile

    def _on_device_status(self, topic: str, payload: str) -> None:
        """设备状态变更回调"""
        try:
            parts = topic.split("/")
            device_id = parts[1] if len(parts) >= 2 else "unknown"
            data = json.loads(payload)
            status = DeviceStatus(data.get("status", "offline"))
            self.update_device_status(device_id, status)
        except Exception as exc:
            logger.error(f"处理设备状态消息失败: {exc}")

    def _on_telemetry(self, topic: str, payload: str) -> None:
        """遥测数据回调"""
        try:
            parts = topic.split("/")
            device_id = parts[1] if len(parts) >= 2 else "unknown"
            metrics = json.loads(payload)
            self.report_telemetry(device_id, metrics)
        except Exception as exc:
            logger.error(f"处理遥测数据失败: {exc}")

    def _on_command_response(self, topic: str, payload: str) -> None:
        """命令响应回调"""
        try:
            data = json.loads(payload)
            command_id = data.get("command_id", "")
            command = self._commands.get(command_id)
            if command:
                status_str = data.get("status", "completed")
                command.status = CommandStatus(status_str)
                command.result = data.get("result")
                command.completed_at = datetime.now()

                # 调用回调
                callback = self._command_callbacks.get(command_id)
                if callback:
                    callback(command)

                logger.info(f"命令完成: {command_id} -> {command.status.value}")
        except Exception as exc:
            logger.error(f"处理命令响应失败: {exc}")
