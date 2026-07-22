"""移动端 SDK - iOS/Android 接口定义与通信协议"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("interfaces.edge.mobile_sdk")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class Platform(str, Enum):
    """移动平台"""

    IOS = "ios"
    ANDROID = "android"
    UNKNOWN = "unknown"


class ConnectionState(str, Enum):
    """连接状态"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class MessageType(str, Enum):
    """消息类型"""

    REQUEST = "request"
    RESPONSE = "response"
    EVENT = "event"
    HEARTBEAT = "heartbeat"
    ERROR = "error"


class SDKConfig(BaseModel):
    """SDK 配置"""

    platform: Platform = Platform.UNKNOWN
    api_base_url: str = "https://api.symbio.ai"
    api_key: str = ""
    app_id: str = ""
    device_id: str = ""
    timeout_sec: float = 30.0
    max_retries: int = 3
    retry_delay_sec: float = 1.0
    heartbeat_interval_sec: float = 30.0
    enable_offline_cache: bool = True
    cache_max_size_mb: float = 50.0
    enable_telemetry: bool = True
    log_level: str = "INFO"


class SDKMessage(BaseModel):
    """SDK 消息"""

    message_id: str = Field(default_factory=lambda: str(uuid4()))
    type: MessageType
    method: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    error_code: int = 0
    error_message: str = ""
    timestamp: datetime = Field(default_factory=datetime.now)
    correlation_id: str = ""


class DeviceInfo(BaseModel):
    """设备信息"""

    device_id: str
    platform: Platform
    os_version: str = ""
    app_version: str = ""
    sdk_version: str = "1.0.0"
    screen_width: int = 0
    screen_height: int = 0
    locale: str = ""
    timezone: str = ""
    network_type: str = ""


class OfflineCacheEntry(BaseModel):
    """离线缓存条目"""

    cache_id: str = Field(default_factory=lambda: str(uuid4()))
    key: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None
    synced: bool = False


class TelemetryEvent(BaseModel):
    """遥测事件"""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: str
    properties: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# 平台接口定义
# ---------------------------------------------------------------------------


class iOSInterface:
    """iOS 平台接口定义

    定义 iOS 端需要实现的协议方法, 通过 Swift/ObjC 桥接层调用。
    """

    PROTOCOL_DEFINITION = """
    // SymbioSDKProtocol.swift
    import Foundation

    @objc public protocol SymbioSDKProtocol {
        /// 初始化 SDK
        @objc func initialize(config: [String: Any], completion: @escaping (Result<Bool, Error>) -> Void)

        /// 发送请求
        @objc func sendRequest(method: String, payload: [String: Any], completion: @escaping (Result<[String: Any], Error>) -> Void)

        /// 接收事件
        @objc func addEventListener(eventType: String, handler: @escaping ([String: Any]) -> Void)

        /// 移除事件监听
        @objc func removeEventListener(eventType: String)

        /// 获取设备信息
        @objc func getDeviceInfo() -> [String: Any]

        /// 缓存数据
        @objc func cacheData(key: String, data: [String: Any], ttl: TimeInterval)

        /// 获取缓存
        @objc func getCachedData(key: String) -> [String: Any]?

        /// 清除缓存
        @objc func clearCache()

        /// 获取连接状态
        @objc func getConnectionState() -> String

        /// 断开连接
        @objc func disconnect()
    }

    // SymbioSDK.swift - 使用示例
    /*
    let sdk = SymbioSDK(config: [
        "apiBaseUrl": "https://api.symbio.ai",
        "apiKey": "your-api-key",
        "appId": "your-app-id"
    ])

    sdk.initialize { result in
        switch result {
        case .success:
            print("SDK 初始化成功")
        case .failure(let error):
            print("SDK 初始化失败: \\(error)")
        }
    }

    sdk.sendRequest(method: "chat", payload: ["message": "你好"]) { result in
        switch result {
        case .success(let response):
            print("回复: \\(response["content"] ?? "")")
        case .failure(let error):
            print("请求失败: \\(error)")
        }
    }
    */
    """

    @staticmethod
    def get_swift_config_mapping() -> dict[str, str]:
        """获取 Swift 配置字段映射"""
        return {
            "api_base_url": "apiBaseUrl",
            "api_key": "apiKey",
            "app_id": "appId",
            "device_id": "deviceId",
            "timeout_sec": "timeout",
            "enable_offline_cache": "enableOfflineCache",
            "cache_max_size_mb": "cacheMaxSize",
        }


