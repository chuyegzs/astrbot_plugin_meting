import asyncio
import ipaddress
import os
import re
import shutil
import tempfile
import time
import uuid
import aiohttp
from pydub import AudioSegment
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import parse_qs, urljoin, urlparse
from packaging.version import parse as parse_version
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Json, Record
from astrbot.api.star import Context, Star, register
from astrbot.core.config.default import VERSION
from astrbot.core.pipeline.respond import stage



SOURCE_DISPLAY = {
    "tencent": "QQ音乐",
    "netease": "网易云音乐",
    "kugou": "酷狗音乐",
    "kuwo": "酷我音乐",
}

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=120)
CHUNK_SIZE = 8192
MAX_SESSION_AGE = 3600
AUDIO_CONTENT_TYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/ogg",
    "audio/x-m4a",
    "audio/mp4",
    "audio/x-matroska",
    "application/octet-stream",
}
TEMP_FILE_PREFIX = "astrbot_meting_plugin_"


class MetingPluginError(Exception):
    """插件基础异常"""

    pass


class DownloadError(MetingPluginError):
    """下载错误"""

    pass


class UnsafeURLError(MetingPluginError):
    """不安全的URL错误"""

    pass


class AudioFormatError(MetingPluginError):
    """音频格式错误"""

    pass


class SessionData:
    """会话数据封装类"""

    def __init__(self, default_source: str):
        self._source = default_source
        self._results = []
        self._timestamp = time.time()
        self._user_results = {}  # {user_id: {"results": [...], "timestamp": float, "msg_id": str|int}}
        self._shared_msg_id = None  # For non-restricted mode

    @property
    def source(self) -> str:
        return self._source

    @source.setter
    def source(self, value: str):
        self._source = value

    @property
    def results(self) -> list:
        return self._results

    @results.setter
    def results(self, value: list):
        self._results = value

    @property
    def timestamp(self) -> float:
        return self._timestamp

    def update_timestamp(self):
        self._timestamp = time.time()


def _detect_audio_format(data: bytes) -> str | None:
    """根据文件头检测音频格式

    Args:
        data: 文件开头字节

    Returns:
        str | None: 音频格式标识，未知返回 None
    """
    if len(data) < 4:
        return None

    if data.startswith(b"\xff\xfb") or data.startswith(b"\xff\xf3"):
        return "mp3"
    if data.startswith(b"\xff\xf2"):
        return "mp3"
    if data.startswith(b"ID3"):
        return "mp3"
    if data.startswith(b"RIFF"):
        return "wav"
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"fLaC"):
        return "flac"
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return "mp4"
    if data.startswith(b"\x00\x00\x00"):
        if len(data) >= 8 and data[4:8] == b"ftyp":
            return "mp4"

    return None


def _check_audio_magic(data: bytes) -> bool:
    """检查文件头是否为有效的音频格式

    Args:
        data: 文件开头字节

    Returns:
        bool: 是否为有效的音频文件头
    """
    return _detect_audio_format(data) is not None


def _get_extension_from_format(audio_format: str | None) -> str:
    """根据音频格式获取文件扩展名

    Args:
        audio_format: 音频格式标识

    Returns:
        str: 文件扩展名
    """
    mapping = {
        "mp3": ".mp3",
        "wav": ".wav",
        "ogg": ".ogg",
        "flac": ".flac",
        "mp4": ".m4a",
    }
    if audio_format is None:
        return ".mp3"
    return mapping.get(audio_format, ".mp3")


T = TypeVar("T")


