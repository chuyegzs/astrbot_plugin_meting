"""MetingAPI 请求、音乐卡片签名与音频后缀探针客户端。"""

import asyncio
import colorsys
import io
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import aiohttp
from PIL import Image

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
        if cover and "picsize=" not in cover:
            if source == "netease":
                connector = "&" if "?" in cover else "?"
                cover = f"{cover}{connector}picsize=320"
            elif source == "tencent":
                connector = "&" if "?" in cover else "?"
                cover = f"{cover}{connector}picsize=300"

        song_url = force_https(song_meta["song_url"])
        cover_url = await self.resolve_cover_url(cover) if cover else ""
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

    async def resolve_cover_url(self, cover_url: str) -> str:
        """解析封面接口的 302 跳转并返回真实图床地址；若接口直接返回图片或探测失败则保留原地址。

        Args:
            cover_url: 原始封面地址。

        Returns:
            规范化后的真实封面 URL；输入为空时返回空字符串。
        """
        current_url = force_https(cover_url)
        if not current_url:
            return ""

        session = self.ensure_session()
        timeout = aiohttp.ClientTimeout(total=5)
        try:
            for _ in range(3):
                location = ""
                status = 0
                async with session.head(
                    current_url, allow_redirects=False, timeout=timeout
                ) as resp:
                    status = resp.status
                    location = resp.headers.get("Location", "")

                if status in (405, 501):
                    async with session.get(
                        current_url, allow_redirects=False, timeout=timeout
                    ) as resp:
                        status = resp.status
                        location = resp.headers.get("Location", "")

                if 300 <= status < 400 and location:
                    next_url = force_https(urljoin(current_url, location))
                    prev_host = urlparse(current_url).netloc.lower()
                    next_host = urlparse(next_url).netloc.lower()
                    current_url = next_url
                    if next_host and next_host != prev_host:
                        return current_url
                    continue
                break
        except Exception as e:
            logger.debug(
                f"{LOG_PREFIX}[接口] 解析封面 302 跳转失败 ({cover_url})，保留原地址: {e}"
            )
        return current_url

    async def resolve_cover_and_accent(
        self,
        cover_url: str,
        theme: str,
    ) -> tuple[str, str]:
        """单次请求解析封面 302 真实图床 URL，并提取适配当前主题的高饱和点缀色。

        若 Meting 接口返回 302 跳转，仅调用一次 Meting 接口获取图床地址，随后直接拉取该图床 URL 取色；
        若接口直接返回 200 图片正文，则直接复用本次响应字节取色，避免重复请求 Meting 接口。
        若未能提取到有效颜色（黑白封面或拉取失败），返回空字符串交由模板使用无色胶囊与封面模糊光晕。

        Args:
            cover_url: 原始封面地址。
            theme: 当前渲染主题（'dark' 或 'light'）。

        Returns:
            二元组 (提供给 T2I 的真实封面 URL, 十六进制主题点缀色或空字符串)。
        """
        current_url = force_https(cover_url)
        if not current_url:
            return "", ""

        session = self.ensure_session()
        resolve_timeout = aiohttp.ClientTimeout(total=5)
        sample_timeout = aiohttp.ClientTimeout(total=3)
        raw_bytes = b""

        try:
            for _ in range(3):
                async with session.get(
                    current_url, allow_redirects=False, timeout=resolve_timeout
                ) as resp:
                    status = resp.status
                    location = resp.headers.get("Location", "")
                    if 300 <= status < 400 and location:
                        next_url = force_https(urljoin(current_url, location))
                        prev_host = urlparse(current_url).netloc.lower()
                        next_host = urlparse(next_url).netloc.lower()
                        current_url = next_url
                        if next_host and next_host != prev_host:
                            break
                        continue
                    if status == 200:
                        raw_bytes = await resp.content.read(2 * 1024 * 1024)
                    break

            if not raw_bytes and current_url:
                async with session.get(
                    current_url, allow_redirects=True, timeout=sample_timeout
                ) as img_resp:
                    if img_resp.status == 200:
                        raw_bytes = await img_resp.content.read(2 * 1024 * 1024)

            if not raw_bytes:
                return current_url, ""

            def _compute() -> str:
                with Image.open(io.BytesIO(raw_bytes)) as img:
                    rgb_bytes = (
                        img.convert("RGB")
                        .resize((32, 32), Image.Resampling.BILINEAR)
                        .tobytes()
                    )
                buckets: dict[int, list[float]] = {}
                for idx in range(0, len(rgb_bytes), 3):
                    r = rgb_bytes[idx] / 255.0
                    g = rgb_bytes[idx + 1] / 255.0
                    b = rgb_bytes[idx + 2] / 255.0
                    h, s, v = colorsys.rgb_to_hsv(r, g, b)
                    if s < 0.22 or v < 0.18 or v > 0.96:
                        continue
                    w = (s**1.5) * (0.4 + 0.6 * v)
                    b_idx = int(h * 24) % 24
                    prev = buckets.get(b_idx)
                    if prev is None:
                        buckets[b_idx] = [w, h * w, s * w]
                    else:
                        prev[0] += w
                        prev[1] += h * w
                        prev[2] += s * w

                if not buckets:
                    return ""
                best = max(buckets.values(), key=lambda x: x[0])
                if best[0] < 3.0:
                    return ""
                avg_h = best[1] / best[0]
                avg_s = best[2] / best[0]
                if theme == "light":
                    rr, gg, bb = colorsys.hls_to_rgb(
                        avg_h, 0.41, min(max(avg_s * 1.1, 0.65), 0.88)
                    )
                else:
                    dark_l = 0.71 if 0.57 <= avg_h <= 0.80 else 0.64
                    rr, gg, bb = colorsys.hls_to_rgb(
                        avg_h, dark_l, min(max(avg_s * 1.15, 0.72), 0.95)
                    )
                return f"#{int(rr * 255):02x}{int(gg * 255):02x}{int(bb * 255):02x}"

            accent = await asyncio.to_thread(_compute)
            return current_url, accent
        except Exception as e:
            logger.debug(
                f"{LOG_PREFIX}[接口] 解析封面或提取主题色失败 ({cover_url}): {e}"
            )
            return current_url, ""

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
