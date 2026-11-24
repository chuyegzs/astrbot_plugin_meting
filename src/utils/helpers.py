"""无状态纯函数工具集。"""

import hashlib
import ipaddress
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import machineid

from astrbot.core.utils.metrics import Metric

from .constants import (
    AUDIO_CONTENT_TYPES,
    CGNAT_NET,
    QQ_PLATFORMS,
    VALID_AUDIO_EXTS,
)

_HTTP_SCHEME_RE = re.compile(r"^http://", re.IGNORECASE)
_HTTPS_SCHEME_RE = re.compile(r"^https://", re.IGNORECASE)


def is_private_host(host: str) -> bool:
    """检查主机名是否为本地地址或私有 IP 地址。

    Args:
        host: 待检查的主机名或 IP 字符串。

    Returns:
        若为本地地址或字面量私有 IP 则返回 True，否则返回 False。
    """
    if not host:
        return False

    clean_host = host.strip("[]").lower().rstrip(".")
    if clean_host in (
        "localhost",
        "localhost.localdomain",
        "local",
    ) or clean_host.endswith((".localhost", ".local", ".localdomain")):
        return True

    try:
        ip = ipaddress.ip_address(clean_host)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or (isinstance(ip, ipaddress.IPv4Address) and ip in CGNAT_NET)
        )
    except ValueError:
        return False


def force_https(url: str) -> str:
    """将 URL 协议规范化为 https，同时保留本地或私有 IP 的 http 协议。

    Args:
        url: 待规范化的原始 URL 字符串。

    Returns:
        规范化后的 URL 字符串；若输入为空则返回空字符串。
    """
    url = url.strip()
    if not url:
        return ""
    if _HTTPS_SCHEME_RE.match(url):
        return url
    if _HTTP_SCHEME_RE.match(url):
        parsed = urlparse(url)
        if parsed.hostname and is_private_host(parsed.hostname):
            return url
        return _HTTP_SCHEME_RE.sub("https://", url, count=1)
    if url.startswith("//"):
        return f"https:{url}"
    return f"https://{url}"


def generate_guid() -> str:
    """生成基于机器 ID、MAC 地址与 AstrBot 安装 ID 的唯一标识。

    Returns:
        32 位十六进制 MD5 字符串。
    """
    try:
        mid = machineid.id()
    except Exception:
        mid = ""
    raw = f"{mid}{uuid.getnode()}{Metric.get_installation_id()}"
    return hashlib.md5(raw.encode()).hexdigest()


def detect_platform(event: Any) -> tuple[str, bool]:
    """从消息事件中识别消息平台名称及是否属于 QQ 平台体系。

    Args:
        event: AstrBot 消息事件对象。

    Returns:
        二元组 (平台名称字符串, 是否为QQ平台)。
    """
    umo = getattr(event, "unified_msg_origin", "")
    candidates: set[str] = set()
    primary_name = ""

    if hasattr(event, "get_platform_name"):
        try:
            pname = event.get_platform_name()
            if isinstance(pname, str) and pname:
                primary_name = pname
                candidates.add(pname.lower())
        except Exception:
            pass

    if hasattr(event, "platform_meta") and event.platform_meta:
        pname = getattr(event.platform_meta, "name", "")
        if isinstance(pname, str) and pname:
            if not primary_name:
                primary_name = pname
            candidates.add(pname.lower())

    if not candidates and umo and isinstance(umo, str):
        umo_prefix = umo.split(":")[0].strip()
        if umo_prefix:
            primary_name = umo_prefix
            candidates.add(umo_prefix.lower())

    is_qq = bool(candidates & QQ_PLATFORMS)
    return primary_name or "unknown", is_qq