@register("astrbot_plugin_meting", "chuyegzs", "基于 MetingAPI 的点歌插件", "1.0.8")
class MetingPlugin(Star):
    """MetingAPI 点歌插件

    支持多音源搜索和播放，自动分段发送长歌曲
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self._sessions: dict[str, SessionData] = {}
        self._sessions_lock: asyncio.Lock | None = None
        self._http_session: aiohttp.ClientSession | None = None
        self._ffmpeg_path = self._find_ffmpeg()
        self._cleanup_task = None
        self._download_semaphore: asyncio.Semaphore | None = None
        self._initialized = False
        self._init_lock: asyncio.Lock | None = None
        self._session_audio_locks = {}
        self._audio_locks_lock: asyncio.Lock | None = None

    async def _ensure_initialized(self):
        """确保插件已初始化（惰性初始化）"""
        if self._initialized:
            return

        if self._init_lock is None:
            self._init_lock = asyncio.Lock()

        async with self._init_lock:
            if self._initialized:
                return

            logger.info("MetingAPI 点歌插件正在初始化...")

            self._sessions_lock = asyncio.Lock()
            self._audio_locks_lock = asyncio.Lock()
            self._download_semaphore = asyncio.Semaphore(3)

            if not self._http_session:
                self._http_session = aiohttp.ClientSession(
                    timeout=REQUEST_TIMEOUT,
                    # 标识请求来源
                    headers={
                        "Referer": "https://astrbot.app/",
                        "User-Agent": f"AstrBot/{VERSION}",
                        "UAK": "AstrBot/plugin_meting",
                    },
                )

            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
            self._initialized = True
            logger.info("MetingAPI 点歌插件初始化完成")

            if self.use_music_card():
                try:
                    # 进行版本校验，检查是否支持 JSON 消息组件
                    is_unsupported = False
                    if parse_version(VERSION) < parse_version("4.17.6"):
                        # 版本号不得小于 4.17.6
                        is_unsupported = True
                    else:
                        with open(stage.__file__, encoding="utf-8") as f:
                            content = f.read()
                            # 不存在"Comp.Json"字样说明可能没有 JSON 消息组件支持
                            if "Comp.Json" not in content:
                                is_unsupported = True
                    if is_unsupported:
                        logger.warning(
                            "检测到当前 AstrBot 版本可能不支持 JSON 消息组件。请更新 AstrBot 版本，否则音乐卡片可能无法发送。"
                        )
                    else:
                        logger.debug("AstrBot 兼容性检查通过。")
                except Exception as e:
                    logger.debug(f"AstrBot 兼容性检查失败: {e}")

    async def initialize(self):
        """插件初始化（框架调用）"""
        await self._ensure_initialized()

    def _get_config(
        self, key: str, default: T, validator: Callable[[Any], Any] | None = None
    ) -> T:
        """获取配置值，支持类型和范围校验

        Args:
            key: 配置键
            default: 默认值
            validator: 校验函数，接受配置值，返回校验是否通过

        Returns:
            配置值或默认值
        """
        if not self.config:
            return default

        value = self.config.get(key, default)
        if validator is not None and not validator(value):
            return default

        return value

    def _get_api_config(self) -> dict:
        """获取 API 配置字典"""
        return self._get_config("api_config", {}, lambda x: isinstance(x, dict))

    def get_api_url(self) -> str:
        """获取 API 地址

        Returns:
            str: API 地址，如果未配置则返回空字符串
        """
        api_config = self._get_api_config()
        api_url = api_config.get("api_url", "https://musicapi.chuyel.top/meting/")
        if api_url == "custom":
            # 仅当选择了自定义 API 类型时才使用 custom_api_url 配置项
            url = api_config.get("custom_api_url", "")
            if not url:
                logger.warning(
                    "API 地址设置为 custom 但未填写 custom_api_url，将回退到默认接口"
                )
                url = "https://musicapi.chuyel.top/meting/"
        else:
            url = api_url
        if not url:
            return ""
        url = url.replace("http://", "https://")
        return url if url.endswith("/") else f"{url}/"

    def get_api_type(self) -> int:
        """获取 API 类型

        Returns:
            int: API 类型，1=Node API, 2=PHP API, 3=自定义参数
        """
        api_config = self._get_api_config()
        api_url = api_config.get("api_url", "https://musicapi.chuyel.top/meting/")
        if api_url == "https://musicapi.chuyel.top/meting/":
            return 1
        elif api_url == "https://musictsapi.chuye.us.kg/":
            return 1
        elif api_url == "https://musicapi.chuyel.top/":
            return 1
        elif api_url == "https://metingapi.nanorocky.top/":
            return 2
        elif api_url == "custom":
            if not api_config.get("custom_api_url", ""):
                return 1

            # 仅当选择了自定义 API 地址时才使用 api_type 配置项
            api_type = api_config.get("api_type", 1)
            api_type = (
                api_type if isinstance(api_type, int) and api_type in (1, 2, 3) else 1
            )

            if api_type == 3:
                template = api_config.get("custom_api_template", "")
                if not template:
                    logger.warning(
                        "API 类型设置为 3 但未填写 custom_api_template，将回退到类型 1"
                    )
                    return 1
            return api_type

        return 1

    def get_custom_api_template(self) -> str:
        """获取自定义 API 模板

        Returns:
            str: 自定义 API 模板，如果未配置则返回空字符串
        """
        api_config = self._get_api_config()
        api_url = api_config.get("api_url", "")

        if api_url == "custom":
            if not api_config.get("custom_api_url", ""):
                return ""
            template = api_config.get("custom_api_template", "")
            return template if isinstance(template, str) else ""
        return ""

    def get_sign_api_url(self) -> str:
        """音乐卡片签名 API 地址

        Returns:
            str: 签名 API 地址
        """
        url = str(
            self._get_config("api_sign_url", "https://oiapi.net/api/QQMusicJSONArk/")
        ).rstrip("/")
        url = url.replace("http://", "https://")
        return url if url.endswith("/") else f"{url}/"

    def use_music_card(self) -> bool:
        """音乐卡片开关

        Returns:
            bool: 是否启用音乐卡片
        """
        return bool(self._get_config("use_music_card", True))

    def _build_api_url_for_custom(
        self, api_url: str, template: str, server: str, req_type: str, id_val: str
    ) -> str:
        """根据模板构建 API URL（自定义参数类型）

        Args:
            api_url: 基础 API 地址
            template: API 模板
            server: 音源
            req_type: 请求类型
            id_val: ID 值

        Returns:
            str: 完整的 API URL
        """
        query = template.replace(":server", server)
        query = query.replace(":type", req_type)
        query = query.replace(":id", id_val)
        query = query.replace(":r", str(int(time.time() * 1000)))

        if query.startswith("/") or query.startswith("?"):
            return f"{api_url.rstrip('/')}{query}"

        if "?" in api_url:
            return f"{api_url}&{query}"
        else:
            return f"{api_url}?{query}"

    def get_default_source(self) -> str:
        """获取默认音源

        Returns:
            str: 默认音源，默认为 netease
        """
        return self._get_config(
            "default_source", "netease", lambda x: x in SOURCE_DISPLAY
        )

    def get_search_result_count(self) -> int:
        """获取搜索结果显示数量

        Returns:
            int: 搜索结果显示数量，范围 5-30，默认 10
        """
        return self._get_config(
            "search_result_count", 10, lambda x: isinstance(x, int) and 5 <= x <= 30
        )

    def get_segment_duration(self) -> int:
        """获取分段时长

        Returns:
            int: 分段时长（秒），默认 120
        """
        return self._get_config(
            "segment_duration", 120, lambda x: isinstance(x, int) and 30 <= x <= 300
        )

    def get_send_interval(self) -> float:
        """获取发送间隔

        Returns:
            float: 发送间隔（秒），默认 1.0
        """
        return self._get_config(
            "send_interval", 1.0, lambda x: isinstance(x, (int, float)) and 0 <= x <= 10
        )

    def get_max_file_size(self) -> int:
        """获取最大文件大小

        Returns:
            int: 最大文件大小（字节），默认 50MB = 52428800 字节
        """
        try:
            mb = self._get_config(
                "max_file_size",
                50,
                lambda x: isinstance(x, (int, float)) and 10 <= x <= 200,
            )
            # 确保 mb 是数值类型
            if not isinstance(mb, (int, float)):
                logger.warning(f"max_file_size 配置无效: {mb}，使用默认值 50")
                mb = 50
            return int(mb) * 1024 * 1024
        except Exception as e:
            logger.error(f"获取 max_file_size 配置时出错: {e}，使用默认值 50MB")
            return 50 * 1024 * 1024

    def get_search_result_expiration_time(self) -> int:
        """获取搜索结果过期时间"""
        return self._get_config(
            "search_result_expiration_time",
            120,
            lambda x: isinstance(x, int) and 30 <= x <= 300,
        )

    def get_search_results_withdrawn_after_timeout(self) -> int:
        """获取搜索结果超时撤回时间"""
        return self._get_config(
            "search_results_withdrawn_after_timeout",
            60,
            lambda x: isinstance(x, int) and -1 <= x <= 300,
        )

    def get_search_result_restrictions(self) -> bool:
        """获取搜索结果限制"""
        return self._get_config(
            "search_result_restrictions", False, lambda x: isinstance(x, bool)
        )

    async def _get_session(self, session_id: str) -> SessionData:
        """获取会话状态（线程安全）

        Args:
            session_id: 会话 ID

        Returns:
            SessionData: 会话状态对象
        """
        if self._sessions_lock is None:
            raise MetingPluginError("插件未正确初始化：_sessions_lock 为空")
        async with self._sessions_lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = SessionData(self.get_default_source())
            return self._sessions[session_id]

    async def _update_session_timestamp(self, session_id: str):
        """更新会话时间戳（线程安全）

        Args:
            session_id: 会话 ID
        """
        if self._sessions_lock is None:
            raise MetingPluginError("插件未正确初始化：_sessions_lock 为空")
        async with self._sessions_lock:
            if session_id in self._sessions:
                self._sessions[session_id].update_timestamp()
            await self._cleanup_old_sessions_locked()

    async def _get_session_audio_lock(self, session_id: str) -> asyncio.Lock:
        """获取会话级别的音频处理锁

        Args:
            session_id: 会话 ID

        Returns:
            asyncio.Lock: 音频处理锁
        """
        if self._audio_locks_lock is None:
            raise MetingPluginError("插件未正确初始化：_audio_locks_lock 为空")
        async with self._audio_locks_lock:
            if session_id not in self._session_audio_locks:
                self._session_audio_locks[session_id] = asyncio.Lock()
            return self._session_audio_locks[session_id]

    def _find_ffmpeg(self) -> str:
        """查找 FFmpeg 路径

        Returns:
            str: FFmpeg 可执行文件路径，未找到返回空字符串
        """
        ffmpeg_exe = shutil.which("ffmpeg")
        if ffmpeg_exe:
            logger.info(f"找到 FFmpeg: {ffmpeg_exe}")
            return ffmpeg_exe
        logger.warning("未找到 FFmpeg，请确保已安装 FFmpeg")
        return ""

    # 本地主机名黑名单（包括各种变体）
    _LOCAL_HOSTNAMES = frozenset(
        {
            "localhost",
            "127.0.0.1",
            "0.0.0.0",
            "::1",
            "[::1]",
            "0000:0000:0000:0000:0000:0000:0000:0001",
            "0:0:0:0:0:0:0:1",
            "0177.0.0.1",  # 八进制形式的 127.0.0.1
            "0x7f.0.0.1",  # 十六进制形式的 127.0.0.1
        }
    )

    def _is_private_ip(self, ip_str: str) -> bool:
        """判断 IP 是否为私网地址

        Args:
            ip_str: IP 地址字符串

        Returns:
            bool: 是否为私网地址
        """
        try:
            ip = ipaddress.ip_address(ip_str)
            return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        except ValueError:
            return False

    def _is_local_hostname(self, hostname: str) -> bool:
        """检查主机名是否为本地地址

        Args:
            hostname: 主机名

        Returns:
            bool: 是否为本地地址
        """
        hostname_lower = hostname.lower().strip("[]")  # 移除 IPv6 的方括号
        if hostname_lower in self._LOCAL_HOSTNAMES:
            return True

        # 检查是否是 127.0.0.0/8 网段的其他地址（如 127.0.0.2, 127.1.2.3 等）
        try:
            ip = ipaddress.ip_address(hostname_lower)
            if ip.is_loopback:
                return True
        except ValueError:
            pass

        return False

    async def _resolve_hostname_async(self, hostname: str) -> list:
        """异步解析主机名为 IP 地址列表

        Args:
            hostname: 主机名

        Returns:
            list: IP 地址列表
        """
        try:
            loop = asyncio.get_running_loop()
            addrinfo = await loop.getaddrinfo(hostname, None)
            return [addr[4][0] for addr in addrinfo]
        except Exception:
            return []

    async def _validate_url(
        self, url: str, strict_dns: bool = True
    ) -> tuple[bool, str]:
        """验证 URL 是否安全，防止 SSRF 攻击

        Args:
            url: 要验证的 URL
            strict_dns: 是否严格检查 DNS 解析，默认为 True。
                        对于歌曲下载 URL 可设为 False，允许 DNS 解析失败

        Returns:
            tuple[bool, str]: (是否安全, 失败原因)
        """
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return False, f"不支持的协议: {parsed.scheme}"

            hostname = parsed.hostname or ""
            if not hostname:
                return False, "URL 缺少主机名"

            # 使用新的本地主机名检查方法
            if self._is_local_hostname(hostname):
                return False, f"禁止访问本地地址: {hostname}"

            # 检查是否是 IP 地址
            ip_match = re.match(r"^(\d+\.){3}\d+$", hostname)
            if ip_match:
                if self._is_private_ip(hostname):
                    return False, f"禁止访问私网地址: {hostname}"
            else:
                ips = await self._resolve_hostname_async(hostname)
                if not ips:
                    if strict_dns:
                        logger.warning(f"[URL 验证] 无法解析主机名: {hostname}")
                        return False, f"无法解析主机名: {hostname}"
                    else:
                        # 非严格模式下，记录警告但允许通过
                        logger.warning(
                            f"[URL 验证] 无法解析主机名: {hostname}，但允许继续"
                        )
                        return True, ""
                for ip in ips:
                    if self._is_private_ip(ip):
                        return False, f"主机名解析到私网地址: {hostname} -> {ip}"

            return True, ""
        except Exception as e:
            logger.error(f"URL 验证失败: {e}")
            return False, f"URL 验证异常: {e}"

    async def _cleanup_old_sessions_locked(self):
        """清理过期的会话状态（必须在持锁状态下调用）"""
        current_time = time.time()
        expired_sessions = [
            sid
            for sid, session in self._sessions.items()
            if current_time - session.timestamp > MAX_SESSION_AGE
        ]
        for sid in expired_sessions:
            self._sessions.pop(sid, None)
            self._session_audio_locks.pop(sid, None)
        if expired_sessions:
            logger.debug(f"清理了 {len(expired_sessions)} 个过期会话")

    async def _periodic_cleanup(self):
        """定期清理过期的会话状态和临时文件"""
        while True:
            try:
                await asyncio.sleep(3600)
                lock = self._sessions_lock
                if lock is None:
                    logger.error(
                        "定期清理任务检测到 _sessions_lock 为 None，停止清理循环"
                    )
                    break
                async with lock:
                    await self._cleanup_old_sessions_locked()

                # 在线程池中执行清理文件操作
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._cleanup_temp_files)

                logger.debug("定期清理完成")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定期清理时发生错误: {e}")

    def _cleanup_temp_files(self):
        """清理本插件产生的临时文件"""
        try:
            temp_dir = tempfile.gettempdir()
            count = 0
            for filename in os.listdir(temp_dir):
                if filename.startswith(TEMP_FILE_PREFIX):
                    filepath = os.path.join(temp_dir, filename)
                    try:
                        if os.path.isfile(filepath):
                            file_age = time.time() - os.path.getmtime(filepath)
                            if file_age > 300:
                                os.remove(filepath)
                                count += 1
                    except Exception:
                        pass
            if count > 0:
                logger.debug(f"清理了 {count} 个临时文件")
        except Exception as e:
            logger.error(f"清理临时文件时发生错误: {e}")

    async def _get_session_source(self, session_id: str) -> str:
        """获取会话音源

        Args:
            session_id: 会话 ID

        Returns:
            str: 会话音源，如果未设置则返回默认音源
        """
        session = await self._get_session(session_id)
        return session.source

    async def _set_session_source(self, session_id: str, source: str):
        """设置会话音源

        Args:
            session_id: 会话 ID
            source: 音源
        """
        session = await self._get_session(session_id)
        session.source = source
        await self._update_session_timestamp(session_id)

    async def _set_session_results(
        self,
        session_id: str,
        results: list,
        sender_id: str | None = None,
        msg_id: Any = None,
    ):
        """设置会话搜索结果（线程安全）

        Args:
            session_id: 会话 ID
            results: 搜索结果列表
            sender_id: 发送者 ID
            msg_id: 消息 ID
        """
        if self._sessions_lock is None:
            raise MetingPluginError("插件未正确初始化：_sessions_lock 为空")
        async with self._sessions_lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = SessionData(self.get_default_source())

            session = self._sessions[session_id]
            restriction = self.get_search_result_restrictions()

            if restriction and sender_id:
                session._user_results[sender_id] = {
                    "results": results,
                    "timestamp": time.time(),
                    "msg_id": msg_id,
                }
                session.update_timestamp()
            else:
                session.results = results
                session.update_timestamp()
                session._shared_msg_id = msg_id

            await self._cleanup_old_sessions_locked()

    async def _get_session_results(
        self, session_id: str, sender_id: str | None = None
    ) -> list:
        """获取会话搜索结果（线程安全）

        Args:
            session_id: 会话 ID
            sender_id: 发送者 ID，如果启用限制则用于获取特定用户的结果

        Returns:
            list: 搜索结果列表
        """
        if self._sessions_lock is None:
            raise MetingPluginError("插件未正确初始化：_sessions_lock 为空")

        expiration_time = self.get_search_result_expiration_time()
        restriction = self.get_search_result_restrictions()

        async with self._sessions_lock:
            if session_id not in self._sessions:
                return []

            session = self._sessions[session_id]
            current_time = time.time()

            # 优先检查用户专属结果
            if restriction and sender_id and sender_id in session._user_results:
                user_data = session._user_results[sender_id]
                timestamp = user_data["timestamp"]

                if current_time - timestamp > expiration_time:
                    # 已过期，清除
                    del session._user_results[sender_id]
                    logger.debug(
                        f"Session {session_id} User {sender_id} search results expired."
                    )
                    return []
                return user_data["results"]

            # 如果没有专属结果或未启用限制，检查共享结果
            if restriction:
                return []

            # 未开启限制，使用共享结果
            if current_time - session.timestamp > expiration_time:
                session.results = []
                logger.debug(f"Session {session_id} shared search results expired.")
                return []

            return session.results

    async def _perform_search(self, keyword: str, source: str) -> list | None:
        """执行搜索并返回结果列表"""
        api_url = self.get_api_url()
        api_type = self.get_api_type()
        custom_api_template = self.get_custom_api_template()

        try:
            if api_type == 3:
                api_endpoint = self._build_api_url_for_custom(
                    api_url, custom_api_template, source, "search", keyword
                )
                logger.info(f"[搜歌] 自定义API URL: {api_endpoint}")
                if self._http_session is None:
                    return None
                async with self._http_session.get(api_endpoint) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            elif api_type == 2:
                params = {
                    "server": source,
                    "type": "search",
                    "id": "0",
                    "dwrc": "false",
                    "keyword": keyword,
                }
                logger.info(f"[搜歌] PHP API URL: {api_url}, 参数: {params}")
                if self._http_session is None:
                    return None
                async with self._http_session.get(api_url, params=params) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            else:
                params = {"server": source, "type": "search", "id": keyword}
                api_endpoint = f"{api_url}api"
                logger.info(f"[搜歌] Node API URL: {api_endpoint}, 参数: {params}")
                if self._http_session is None:
                    return None
                async with self._http_session.get(api_endpoint, params=params) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()

            if not isinstance(data, list) or not data:
                return []

            result_count = self.get_search_result_count()
            return data[:result_count]

        except Exception as e:
            logger.error(f"搜索歌曲时发生错误: {e}", exc_info=True)
            return None

    async def _play_song_logic(
        self, event: AstrMessageEvent, song: dict, session_id: str
    ):
        """播放歌曲的通用逻辑"""
        song_url = song.get("url")

        if not song_url:
            yield event.plain_result("获取歌曲播放地址失败")
            return

        # 歌曲 URL 使用非严格模式验证，允许 DNS 解析失败
        # 因为歌曲 URL 来自可信的 MetingAPI，且可能是临时 CDN 地址
        is_valid, reason = await self._validate_url(song_url, strict_dns=False)
        if not is_valid:
            logger.error(f"检测到不安全的 URL: {song_url}, 原因: {reason}")
            yield event.plain_result(f"歌曲地址无效: {reason}")
            return

        # 音乐卡片
        if self.use_music_card():
            title = song.get("name") or song.get("title") or "未知"
            artist = song.get("artist") or song.get("author") or "未知歌手"
            cover = song.get("pic", "")
            source = song.get("source") or await self._get_session_source(session_id)

            if cover:
                # 设置封面 URL
                if source == "netease":
                    # 不知道为什么，Q音接口现在指定封面大小有概率爆炸...
                    connector = "&" if "?" in cover else "?"
                    cover = f"{cover}{connector}picsize=320"
                try:
                    if self._http_session:
                        async with self._http_session.get(
                            cover, allow_redirects=False
                        ) as c_resp:
                            if c_resp.status in (301, 302):
                                cover = c_resp.headers.get("Location", cover)
                except Exception as e:
                    logger.warning(f"解析封面跳转失败: {e}")

            song_id = ""
            try:
                query = urlparse(song_url).query
                song_id = parse_qs(query).get("id", [""])[0]
            except Exception:
                pass

            # 根据音源设置对应的跳转链接
            if source == "netease":
                jump_url = f"https://music.163.com/#/song?id={song_id}"
                fmt = "163"
            elif source == "tencent":
                jump_url = f"https://y.qq.com/n/ryqq/songDetail/{song_id}"
                fmt = "qq"
            elif source == "bilibili":
                jump_url = f"https://www.bilibili.com/audio/{song_id}"
                fmt = "bilibili"
            elif source == "kugou":
                jump_url = f"https://www.kugou.com/song/#{song_id}"
                fmt = "kugou"
            elif source == "kuwo":
                jump_url = f"https://kuwo.cn/play_detail/{song_id}"
                fmt = "kuwo"
            else:
                jump_url = song_url.replace("type=url", "type=song")
                fmt = "163"

            if not self._http_session:
                yield event.plain_result("HTTP Session 未初始化")
                return

            # 强制将所有 URL 转换为 https
            song_url = song_url.replace("http://", "https://")
            if cover:
                cover = cover.replace("http://", "https://")
            if jump_url:
                jump_url = jump_url.replace("http://", "https://")

            sign_api = self.get_sign_api_url()
            params = {
                "url": song_url,
                "song": title,
                "singer": artist,
                "cover": cover,
                "jump": jump_url,
                "format": fmt,
            }
            try:
                async with self._http_session.get(sign_api, params=params) as resp:
                    if resp.status != 200:
                        yield event.plain_result(f"签名接口请求失败: {resp.status}")
                        return
                    res_json = await resp.json()
                    if res_json.get("code") == 1:
                        ark_data = res_json.get("data")
                        token = ark_data.get("config", {}).get("token", "")
                        json_card = Json(data=ark_data, config={"token": token})
                        logger.info("音乐卡片签名成功，发送卡片")
                        logger.debug(f"卡片数据: {json_card}")
                        yield event.chain_result([json_card])

                        preview = res_json.get("preview","")
                        music_url = res_json.get("music_url","")

                        try:
                            async with self._http_session.get(sign_api, params=params) as resp:
                                if resp.status != 200:
                                    yield event.plain_result(f"音樂请求失败: {resp.status}")
                                    return
                                res_data = await resp.read()
                                file_type = _detect_audio_format(res_data)
                                logger.info(f"Type:{file_type}")
                                
                        except Exception as e:
                            logger.error(f"音樂请求异常: {e}")
                            file_type = "mp3"
                                
          
                        chain = [
                            Json.Plain(f"{title}"),
                            Json.Image.fromURL(preview),
                            Json.File(file=f"{music_url}", name=f"{title}.{file_type}"),
                            Json.Plain(f"URL:\n{music_url}")
                        ]

                        yield event.chain_result(chain)
                    
                    else:
                        yield event.plain_result(
                            f"签名失败: {res_json.get('message', '未知错误')}"
                        )
                        
            except Exception as e:
                logger.error(f"音乐卡片请求异常: {e}")
                yield event.plain_result("制作卡片时出错")
            return

        # 普通语音发送模式
        try:
            temp_file = await self._download_song(song_url, event.get_sender_id())
            if not temp_file:
                return

            yield event.plain_result("正在分段录制歌曲...")
            async for result in self._split_and_send_audio(
                event, temp_file, session_id
            ):
                yield result

        except asyncio.CancelledError:
            logger.info("播放任务被取消")
            yield event.plain_result("播放已取消")
        except DownloadError as e:
            logger.error(f"下载歌曲失败: {e}")
            yield event.plain_result(f"下载失败: {e}")
        except UnsafeURLError as e:
            logger.error(f"URL 安全检查失败: {e}")
            yield event.plain_result(f"安全检查失败: {e}")
        except AudioFormatError as e:
            logger.error(f"音频格式错误: {e}")
            yield event.plain_result(f"格式不支持: {e}")
        except Exception as e:
            logger.error(f"播放歌曲时发生错误: {e}", exc_info=True)
            yield event.plain_result("播放失败，请稍后重试")

    @filter.command("toqqmusic", alias={"切换腾讯音乐", "切换QQMusic"})
    async def switch_tencent(self, event: AstrMessageEvent):
        """切换当前会话的音源为QQ音乐"""
        await self._ensure_initialized()
        session_id = event.unified_msg_origin
        await self._set_session_source(session_id, "tencent")
        yield event.plain_result("已切换音源为QQ音乐")

    @filter.command(
        "to163",
        alias={
            "切换网易",
            "切换网易云音乐",
            "切换网抑云",
            "切换网抑云音乐",
            "切换CloudMusic",
        },
    )
    async def switch_netease(self, event: AstrMessageEvent):
        """切换当前会话的音源为网易云"""
        await self._ensure_initialized()
        session_id = event.unified_msg_origin
        await self._set_session_source(session_id, "netease")
        yield event.plain_result("已切换音源为网易云")

    @filter.command("tocodog", alias={"切换酷狗音乐"})
    async def switch_kugou(self, event: AstrMessageEvent):
        """切换当前会话的音源为酷狗"""
        await self._ensure_initialized()
        session_id = event.unified_msg_origin
        await self._set_session_source(session_id, "kugou")
        yield event.plain_result("已切换音源为酷狗")

    @filter.command("tocowa", alias={"切换酷我音乐"})
    async def switch_kuwo(self, event: AstrMessageEvent):
        """切换当前会话的音源为酷我"""
        await self._ensure_initialized()
        session_id = event.unified_msg_origin
        await self._set_session_source(session_id, "kuwo")
        yield event.plain_result("已切换音源为酷我")

    async def _handle_specific_source_play(
        self, event: AstrMessageEvent, source: str, prefixes: list[str]
    ):
        """处理特定音源的点歌请求

        Args:
            event: 消息事件
            source: 音源标识
            prefixes: 命令前缀列表
        """
        await self._ensure_initialized()
        msg = event.get_message_str().strip()
        kw = msg
        for prefix in prefixes:
            if kw.startswith(prefix):
                kw = kw[len(prefix) :].strip()
                break

        if not kw:
            yield event.plain_result(
                f"请输入要点播的歌曲名称，例如：{prefixes[0]} 一期一会"
            )
            return

        results = await self._perform_search(kw, source)
        if not results:
            yield event.plain_result(f"未找到歌曲: {kw}")
            return

        song = results[0]
        if "source" not in song:
            song["source"] = source

        async for result in self._play_song_logic(
            event, song, event.unified_msg_origin
        ):
            yield result

    @filter.command("163song", alias={"网易云点歌", "网抑云点歌", "网易云音乐点歌"})
    async def play_netease_first_song(self, event: AstrMessageEvent):
        """网易云点歌"""
        async for result in self._handle_specific_source_play(
            event, "netease", ["网易云音乐点歌", "网易云点歌", "网抑云点歌", "网易点歌"]
        ):
            yield result

    @filter.command("qqsong", alias={"QQ点歌", "QQ音乐点歌", "腾讯音乐点歌"})
    async def play_tencent_first_song(self, event: AstrMessageEvent):
        """QQ音乐点歌"""
        async for result in self._handle_specific_source_play(
            event, "tencent", ["腾讯音乐点歌", "QQ音乐点歌", "腾讯点歌", "QQ点歌"]
        ):
            yield result

    @filter.command("codogsong", alias={"酷狗音乐点歌"})
    async def play_kugou_first_song(self, event: AstrMessageEvent):
        """酷狗点歌"""
        async for result in self._handle_specific_source_play(
            event, "kugou", ["酷狗音乐点歌", "酷狗点歌"]
        ):
            yield result

    @filter.command("cowasong", alias={"酷我音乐点歌"})
    async def play_kuwo_first_song(self, event: AstrMessageEvent):
        """酷我点歌"""
        async for result in self._handle_specific_source_play(
            event, "kuwo", ["酷我音乐点歌", "酷我点歌"]
        ):
            yield result

    @filter.command("song_help", alias={"点歌帮助", "点歌说明", "点歌指南", "点歌菜单"})
    async def show_commands(self, event: AstrMessageEvent):
        # 显示所有可用指令
        commands = [
            "🎵 MetingAPI 点歌插件指令列表 🎵",
            "========================",
            "【基础指令】",
            "• 搜歌 <歌名> - 搜索歌曲并显示列表",
            "• 点歌 <序号> - 播放搜索列表中的指定歌曲",
            "• 点歌 <歌名> - 直接搜索并播放第一首歌曲",
            "",
            "【快捷点歌】(忽略全局音源设置)",
            "• 网易点歌 <歌名> - 在网易云音乐中搜索并播放",
            "• QQ点歌 <歌名> - 在QQ音乐中搜索并播放",
            "• 酷狗点歌 <歌名> - 在酷狗音乐中搜索并播放",
            "• 酷我点歌 <歌名> - 在酷我音乐中搜索并播放",
            "",
            "【音源切换】(影响'搜歌'和'点歌'指令)",
            "• 切换网易云 - 切换默认音源为网易云音乐",
            "• 切换QQ音乐 - 切换默认音源为QQ音乐",
            "• 切换酷狗 - 切换默认音源为酷狗音乐",
            "• 切换酷我 - 切换默认音源为酷我音乐",
            "========================",
        ]
        yield event.plain_result("\n".join(commands))

    @filter.command("song_ns",alias={"点歌", "點歌"})
    async def play_song_cmd(self, event: AstrMessageEvent):
        """点歌指令，支持序号或歌名"""
        await self._ensure_initialized()

        message_str = event.get_message_str().strip()
        session_id = event.unified_msg_origin
        sender_id = event.get_sender_id()

        logger.info(f"MES:{message_str}")
        logger.info(f"SESID:{session_id}")
        logger.info(f"SENDID:{sender_id}")

        
        arg = message_str[1:]
        logger.info(f"取得：{arg}")

        if not arg:
            yield event.plain_result(
                "请输入要点播的歌曲序号或名称，例如：点歌 1 或 点歌 一期一会"
            )
            return

        if arg.isdigit() and 1 <= int(arg) <= 100:
            index = int(arg)
            logger.info(f"[点歌] 播放模式，序号: {index}")

            results = await self._get_session_results(session_id, sender_id)
            logger.info(f"[点歌] 会话结果数量: {len(results)}")

            if not results:
                yield event.plain_result('请先使用"搜歌 歌曲名"搜索歌曲')
                return

            if index < 1 or index > len(results):
                yield event.plain_result(
                    f"序号超出范围，请输入 1-{len(results)} 之间的序号"
                )
                return

            # 如果 withdrawn_after_timeout 为 0，点歌成功后撤回搜索结果
            withdrawn_timeout = self.get_search_results_withdrawn_after_timeout()
            if withdrawn_timeout == 0:
                # 立即清除搜索结果
                if self._sessions_lock:
                    msg_to_delete = None
                    async with self._sessions_lock:
                        if session_id in self._sessions:
                            sess = self._sessions[session_id]
                            if self.get_search_result_restrictions():
                                if sender_id in sess._user_results:
                                    msg_to_delete = sess._user_results[sender_id].get(
                                        "msg_id"
                                    )
                                    del sess._user_results[sender_id]
                            else:
                                msg_to_delete = sess._shared_msg_id
                                sess.results = []
                                sess._shared_msg_id = None

                    logger.debug(f"点歌成功，立即清除搜索结果 (Session: {session_id})")
                    if msg_to_delete:
                        await self._delete_search_msg(event, msg_to_delete)

            song = results[index - 1]
            async for result in self._play_song_logic(event, song, session_id):
                yield result
        else:
            logger.info(f"[点歌] 搜索并播放模式，歌名: {arg}")
            source = await self._get_session_source(session_id)
            results = await self._perform_search(arg, source)
            if not results:
                yield event.plain_result(f"未找到歌曲: {arg}")
                return

            song = results[0]
            if "source" not in song:
                song["source"] = source

            async for result in self._play_song_logic(event, song, session_id):
                yield result

    async def _delete_search_msg(self, event: AstrMessageEvent, msg_id: Any):
        """尝试撤回消息"""
        if not msg_id:
            return

        try:
            # 针对 OneBot V11 (aiocqhttp/NapCat) 的撤回逻辑
            if event.platform_meta.name == "aiocqhttp" or hasattr(event, "bot"):
                bot = getattr(event, "bot", None)
                if bot and hasattr(bot, "delete_msg"):
                    logger.info(f"尝试撤回搜索结果消息: {msg_id}")
                    await bot.delete_msg(message_id=msg_id)
        except Exception as e:
            logger.warning(f"撤回消息失败: {e}")

    async def _clear_search_results_delayed(
        self,
        session_id: str,
        sender_id: str,
        delay: int,
        event: AstrMessageEvent | None = None,
    ):
        """延迟清除搜索结果"""
        logger.debug(
            f"Scheduled to clear search results for {session_id} (user {sender_id}) in {delay}s"
        )
        await asyncio.sleep(delay)

        if self._sessions_lock is None:
            return

        async with self._sessions_lock:
            if session_id not in self._sessions:
                return
            sess = self._sessions[session_id]
            msg_to_delete = None

            if self.get_search_result_restrictions():
                if sender_id in sess._user_results:
                    user_data = sess._user_results[sender_id]
                    # Check if the result is still the one we scheduled for (by checking timestamp)
                    if time.time() - user_data["timestamp"] >= delay - 0.5:
                        msg_to_delete = user_data.get("msg_id")
                        del sess._user_results[sender_id]
                        logger.debug(
                            f"Search results for user {sender_id} in {session_id} cleared due to timeout."
                        )
                    else:
                        logger.debug(
                            f"Skipping clearance for {sender_id}: results updated recently."
                        )
            else:
                # Shared mode
                if time.time() - sess.timestamp >= delay - 0.5:
                    msg_to_delete = sess._shared_msg_id
                    sess.results = []
                    sess._shared_msg_id = None
                    logger.debug(
                        f"Shared search results in {session_id} cleared due to timeout."
                    )

        if msg_to_delete and event:
            await self._delete_search_msg(event, msg_to_delete)

    @filter.command("搜歌")
    async def search_song(self, event: AstrMessageEvent):
        """搜索歌曲（搜歌 xxx格式）

        Args:
            event: 消息事件
        """
        await self._ensure_initialized()

        message_str = event.get_message_str().strip()
        session_id = event.unified_msg_origin
        sender_id = event.get_sender_id()

        if message_str.startswith("搜歌"):
            keyword = message_str[2:].strip()
        else:
            keyword = message_str

        if not keyword:
            yield event.plain_result("请输入要搜索的歌曲名称，例如：搜歌 一期一会")
            return

        logger.info(f"[搜歌] 搜索模式，关键词: {keyword}")

        source = await self._get_session_source(session_id)
        results = await self._perform_search(keyword, source)

        if results is None:
            yield event.plain_result("搜索失败，请稍后重试")
            return

        if not results:
            yield event.plain_result(f"未找到歌曲: {keyword}")
            return

        message = f"搜索结果（音源: {SOURCE_DISPLAY.get(source, source)}）:\n"
        for idx, song in enumerate(results, 1):
            name = song.get("name") or song.get("title") or "未知"
            artist = song.get("artist") or song.get("author") or "未知歌手"
            message += f"{idx}. {name} - {artist}\n"

        message += '\n发送"点歌 1"播放第一首歌曲'

        # 尝试直接发送消息以获取 Message ID (针对自动撤回功能)
        msg_id = None
        sent_success = False
        withdrawn_timeout = self.get_search_results_withdrawn_after_timeout()

        if withdrawn_timeout != -1:
            try:
                if event.platform_meta.name == "aiocqhttp" or hasattr(event, "bot"):
                    bot = getattr(event, "bot", None)
                    if bot:
                        group_id = event.message_obj.group_id
                        ret = None
                        if group_id:
                            ret = await bot.send_group_msg(
                                group_id=int(group_id), message=message
                            )
                        else:
                            # 私聊 注意：AiocqhttpMessageEvent 的 session_id 通常是 user_id
                            if event.session_id and event.session_id.isdigit():
                                ret = await bot.send_private_msg(
                                    user_id=int(event.session_id), message=message
                                )

                        if ret and isinstance(ret, dict) and "message_id" in ret:
                            msg_id = ret["message_id"]
                            sent_success = True
            except Exception as e:
                logger.warning(f"尝试直接发送搜索结果失败，回退到默认方式: {e}")

        if not sent_success:
            yield event.plain_result(message)

        await self._set_session_results(session_id, results, sender_id, msg_id)

        # 处理超时自动撤回（实际上是清除缓存 + 撤回消息）
        if withdrawn_timeout > 0:
            asyncio.create_task(
                self._clear_search_results_delayed(
                    session_id, sender_id, withdrawn_timeout, event
                )
            )

    async def _download_song(self, url: str, sender_id: str) -> str | None:
        """下载歌曲文件

        Args:
            url: 歌曲 URL
            sender_id: 发送者 ID

        Returns:
            str | None: 临时文件路径，失败返回 None
        """
        url = url.replace("http://", "https://")
        http_session = self._http_session
        if not http_session:
            raise DownloadError("HTTP session 未初始化")

        temp_dir = tempfile.gettempdir()
        safe_sender_id = "".join(c for c in str(sender_id) if c.isalnum() or c in "._-")

        download_success = False
        max_retries = 3
        retry_count = 0
        temp_file = None
        detected_format = None

        while retry_count < max_retries:
            try:
                if self._download_semaphore is None:
                    raise DownloadError("下载限流器未初始化")
                semaphore = self._download_semaphore
                async with semaphore:
                    logger.debug(
                        f"开始下载歌曲 (尝试 {retry_count + 1}/{max_retries}): {url}"
                    )

                    current_url = url
                    redirect_count = 0
                    max_redirects = 5

                    while redirect_count < max_redirects:
                        # 下载时使用非严格模式，允许 DNS 解析失败
                        is_valid, reason = await self._validate_url(
                            current_url, strict_dns=False
                        )
                        if not is_valid:
                            raise UnsafeURLError(f"URL 验证失败: {reason}")

                        async with http_session.get(
                            current_url, allow_redirects=False
                        ) as resp:
                            if resp.status in (301, 302, 307, 308):
                                redirect_url = resp.headers.get("Location", "")
                                if not redirect_url:
                                    raise DownloadError("重定向响应缺少 Location 头")

                                current_url = urljoin(current_url, redirect_url)
                                logger.debug(f"跟随重定向: {current_url}")
                                redirect_count += 1
                                continue

                            if resp.status != 200:
                                raise DownloadError(f"下载失败，状态码: {resp.status}")

                            content_type = resp.headers.get("Content-Type", "")
                            if not self._is_audio_content(content_type):
                                raise AudioFormatError(
                                    f"不支持的 Content-Type: {content_type}"
                                )

                            max_file_size_bytes = self.get_max_file_size()
                            max_file_size_mb = max_file_size_bytes // (1024 * 1024)
                            total_size = 0
                            first_chunk = None
                            temp_file = os.path.join(
                                temp_dir,
                                f"{TEMP_FILE_PREFIX}{safe_sender_id}_{uuid.uuid4()}.tmp",
                            )

                            with open(temp_file, "wb") as f:
                                try:
                                    async for chunk in resp.content.iter_chunked(
                                        CHUNK_SIZE
                                    ):
                                        if first_chunk is None and chunk:
                                            first_chunk = chunk
                                            detected_format = _detect_audio_format(
                                                first_chunk
                                            )
                                            if not detected_format:
                                                raise AudioFormatError(
                                                    "文件头检测失败，不是有效的音频文件"
                                                )

                                        f.write(chunk)
                                        total_size += len(chunk)
                                        if total_size > max_file_size_bytes:
                                            raise DownloadError(
                                                f"文件过大，已超过 {max_file_size_mb} MB"
                                            )
                                except aiohttp.ClientPayloadError as e:
                                    raise DownloadError(f"连接中断: {e}") from e

                            file_size_bytes = os.path.getsize(temp_file)
                            if file_size_bytes == 0:
                                raise DownloadError("下载的文件为空")

                            file_ext = _get_extension_from_format(detected_format)
                            final_file = temp_file + file_ext
                            os.rename(temp_file, final_file)
                            temp_file = final_file

                            file_size_mb = file_size_bytes / (1024 * 1024)
                            logger.info(
                                f"歌曲下载成功，文件大小: {file_size_mb:.2f} MB，格式: {detected_format}"
                            )
                            download_success = True
                            return temp_file

                    raise DownloadError(f"重定向次数超过限制: {max_redirects}")

            except (aiohttp.ClientError, aiohttp.ClientPayloadError) as e:
                retry_count += 1
                logger.error(
                    f"下载歌曲时网络错误 (尝试 {retry_count}/{max_retries}): {e}"
                )
                if retry_count >= max_retries:
                    raise DownloadError(f"网络错误: {e}") from e
                await asyncio.sleep(1)
            except (DownloadError, UnsafeURLError, AudioFormatError):
                raise
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"下载歌曲时发生错误: {e}", exc_info=True)
                raise DownloadError(f"下载失败: {e}") from e
            finally:
                if not download_success and temp_file and os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                        logger.debug("清理临时文件")
                    except Exception:
                        pass

        return None

    def _is_audio_content(self, content_type: str) -> bool:
        """判断 Content-Type 是否为音频

        Args:
            content_type: Content-Type 头

        Returns:
            bool: 是否为音频
        """
        if not content_type:
            return False
        content_type_lower = content_type.lower().split(";")[0].strip()
        return content_type_lower in AUDIO_CONTENT_TYPES

    def _iterate_audio_segments(self, audio, segment_ms: int):
        """迭代音频片段（生成器方式，降低内存占用）

        Args:
            audio: AudioSegment 对象
            segment_ms: 每段的毫秒数

        Yields:
            tuple: (片段索引, 音频片段)
        """
        total_duration = len(audio)
        idx = 1
        for start in range(0, total_duration, segment_ms):
            end = min(start + segment_ms, total_duration)
            segment = audio[start:end]
            yield idx, segment
            idx += 1

    def _export_segment_sync(self, segment, segment_file: str) -> bool:
        """同步导出音频片段到文件（供 run_in_executor 调用）

        Args:
            segment: AudioSegment 片段
            segment_file: 目标文件路径

        Returns:
            bool: 是否成功
        """
        try:
            segment.export(segment_file, format="wav")
            return True
        except Exception as e:
            logger.error(f"导出音频片段失败: {e}")
            return False

    async def _split_and_send_audio(
        self, event: AstrMessageEvent, temp_file: str, session_id: str
    ):
        """分割音频并发送

        Args:
            event: 消息事件
            temp_file: 音频文件路径
            session_id: 会话 ID，用于获取会话级别的锁
        """
        temp_files_to_cleanup = [temp_file]

        try:
            if not self._ffmpeg_path:
                logger.error("FFmpeg 路径为空")
                yield event.plain_result("未找到 FFmpeg，请确保已安装 FFmpeg")
                return

            try:
                AudioSegment.converter = self._ffmpeg_path
            except ImportError as e:
                logger.error(f"导入 pydub 失败: {e}")
                yield event.plain_result("缺少音频处理依赖，请联系管理员")
                return

            audio_lock = await self._get_session_audio_lock(session_id)
            async with audio_lock:
                try:
                    logger.debug(f"开始处理音频文件: {temp_file}")
                    loop = asyncio.get_running_loop()

                    try:
                        # 在线程池中执行音频文件解码操作，避免阻塞主线程
                        audio = await loop.run_in_executor(
                            None, AudioSegment.from_file, temp_file
                        )
                    except Exception as e:
                        logger.error(f"音频文件解码失败: {e}")
                        yield event.plain_result("音频文件格式不支持或已损坏")
                        return

                    total_duration = len(audio)
                    segment_ms = self.get_segment_duration() * 1000
                    send_interval = self.get_send_interval()
                    logger.debug(
                        f"音频总时长: {total_duration}ms, 分段时长: {segment_ms}ms"
                    )

                    base_name = os.path.splitext(os.path.basename(temp_file))[0]
                    success_count = 0

                    for idx, segment in self._iterate_audio_segments(audio, segment_ms):
                        segment_file = os.path.join(
                            tempfile.gettempdir(),
                            f"{base_name}_segment_{idx}_{uuid.uuid4()}.wav",
                        )
                        temp_files_to_cleanup.append(segment_file)

                        # 在线程池中执行音频导出操作
                        success = await loop.run_in_executor(
                            None, self._export_segment_sync, segment, segment_file
                        )
                        if not success:
                            continue

                        try:
                            record = Record.fromFileSystem(segment_file)
                            yield event.chain_result([record])
                            await asyncio.sleep(send_interval)
                            success_count += 1
                        except Exception as e:
                            logger.error(f"发送语音片段 {idx} 时发生错误: {e}")
                            yield event.plain_result(f"发送语音片段 {idx} 失败")

                        try:
                            if os.path.exists(segment_file):
                                os.remove(segment_file)
                            temp_files_to_cleanup.remove(segment_file)
                        except Exception:
                            pass

                    if success_count > 0:
                        yield event.plain_result("歌曲播放完成")

                except asyncio.CancelledError:
                    logger.info("音频处理任务被取消")
                    yield event.plain_result("音频处理已取消")
                except Exception as e:
                    logger.error(f"分割音频时发生错误: {e}", exc_info=True)
                    yield event.plain_result("音频处理失败，请稍后重试")
        finally:
            for f in temp_files_to_cleanup:
                try:
                    if os.path.exists(f):
                        os.remove(f)
                        logger.debug(f"清理临时文件: {f}")
                except Exception:
                    pass

    async def terminate(self):
        """插件终止时清理资源"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        if self._http_session:
            await self._http_session.close()
            self._http_session = None

        self._sessions.clear()
        self._session_audio_locks.clear()

        self._initialized = False
        self._cleanup_temp_files()
