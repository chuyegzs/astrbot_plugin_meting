import asyncio
import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
import uuid
import aiohttp
import machineid
import imageio_ffmpeg as ffmpeg
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import parse_qs, quote, urlparse
from packaging.version import parse as parse_version
from astrbot.api import logger
from astrbot.core.utils.metrics import Metric
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Json, Record
from astrbot.api.star import Context, Star, register
from astrbot.core.config.default import VERSION
from astrbot.core.pipeline.respond import stage

PL_VERSION = "1.1.2"

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
FFMPEG_INFO_TIMEOUT = 30  # seconds, for -i probe only
FFMPEG_CONVERT_TIMEOUT = 120  # seconds, for format conversion / segmentation
_HTTPS_SCHEME_RE = re.compile(r"^http://", re.IGNORECASE)
PHP_API_SUPPORTED_URLS = {
    "https://metingapi.nanorocky.top/",
    "https://api.injahow.cn/meting/",
    "https://metingapi.mo-app.cn/",
}


def _force_https(url: str) -> str:
    """Replace the URL scheme with https, only at the start of the string."""
    return _HTTPS_SCHEME_RE.sub("https://", url, count=1)

def _generate_guid() -> str:
    """生成基于 machine-id 和 MAC 地址的 GUID"""
    try:
        mid = machineid.id()
    except:
        mid = ""
    return hashlib.md5(f"{str(mid)}{str(uuid.getnode())}".encode()).hexdigest()


class MetingPluginError(Exception):
    """插件基础异常"""

    pass


class DownloadError(MetingPluginError):
    """下载错误"""

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


T = TypeVar("T")


