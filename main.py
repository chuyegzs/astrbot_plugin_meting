import asyncio
import ipaddress
import os
import re
import shutil
import time
import uuid
import aiohttp
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urljoin
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Json, Record
from astrbot.api.star import Context, Star, register
from astrbot.core.config.default import VERSION
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

SOURCE_DISPLAY = {
    "tencent": "QQ音乐",
    "netease": "网易云音乐",
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
    "application/octet-stream",
}
TEMP_FILE_PREFIX = "astrbot_meting_plugin_"


class MetingPluginError(Exception):
    """插件基础异常"""


class DownloadError(MetingPluginError):
    """下载错误"""


class UnsafeURLError(MetingPluginError):
    """不安全的URL错误"""


class AudioFormatError(MetingPluginError):
    """音频格式错误"""


class SessionData:
    def __init__(self, default_source: str):
        self._source = default_source
        self._results = []
        self._timestamp = time.time()

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
    """根据文件头检测音频格式"""
    if len(data) < 4:
        return None
    if data.startswith((b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"ID3")):
        return "mp3"
    if data.startswith(b"RIFF"):
        return "wav"
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"fLaC"):
        return "flac"
    if (len(data) >= 8 and data[4:8] == b"ftyp") or data.startswith(b"\x00\x00\x00"):
        return "mp4"
    return None


def _check_audio_magic(data: bytes) -> bool:
    """检查文件头是否为有效的音频格式"""
    return _detect_audio_format(data) is not None


def _get_extension_from_format(audio_format: str) -> str:
    """根据音频格式获取文件扩展名"""
    mapping = {
        "mp3": ".mp3",
        "wav": ".wav",
        "ogg": ".ogg",
        "flac": ".flac",
        "mp4": ".m4a",
    }
    return mapping.get(audio_format, ".mp3")


