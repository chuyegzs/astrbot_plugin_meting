"""插件全局常量与自定义异常定义。"""

import ipaddress
from pathlib import Path

import aiohttp
import yaml

_META_PATH = Path(__file__).resolve().parents[2] / "metadata.yaml"
PL_VERSION: str = (
    str(yaml.safe_load(_META_PATH.read_text(encoding="utf-8"))["version"])
    .strip()
    .lstrip("v")
)

LOG_PREFIX = "[Meting点歌]"

DEFAULT_API_URL = "https://musicapi.chuyel.top/meting/"
DEFAULT_SIGN_API_URL = "https://oiapi.net/api/QQMusicJSONArk/"

SOURCE_DISPLAY: dict[str, str] = {
    "tencent": "QQ音乐",
    "netease": "网易云音乐",
    "kugou": "酷狗音乐",
    "kuwo": "酷我音乐",
}

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300, sock_connect=15, sock_read=30)
CHUNK_SIZE = 512 * 1024
MAX_SESSION_AGE = 3600

AUDIO_CONTENT_TYPES: set[str] = {
    "audio/mpeg",
    "audio/mp3",
    "audio/flac",
    "audio/x-flac",
    "application/x-flac",
    "audio/wav",
    "audio/x-wav",
    "audio/ogg",
    "application/ogg",
    "audio/aac",
    "audio/x-aac",
    "audio/x-m4a",
    "audio/mp4",
    "audio/webm",
    "audio/x-matroska",
    "application/octet-stream",
}

VALID_AUDIO_EXTS: set[str] = {
    ".mp3",
    ".flac",
    ".wav",
    ".m4a",
    ".ogg",
    ".aac",
    ".wma",
    ".ape",
}

TEMP_DIR_NAME = "meting_nano"
TEMP_FILE_PREFIX = "astrbot_meting_plugin_"
CACHE_DIR_NAME = "astrbot_meting_cache"

FFMPEG_INFO_TIMEOUT = 30
FFMPEG_CONVERT_TIMEOUT = 120

CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

PHP_API_SUPPORTED_URLS: set[str] = {
    "https://metingapi.nanorocky.top/",
    "https://api.injahow.cn/meting/",
    "https://metingapi.mo-app.cn/",
}

QQ_PLATFORMS: set[str] = {
    "aiocqhttp",
    "nakuru",
    "mirai",
    "qq",
    "shamrock",
    "red",
    "lagrange",
    "napcat",
    "gocqhttp",
    "llonebot",
    "chronocat",
}


class MetingPluginError(Exception):
    """插件基础异常。"""


class DownloadError(MetingPluginError):
    """音频下载异常。"""


class AudioFormatError(MetingPluginError):
    """音频格式校验异常。"""
