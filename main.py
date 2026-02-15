import asyncio
import os
import shutil
import tempfile
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

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)
SEGMENT_DURATION = 120
CHUNK_SIZE = 8192
SEND_INTERVAL = 1
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_SESSION_AGE = 3600


@register("astrbot_plugin_meting", "chuyegzs", "基于 MetingAPI 的点歌插件", "1.0.2")
class MetingPlugin(Star):
    """MetingAPI 点歌插件

    支持多音源搜索和播放，自动分段发送长歌曲
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self.session_sources = {}
        self.last_search_results = {}
        self.session_timestamps = {}
        self._http_session = None
        self._ffmpeg_path = self._find_ffmpeg()

    async def initialize(self):
        """插件初始化"""
        logger.info("MetingAPI 点歌插件已初始化")
        self._http_session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)

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

    def get_api_url(self) -> str:
        """获取 API 地址

        Returns:
            str: API 地址，如果未配置则返回空字符串
        """
        if self.config and self.config.get("api_url"):
            return self.config["api_url"]
        return ""

    def get_default_source(self) -> str:
        """获取默认音源

        Returns:
            str: 默认音源，默认为 netease
        """
        if self.config and self.config.get("default_source"):
            return self.config["default_source"]
        return "netease"

    def get_search_result_count(self) -> int:
        """获取搜索结果显示数量

        Returns:
            int: 搜索结果显示数量，范围 5-30，默认 10
        """
        if self.config and self.config.get("search_result_count"):
            count = self.config["search_result_count"]
            if isinstance(count, int) and 5 <= count <= 30:
                return count
        return 10

    def get_session_source(self, session_id: str) -> str:
        """获取会话音源

        Args:
            session_id: 会话 ID

        Returns:
            str: 会话音源，如果未设置则返回默认音源
        """
        return self.session_sources.get(session_id, self.get_default_source())

    def set_session_source(self, session_id: str, source: str):
        """设置会话音源

        Args:
            session_id: 会话 ID
            source: 音源
        """
        self.session_sources[session_id] = source

    def _validate_url(self, url: str) -> bool:
        """验证 URL 是否安全

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
            if hostname in ("localhost", "127.0.0.1", "::1"):
                return False
            if (
                hostname.startswith("192.168.")
                or hostname.startswith("10.")
                or hostname.startswith("172.")
            ):
                return False
            return True
        except Exception:
            return False

    def _cleanup_old_sessions(self):
        """清理过期的会话状态"""
        import time

        current_time = time.time()
        expired_sessions = [
            sid
            for sid, timestamp in self.session_timestamps.items()
            if current_time - timestamp > MAX_SESSION_AGE
        ]
        for sid in expired_sessions:
            self.session_sources.pop(sid, None)
            self.last_search_results.pop(sid, None)
            self.session_timestamps.pop(sid, None)

    def _update_session_timestamp(self, session_id: str):
        """更新会话时间戳

        Args:
            session_id: 会话 ID
        """
        import time

        self.session_timestamps[session_id] = time.time()
        self._cleanup_old_sessions()

    @filter.command("切换QQ音乐")
    async def switch_tencent(self, event: AstrMessageEvent):
        """切换当前会话的音源为QQ音乐"""
        session_id = event.unified_msg_origin
        self.set_session_source(session_id, "tencent")
        yield event.plain_result("已切换音源为QQ音乐")

    @filter.command("切换网易云")
    async def switch_netease(self, event: AstrMessageEvent):
        """切换当前会话的音源为网易云"""
        session_id = event.unified_msg_origin
        self.set_session_source(session_id, "netease")
        yield event.plain_result("已切换音源为网易云")

    @filter.command("切换酷狗")
    async def switch_kugou(self, event: AstrMessageEvent):
        """切换当前会话的音源为酷狗"""
        session_id = event.unified_msg_origin
        self.set_session_source(session_id, "kugou")
        yield event.plain_result("已切换音源为酷狗")

    @filter.command("切换酷我")
    async def switch_kuwo(self, event: AstrMessageEvent):
        """切换当前会话的音源为酷我"""
        session_id = event.unified_msg_origin
        self.set_session_source(session_id, "kuwo")
        yield event.plain_result("已切换音源为酷我")

    @filter.command("点歌")
    async def search_song(self, event: AstrMessageEvent, keyword: str = ""):
        """搜索歌曲，使用当前会话的音源

        Args:
            event: 消息事件
            keyword: 搜索关键词
        """
        keyword = keyword.strip()
        if not keyword:
            yield event.plain_result("请输入要搜索的歌曲名称，例如：点歌一期一会")
            return

        api_url = self.get_api_url()
        if not api_url:
            yield event.plain_result("请先在插件配置中设置 MetingAPI 地址")
            return

        session_id = event.unified_msg_origin
        source = self.get_session_source(session_id)

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
            self.last_search_results[session_id] = results
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
    async def play_song_by_number(self, event: AstrMessageEvent, number: str = ""):
        """播放指定序号的歌曲，以语音形式发送

        Args:
            event: 消息事件
            number: 歌曲序号
        """
        if not number:
            return

        try:
            index = int(number)
        except ValueError:
            return
        session_id = event.unified_msg_origin

        if (
            session_id not in self.last_search_results
            or not self.last_search_results[session_id]
        ):
            yield event.plain_result('请先使用"点歌"命令搜索歌曲')
            return

        results = self.last_search_results[session_id]
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
            await self._split_and_send_audio(event, temp_file)

        except aiohttp.ClientError as e:
            logger.error(f"下载歌曲时网络错误: {e}")
            yield event.plain_result("下载失败，请检查网络连接")
        except Exception as e:
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
        temp_dir = tempfile.gettempdir()
        temp_file = os.path.join(
            temp_dir, f"meting_song_{sender_id}_{uuid.uuid4()}.mp3"
        )

        try:
            async with self._http_session.get(url) as resp:
                if resp.status != 200:
                    logger.error(f"下载歌曲失败，状态码: {resp.status}")
                    return None

                content_type = resp.headers.get("Content-Type", "")
                if not self._is_audio_content(content_type):
                    logger.warning(f"返回的 Content-Type: {content_type}")
                    return None

                total_size = 0
                with open(temp_file, "wb") as f:
                    async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                        f.write(chunk)
                        total_size += len(chunk)
                        if total_size > MAX_FILE_SIZE:
                            logger.error(f"文件过大，已超过 {MAX_FILE_SIZE} 字节")
                            return None

                file_size = os.path.getsize(temp_file)
                if file_size == 0:
                    logger.error("下载的歌曲文件为空")
                    return None

                return temp_file

        except Exception as e:
            logger.error(f"下载歌曲时发生错误: {e}")
            if os.path.exists(temp_file):
                os.remove(temp_file)
            return None

    def _is_audio_content(self, content_type: str) -> bool:
        """判断 Content-Type 是否为音频

        Args:
            content_type: Content-Type 头

        Returns:
            bool: 是否为音频
        """
        if not content_type:
            return True
        content_type_lower = content_type.lower()
        return "audio" in content_type_lower or content_type_lower in (
            "application/octet-stream",
            "application/x-mpegurl",
        )

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

            AudioSegment.converter = self._ffmpeg_path
            logger.info(f"FFmpeg 路径已设置为: {self._ffmpeg_path}")
        except ImportError as e:
            logger.error(f"导入 pydub 失败: {e}")
            yield event.plain_result("缺少音频处理依赖，请联系管理员")
            return

        try:
            logger.info(f"开始处理音频文件: {temp_file}")
            audio = AudioSegment.from_file(temp_file)
            total_duration = len(audio)
            segment_ms = SEGMENT_DURATION * 1000
            logger.info(f"音频总时长: {total_duration}ms, 分段时长: {segment_ms}ms")

            segments = []
            for start in range(0, total_duration, segment_ms):
                end = min(start + segment_ms, total_duration)
                segment = audio[start:end]
                segments.append(segment)

            base_name = os.path.splitext(os.path.basename(temp_file))[0]

            for idx, segment in enumerate(segments, 1):
                segment_file = os.path.join(
                    tempfile.gettempdir(),
                    f"{base_name}_segment_{idx}_{uuid.uuid4()}.wav",
                )
                try:
                    logger.info(f"导出音频片段 {idx} 到: {segment_file}")
                    segment.export(segment_file, format="wav")
                    logger.info(f"创建 Record 对象: {segment_file}")
                    record = Record.fromFileSystem(segment_file)
                    logger.info(f"发送语音片段 {idx}")
                    yield event.chain_result([record])
                    await asyncio.sleep(SEND_INTERVAL)
                except Exception as e:
                    logger.error(f"发送语音片段 {idx} 时发生错误: {e}")
                    yield event.plain_result(f"发送语音片段 {idx} 失败")
                finally:
                    if os.path.exists(segment_file):
                        os.remove(segment_file)

            yield event.plain_result("歌曲播放完成")

        except Exception as e:
            logger.error(f"分割音频时发生错误: {e}")
            yield event.plain_result("音频处理失败，请稍后重试")
        finally:
            if os.path.exists(temp_file):
                os.remove(temp_file)

    async def terminate(self):
        """插件终止时清理资源"""
        if self._http_session:
            await self._http_session.close()