class AndroidInterface:
    """Android 平台接口定义

    定义 Android 端需要实现的接口方法。
    """

    INTERFACE_DEFINITION = """
    // ISymbioSDK.kt
    package ai.symbio.sdk

    import kotlinx.coroutines.flow.Flow

    interface ISymbioSDK {
        /// 初始化 SDK
        suspend fun initialize(config: SDKConfig): Result<Boolean>

        /// 发送请求
        suspend fun sendRequest(method: String, payload: Map<String, Any>): Result<Map<String, Any>>

        /// 接收事件 (响应式流)
        fun observeEvents(eventType: String): Flow<Map<String, Any>>

        /// 获取设备信息
        fun getDeviceInfo(): DeviceInfo

        /// 缓存数据
        suspend fun cacheData(key: String, data: Map<String, Any>, ttlMs: Long)

        /// 获取缓存
        suspend fun getCachedData(key: String): Map<String, Any>?

        /// 清除缓存
        suspend fun clearCache()

        /// 获取连接状态
        fun getConnectionState(): ConnectionState

        /// 断开连接
        fun disconnect()
    }

    // 使用示例
    /*
    class MainActivity : AppCompatActivity() {
        private lateinit var sdk: ISymbioSDK

        override fun onCreate(savedInstanceState: Bundle?) {
            super.onCreate(savedInstanceState)

            sdk = SymbioSDK(context = this)
            lifecycleScope.launch {
                sdk.initialize(SDKConfig(
                    apiBaseUrl = "https://api.symbio.ai",
                    apiKey = "your-api-key",
                    appId = "your-app-id"
                ))

                val response = sdk.sendRequest("chat", mapOf("message" to "你好"))
                response.onSuccess { data ->
                    Log.d("Symbio", "回复: ${data["content"]}")
                }
            }
        }
    }
    */
    """

    @staticmethod
    def get_kotlin_config_mapping() -> dict[str, str]:
        """获取 Kotlin 配置字段映射"""
        return {
            "api_base_url": "apiBaseUrl",
            "api_key": "apiKey",
            "app_id": "appId",
            "device_id": "deviceId",
            "timeout_sec": "timeoutSec",
            "enable_offline_cache": "enableOfflineCache",
            "cache_max_size_mb": "cacheMaxSizeMb",
        }


# ---------------------------------------------------------------------------
# 离线缓存管理
# ---------------------------------------------------------------------------


class OfflineCacheManager:
    """离线缓存管理器"""

    def __init__(self, max_size_mb: float = 50.0):
        self._max_size_mb = max_size_mb
        self._cache: dict[str, OfflineCacheEntry] = {}

    def put(self, key: str, data: dict[str, Any], ttl_sec: float | None = None) -> None:
        """存入缓存"""
        from datetime import timedelta

        entry = OfflineCacheEntry(
            key=key,
            data=data,
            expires_at=datetime.now() + timedelta(seconds=ttl_sec) if ttl_sec else None,
        )
        self._cache[key] = entry
        self._evict_if_needed()

    def get(self, key: str) -> dict[str, Any] | None:
        """获取缓存"""
        entry = self._cache.get(key)
        if not entry:
            return None
        if entry.expires_at and entry.expires_at < datetime.now():
            del self._cache[key]
            return None
        return entry.data

    def remove(self, key: str) -> bool:
        """删除缓存"""
        if key in self._cache:
            del self._cache[key]
            return True
        return False

    def clear(self) -> int:
        """清空缓存"""
        count = len(self._cache)
        self._cache.clear()
        return count

    def get_pending_sync(self) -> list[OfflineCacheEntry]:
        """获取待同步的条目"""
        return [e for e in self._cache.values() if not e.synced]

    def mark_synced(self, key: str) -> None:
        """标记为已同步"""
        entry = self._cache.get(key)
        if entry:
            entry.synced = True

    def _evict_if_needed(self) -> None:
        """按需淘汰过期条目"""
        now = datetime.now()
        expired = [k for k, v in self._cache.items() if v.expires_at and v.expires_at < now]
        for k in expired:
            del self._cache[k]

    @property
    def size(self) -> int:
        return len(self._cache)


# ---------------------------------------------------------------------------
# 移动端 SDK
# ---------------------------------------------------------------------------