@register("astrbot_plugin_meting", "chuyegzs", "基于 MetingAPI 的点歌插件", PL_VERSION)
class MetingPlugin(Star):
    """MetingAPI 点歌插件

    支持多音源搜索和播放，自动分段发送长歌曲
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self._sessions: dict[str, SessionData] = {}
        self._send_overrides: dict[str, int] = {}
        self._sessions_lock: asyncio.Lock | None = None
        self._http_session: aiohttp.ClientSession | None = None
        self._ffmpeg_path = ffmpeg.get_ffmpeg_exe()
        self._cleanup_task = None
        self._download_semaphore: asyncio.Semaphore | None = None
        self._initialized = False
        self._init_lock: asyncio.Lock | None = None
        self._session_audio_locks = {}
        self._audio_locks_lock: asyncio.Lock | None = None

    async def _ensure_initialized(self):
        """确保插件已初始化"""
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
                        "UAK": "AstrBot/plugin_meting * " + PL_VERSION,
                        "UUID": Metric.get_installation_id(),
                        "GUID": _generate_guid(),
                    },
                )

            self._clear_all_cache()

            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
            self._initialized = True
            logger.info("MetingAPI 点歌插件初始化完成")

            if self.get_send_as_music() == 0:
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

    def _get_group_config(
        self,
        group: str,
        key: str,
        default: T,
        validator: Callable[[Any], Any] | None = None,
    ) -> T:
        group_conf = self._get_config(group, {}, lambda x: isinstance(x, dict))
        value = group_conf.get(key, default)
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
        url = _force_https(url)
        return url if url.endswith("/") else f"{url}/"

    def get_api_type(self) -> int:
        """获取 API 类型

        Returns:
            int: API 类型，1=Node API, 2=PHP API, 3=自定义参数
        """
        api_config = self._get_api_config()
        api_url = api_config.get("api_url", "https://musicapi.chuyel.top/meting/")
        if api_url == "custom":
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

        if api_url in PHP_API_SUPPORTED_URLS:
            return 2
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
            self._get_group_config(
                "music_card_config",
                "api_sign_url",
                "https://oiapi.net/api/QQMusicJSONArk/",
            )
        ).rstrip("/")
        url = _force_https(url)
        return url if url.endswith("/") else f"{url}/"

    def get_send_as_music(self, event: AstrMessageEvent | None = None) -> int:
        """获取音乐发送方式

        Returns:
            int: 0=卡片, 1=语音切片, 2=文件发送
        """
        if event and hasattr(self, "_send_overrides"):
            uid = event.unified_msg_origin
            if uid in self._send_overrides:
                return self._send_overrides[uid]

        return self._get_group_config(
            "music_send_config",
            "send_as_music",
            0,
            lambda x: isinstance(x, int) and x in [0, 1, 2],
        )

    def get_send_if_not_supported(self) -> int:
        """获取平台不支持音乐卡片时的发送方式

        Returns:
            int: 0=不发送, 1=语音切片, 2=文件发送
        """
        return self._get_group_config(
            "music_send_config",
            "send_if_not_supported",
            2,
            lambda x: isinstance(x, int) and x in [0, 1, 2],
        )

    def get_send_music_info(self) -> int:
        """获取音质设置 (br)"""
        return self._get_group_config(
            "music_send_config",
            "send_music_info",
            999,
            lambda x: isinstance(x, int) and x >= -1 and x <= 2147483,
        )

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
        query = template.replace(":server", quote(server, safe=""))
        query = query.replace(":type", quote(req_type, safe=""))
        query = query.replace(":id", quote(id_val, safe=""))
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
        return self._get_group_config(
            "search_config", "default_source", "netease", lambda x: x in SOURCE_DISPLAY
        )

    def get_search_result_count(self) -> int:
        """获取搜索结果显示数量

        Returns:
            int: 搜索结果显示数量，范围 5-30，默认 10
        """
        return self._get_group_config(
            "search_config",
            "search_result_count",
            10,
            lambda x: isinstance(x, int) and 5 <= x <= 30,
        )

    def get_segment_duration(self) -> int:
        """获取分段时长

        Returns:
            int: 分段时长（秒），默认 120
        """
        return self._get_group_config(
            "audio_send_config",
            "segment_duration",
            120,
            lambda x: isinstance(x, int) and 30 <= x <= 300,
        )

    def get_send_interval(self) -> float:
        """获取发送间隔

        Returns:
            float: 发送间隔（秒），默认 1.0
        """
        return self._get_group_config(
            "audio_send_config",
            "send_interval",
            1.0,
            lambda x: isinstance(x, (int, float)) and 0 <= x <= 10,
        )

    def get_max_file_size(self) -> int:
        """获取最大文件大小

        Returns:
            int: 最大文件大小（字节），默认 80MB = 83886080 字节
        """
        try:
            mb = self._get_group_config(
                "download_config",
                "max_file_size",
                80,
                lambda x: isinstance(x, (int, float)) and 10 <= x <= 200,
            )
            if not isinstance(mb, (int, float)):
                logger.warning(f"max_file_size 配置无效: {mb}，使用默认值 80")
                mb = 80
            return int(mb) * 1024 * 1024
        except Exception as e:
            logger.error(f"获取 max_file_size 配置时出错: {e}，使用默认值 80MB")
            return 80 * 1024 * 1024

    def get_search_result_expiration_time(self) -> int:
        """获取搜索结果过期时间"""
        return self._get_group_config(
            "search_config",
            "search_result_expiration_time",
            120,
            lambda x: isinstance(x, int) and 30 <= x <= 300,
        )

    def get_search_results_withdrawn_after_timeout(self) -> int:
        """获取搜索结果超时撤回时间"""
        return self._get_group_config(
            "search_config",
            "search_results_withdrawn_after_timeout",
            60,
            lambda x: isinstance(x, int) and -1 <= x <= 300,
        )

    def get_search_result_restrictions(self) -> bool:
        """获取搜索结果限制"""
        return self._get_group_config(
            "search_config",
            "search_result_restrictions",
            False,
            lambda x: isinstance(x, bool),
        )

    async def _get_session(self, session_id: str) -> SessionData:
        """获取会话状态

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
        """更新会话时间戳

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
        last_cleanup_date = None
        while True:
            try:
                await asyncio.sleep(60)
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

                now = time.localtime()
                current_time_str = f"{now.tm_hour:02d}:{now.tm_min:02d}"
                daily_cleanup_time = self._get_group_config(
                    "download_config",
                    "daily_cleanup_time",
                    "04:30",
                    lambda x: isinstance(x, str) and len(x.split(":")) == 2,
                )
                current_date = f"{now.tm_year}-{now.tm_mon}-{now.tm_mday}"
                if (
                    current_time_str == daily_cleanup_time
                    and last_cleanup_date != current_date
                ):
                    logger.info(
                        f"到达定时清理时间 {daily_cleanup_time}，开始清理所有音乐缓存..."
                    )
                    await loop.run_in_executor(None, self._clear_all_cache)
                    last_cleanup_date = current_date

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

    def _clear_all_cache(self):
        """清理过期的音乐文件缓存"""
        try:
            cache_dir = os.path.join(tempfile.gettempdir(), "astrbot_meting_cache")
            if not os.path.exists(cache_dir):
                return
            count = 0
            current_time = time.time()
            for filename in os.listdir(cache_dir):
                filepath = os.path.join(cache_dir, filename)
                if os.path.isfile(filepath):
                    try:
                        # 忽略一小时内的文件，避免冲突
                        if current_time - os.path.getmtime(filepath) > 3600:
                            os.remove(filepath)
                            count += 1
                    except Exception:
                        pass
            if count > 0:
                logger.info(f"已清理 {count} 个过期的音乐缓存文件")
        except Exception as e:
            logger.error(f"清理音乐文件缓存时发生错误: {e}")

    async def _enforce_cache_size(self, cache_dir: str):
        """检查并清理超出预设大小的缓存"""
        try:
            max_cache_gb = self._get_group_config(
                "download_config",
                "max_cache_size",
                1,
                lambda x: isinstance(x, (int, float)) and x >= 1,
            )
            max_bytes = max_cache_gb * 1024 * 1024 * 1024

            loop = asyncio.get_running_loop()

            def clear_cache():
                if not os.path.exists(cache_dir):
                    return
                total_size = 0
                files = []
                for f in os.listdir(cache_dir):
                    fp = os.path.join(cache_dir, f)
                    if os.path.isfile(fp):
                        size = os.path.getsize(fp)
                        total_size += size
                        files.append((fp, os.path.getmtime(fp), size))

                if total_size > max_bytes:
                    logger.info(
                        f"缓存总大小({total_size / 1024 / 1024:.2f}MB)超过限制({max_cache_gb}GB)，开始清理..."
                    )
                    target_size = max_bytes * 0.5
                    # 按时间排序，旧的在前
                    files.sort(key=lambda x: x[1])
                    for fp, mtime, size in files:
                        try:
                            os.remove(fp)
                            total_size -= size
                            if total_size <= target_size:
                                break
                        except Exception as e:
                            logger.warning(f"清理缓存文件失败 {fp}: {e}")
                    logger.info(
                        f"缓存清理完成，当前剩余大小: {total_size / 1024 / 1024:.2f}MB"
                    )

            await loop.run_in_executor(None, clear_cache)
        except Exception as e:
            logger.error(f"执行缓存大小限制时出错: {e}")

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
        """设置会话搜索结果

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
        """获取会话搜索结果

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
            if not self._http_session:
                return None

            if api_type == 3:
                api_endpoint = self._build_api_url_for_custom(
                    api_url, custom_api_template, source, "search", keyword
                )
                logger.info(f"[搜歌] 自定义API URL: {api_endpoint}")
                async with self._http_session.get(api_endpoint) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            else:
                if api_type == 2:
                    params = {
                        "server": source,
                        "type": "search",
                        "id": "0",
                        "dwrc": "false",
                        "keyword": keyword,
                    }
                    api_endpoint = api_url
                    logger.info(f"[搜歌] PHP API URL: {api_endpoint}, 参数: {params}")
                else:
                    params = {"server": source, "type": "search", "id": keyword}
                    api_endpoint = f"{api_url}api"
                    logger.info(f"[搜歌] Node API URL: {api_endpoint}, 参数: {params}")

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
        self,
        event: AstrMessageEvent,
        song: dict,
        session_id: str,
        force_card: bool = False,
    ):
        """播放歌曲的通用逻辑"""
        song_url = song.get("url")

        if not song_url:
            yield event.plain_result("获取歌曲播放地址失败")
            return

        br = self.get_send_music_info()
        if br > 0 and "type=url" in song_url and "br=" not in song_url:
            connector = "&" if "?" in song_url else "?"
            song_url = f"{song_url}{connector}br={br}"

        send_val = self.get_send_as_music(event)
        if force_card:
            send_val = 0

        fallback_val = self.get_send_if_not_supported()
        is_qq = False
        platform_name = ""
        umo = getattr(event, "unified_msg_origin", "")

        if umo and isinstance(umo, str):
            platform_name = umo.split(":")[0]

        if (
            not platform_name
            and hasattr(event, "platform_meta")
            and event.platform_meta
        ):
            platform_name = getattr(event.platform_meta, "name", "")

        if platform_name:
            if platform_name.lower() in (
                # 我也不知道有多少 QQ 框架，就问了 Gemini 一嘴，这么多吗？（
                "aiocqhttp",
                "nakuru",
                "satori",
                "mirai",
                "qq",
                "ntchat",
                "shamrock",
                "red",
                "lagrange",
                "qqguild",
                "napcat",
                "gocqhttp",
                "llonebot",
                "chronocat",
                "qqofficial"
            ):
                is_qq = True

        if send_val == 0 and not force_card and not is_qq and fallback_val != 0:
            logger.info(
                f"消息来源非 QQ ({platform_name})，自动切换为备选发送方式: {fallback_val}"
            )
            send_val = fallback_val

        title = song.get("name") or song.get("title") or "未知歌名"
        artist = song.get("artist") or song.get("author") or "未知歌手"
        album = song.get("album") or "未知专辑"

        source = song.get("source") or await self._get_session_source(session_id)
        song_id = str(song.get("songmid", "")) or str(song.get("id", ""))
        try:
            if not song_id:
                query = urlparse(song_url).query
                song_id = parse_qs(query).get("id", [""])[0]
        except Exception:
            pass

        # 音乐卡片
        if send_val == 0:
            cover = song.get("pic", "")

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
                            await c_resp.read()  # 确保连接被正确释放
                except Exception as e:
                    logger.warning(f"解析封面跳转失败: {e}")

            # 根据音源设置对应的跳转链接
            jump_url_map = {
                "netease": (f"https://music.163.com/#/song?id={song_id}", "163"),
                "tencent": (f"https://y.qq.com/n/ryqq/songDetail/{song_id}", "qq"),
                "bilibili": (f"https://www.bilibili.com/audio/{song_id}", "bilibili"),
                "kugou": (f"https://www.kugou.com/song/#{song_id}", "kugou"),
                "kuwo": (f"https://kuwo.cn/play_detail/{song_id}", "kuwo"),
            }
            jump_url, fmt = jump_url_map.get(
                source, (song_url.replace("type=url", "type=song"), "163")
            )

            if not self._http_session:
                yield event.plain_result("HTTP Session 未初始化")
                return

            # 强制将所有 URL 的 scheme 转换为 https
            song_url = _force_https(song_url)
            if cover:
                cover = _force_https(cover)
            if jump_url:
                jump_url = _force_https(jump_url)

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
                        raise MetingPluginError(f"签名接口请求失败: {resp.status}")

                    res_json = await resp.json()
                    if res_json.get("code") == 1:
                        ark_data = res_json.get("data")
                        token = ark_data.get("config", {}).get("token", "")
                        json_card = Json(data=ark_data, config={"token": token})
                        logger.info("音乐卡片签名成功，尝试发送卡片")
                        logger.debug(f"卡片数据: {json_card}")

                        try:
                            # 尝试主动发送卡片以捕获异常
                            await event.send(event.chain_result([json_card]))
                            return
                        except Exception as send_e:
                            logger.error(f"发送音乐卡片异常: {send_e}")
                            if force_card or fallback_val == 0:
                                yield event.plain_result(f"发送音乐卡片失败: {send_e}")
                                return
                            logger.info(
                                f"由于发卡片失败，将切换为备选发送方式: {fallback_val}"
                            )
                            send_val = fallback_val
                    else:
                        raise MetingPluginError(
                            f"签名失败: {res_json.get('message', '未知错误')}"
                        )
            except Exception as e:
                logger.error(f"音乐卡片请求或处理异常: {e}")
                if force_card or fallback_val == 0:
                    yield event.plain_result("制作卡片时出错")
                    return
                logger.info(f"由于制作卡片异常，将切换为备选发送方式: {fallback_val}")
                send_val = fallback_val

        if send_val == 1:
            # 普通语音发送模式
            try:
                temp_file, duration = await self._download_song(
                    _force_https(song_url), event.get_sender_id(), source, song_id
                )

                yield event.plain_result("正在分段录制歌曲...")
                async for result in self._split_and_send_audio(
                    event, temp_file, session_id, duration
                ):
                    yield result

            except asyncio.CancelledError:
                logger.info("播放任务被取消")
                yield event.plain_result("播放已取消")
            except DownloadError as e:
                logger.error(f"下载歌曲失败: {e}")
                yield event.plain_result(f"下载失败: {e}")
            except AudioFormatError as e:
                logger.error(f"音频格式错误: {e}")
                yield event.plain_result(f"格式不支持: {e}")
            except Exception as e:
                logger.error(f"播放歌曲时发生错误: {e}", exc_info=True)
                yield event.plain_result("播放失败，请稍后重试")

        elif send_val == 2:
            # 文件发送模式 - 直接透传 URL
            try:
                async for result in self._send_as_file(
                    event, title, artist, album, song_url=_force_https(song_url)
                ):
                    yield result
            except Exception as e:
                logger.error(f"文件发送失败: {e}", exc_info=True)
                yield event.plain_result("文件发送失败，请稍后重试")

    async def _probe_song_meta(self, song_url: str) -> str:
        """发送 HEAD 请求以跟踪重定向并提取文件扩展名。
        使用现有的 _guess_file_extension 逻辑来猜测最终 URL 和请求头。
        不下载文件主体。出错时回退到 URL 路径解析。
        """
        try:
            url = _force_https(song_url)
            if not self._http_session:
                raise MetingPluginError("HEAD探针失败: HTTP 会话未初始化。")
            async with self._http_session.head(url, allow_redirects=True) as resp:
                final_url = str(resp.url)
                ext = self._guess_file_extension(final_url, resp.headers)
                return ext if ext else ".mp3"
        except Exception as e:
            logger.warning(f"HEAD probe failed for {song_url}: {e}")
            parsed = urlparse(song_url).path
            ext = os.path.splitext(parsed)[1].lower()
            return ext if ext else ".mp3"

    async def _send_as_file(
        self,
        event: AstrMessageEvent,
        title: str,
        artist: str,
        album: str,
        song_url: str = "",
    ):
        """以文件方式发送歌曲"""
        name = f"{title} - {artist}"
        if album and album != "未知专辑":
            name = f"{name} - [{album}]"

        name = "".join(c for c in name if c not in r'\/:*?"<>|')
        ext = await self._probe_song_meta(song_url)
        file_name = f"{name}{ext}"
        yield event.chain_result([File(name=file_name, url=song_url)])

    async def _switch_source(
        self, event: AstrMessageEvent, source: str, source_name: str
    ):
        """公共切换音源逻辑"""
        await self._ensure_initialized()
        session_id = event.unified_msg_origin
        await self._set_session_source(session_id, source)
        yield event.plain_result(f"已切换音源为{source_name}")

    @filter.command("切换发送模式", alias={"switch meting mode"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def switch_send_mode(self, event: AstrMessageEvent, mode: str = ""):
        """临时切换当前会话的发送方式 [卡片/语音/文件/默认]"""
        if not mode:
            # Check arguments
            message_str = event.get_message_str().strip()
            # Try to extract the parameter if mode is empty but string has it
            for prefix in ["切换发送模式", "切换点歌模式", "switch meting mode"]:
                if message_str.startswith(prefix):
                    mode = message_str[len(prefix) :].strip()
                    break

        if not mode:
            yield event.plain_result(
                "请指定模式：卡片(0)、语音(1)、文件(2) 或 默认(恢复)\n"
                "示例：/切换发送模式 语音"
            )
            return

        mode_map = {
            "卡片": 0,
            "0": 0,
            "语音": 1,
            "1": 1,
            "文件": 2,
            "2": 2,
        }

        if mode in ("默认", "恢复", "default", "-1"):
            uid = event.unified_msg_origin
            if uid in self._send_overrides:
                del self._send_overrides[uid]
            yield event.plain_result("已恢复当前会话的音乐发送模式为全局默认配置。")
            return

        if mode in mode_map:
            val = mode_map[mode]
            uid = event.unified_msg_origin
            self._send_overrides[uid] = val
            mode_name = {0: "卡片", 1: "语音切片", 2: "文件发送"}[val]
            yield event.plain_result(
                f"已临时将当前会话的音乐发送模式切换为：{mode_name}。"
            )
        else:
            yield event.plain_result("未知的模式！支持的模式：卡片、语音、文件、默认。")

    @filter.command(
        "切换QQ音乐",
        alias={"切换腾讯音乐", "切换QQMusic", "switch tencent", "switch qqmusic"},
    )
    async def switch_tencent(self, event: AstrMessageEvent):
        """切换当前会话的音源为QQ音乐"""
        async for res in self._switch_source(event, "tencent", "QQ音乐"):
            yield res

    @filter.command(
        "切换网易云",
        alias={
            "切换网易",
            "切换网易云音乐",
            "切换网抑云",
            "切换网抑云音乐",
            "切换CloudMusic",
            "switch netease",
            "switch cloudmusic",
        },
    )
    async def switch_netease(self, event: AstrMessageEvent):
        """切换当前会话的音源为网易云"""
        async for res in self._switch_source(event, "netease", "网易云"):
            yield res

    @filter.command("切换酷狗", alias={"切换酷狗音乐", "switch kugou"})
    async def switch_kugou(self, event: AstrMessageEvent):
        """切换当前会话的音源为酷狗"""
        async for res in self._switch_source(event, "kugou", "酷狗"):
            yield res

    @filter.command("切换酷我", alias={"切换酷我音乐", "switch kuwo"})
    async def switch_kuwo(self, event: AstrMessageEvent):
        """切换当前会话的音源为酷我"""
        async for res in self._switch_source(event, "kuwo", "酷我"):
            yield res

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

    @filter.command(
        "网易点歌",
        alias={
            "网易云点歌",
            "网抑云点歌",
            "网易云音乐点歌",
            "netease play",
            "netease song",
        },
    )
    async def play_netease_first_song(self, event: AstrMessageEvent):
        """网易云点歌"""
        async for result in self._handle_specific_source_play(
            event,
            "netease",
            [
                "网易云音乐点歌",
                "网易云点歌",
                "网抑云点歌",
                "网易点歌",
                "netease play",
                "netease song",
            ],
        ):
            yield result

    @filter.command(
        "腾讯点歌",
        alias={"QQ点歌", "QQ音乐点歌", "腾讯音乐点歌", "tencent play", "qq play"},
    )
    async def play_tencent_first_song(self, event: AstrMessageEvent):
        """QQ音乐点歌"""
        async for result in self._handle_specific_source_play(
            event,
            "tencent",
            [
                "腾讯音乐点歌",
                "QQ音乐点歌",
                "腾讯点歌",
                "QQ点歌",
                "tencent play",
                "qq play",
            ],
        ):
            yield result

    @filter.command("酷狗点歌", alias={"酷狗音乐点歌", "kugou play"})
    async def play_kugou_first_song(self, event: AstrMessageEvent):
        """酷狗点歌"""
        async for result in self._handle_specific_source_play(
            event, "kugou", ["酷狗音乐点歌", "酷狗点歌", "kugou play"]
        ):
            yield result

    @filter.command("酷我点歌", alias={"酷我音乐点歌", "kuwo play"})
    async def play_kuwo_first_song(self, event: AstrMessageEvent):
        """酷我点歌"""
        async for result in self._handle_specific_source_play(
            event, "kuwo", ["酷我音乐点歌", "酷我点歌", "kuwo play"]
        ):
            yield result

    @filter.command(
        "点歌指令",
        alias={
            "点歌帮助",
            "点歌说明",
            "点歌指南",
            "点歌菜单",
            "song help",
            "meting help",
        },
    )
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

    @filter.command("点歌", alias={"play", "play song"})
    async def play_song_cmd(self, event: AstrMessageEvent):
        """点歌指令，支持序号或歌名"""
        await self._ensure_initialized()

        message_str = event.get_message_str().strip()
        session_id = event.unified_msg_origin
        sender_id = event.get_sender_id()

        arg = message_str
        for prefix in ["点歌", "play song", "play"]:
            if arg.startswith(prefix):
                arg = arg[len(prefix) :].strip()
                break

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
            bot = getattr(event, "bot", None)
            if bot:
                logger.info(f"尝试撤回搜索结果消息: {msg_id}")
                # 兼容 OneBot v11 原生 API (如 Nakuru, aiocqhttp)
                if hasattr(bot, "delete_msg"):
                    await bot.delete_msg(message_id=msg_id)
                # 兼容 AstrBot 的通用的 Provider (如果有暴露 API 操作)
                elif hasattr(bot, "api") and hasattr(bot.api, "call_action"):
                    await bot.api.call_action("delete_msg", message_id=msg_id)
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

    @filter.command("搜歌", alias={"search", "search song"})
    async def search_song(self, event: AstrMessageEvent):
        """搜索歌曲（搜歌 xxx格式）

        Args:
            event: 消息事件
        """
        await self._ensure_initialized()

        message_str = event.get_message_str().strip()
        session_id = event.unified_msg_origin
        sender_id = event.get_sender_id()

        keyword = message_str
        for prefix in ["搜歌", "search song", "search"]:
            if keyword.startswith(prefix):
                keyword = keyword[len(prefix) :].strip()
                break

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

        message = f"🎵 搜索结果 ({SOURCE_DISPLAY.get(source, source)})\n"
        message += "━━━━━━━━━━━━━━\n"
        for idx, song in enumerate(results, 1):
            name = song.get("name") or song.get("title") or "未知歌名"
            artist = song.get("artist") or song.get("author") or "未知歌手"
            album = song.get("album") or "未知专辑"

            if isinstance(artist, list):
                artist = " / ".join(artist)

            if album != "未知专辑":
                message += f"[{idx}] {name}  👤 {artist}  💿 {album}\n"
            else:
                message += f"[{idx}] {name}  👤 {artist}\n"

        message += "━━━━━━━━━━━━━━\n"
        message += '💡 提示：发送 "点歌 1" 即可播放第一首歌'

        # 尝试直接发送消息以获取 Message ID (针对自动撤回功能)
        msg_id = None
        sent_success = False
        withdrawn_timeout = self.get_search_results_withdrawn_after_timeout()

        if withdrawn_timeout != -1:
            try:
                bot = getattr(event, "bot", None)
                if bot:
                    group_id = getattr(event.message_obj, "group_id", None)
                    ret = None
                    if group_id:
                        if hasattr(bot, "send_group_msg"):
                            ret = await bot.send_group_msg(
                                group_id=int(group_id), message=message
                            )
                        elif hasattr(bot, "api") and hasattr(bot.api, "call_action"):
                            ret = await bot.api.call_action(
                                "send_group_msg",
                                group_id=int(group_id),
                                message=message,
                            )
                    else:
                        if event.session_id and event.session_id.isdigit():
                            if hasattr(bot, "send_private_msg"):
                                ret = await bot.send_private_msg(
                                    user_id=int(event.session_id), message=message
                                )
                            elif hasattr(bot, "api") and hasattr(
                                bot.api, "call_action"
                            ):
                                ret = await bot.api.call_action(
                                    "send_private_msg",
                                    user_id=int(event.session_id),
                                    message=message,
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

    async def _download_song(
        self, url: str, sender_id: str, source: str = "", song_id: str = ""
    ) -> tuple[str, float]:
        """下载歌曲文件，验证音频有效性，并按需缓存。

        Args:
            url: 歌曲 URL
            sender_id: 发送者 ID
            source: 歌曲来源
            song_id: 歌曲 ID

        Returns:
            tuple[str, float]: (临时文件路径, 音频时长秒数)

        Raises:
            DownloadError: 下载失败
            AudioFormatError: 下载的文件不是有效音频
        """
        url = _force_https(url)
        http_session = self._http_session
        if not http_session:
            raise DownloadError("HTTP session 未初始化")

        temp_dir = tempfile.gettempdir()
        cache_dir = os.path.join(temp_dir, "astrbot_meting_cache")
        safe_sender_id = "".join(c for c in str(sender_id) if c.isalnum() or c in "._-")

        cache_enabled = self._get_group_config(
            "download_config",
            "enable_cache",
            False,
            lambda x: isinstance(x, bool),
        )

        if cache_enabled:
            cache_key = f"{source}_{song_id}" if source and song_id else url
            url_hash = hashlib.md5(cache_key.encode()).hexdigest()
            os.makedirs(cache_dir, exist_ok=True)
            for filename in os.listdir(cache_dir):
                if filename.startswith(f"{url_hash}_"):
                    try:
                        duration_str = filename.split("_")[1].rsplit(".", 1)[0]
                        duration = float(duration_str)
                        cached_file = os.path.join(cache_dir, filename)
                        logger.info(
                            f"命中缓存，跳过下载和文件检查，直接使用: {cached_file}"
                        )
                        return cached_file, duration
                    except Exception as e:
                        logger.warning(f"读取缓存文件失败: {e}")

        download_success = False
        max_retries = 3
        retry_count = 0
        temp_file = None

        while retry_count < max_retries:
            try:
                if self._download_semaphore is None:
                    raise DownloadError("下载限流器未初始化")
                semaphore = self._download_semaphore
                async with semaphore:
                    logger.debug(
                        f"开始下载歌曲 (尝试 {retry_count + 1}/{max_retries}): {url}"
                    )

                    async with http_session.get(url, allow_redirects=True) as resp:
                        if resp.status != 200:
                            if resp.status >= 500:
                                raise aiohttp.ClientError(
                                    f"上游服务器错误，状态码: {resp.status}"
                                )
                            else:
                                raise DownloadError(f"下载失败，状态码: {resp.status}")

                        content_type = resp.headers.get("Content-Type", "")
                        if not self._is_audio_content(content_type):
                            raise AudioFormatError(
                                f"不支持的 Content-Type: {content_type}"
                            )

                        file_ext = self._guess_file_extension(url, resp.headers)
                        max_file_size_bytes = self.get_max_file_size()
                        max_file_size_mb = max_file_size_bytes // (1024 * 1024)
                        total_size = 0
                        temp_file = os.path.join(
                            temp_dir,
                            f"{TEMP_FILE_PREFIX}{safe_sender_id}_{uuid.uuid4()}{file_ext}",
                        )

                        with open(temp_file, "wb") as f:
                            try:
                                async for chunk in resp.content.iter_chunked(
                                    CHUNK_SIZE
                                ):
                                    f.write(chunk)
                                    total_size += len(chunk)
                                    if total_size > max_file_size_bytes:
                                        raise DownloadError(
                                            f"文件过大，已超过 {max_file_size_mb} MB"
                                        )
                            except aiohttp.ClientPayloadError as e:
                                logger.warning(f"下载时连接断开: {e}")
                                raise e

                        file_size_bytes = os.path.getsize(temp_file)
                        if file_size_bytes == 0:
                            raise DownloadError("下载的文件为空")
                        file_size_mb = file_size_bytes / (1024 * 1024)
                        logger.info(
                            f"歌曲下载成功，临时文件: {temp_file}，文件大小: {file_size_mb:.2f} MB"
                        )

                        # Validate audio and get duration
                        duration = await self._get_audio_info(temp_file)
                        if duration is None or duration <= 0:
                            raise AudioFormatError("下载的文件不是有效音频")

                        logger.debug(f"音频验证通过，时长: {duration:.2f}秒")

                        if cache_enabled:
                            try:
                                cached_filename = f"{url_hash}_{duration:.2f}{file_ext}"
                                cached_file = os.path.join(cache_dir, cached_filename)
                                shutil.move(temp_file, cached_file)
                                logger.debug(f"已缓存音频到 {cached_file}")
                                temp_file = cached_file

                                # 进行缓存大小限制
                                asyncio.create_task(self._enforce_cache_size(cache_dir))
                            except Exception as e:
                                logger.warning(f"缓存音频失败: {e}")

                        download_success = True
                        return temp_file, duration

            except (aiohttp.ClientError, aiohttp.ClientPayloadError) as e:
                retry_count += 1
                logger.error(
                    f"下载歌曲时网络错误 (尝试 {retry_count}/{max_retries}): {e}"
                )
                if retry_count >= max_retries:
                    raise DownloadError(f"网络错误: {e}") from e
                await asyncio.sleep(1)
            except (DownloadError, AudioFormatError):
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

        raise DownloadError("下载失败：已达最大重试次数")

    def _guess_file_extension(self, url: str, headers) -> str:
        """综合嗅探音频文件后缀名

        按照浏览器级别的逻辑降级嗅探文件后缀名：
        1. URL 显式后缀
        2. Content-Disposition 响应头
        3. Content-Type 响应头 (MIME 映射)

        Args:
            url: 下载产生的最终 HTTP URL (或重定向后的 URL)
            headers: aiohttp 响应头

        Returns:
            str: 嗅探得到的文件扩展名，例如 '.mp3', '.flac'，保底返回 '.tmp'
        """
        file_ext = ""
        valid_exts = {".mp3", ".flac", ".wav", ".m4a", ".ogg", ".aac", ".wma", ".ape"}
        parsed_path = urlparse(url).path
        url_ext = os.path.splitext(parsed_path)[1].lower()
        if url_ext in valid_exts:
            file_ext = url_ext
        if not file_ext:
            cd = headers.get("Content-Disposition", "")
            if "filename=" in cd:
                m = re.search(r'filename=["\']?([^";\']+)', cd)
                if m:
                    cd_ext = os.path.splitext(m.group(1))[1].lower()
                    if cd_ext in valid_exts:
                        file_ext = cd_ext
        if not file_ext:
            content_type = headers.get("Content-Type", "")
            mime_pure = content_type.lower().split(";")[0].strip()
            if mime_pure in ("audio/flac", "audio/x-flac", "application/x-flac"):
                file_ext = ".flac"
            elif mime_pure in ("audio/wav", "audio/x-wav"):
                file_ext = ".wav"
            elif mime_pure in ("audio/mpeg", "audio/mp3"):
                file_ext = ".mp3"
            elif mime_pure in ("audio/mp4", "audio/x-m4a"):
                file_ext = ".m4a"
            elif mime_pure in ("audio/ogg", "application/ogg"):
                file_ext = ".ogg"
            else:
                file_ext = mimetypes.guess_extension(mime_pure) or ".tmp"

        return file_ext

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

    async def _run_ffmpeg(
        self, process: asyncio.subprocess.Process, timeout: int
    ) -> bytes:
        """Wait for an ffmpeg process with a timeout.

        Args:
            process: the running ffmpeg subprocess
            timeout: timeout in seconds

        Returns:
            stderr output (bytes) from ffmpeg.

        Raises:
            asyncio.TimeoutError: if the process did not finish within the timeout.
        """
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            return stderr
        except asyncio.TimeoutError:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            raise

    async def _get_audio_info(self, file_path: str) -> float | None:
        """使用 FFmpeg 获取音频时长。如果不是有效音频，返回 None。"""
        if not self._ffmpeg_path:
            return None
        process = await asyncio.create_subprocess_exec(
            self._ffmpeg_path,
            "-i",
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stderr = await self._run_ffmpeg(process, FFMPEG_INFO_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"FFmpeg 获取音频信息超时: {file_path}")
            return None
        output = stderr.decode("utf-8", errors="ignore")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", output)
        if match:
            hours, minutes, seconds = map(float, match.groups())
            return hours * 3600 + minutes * 60 + seconds
        return None

    async def _split_and_send_audio(
        self, event: AstrMessageEvent, temp_file: str, session_id: str, duration: float
    ):
        """处理、分割并发送音频

        Args:
            event: 消息事件
            temp_file: 已验证的音频文件路径
            session_id: 会话 ID
            duration: 音频时长（秒），由 _download_song 预先获取
        """
        temp_files_to_cleanup = []
        cache_dir = os.path.join(tempfile.gettempdir(), "astrbot_meting_cache")
        if not os.path.normcase(os.path.abspath(temp_file)).startswith(
            os.path.normcase(os.path.abspath(cache_dir))
        ):
            temp_files_to_cleanup.append(temp_file)

        try:
            if not self._ffmpeg_path:
                logger.error("FFmpeg 调用失败")
                yield event.plain_result("音频处理组件依赖加载失败。")
                return

            audio_lock = await self._get_session_audio_lock(session_id)
            async with audio_lock:
                try:
                    logger.debug(
                        f"开始处理音频文件: {temp_file}，时长: {duration:.2f}秒"
                    )

                    # 转换压缩为高压缩率通用格式以减小发送体积
                    base_name = os.path.splitext(os.path.basename(temp_file))[0]
                    # 确保它带有完整前缀并放在临时目录，避免原先可能有后缀名或路径冲突
                    if not base_name.startswith(TEMP_FILE_PREFIX):
                        base_name = f"{TEMP_FILE_PREFIX}{base_name}"

                    processed_file = os.path.join(
                        tempfile.gettempdir(), f"{base_name}_processed.wav"
                    )
                    temp_files_to_cleanup.append(processed_file)

                    process = await asyncio.create_subprocess_exec(
                        self._ffmpeg_path,
                        "-i",
                        temp_file,
                        "-y",
                        "-ar",
                        "24000",
                        "-ac",
                        "1",
                        processed_file,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        await self._run_ffmpeg(process, FFMPEG_CONVERT_TIMEOUT)
                    except asyncio.TimeoutError:
                        logger.error("音频转码超时，尝试清理损坏的文件。")
                        if temp_file and os.path.exists(temp_file):
                            try:
                                os.remove(temp_file)
                            except Exception:
                                pass
                        yield event.plain_result("音频转换超时")
                        return

                    if process.returncode != 0 or not os.path.exists(processed_file):
                        logger.error("音频转码失败，尝试清理损坏的文件。")
                        if temp_file and os.path.exists(temp_file):
                            try:
                                os.remove(temp_file)
                                logger.debug(f"已清理损坏媒体文件: {temp_file}")
                            except Exception:
                                pass
                        yield event.plain_result("音频转换失败")
                        return

                    segment_duration = self.get_segment_duration()
                    send_interval = self.get_send_interval()

                    # 判断是否需要分段，并允许 7s 的剩余进行合并
                    tolerance = 7.0 if segment_duration <= 293 else 0.0
                    if duration <= segment_duration + tolerance:
                        logger.debug("音频未超出分段限制，直接发送")
                        yield event.chain_result(
                            [Record.fromFileSystem(processed_file)]
                        )
                        yield event.plain_result("歌曲播放完成")
                        return

                    # 分片发送
                    base_name = os.path.splitext(os.path.basename(temp_file))[0]
                    success_count = 0

                    start_time = 0
                    while start_time < duration:
                        remaining = duration - start_time

                        if remaining <= segment_duration + tolerance:
                            current_duration = remaining
                            is_final_segment = True
                        else:
                            current_duration = segment_duration
                            is_final_segment = False

                        segment_file = os.path.join(
                            tempfile.gettempdir(),
                            f"{base_name}_segment_{int(start_time)}.wav",
                        )
                        temp_files_to_cleanup.append(segment_file)

                        # 使用 ffmpeg 提取切片
                        process = await asyncio.create_subprocess_exec(
                            self._ffmpeg_path,
                            "-ss",
                            str(start_time),
                            "-t",
                            str(current_duration),
                            "-i",
                            processed_file,
                            "-y",
                            "-ar",
                            "24000",
                            "-ac",
                            "1",
                            segment_file,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        try:
                            await self._run_ffmpeg(process, FFMPEG_CONVERT_TIMEOUT)
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"音频片段提取超时, start={start_time}, dur={current_duration}"
                            )
                            start_time += segment_duration
                            continue

                        if not os.path.exists(segment_file):
                            if is_final_segment:
                                break
                            start_time += segment_duration
                            continue

                        try:
                            yield event.chain_result(
                                [Record.fromFileSystem(segment_file)]
                            )
                            await asyncio.sleep(send_interval)
                            success_count += 1
                        except Exception as e:
                            logger.error(f"发送语音片段遇到错误: {e}")
                            yield event.plain_result("发送语音片段失败")

                        # 送完即删
                        try:
                            if os.path.exists(segment_file):
                                os.remove(segment_file)
                            temp_files_to_cleanup.remove(segment_file)
                        except Exception:
                            pass

                        if is_final_segment:
                            break

                        start_time += segment_duration

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

    @filter.llm_tool("astr_meting_music")
    async def astr_meting_music(
        self,
        event: AstrMessageEvent,
        keyword: str,
        source: str = "netease",
        index: int = -1,
    ) -> str:
        """这是一个用于搜索和播放音乐的函数。
        搜索音乐：你可以通过提供 keyword (如歌曲名或歌手) 和 source (点歌源： netease, tencent 默认 netease)，不要提供 index (保留为 -1)或将 index 指定为 -1 来进行搜索。函数将返回由搜索到的结果列表（包含序号、歌名、歌手等）的 JSON 数据给你。
        播放音乐：你可以通过提供 keyword, source 以及 index (从 0 开始计数的有效序号)。函数将直接通过插件向用户发送音乐卡片。
        至于音乐源和点搜索结果内的哪一首歌，你可以自行判断，也可以询问用户喔。
        注意：点歌成功时函数会返回“点歌任务执行成功！”，此时意味着音乐已发送，这时你无需再进行任何回复。

        Args:
            keyword (string): 搜索关键词（歌手名、歌曲名等）
            source (string): 音乐源，必须是 netease, tencent 之一
            index (number): 歌曲序号。-1 表示仅搜索，0 表示第一首，依次类推
        """
        try:
            if source not in SOURCE_DISPLAY:
                return f"不支持的点歌源：{source}，请从 netease, tencent 中选择。"

            results = await self._perform_search(keyword, source)
            if not results:
                return "未搜索到任何相关歌曲。"

            if index < 0:
                summary_results = []
                for i, r in enumerate(results[: self.get_search_result_count()]):
                    title = r.get("name") or r.get("title") or "未知歌名"
                    raw_artist = r.get("artist") or r.get("author") or "未知歌手"
                    raw_album = r.get("album") or "未知专辑"
                    artist_str = (
                        ", ".join(raw_artist)
                        if isinstance(raw_artist, list)
                        else str(raw_artist)
                    )
                    album_str = (
                        ", ".join(raw_album)
                        if isinstance(raw_album, list)
                        else str(raw_album)
                    )
                    item = {
                        "index": i,
                        "name": title,
                        "artist": artist_str,
                        "album": album_str,
                    }
                    if r.get("source"):
                        item["source"] = r.get("source")
                    if r.get("duration"):
                        item["duration"] = r.get("duration")
                    summary_results.append(item)
                return json.dumps(summary_results, ensure_ascii=False)

            if index >= len(results):
                return f"指定的序号 {index} 超出搜索结果范围，最大可选序号为 {len(results) - 1}。"

            target_song = results[index]
            target_song["source"] = source
            session_id = event.unified_msg_origin

            async for result in self._play_song_logic(
                event, target_song, session_id, force_card=True
            ):
                await event.send(result)

            return "点歌任务执行成功！"

        except Exception as e:
            logger.error(f"音乐搜索/播放失败：{e}", exc_info=True)
            return f"发生了错误：{e}"

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
        self._clear_all_cache()