def extract_song_meta(
    song: dict[str, Any],
    default_source: str = "netease",
    br: int = -1,
    artist_sep: str = "/",
) -> dict[str, str]:
    """从歌曲字典中提取并规范化元数据与链接。

    Args:
        song: 接口返回的原始歌曲字典。
        default_source: 当歌曲字典未携带 source 时使用的回退音源。
        br: 目标音质码率，大于 0 时自动附加到播放 URL。
        artist_sep: 多位歌手或专辑列表拼接时的分隔符。

    Returns:
        包含 title, artist, album, source, song_id, raw_url, song_url,
        cover, jump_url, card_format 的标准化字符串字典。
    """
    raw_url = str(song.get("url") or "").strip()
    song_url = raw_url
    if br > 0 and "type=url" in song_url and "br=" not in song_url:
        connector = "&" if "?" in song_url else "?"
        song_url = f"{song_url}{connector}br={br}"

    title = str(song.get("name") or song.get("title") or "未知歌名")

    raw_artist = song.get("artist") or song.get("author") or "未知歌手"
    artist = (
        artist_sep.join(str(a) for a in raw_artist)
        if isinstance(raw_artist, list)
        else str(raw_artist)
    )

    raw_album = song.get("album") or "未知专辑"
    album = (
        artist_sep.join(str(a) for a in raw_album)
        if isinstance(raw_album, list)
        else str(raw_album)
    )

    source = str(song.get("source") or default_source or "netease")
    song_id = str(song.get("songmid") or song.get("id") or "")
    if not song_id and raw_url:
        try:
            query = urlparse(raw_url).query
            song_id = parse_qs(query).get("id", [""])[0]
        except Exception:
            pass

    cover = str(song.get("pic") or song.get("cover") or "")
    jump_url_map = {
        "netease": (f"https://music.163.com/#/song?id={song_id}", "163"),
        "tencent": (f"https://y.qq.com/n/ryqq/songDetail/{song_id}", "qq"),
        "bilibili": (f"https://www.bilibili.com/audio/{song_id}", "bilibili"),
        "kugou": (f"https://www.kugou.com/song/#{song_id}", "kugou"),
        "kuwo": (f"https://kuwo.cn/play_detail/{song_id}", "kuwo"),
    }
    jump_url, card_format = jump_url_map.get(
        source, (song_url.replace("type=url", "type=song"), "163")
    )

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "source": source,
        "song_id": song_id,
        "raw_url": raw_url,
        "song_url": song_url,
        "cover": cover,
        "jump_url": jump_url,
        "card_format": card_format,
    }


def guess_file_extension(url: str, headers: Any) -> str:
    """综合 URL 路径、Content-Disposition 与 Content-Type 嗅探音频文件扩展名。

    Args:
        url: 最终请求的 HTTP URL。
        headers: HTTP 响应头映射对象。

    Returns:
        嗅探到的扩展名（如 '.mp3'、'.flac'），若无法识别则返回 '.tmp'。
    """
    parsed_path = urlparse(url).path
    url_ext = Path(parsed_path).suffix.lower()
    if url_ext in VALID_AUDIO_EXTS:
        return url_ext

    cd = headers.get("Content-Disposition", "") if headers else ""
    if "filename=" in cd:
        match = re.search(r'filename=["\']?([^";\']+)', cd)
        if match:
            cd_ext = Path(match.group(1)).suffix.lower()
            if cd_ext in VALID_AUDIO_EXTS:
                return cd_ext

    content_type = headers.get("Content-Type", "") if headers else ""
    mime_pure = content_type.lower().split(";")[0].strip()
    mime_map = {
        "audio/flac": ".flac",
        "audio/x-flac": ".flac",
        "application/x-flac": ".flac",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/ogg": ".ogg",
        "application/ogg": ".ogg",
    }
    if mime_pure in mime_map:
        return mime_map[mime_pure]

    guessed = mimetypes.guess_extension(mime_pure)
    return guessed if guessed in VALID_AUDIO_EXTS else ".tmp"


def is_audio_content(content_type: str) -> bool:
    """判断 HTTP Content-Type 响应头是否为支持的音频类型。

    Args:
        content_type: HTTP 响应中的 Content-Type 字符串。

    Returns:
        若属于有效音频 MIME 类型则返回 True，否则返回 False。
    """
    if not content_type:
        return False
    mime_pure = content_type.lower().split(";")[0].strip()
    return mime_pure.startswith("audio/") or mime_pure in AUDIO_CONTENT_TYPES