class MobileSDK:
    """移动端 SDK

    提供统一的移动端 API 接口, 支持 iOS 和 Android 平台。

    用法:
        sdk = MobileSDK(config=SDKConfig(platform=Platform.IOS, api_key="xxx"))
        sdk.connect()
        response = sdk.send_request("chat", {"message": "你好"})
    """

    def __init__(self, config: SDKConfig):
        self._config = config
        self._connection_state = ConnectionState.DISCONNECTED
        self._cache = OfflineCacheManager(config.cache_max_size_mb)
        self._event_listeners: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        self._request_log: list[SDKMessage] = []
        self._retry_count: int = 0

    @property
    def connection_state(self) -> ConnectionState:
        return self._connection_state

    @property
    def platform(self) -> Platform:
        return self._config.platform

    def connect(self) -> bool:
        """建立连接"""
        self._connection_state = ConnectionState.CONNECTING
        logger.info(
            f"SDK 连接中: platform={self._config.platform.value}, url={self._config.api_base_url}"
        )

        # 模拟连接
        self._connection_state = ConnectionState.CONNECTED
        self._retry_count = 0
        logger.info("SDK 已连接")
        return True

    def disconnect(self) -> None:
        """断开连接"""
        self._connection_state = ConnectionState.DISCONNECTED
        logger.info("SDK 已断开")

    def send_request(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        """发送请求

        Args:
            method: API 方法名
            payload: 请求数据
            timeout_sec: 超时时间

        Returns:
            响应数据

        Raises:
            ConnectionError: 未连接
            TimeoutError: 请求超时
        """
        if self._connection_state != ConnectionState.CONNECTED:
            # 离线模式: 尝试从缓存获取
            if self._config.enable_offline_cache:
                cached = self._cache.get(f"response:{method}")
                if cached:
                    logger.info(f"使用离线缓存: {method}")
                    return cached
            raise ConnectionError("SDK 未连接")

        message = SDKMessage(
            type=MessageType.REQUEST,
            method=method,
            payload=payload or {},
        )
        self._request_log.append(message)

        # 模拟请求
        response_data = {
            "status": "ok",
            "method": method,
            "data": payload or {},
            "message_id": message.message_id,
        }

        # 缓存响应
        if self._config.enable_offline_cache:
            self._cache.put(f"response:{method}", response_data, ttl_sec=3600)

        response_msg = SDKMessage(
            type=MessageType.RESPONSE,
            method=method,
            payload=response_data,
            correlation_id=message.message_id,
        )
        self._request_log.append(response_msg)

        return response_data

    def add_event_listener(
        self,
        event_type: str,
        handler: Callable[[dict[str, Any]], None],
    ) -> None:
        """添加事件监听"""
        if event_type not in self._event_listeners:
            self._event_listeners[event_type] = []
        self._event_listeners[event_type].append(handler)
        logger.debug(f"添加事件监听: {event_type}")

    def remove_event_listener(self, event_type: str) -> None:
        """移除事件监听"""
        self._event_listeners.pop(event_type, None)

    def dispatch_event(self, event_type: str, data: dict[str, Any]) -> None:
        """分发事件"""
        handlers = self._event_listeners.get(event_type, [])
        for handler in handlers:
            try:
                handler(data)
            except Exception as exc:
                logger.error(f"事件处理异常: {event_type} - {exc}")

    def cache_data(self, key: str, data: dict[str, Any], ttl_sec: float | None = None) -> None:
        """缓存数据"""
        self._cache.put(key, data, ttl_sec)

    def get_cached_data(self, key: str) -> dict[str, Any] | None:
        """获取缓存"""
        return self._cache.get(key)

    def clear_cache(self) -> int:
        """清空缓存"""
        return self._cache.clear()

    def get_device_info(self) -> DeviceInfo:
        """获取设备信息"""
        return DeviceInfo(
            device_id=self._config.device_id,
            platform=self._config.platform,
            sdk_version="1.0.0",
        )

    def get_api_spec(self) -> dict[str, Any]:
        """获取 API 规范 (供客户端代码生成)"""
        return {
            "sdk_version": "1.0.0",
            "platform": self._config.platform.value,
            "base_url": self._config.api_base_url,
            "endpoints": {
                "chat": {"method": "POST", "path": "/v1/chat"},
                "completion": {"method": "POST", "path": "/v1/completion"},
                "embedding": {"method": "POST", "path": "/v1/embedding"},
                "status": {"method": "GET", "path": "/v1/status"},
            },
            "events": {
                "message": "新消息事件",
                "typing": "输入中事件",
                "error": "错误事件",
                "connection_state": "连接状态变更事件",
            },
        }

    @staticmethod
    def get_platform_interface(platform: Platform) -> str:
        """获取平台接口定义"""
        if platform == Platform.IOS:
            return iOSInterface.PROTOCOL_DEFINITION
        elif platform == Platform.ANDROID:
            return AndroidInterface.INTERFACE_DEFINITION
        return "不支持的平台"