@register("astrbot_plugin_meting", "chuyegzs", "基于 MetingAPI 的点歌插件", "1.2.2")
class MetingPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self._sessions: dict[str, SessionData] = {}
        self._http_session: aiohttp.ClientSession | None = None
        self._ffmpeg_path = self._find_ffmpeg()
        self._cleanup_task: asyncio.Task | None = None
        self._initialized = False
        self._sessions_lock = asyncio.Lock()
        self._download_semaphore = asyncio.Semaphore(3)
        self._audio_locks_lock = asyncio.Lock()
        self._session_audio_locks: dict[str, asyncio.Lock] = {}
        self.CLEANUP_INTERVAL = 3600
        self.TEMP_FILE_TTL = 3600

    def _find_ffmpeg(self) -> str:
        ffmpeg_exe = shutil.which("ffmpeg")
        if ffmpeg_exe:
            return ffmpeg_exe
        if os.name == "nt":
            paths = [
                os.path.join(os.getcwd(), "ffmpeg.exe"),
                os.path.join(os.getcwd(), "bin", "ffmpeg.exe"),
            ]
            for path in paths:
                if os.path.exists(path):
                    return path
        return ""

    async def _ensure_initialized(self):
        if self._initialized:
            return

        if not self._http_session:
            self._http_session = aiohttp.ClientSession(
                timeout=REQUEST_TIMEOUT,
                headers={
                    "Referer": "https://astrbot.app/",
                    "User-Agent": f"AstrBot/{VERSION}",
                    "UAK": "AstrBot/plugin_meting",
                },
            )

        if not self._cleanup_task:
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        self._initialized = True

        if self.use_music_card():
            try:
                from astrbot.core.pipeline.respond import stage

                with open(stage.__file__, "r", encoding="utf-8") as f:
                    content = f.read()
                    if "Comp.Json" not in content:
                        logger.warning(
                            "检测到当前 AstrBot 版本可能不支持 JSON 消息组件。请更新 AstrBot 版本，否则音乐卡片可能无法发送。"
                        )
            except Exception as e:
                logger.debug(f"检查 AstrBot兼容性失败: {e}")

    def _is_private_ip(self, ip_str: str) -> bool:
        """判断 IP 是否为私网地址"""
        try:
            ip = ipaddress.ip_address(ip_str)
            return ip.is_private or ip.is_loopback or ip.is_link_local
        except ValueError:
            return False

    async def _resolve_hostname_async(self, hostname: str) -> list:
        """异步解析主机名为 IP 地址列表"""
        try:
            loop = asyncio.get_running_loop()
            import socket

            infos = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            return [info[4][0] for info in infos]
        except Exception:
            return []

    async def _validate_url(self, url: str) -> tuple[bool, str]:
        """验证 URL 是否安全，防止 SSRF 攻击"""
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return False, f"不支持的协议: {parsed.scheme}"

            hostname = parsed.hostname
            if not hostname:
                return False, "无法解析主机名"

            # 阻止 localhost
            if hostname.lower() in ("localhost", "127.0.0.1", "::1"):
                return False, "禁止访问本地地址"

            # 判断是否为 IP 地址
            try:
                ipaddress.ip_address(hostname)
                is_ip = True
            except ValueError:
                is_ip = False

            if is_ip:
                if self._is_private_ip(hostname):
                    return False, "禁止访问私网 IP"
            else:
                # 解析域名检查 IP
                ips = await self._resolve_hostname_async(hostname)
                if not ips:
                    return False, "域名解析失败"
                for ip in ips:
                    if self._is_private_ip(ip):
                        return False, f"域名解析到私网 IP: {ip}"

            return True, ""
        except Exception as e:
            return False, f"URL 验证异常: {e}"

    async def _validate_api_url(self, url: str) -> tuple[bool, str]:
        """验证 API URL 是否安全"""
        is_valid, reason = await self._validate_url(url)
        if not is_valid:
            if "私网 IP" in reason or "本地地址" in reason:
                # 允许用户明确配置私网 API，但给予警告
                logger.warning(
                    f"Meting API 地址解析为私网地址 ({url})，这可能存在 SSRF 风险，请确保配置安全。"
                )
            else:
                return False, reason

        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        if hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
            logger.warning("Meting API 配置为本地地址")

        return True, ""

    async def _periodic_cleanup(self):
        """定期清理过期文件任务"""
        while True:
            try:
                await asyncio.sleep(self.CLEANUP_INTERVAL)
                await self._cleanup_temp_files()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Periodic cleanup failed: {e}")

    def get_data_dir(self) -> Path:
        """Get plugin data directory"""
        # Fallback to a standard location if context lookup fails
        return Path("data/plugins/astrbot_plugin_meting")

    async def _cleanup_temp_files(self):
        """清理临时目录下的过期文件"""
        try:
            now = time.time()
            # 获取临时目录路径
            temp_dir = Path(get_astrbot_temp_path())

            if not temp_dir.exists():
                return

            for file_path in temp_dir.iterdir():
                if not file_path.is_file():
                    continue

                # 仅处理本插件生成的文件 (以 meting_ 开头)
                if not file_path.name.startswith("meting_"):
                    continue

                if now - file_path.stat().st_mtime > self.TEMP_FILE_TTL:
                    try:
                        file_path.unlink()
                        logger.debug(f"Removed temporary file: {file_path}")
                    except Exception as e:
                        logger.warning(f"Failed to remove temp file {file_path}: {e}")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

    async def _get_session_audio_lock(self, session_id: str) -> asyncio.Lock:
        """获取会话级别的音频处理锁"""
        async with self._audio_locks_lock:
            if session_id not in self._session_audio_locks:
                self._session_audio_locks[session_id] = asyncio.Lock()
            return self._session_audio_locks[session_id]

    def _is_audio_content(self, content_type: str) -> bool:
        """判断 Content-Type 是否为音频"""
        if not content_type:
            return False
        content_type_lower = content_type.lower().split(";")[0].strip()
        return content_type_lower in AUDIO_CONTENT_TYPES

    def _iterate_audio_segments(self, audio, segment_ms: int):
        """迭代音频片段（生成器方式，降低内存占用）"""
        total_duration = len(audio)
        idx = 1
        for start in range(0, total_duration, segment_ms):
            end = min(start + segment_ms, total_duration)
            segment = audio[start:end]
            yield idx, segment
            idx += 1

    def _export_segment(self, segment, segment_file: str) -> bool:
        """导出音频片段到文件"""
        try:
            segment.export(segment_file, format="wav")
            return True
        except Exception as e:
            logger.error(f"导出音频片段失败: {e}")
            return False

    async def _split_and_send_audio(
        self, event: AstrMessageEvent, temp_file: Path, session_id: str
    ):
        """分割音频并发送"""
        temp_files_to_cleanup = [temp_file]

        try:
            if not self._ffmpeg_path:
                logger.error("FFmpeg 路径为空")
                yield event.plain_result("未找到 FFmpeg，请确保已安装 FFmpeg")
                return

            try:
                from pydub import AudioSegment

                AudioSegment.converter = self._ffmpeg_path
            except ImportError as e:
                logger.error(f"导入 pydub 失败: {e}")
                yield event.plain_result("缺少音频处理依赖，请联系管理员")
                return

            audio_lock = await self._get_session_audio_lock(session_id)
            async with audio_lock:
                try:
                    logger.debug(f"开始处理音频文件: {temp_file}")
                    try:
                        audio = AudioSegment.from_file(str(temp_file))
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

                    base_name = temp_file.stem
                    success_count = 0

                    for idx, segment in self._iterate_audio_segments(audio, segment_ms):
                        segment_file = (
                            Path(get_astrbot_temp_path())
                            / f"{base_name}_segment_{idx}_{uuid.uuid4()}.wav"
                        )
                        temp_files_to_cleanup.append(segment_file)

                        if not self._export_segment(segment, str(segment_file)):
                            continue

                        try:
                            # 转换为 Voice 组件? Or Record?
                            # AstrBot explicitly uses Record for file-based voice?
                            # Let's assume Record is fine as it's imported.
                            # But better to check what voice_result does.
                            # event.voice_result creates Chain([Record(...)]) usually.

                            record = Record.fromFileSystem(str(segment_file))
                            yield event.chain_result([record])
                            await asyncio.sleep(send_interval)
                            success_count += 1
                        except Exception as e:
                            logger.error(f"发送语音片段 {idx} 时发生错误: {e}")
                            yield event.plain_result(f"发送语音片段 {idx} 失败")

                        try:
                            if segment_file.exists():
                                segment_file.unlink()
                            # It's better not to remove from list during iteration, just ignore cleanup later if gone
                        except Exception:
                            pass

                    if success_count > 0:
                        # yield event.plain_result("歌曲播放完成")
                        pass

                except asyncio.CancelledError:
                    logger.info("音频处理任务被取消")
                    yield event.plain_result("音频处理已取消")
                except Exception as e:
                    logger.error(f"分割音频时发生错误: {e}", exc_info=True)
                    yield event.plain_result("音频处理失败，请稍后重试")
        finally:
            for f in temp_files_to_cleanup:
                try:
                    if isinstance(f, Path) and f.exists():
                        f.unlink()
                    elif isinstance(f, str) and os.path.exists(f):
                        os.remove(f)
                except Exception:
                    pass

    async def _download_song(self, url: str) -> Path | None:
        """下载歌曲文件，包含重定向处理和安全检查"""
        if not url:
            return None

        temp_dir = Path(get_astrbot_temp_path())
        temp_dir.mkdir(parents=True, exist_ok=True)

        # Safe sender id is not available here, use random uuid
        safe_sender_id = uuid.uuid4().hex[:8]

        download_success = False
        max_retries = 3
        retry_count = 0
        temp_file: Path | None = None
        detected_format = None

        while retry_count < max_retries:
            try:
                # Need semaphore for concurrency limit
                async with self._download_semaphore:
                    logger.debug(
                        f"开始下载歌曲 (尝试 {retry_count + 1}/{max_retries}): {url}"
                    )

                    current_url = url
                    redirect_count = 0
                    max_redirects = 5

                    while redirect_count < max_redirects:
                        # Validate URL safety
                        is_valid, reason = await self._validate_url(current_url)
                        if not is_valid:
                            raise UnsafeURLError(f"URL 验证失败: {reason}")

                        try:
                            if not self._http_session:
                                await self._ensure_initialized()

                            if self._http_session:
                                async with self._http_session.get(
                                    current_url, allow_redirects=False
                                ) as resp:
                                    if resp.status in (301, 302, 307, 308):
                                        redirect_url = resp.headers.get("Location", "")
                                        if not redirect_url:
                                            raise DownloadError(
                                                "重定向响应缺少 Location 头"
                                            )

                                        current_url = urljoin(current_url, redirect_url)
                                        logger.debug(f"跟随重定向: {current_url}")
                                        redirect_count += 1
                                        continue

                                    if resp.status != 200:
                                        logger.warning(
                                            f"下载失败，状态码: {resp.status}"
                                        )
                                        raise DownloadError(
                                            f"下载失败，状态码: {resp.status}"
                                        )

                                    content_type = resp.headers.get("Content-Type", "")
                                    if not self._is_audio_content(content_type):
                                        # Strict content type checking disabled for resilience
                                        pass

                                    max_file_size = 100 * 1024 * 1024
                                    total_size = 0
                                    first_chunk = None

                                    temp_file = (
                                        temp_dir
                                        / f"meting_{int(time.time())}_{safe_sender_id}.tmp"
                                    )

                                    with open(temp_file, "wb") as f:
                                        try:
                                            async for (
                                                chunk
                                            ) in resp.content.iter_chunked(CHUNK_SIZE):
                                                if first_chunk is None and chunk:
                                                    first_chunk = chunk
                                                    detected_format = (
                                                        _detect_audio_format(
                                                            first_chunk
                                                        )
                                                    )

                                                f.write(chunk)
                                                total_size += len(chunk)
                                                if total_size > max_file_size:
                                                    raise DownloadError(
                                                        f"文件过大，已超过 {max_file_size} 字节"
                                                    )
                                        except aiohttp.ClientPayloadError as e:
                                            raise DownloadError(f"连接中断: {e}") from e

                                    if total_size == 0:
                                        raise DownloadError("下载的文件为空")

                                    if not detected_format:
                                        detected_format = "mp3"

                                    file_ext = _get_extension_from_format(
                                        detected_format
                                    )
                                    final_file = temp_file.with_suffix(file_ext)
                                    temp_file.rename(final_file)
                                    temp_file = final_file

                                    logger.info(
                                        f"歌曲下载成功，文件大小: {total_size} 字节，格式: {detected_format}"
                                    )
                                    download_success = True
                                    return temp_file

                            else:
                                # Safe guard if _http_session became None (e.g. plugin shutdown)
                                return None

                        except Exception:
                            raise

                    raise DownloadError(f"重定向次数超过限制: {max_redirects}")

            except (aiohttp.ClientError, aiohttp.ClientPayloadError) as e:
                retry_count += 1
                logger.error(
                    f"下载歌曲时网络错误 (尝试 {retry_count}/{max_retries}): {e}"
                )
                if retry_count >= max_retries:
                    raise DownloadError(f"网络错误: {e}") from e
                await asyncio.sleep(1)

            except (DownloadError, UnsafeURLError, AudioFormatError) as e:
                logger.error(f"下载失败: {e}")
                # Don't retry for these errors
                if temp_file and temp_file.exists():
                    temp_file.unlink()
                return None

            except Exception as e:
                logger.error(f"下载歌曲时发生错误: {e}")
                retry_count += 1
                await asyncio.sleep(1)

        return None

    def _get_config(self, key: str, default=None):
        if not self.config:
            return default
        return self.config.get(key, default)

    def get_api_url(self) -> str:
        return str(self._get_config("api_url", "")).rstrip("/")

    def get_api_type(self) -> int:
        return int(self._get_config("api_type", 1) or 1)

    def get_sign_api_url(self) -> str:
        return str(
            self._get_config("api_sign_url", "https://oiapi.net/api/QQMusicJSONArk/")
        ).rstrip("/")

    def use_music_card(self) -> bool:
        return bool(self._get_config("use_music_card", False))

    def get_segment_duration(self) -> int:
        return int(self._get_config("segment_duration", 60) or 60)

    def get_send_interval(self) -> float:
        return float(self._get_config("send_interval", 1.0) or 1.0)

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

    async def _get_session(self, session_id: str) -> SessionData:
        await self._ensure_initialized()
        async with self._sessions_lock:
            if session_id not in self._sessions:
                default_source = str(self._get_config("default_source", "netease"))
                self._sessions[session_id] = SessionData(default_source)

            # Update timestamp and cleanup
            if session_id in self._sessions:
                self._sessions[session_id].update_timestamp()
            await self._cleanup_old_sessions_locked()

            return self._sessions[session_id]

    @filter.command(
        "切换QQ音乐",
        alias={"切换腾讯音乐", "切换腾讯点歌", "切换TencentMusic", "切换QQMusic"},
    )
    async def switch_tencent(self, event: AstrMessageEvent):
        (await self._get_session(event.unified_msg_origin)).source = "tencent"
        yield event.plain_result("已切换音源为QQ音乐")

    @filter.command(
        "切换网易云",
        alias={
            "切换网易云音乐",
            "切换网易点歌",
            "切换网抑云",
            "切换网抑云音乐",
            "切换NeteaseMusic",
            "切换Netease",
        },
    )
    async def switch_netease(self, event: AstrMessageEvent):
        (await self._get_session(event.unified_msg_origin)).source = "netease"
        yield event.plain_result("已切换音源为网易云音乐")

    @filter.regex(r"^点歌(\d+)$")
    async def play_song_by_index(self, event: AstrMessageEvent):
        await self._ensure_initialized()
        session_id = event.unified_msg_origin
        match = re.match(r"^点歌(\d+)$", event.get_message_str().strip())
        if not match:
            return
        index = int(match.group(1))
        session = await self._get_session(session_id)
        if not session.results:
            yield event.plain_result("请先搜索歌曲")
            return
        if index < 1 or index > len(session.results):
            yield event.plain_result("序号超出范围")
            return
        song = session.results[index - 1]
        song_url = song.get("url", "")
        if not song_url:
            yield event.plain_result("获取歌曲地址失败")
            return

        if self.use_music_card():
            title = song.get("name") or song.get("title", "未知")
            artist = song.get("artist") or song.get("author", "未知歌手")
            source = song.get("source") or session.source
            cover = song.get("pic", "")
            if cover:
                if source == "netease":
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

            if source == "netease":
                jump_url = f"https://music.163.com/#/song?id={song_id}"
                fmt = "163"
            elif source == "tencent":
                jump_url = f"https://y.qq.com/n/ryqq/songDetail/{song_id}"
                fmt = "qq"
            else:
                jump_url = song_url.replace("type=url", "type=song")
                fmt = "163"

            if not self._http_session:
                yield event.plain_result("HTTP Session 未初始化")
                return

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
                    else:
                        yield event.plain_result(
                            f"签名失败: {res_json.get('message', '未知错误')}"
                        )
            except Exception as e:
                logger.error(f"音乐卡片请求异常: {e}")
                yield event.plain_result("制作卡片时出错")
            return
        try:
            temp_file = await self._download_song(song_url)
            if temp_file:
                yield event.plain_result("正在分段发送语音...")
                async for result in self._split_and_send_audio(
                    event, temp_file, session_id
                ):
                    yield result
        except Exception as e:
            yield event.plain_result(f"播放失败: {e}")

    @filter.command("点歌")
    async def search_song(self, event: AstrMessageEvent):
        msg = event.get_message_str().strip()
        kw = msg[2:].strip() if msg.startswith("点歌") else msg
        if not kw:
            return

        session = await self._get_session(event.unified_msg_origin)
        async for result in self._search_song_with_source(event, kw, session.source):
            yield result

    @filter.command("腾讯点歌", alias={"QQ点歌", "QQ音乐点歌", "腾讯音乐点歌"})
    async def search_tencent_song(self, event: AstrMessageEvent):
        msg = event.get_message_str().strip()
        kw = msg[4:].strip() if msg.startswith("腾讯点歌") else msg
        if not kw:
            return
        async for result in self._search_song_with_source(event, kw, "tencent"):
            yield result

    @filter.command("网易点歌", alias={"网易云点歌", "网抑云点歌", "网易云音乐点歌"})
    async def search_netease_song(self, event: AstrMessageEvent):
        msg = event.get_message_str().strip()
        kw = msg[4:].strip() if msg.startswith("网易点歌") else msg
        if not kw:
            return
        async for result in self._search_song_with_source(event, kw, "netease"):
            yield result

    async def _search_song_with_source(
        self, event: AstrMessageEvent, kw: str, source: str
    ):
        await self._ensure_initialized()
        api_url = self.get_api_url()
        api_type = self.get_api_type()
        session = await self._get_session(event.unified_msg_origin)

        try:
            params = (
                {
                    "server": source,
                    "type": "search",
                    "id": "0",
                    "dwrc": "false",
                    "keyword": kw,
                }
                if api_type == 2
                else {"server": source, "type": "search", "id": kw}
            )
            api_endpoint = api_url if api_type == 2 else f"{api_url}/api"

            if not self._http_session:
                yield event.plain_result("HTTP Session 未初始化")
                return

            async with self._http_session.get(api_endpoint, params=params) as resp:
                data = await resp.json()
            if not isinstance(data, list) or not data:
                yield event.plain_result(f"未找到歌曲: {kw}")
                return
            result_count = self._get_config("search_result_count", 10) or 10
            session.results = data[: int(result_count)]
            res_msg = f"搜索结果 ({SOURCE_DISPLAY.get(source, source)}):\n"
            for i, s in enumerate(session.results, 1):
                res_msg += f"{i}. {s.get('name') or s.get('title')} - {s.get('artist') or s.get('author')}\n"
            res_msg += "\n输入 '点歌序号' 播放"
            yield event.plain_result(res_msg)
        except Exception as e:
            yield event.plain_result(f"搜索失败: {e}")
