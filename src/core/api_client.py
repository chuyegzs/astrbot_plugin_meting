"""MetingAPI 请求、音乐卡片签名与音频后缀探针客户端。"""

import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp

from astrbot.api import logger
from astrbot.core.config.default import VERSION

from ..utils.constants import (
    LOG_PREFIX,
    PL_VERSION,
    REQUEST_TIMEOUT,
    VALID_AUDIO_EXTS,
    MetingPluginError,
)
from ..utils.helpers import force_https, generate_guid, guess_file_extension
from .config import PluginConfig


class MetingApiClient:
    """封装与 MetingAPI 及音乐卡片签名服务的全部 HTTP 通信。"""

    def __init__(self, cfg: PluginConfig) -> None:
        """初始化 API 客户端。

        Args:
            cfg: 插件配置管理实例。
        """
        self.cfg = cfg
        self.http_session: aiohttp.ClientSession | None = None

    def ensure_session(self) -> aiohttp.ClientSession:
        """获取或创建共享的 aiohttp.ClientSession 实例。

        Returns:
            活跃的 aiohttp.ClientSession 实例。
        """
        if self.http_session is None or self.http_session.closed:
            self.http_session = aiohttp.ClientSession(
                timeout=REQUEST_TIMEOUT,
                headers={
                    "Referer": "https://astrbot.app/",
                    "User-Agent": f"AstrBot/{VERSION}",
                    "UAK": f"AstrBot/plugin_meting * {PL_VERSION} ",
                    "UUID": generate_guid(),
                },
            )
        return self.http_session

    async def close(self) -> None:
        """关闭底层的 HTTP 会话连接池。"""
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        self.http_session = None

    async def perform_search(
        self,
        keyword: str,
        source: str,
    ) -> list[dict[str, Any]] | None:
        """调用 MetingAPI 搜索歌曲列表。

        Args:
            keyword: 搜索关键词（歌名或歌手）。
            source: 目标音源标识（如 'netease'、'tencent'）。

        Returns:
            截断至配置数量的歌曲字典列表；网络或接口异常时返回 None。
        """
        api_url = self.cfg.get_api_url()
        api_type = self.cfg.get_api_type()
        custom_template = self.cfg.get_custom_api_template()
        session = self.ensure_session()

        try:
            if api_type == 3:
                query = custom_template.replace(":server", quote(source, safe=""))
                query = query.replace(":type", quote("search", safe=""))
                query = query.replace(":id", quote(keyword, safe=""))
                query = query.replace(":r", str(int(time.time() * 1000)))

                if query.startswith(("/", "?")):
                    api_endpoint = f"{api_url.rstrip('/')}{query}"
                elif "?" in api_url:
                    api_endpoint = f"{api_url}&{query}"
                else:
                    api_endpoint = f"{api_url}?{query}"

                logger.info(f"{LOG_PREFIX}[接口] 自定义 API 搜歌: url={api_endpoint}")
                async with session.get(api_endpoint) as resp:
                    if resp.status != 200:
                        return None
                    # 仅接受返回标准 JSON 格式数据 (application/json)
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
                    logger.info(
                        f"{LOG_PREFIX}[接口] PHP API 搜歌: url={api_endpoint}, source={source}, keyword={keyword}"
                    )
                else:
                    params = {"server": source, "type": "search", "id": keyword}
                    api_endpoint = f"{api_url}api"
                    logger.info(
                        f"{LOG_PREFIX}[接口] Node API 搜歌: url={api_endpoint}, source={source}, keyword={keyword}"
                    )

                async with session.get(api_endpoint, params=params) as resp:
                    if resp.status != 200:
                        return None
                    # 仅接受返回标准 JSON 格式数据 (application/json)
                    data = await resp.json()

            if not isinstance(data, list) or not data:
                return []

            result_count = self.cfg.get_search_result_count()
            return data[:result_count]

        except Exception as e:
            logger.error(
                f"{LOG_PREFIX}[接口] 搜索歌曲时发生异常 (source={source}, keyword={keyword}): {e}",
                exc_info=True,
            )
            return None

    async def sign_music_card(
        self,
        song_meta: dict[str, str],
    ) -> Any:
        """请求签名接口生成 QQ 音乐 JSON Ark 卡片数据。

        Args:
            song_meta: 由 extract_song_meta 提取的标准化歌曲元数据字典。

        Returns:
            签名接口返回的 Ark 卡片数据。

        Raises:
            MetingPluginError: 当签名接口返回非 200 状态码或签名失败时抛出。
        """
        session = self.ensure_session()
        source = song_meta["source"]
        cover = song_meta["cover"]
        if cover and source == "netease" and "picsize=" not in cover:
            connector = "&" if "?" in cover else "?"
            cover = f"{cover}{connector}picsize=320"

        song_url = force_https(song_meta["song_url"])
        cover_url = force_https(cover) if cover else ""
        jump_url = force_https(song_meta["jump_url"]) if song_meta["jump_url"] else ""

        sign_api = self.cfg.get_sign_api_url()
        params = {
            "url": song_url,
            "song": song_meta["title"],
            "singer": song_meta["artist"],
            "cover": cover_url,
            "jump": jump_url,
            "format": song_meta["card_format"],
        }

        async with session.get(sign_api, params=params) as resp:
            if resp.status != 200:
                raise MetingPluginError(f"签名接口状态码异常: {resp.status}")

            # 仅接受返回标准 JSON 格式数据 (application/json)
            res_json = await resp.json()
            if res_json.get("code") != 1:
                raise MetingPluginError(
                    f"签名失败: {res_json.get('message', '未知错误')}"
                )
            return res_json.get("data")

    async def probe_song_ext(self, song_url: str) -> str:
        """发送 HEAD 请求跟踪重定向并嗅探音频真实扩展名（不下载正文）。

        Args:
            song_url: 歌曲音频播放地址。

        Returns:
            音频扩展名字符串（如 '.mp3'、'.flac'），失败时回退为 '.mp3'。
        """
        try:
            url = force_https(song_url)
            session = self.ensure_session()
            async with session.head(url, allow_redirects=True) as resp:
                final_url = str(resp.url)
                if 200 <= resp.status < 400:
                    ext = guess_file_extension(final_url, resp.headers)
                    return ext if ext and ext != ".tmp" else ".mp3"
                url_ext = Path(urlparse(final_url).path).suffix.lower()
                if url_ext in VALID_AUDIO_EXTS:
                    return url_ext
                raise MetingPluginError(f"HEAD 请求状态码异常: {resp.status}")
        except Exception as e:
            logger.warning(
                f"{LOG_PREFIX}[接口] HEAD 后缀探测失败 ({song_url})，回退到路径解析: {e}"
            )
            parsed = urlparse(song_url).path
            ext = Path(parsed).suffix.lower()
            return ext if ext in VALID_AUDIO_EXTS else ".mp3"
