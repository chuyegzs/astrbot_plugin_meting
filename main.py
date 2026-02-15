import asyncio
import ipaddress
import os
import re
import shutil
import socket
import tempfile
import time
import uuid
from urllib.parse import urlparse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Record
from astrbot.api.star import Context, Star, register

SOURCE_DISPLAY = {
    "tencent": "QQ音乐",
    "netease": "网易云",
    "kugou": "酷狗",
    "kuwo": "酷我",
}

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=120)
SEGMENT_DURATION = 120
CHUNK_SIZE = 8192
SEND_INTERVAL = 1
MAX_FILE_SIZE = 50 * 1024 * 1024
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
}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".wma"}


@register("astrbot_plugin_meting", "chuyegzs", "基于 MetingAPI 的点歌插件", "1.0.7")
class MetingPlugin(Star):
    """MetingAPI 点歌插件

    支持多音源搜索和播放，自动分段发送长歌曲
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self._sessions = {}
        self._http_session = None
        self._ffmpeg_path = self._find_ffmpeg()
        self._cleanup_task = None
        self._download_semaphore = asyncio.Semaphore(3)
        self._initialized = False

    async def initialize(self):
        """插件初始化"""
        if self._initialized:
            logger.warning("插件已经初始化，跳过重复初始化")
            return

        logger.info("MetingAPI 点歌插件已初始化")
        self._http_session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        self._initialized = True

    def _get_config(self, key: str, default=None, validator=None):
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

    def get_api_url(self) -> str:
        """获取 API 地址

        Returns:
            str: API 地址，如果未配置则返回空字符串
        """
        return self._get_config("api_url", "", lambda x: isinstance(x, str) and x)

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

    def _get_session(self, session_id: str) -> dict:
        """获取会话状态

        Args:
            session_id: 会话 ID

        Returns:
            dict: 会话状态字典
        """
        if session_id not in self._sessions:
            self._sessions[session_id] = {
                "source": self.get_default_source(),
                "results": [],
                "timestamp": time.time(),
            }
        return self._sessions[session_id]

    def _update_session_timestamp(self, session_id: str):
        """更新会话时间戳

        Args:
            session_id: 会话 ID
        """
        session = self._get_session(session_id)
        session["timestamp"] = time.time()
        self._cleanup_old_sessions()

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

    def _is_private_ip(self, ip_str: str) -> bool:
        """判断 IP 是否为私网地址

        Args:
            ip_str: IP 地址字符串

        Returns:
            bool: 是否为私网地址
        """
        try:
            ip = ipaddress.ip_address(ip_str)
            return ip.is_private or ip.is_loopback or ip.is_link_local
        except ValueError:
            return False

    def _resolve_hostname(self, hostname: str) -> list:
        """解析主机名为 IP 地址列表

        Args:
            hostname: 主机名

        Returns:
            list: IP 地址列表
        """
        try:
            addrinfo = socket.getaddrinfo(hostname, None)
            return [addr[4][0] for addr in addrinfo]
        except socket.gaierror:
            return []

    def _validate_url(self, url: str) -> bool:
        """验证 URL 是否安全，防止 SSRF 攻击

        Args:
            url: 要验证的 URL

        Returns:
            bool: URL 是否安全
        """
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return False

            hostname = parsed.hostname or ""
            if not hostname:
                return False

            if hostname in ("localhost", "0.0.0.0"):
                return False

            ip_match = re.match(r"^(\d+\.){3}\d+$", hostname)
            if ip_match:
                if self._is_private_ip(hostname):
                    return False
            else:
                ips = self._resolve_hostname(hostname)
                for ip in ips:
                    if self._is_private_ip(ip):
                        return False

            return True
        except Exception as e:
            logger.error(f"URL 验证失败: {e}")
            return False

    def _cleanup_old_sessions(self):
        """清理过期的会话状态"""
        current_time = time.time()
        expired_sessions = [
            sid
            for sid, session in self._sessions.items()
            if current_time - session.get("timestamp", 0) > MAX_SESSION_AGE
        ]
        for sid in expired_sessions:
            self._sessions.pop(sid, None)
        if expired_sessions:
            logger.debug(f"清理了 {len(expired_sessions)} 个过期会话")

    async def _periodic_cleanup(self):
        """定期清理过期的会话状态"""
        while True:
            try:
                await asyncio.sleep(3600)
                self._cleanup_old_sessions()
                logger.debug("定期清理完成")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定期清理时发生错误: {e}")

    def _get_session_source(self, session_id: str) -> str:
        """获取会话音源

        Args:
            session_id: 会话 ID

        Returns:
            str: 会话音源，如果未设置则返回默认音源
        """
        session = self._get_session(session_id)
        return session.get("source", self.get_default_source())

    def _set_session_source(self, session_id: str, source: str):
        """设置会话音源

        Args:
            session_id: 会话 ID
            source: 音源
        """
        session = self._get_session(session_id)
        session["source"] = source
        self._update_session_timestamp(session_id)

    @filter.command("切换QQ音乐")
    async def switch_tencent(self, event: AstrMessageEvent):
        """切换当前会话的音源为QQ音乐"""
        session_id = event.unified_msg_origin
        self._set_session_source(session_id, "tencent")
        yield event.plain_result("已切换音源为QQ音乐")

    @filter.command("切换网易云")
    async def switch_netease(self, event: AstrMessageEvent):
        """切换当前会话的音源为网易云"""
        session_id = event.unified_msg_origin
        self._set_session_source(session_id, "netease")
        yield event.plain_result("已切换音源为网易云")

    @filter.command("切换酷狗")
    async def switch_kugou(self, event: AstrMessageEvent):
        """切换当前会话的音源为酷狗"""
        session_id = event.unified_msg_origin
        self._set_session_source(session_id, "kugou")
        yield event.plain_result("已切换音源为酷狗")

    @filter.command("切换酷我")
    async def switch_kuwo(self, event: AstrMessageEvent):
        """切换当前会话的音源为酷我"""
        session_id = event.unified_msg_origin
        self._set_session_source(session_id, "kuwo")
        yield event.plain_result("已切换音源为酷我")

    @filter.command("点歌")
    async def search_song(self, event: AstrMessageEvent):
        """搜索歌曲，使用当前会话的音源

        Args:
            event: 消息事件
        """
        message_str = event.get_message_str().strip()

        if re.match(r"^点歌\d+$", message_str):
            return

        if message_str.startswith("点歌"):
            keyword = message_str[2:].strip()
        else:
            keyword = message_str
        if not keyword:
            yield event.plain_result("请输入要搜索的歌曲名称，例如：点歌一期一会")
            return

        api_url = self.get_api_url()
        if not api_url:
            yield event.plain_result("请先在插件配置中设置 MetingAPI 地址")
            return

        session_id = event.unified_msg_origin
        source = self._get_session_source(session_id)

        if not self._http_session:
            logger.error("HTTP session 未初始化")
            yield event.plain_result("插件未正确初始化，请重启插件")
            return

        try:
            params = {"server": source, "type": "search", "id": keyword}
            async with self._http_session.get(f"{api_url}/api", params=params) as resp:
                if resp.status != 200:
                    logger.error(f"搜索失败，API 返回状态码: {resp.status}")
                    yield event.plain_result("搜索失败，请稍后重试")
                    return

                try:
                    data = await resp.json()
                except Exception as e:
                    logger.error(f"解析 JSON 响应失败: {e}")
                    yield event.plain_result("搜索失败，请稍后重试")
                    return

            if not data or len(data) == 0:
                yield event.plain_result(f"未找到歌曲: {keyword}")
                return

            result_count = self.get_search_result_count()
            results = data[:result_count]
            session = self._get_session(session_id)
            session["results"] = results
            self._update_session_timestamp(session_id)

            message = f"搜索结果（音源: {SOURCE_DISPLAY.get(source, source)}）:\n"
            for idx, song in enumerate(results, 1):
                name = song.get("title", "未知")
                artist = song.get("author", "未知歌手")
                message += f"{idx}. {name} - {artist}\n"

            message += '\n发送"点歌1"播放第一首歌曲'
            yield event.plain_result(message)

        except aiohttp.ClientError as e:
            logger.error(f"搜索歌曲时网络错误: {e}")
            yield event.plain_result("搜索失败，请检查网络连接")
        except Exception as e:
            logger.error(f"搜索歌曲时发生错误: {e}")
            yield event.plain_result("搜索失败，请稍后重试")

    @filter.regex(r"^点歌(\d+)$")
    async def play_song_by_number(self, event: AstrMessageEvent):
        """播放指定序号的歌曲，以语音形式发送

        Args:
            event: 消息事件
        """
        message_str = event.get_message_str().strip()
        try:
            index = int(message_str[2:])
        except (ValueError, IndexError):
            return
        session_id = event.unified_msg_origin
        session = self._get_session(session_id)

        if not session.get("results"):
            yield event.plain_result('请先使用"点歌"命令搜索歌曲')
            return

        results = session["results"]
        if index < 1 or index > len(results):
            yield event.plain_result(
                f"序号超出范围，请输入 1-{len(results)} 之间的序号"
            )
            return

        song = results[index - 1]
        song_url = song.get("url")

        if not song_url:
            yield event.plain_result("获取歌曲播放地址失败")
            return

        if not self._validate_url(song_url):
            logger.error(f"检测到不安全的 URL: {song_url}")
            yield event.plain_result("歌曲地址无效，无法播放")
            return

        try:
            temp_file = await self._download_song(song_url, event.get_sender_id())
            if not temp_file:
                return

            yield event.plain_result("正在分段录制歌曲...")
            async for result in self._split_and_send_audio(event, temp_file):
                yield result

        except asyncio.CancelledError:
            logger.info("播放任务被取消")
            yield event.plain_result("播放已取消")
        except Exception as e:
            error_msg = str(e)
            if "下载失败" in error_msg:
                yield event.plain_result(error_msg)
            else:
                logger.error(f"播放歌曲时发生错误: {e}")
                yield event.plain_result("播放失败，请稍后重试")

    async def _download_song(self, url: str, sender_id: str) -> str:
        """下载歌曲文件

        Args:
            url: 歌曲 URL
            sender_id: 发送者 ID

        Returns:
            str: 临时文件路径，失败返回 None
        """
        if not self._http_session:
            logger.error("HTTP session 未初始化")
            return None

        temp_dir = tempfile.gettempdir()

        safe_sender_id = "".join(c for c in str(sender_id) if c.isalnum() or c in "._-")
        temp_file = os.path.join(
            temp_dir, f"meting_song_{safe_sender_id}_{uuid.uuid4()}.mp3"
        )

        download_success = False
        max_retries = 3
        retry_count = 0

        while retry_count < max_retries:
            try:
                async with self._download_semaphore:
                    logger.debug(
                        f"开始下载歌曲 (尝试 {retry_count + 1}/{max_retries}): {url}"
                    )

                    async with self._http_session.get(
                        url, allow_redirects=True
                    ) as resp:
                        if resp.status != 200:
                            logger.error(f"下载歌曲失败，状态码: {resp.status}")
                            raise Exception(f"下载失败，状态码: {resp.status}")

                        final_url = str(resp.url)
                        if not self._validate_url(final_url):
                            logger.error(f"最终重定向到不安全的URL: {final_url}")
                            raise Exception("下载失败，重定向地址不安全")

                        logger.debug(f"最终下载URL: {final_url}")

                        content_type = resp.headers.get("Content-Type", "")
                        if not self._is_audio_content(content_type):
                            logger.warning(f"返回的 Content-Type: {content_type}")
                            raise Exception("下载失败，文件格式不支持")

                        url_path = urlparse(final_url).path
                        file_ext = os.path.splitext(url_path)[1].lower()
                        if file_ext and file_ext not in AUDIO_EXTENSIONS:
                            logger.warning(f"URL 文件扩展名: {file_ext}")
                            raise Exception("下载失败，文件格式不支持")

                        total_size = 0
                        with open(temp_file, "wb") as f:
                            try:
                                async for chunk in resp.content.iter_chunked(
                                    CHUNK_SIZE
                                ):
                                    f.write(chunk)
                                    total_size += len(chunk)
                                    if total_size > MAX_FILE_SIZE:
                                        logger.error(
                                            f"文件过大，已超过 {MAX_FILE_SIZE} 字节"
                                        )
                                        raise Exception("下载失败，文件过大")
                            except aiohttp.ClientPayloadError as e:
                                logger.error(f"读取响应内容时发生错误: {e}")
                                raise Exception("下载失败，连接中断")

                        file_size = os.path.getsize(temp_file)
                        if file_size == 0:
                            logger.error("下载的歌曲文件为空")
                            raise Exception("下载失败，文件为空")

                        logger.info(f"歌曲下载成功，文件大小: {file_size} 字节")
                        download_success = True
                        return temp_file

            except (aiohttp.ClientError, aiohttp.ClientPayloadError) as e:
                retry_count += 1
                logger.error(
                    f"下载歌曲时网络错误 (尝试 {retry_count}/{max_retries}): {e}"
                )
                if retry_count >= max_retries:
                    raise Exception(f"下载失败，网络错误: {e}")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"下载歌曲时发生错误: {e}")
                raise
            finally:
                if not download_success and os.path.exists(temp_file):
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

    def _split_audio_segments(self, audio, segment_ms: int):
        """分割音频为多个片段

        Args:
            audio: AudioSegment 对象
            segment_ms: 每段的毫秒数

        Returns:
            list: 音频片段列表
        """
        total_duration = len(audio)
        segments = []
        for start in range(0, total_duration, segment_ms):
            end = min(start + segment_ms, total_duration)
            segment = audio[start:end]
            segments.append(segment)
        return segments

    def _export_segment(self, segment, segment_file: str) -> bool:
        """导出音频片段到文件

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

    async def _split_and_send_audio(self, event: AstrMessageEvent, temp_file: str):
        """分割音频并发送

        Args:
            event: 消息事件
            temp_file: 音频文件路径
        """
        if not self._ffmpeg_path:
            logger.error("FFmpeg 路径为空")
            yield event.plain_result("未找到 FFmpeg，请确保已安装 FFmpeg")
            return

        try:
            from pydub import AudioSegment

            original_converter = AudioSegment.converter
            AudioSegment.converter = self._ffmpeg_path
            logger.info(f"FFmpeg 路径已设置为: {self._ffmpeg_path}")
        except ImportError as e:
            logger.error(f"导入 pydub 失败: {e}")
            yield event.plain_result("缺少音频处理依赖，请联系管理员")
            return

        try:
            logger.debug(f"开始处理音频文件: {temp_file}")
            audio = AudioSegment.from_file(temp_file)
            total_duration = len(audio)
            segment_ms = SEGMENT_DURATION * 1000
            logger.debug(f"音频总时长: {total_duration}ms, 分段时长: {segment_ms}ms")

            segments = self._split_audio_segments(audio, segment_ms)
            base_name = os.path.splitext(os.path.basename(temp_file))[0]

            success_count = 0
            for idx, segment in enumerate(segments, 1):
                segment_file = os.path.join(
                    tempfile.gettempdir(),
                    f"{base_name}_segment_{idx}_{uuid.uuid4()}.wav",
                )

                if not self._export_segment(segment, segment_file):
                    continue

                try:
                    record = Record.fromFileSystem(segment_file)
                    yield event.chain_result([record])
                    await asyncio.sleep(SEND_INTERVAL)
                    success_count += 1
                except Exception as e:
                    logger.error(f"发送语音片段 {idx} 时发生错误: {e}")
                    yield event.plain_result(f"发送语音片段 {idx} 失败")
                finally:
                    if os.path.exists(segment_file):
                        os.remove(segment_file)

            if success_count > 0:
                yield event.plain_result("歌曲播放完成")

        except asyncio.CancelledError:
            logger.info("音频处理任务被取消")
            yield event.plain_result("音频处理已取消")
        except Exception as e:
            logger.error(f"分割音频时发生错误: {e}")
            yield event.plain_result("音频处理失败，请稍后重试")
        finally:
            if os.path.exists(temp_file):
                os.remove(temp_file)
            try:
                from pydub import AudioSegment

                AudioSegment.converter = original_converter
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
