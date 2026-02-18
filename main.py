import asyncio
import os
import re
import shutil
import time
import uuid
from urllib.parse import parse_qs, urlparse

import aiohttp

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
TEMP_FILE_PREFIX = "astrbot_meting_plugin_"


class MetingPluginError(Exception):
    pass


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


@register("astrbot_plugin_meting", "chuyegzs", "基于 MetingAPI 的点歌插件", "1.2.2")
class MetingPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self._sessions: dict[str, SessionData] = {}
        self._http_session = None
        self._ffmpeg_path = shutil.which("ffmpeg") or ""
        self._initialized = False
        self._sessions_lock = asyncio.Lock()
        self._download_semaphore = asyncio.Semaphore(3)

    async def _ensure_initialized(self):
        if self._initialized:
            return
        self._http_session = aiohttp.ClientSession(
            timeout=REQUEST_TIMEOUT,
            headers={
                "Referer": "https://astrbot.app/",
                "User-Agent": f"AstrBot/{VERSION}",
                "UAK": "AstrBot/plugin_meting",
            },
        )
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

    async def _get_session(self, session_id: str) -> SessionData:
        await self._ensure_initialized()
        async with self._sessions_lock:
            if session_id not in self._sessions:
                default_source = str(self._get_config("default_source", "netease"))
                self._sessions[session_id] = SessionData(default_source)
            return self._sessions[session_id]

    @filter.command("切换QQ音乐", alias={"切换腾讯音乐", "切换腾讯点歌", "切换TencentMusic", "切换QQMusic"})
    async def switch_tencent(self, event: AstrMessageEvent):
        (await self._get_session(event.unified_msg_origin)).source = "tencent"
        yield event.plain_result("已切换音源为QQ音乐")

    @filter.command("切换网易云", alias={"切换网易云音乐", "切换网易点歌", "切换网抑云", "切换网抑云音乐", "切换NeteaseMusic", "切换Netease"})
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
                async for result in self._split_and_send_audio(event, temp_file):
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

    async def _download_song(self, url: str) -> str | None:
        temp_path = get_astrbot_temp_path()
        if not os.path.exists(temp_path):
            os.makedirs(temp_path)

        temp_file = os.path.join(temp_path, f"{TEMP_FILE_PREFIX}{uuid.uuid4()}.tmp")

        async with self._download_semaphore:
            if not self._http_session:
                return None
            async with self._http_session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                content = await resp.read()

                detected_format = _detect_audio_format(content[:1024])
                ext_map = {
                    "mp3": ".mp3",
                    "wav": ".wav",
                    "ogg": ".ogg",
                    "flac": ".flac",
                    "mp4": ".m4a",
                }
                ext = (
                    ext_map.get(str(detected_format), ".mp3")
                    if detected_format
                    else ".mp3"
                )

                with open(temp_file, "wb") as f:
                    f.write(content)
                os.rename(temp_file, temp_file + ext)
                return temp_file + ext

    async def _split_and_send_audio(self, event, temp_file):
        if not self._ffmpeg_path:
            logger.error("未找到 ffmpeg，无法处理音频。请安装 ffmpeg。")
            yield event.plain_result("未找到 ffmpeg，无法播放音频")
            return

        from pydub import AudioSegment

        AudioSegment.converter = self._ffmpeg_path
        audio = AudioSegment.from_file(temp_file)

        duration = self._get_config("segment_duration", 120) or 120
        seg_ms = int(duration) * 1000

        send_interval = self._get_config("send_interval", 1.0) or 1.0
        interval = float(send_interval)

        for i, start in enumerate(range(0, len(audio), seg_ms), 1):
            path = f"{temp_file}_{i}.wav"
            audio[start : start + seg_ms].export(path, format="wav")
            yield event.chain_result([Record(path)])
            if os.path.exists(path):
                os.remove(path)
            await asyncio.sleep(interval)
        if os.path.exists(temp_file):
            os.remove(temp_file)
