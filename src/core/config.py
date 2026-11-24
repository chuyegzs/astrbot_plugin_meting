"""插件配置管理与类型校验模块。"""

from collections.abc import Callable
from typing import Any, TypeVar

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..utils.constants import (
    DEFAULT_API_URL,
    DEFAULT_SIGN_API_URL,
    LOG_PREFIX,
    PHP_API_SUPPORTED_URLS,
    SOURCE_DISPLAY,
)
from ..utils.helpers import force_https

T = TypeVar("T")


class PluginConfig:
    """统一管理插件配置读取、参数校验与会话级发送模式临时覆盖。"""

    def __init__(self, raw_config: Any = None) -> None:
        """初始化插件配置管理器。

        Args:
            raw_config: AstrBot 传入的原始插件配置字典对象。
        """
        self.raw_config = raw_config
        self.send_overrides: dict[str, int] = {}
        self.search_display_overrides: dict[str, bool] = {}
        self.image_theme_overrides: dict[str, str] = {}

    def get_value(
        self,
        key: str,
        default: T,
        validator: Callable[[Any], bool] | None = None,
    ) -> T:
        """读取顶层配置项并进行有效性校验。

        Args:
            key: 配置键名。
            default: 校验不通过或未配置时返回的默认值。
            validator: 可选的校验函数，返回 True 表示配置值合法。

        Returns:
            合法的配置值或默认值。
        """
        if not self.raw_config:
            return default

        value = self.raw_config.get(key, default)
        if validator is not None and not validator(value):
            return default
        return value

    def get_group_value(
        self,
        group: str,
        key: str,
        default: T,
        validator: Callable[[Any], bool] | None = None,
    ) -> T:
        """读取分组下的配置项并进行有效性校验。

        Args:
            group: 配置分组键名。
            key: 分组内的配置项键名。
            default: 校验不通过或未配置时返回的默认值。
            validator: 可选的校验函数，返回 True 表示配置值合法。

        Returns:
            合法的配置值或默认值。
        """
        group_conf = self.get_value(group, {}, lambda x: isinstance(x, dict))
        value = group_conf.get(key, default)
        if validator is not None and not validator(value):
            return default
        return value

    def get_bot_name(self) -> str:
        """获取配置中自定义的机器人称呼。

        Returns:
            去除了首尾空白的自定义称呼；未配置或留空时返回默认值 '猫猫'。
        """
        return self.get_value(
            "bot_name",
            "猫猫",
            lambda x: isinstance(x, str) and bool(x.strip()),
        ).strip()

    def _get_api_config(self) -> dict[str, Any]:
        """获取 API 配置分组字典。

        Returns:
            api_config 分组字典。
        """
        return self.get_value("api_config", {}, lambda x: isinstance(x, dict))

    def get_api_url(self) -> str:
        """获取规范化后的 MetingAPI 基础地址（确保以 '/' 结尾）。

        Returns:
            MetingAPI 地址字符串。
        """
        api_config = self._get_api_config()
        raw_api_url = api_config.get("api_url", DEFAULT_API_URL)
        api_url = raw_api_url.strip() if isinstance(raw_api_url, str) else ""

        if api_url == "custom":
            custom_url = api_config.get("custom_api_url", "")
            url = custom_url.strip() if isinstance(custom_url, str) else ""
            if not url:
                logger.warning(
                    f"{LOG_PREFIX}[配置] API 地址设为 custom 但未填写 custom_api_url，已回退到默认接口喵~"
                )
                url = DEFAULT_API_URL
        else:
            url = api_url or DEFAULT_API_URL

        url = force_https(url) or DEFAULT_API_URL
        return url if url.endswith("/") else f"{url}/"

    def get_api_type(self) -> int:
        """获取当前生效的 MetingAPI 类型。

        Returns:
            API 类型整数：1=Node API，2=PHP API，3=自定义参数模板。
        """
        api_config = self._get_api_config()
        raw_api_url = api_config.get("api_url", DEFAULT_API_URL)
        api_url = raw_api_url.strip() if isinstance(raw_api_url, str) else ""

        if api_url == "custom":
            custom_url = api_config.get("custom_api_url", "")
            if not (isinstance(custom_url, str) and custom_url.strip()):
                return 1

            api_type = api_config.get("api_type", 1)
            api_type = (
                api_type if isinstance(api_type, int) and api_type in (1, 2, 3) else 1
            )

            if api_type == 3:
                template = api_config.get("custom_api_template", "")
                if not (isinstance(template, str) and template.strip()):
                    logger.warning(
                        f"{LOG_PREFIX}[配置] API 类型设为 3 但未填写 custom_api_template，已回退到类型 1 喵~"
                    )
                    return 1
            return api_type

        if api_url in PHP_API_SUPPORTED_URLS:
            return 2
        return 1

    def get_custom_api_template(self) -> str:
        """获取自定义 API 参数模板字符串。

        Returns:
            自定义参数模板字符串；未启用自定义地址时返回空字符串。
        """
        api_config = self._get_api_config()
        raw_api_url = api_config.get("api_url", "")
        api_url = raw_api_url.strip() if isinstance(raw_api_url, str) else ""

        if api_url == "custom":
            custom_url = api_config.get("custom_api_url", "")
            if not (isinstance(custom_url, str) and custom_url.strip()):
                return ""
            template = api_config.get("custom_api_template", "")
            return template.strip() if isinstance(template, str) else ""
        return ""

    def get_sign_api_url(self) -> str:
        """获取音乐卡片 JSON Ark 签名接口地址。

        Returns:
            规范化后的签名接口 URL（以 '/' 结尾）。
        """
        url = (
            self.get_group_value(
                "music_card_config",
                "api_sign_url",
                DEFAULT_SIGN_API_URL,
                lambda x: isinstance(x, str) and bool(x.strip()),
            )
            .strip()
            .rstrip("/")
        )
        url = force_https(url) or DEFAULT_SIGN_API_URL
        return url if url.endswith("/") else f"{url}/"

    def get_send_as_music(self, event: AstrMessageEvent | None = None) -> int:
        """获取当前会话生效的音乐发送方式。

        Args:
            event: 可选的消息事件对象，用于读取当前会话的临时覆盖配置。

        Returns:
            发送方式整数：0=音乐卡片，1=语音切片，2=文件发送。
        """
        if event is not None:
            uid = getattr(event, "unified_msg_origin", "")
            if uid in self.send_overrides:
                return self.send_overrides[uid]

        return self.get_group_value(
            "music_send_config",
            "send_as_music",
            0,
            lambda x: isinstance(x, int) and x in (0, 1, 2),
        )

    def get_send_if_not_supported(self) -> int:
        """获取非 QQ 平台或音乐卡片不可用时的备选发送方式。

        Returns:
            备选方式整数：0=不发送，1=语音切片，2=文件发送。
        """
        return self.get_group_value(
            "music_send_config",
            "send_if_not_supported",
            2,
            lambda x: isinstance(x, int) and x in (0, 1, 2),
        )

    def get_send_music_info(self) -> int:
        """获取配置的音乐音质参数（br）。

        Returns:
            音质码率整数（如 999、320、-1）。
        """
        return self.get_group_value(
            "music_send_config",
            "send_music_info",
            999,
            lambda x: isinstance(x, int) and -1 <= x <= 2147483,
        )

    def get_default_source(self) -> str:
        """获取全局默认搜索音源。

        Returns:
            默认音源标识字符串（默认 'netease'）。
        """
        return self.get_group_value(
            "search_config",
            "default_source",
            "netease",
            lambda x: x in SOURCE_DISPLAY,
        )

    def get_search_result_count(self) -> int:
        """获取单次搜索返回的最大歌曲数量。

        Returns:
            结果数量整数，范围 3~30，默认 8。
        """
        return self.get_group_value(
            "search_config",
            "search_result_count",
            8,
            lambda x: isinstance(x, int) and 3 <= x <= 30,
        )

    def get_search_result_expiration_time(self) -> int:
        """获取搜索结果缓存过期时间（秒）。

        Returns:
            过期秒数，范围 30~300，默认 120。
        """
        return self.get_group_value(
            "search_config",
            "search_result_expiration_time",
            120,
            lambda x: isinstance(x, int) and 30 <= x <= 300,
        )

    def get_search_results_withdrawn_after_timeout(self) -> int:
        """获取搜索结果消息自动撤回等待时间（秒）。

        Returns:
            撤回秒数，范围 -1~300（0 表示点歌后立即撤回，-1 表示不撤回）。
        """
        return self.get_group_value(
            "search_config",
            "search_results_withdrawn_after_timeout",
            60,
            lambda x: isinstance(x, int) and -1 <= x <= 300,
        )

    def get_search_result_restrictions(self) -> bool:
        """获取是否开启搜索结果按搜索者本人隔离。

        Returns:
            若仅搜索者本人可点歌则返回 True，否则返回 False。
        """
        return self.get_group_value(
            "search_config",
            "search_result_restrictions",
            False,
            lambda x: isinstance(x, bool),
        )

    def get_render_search_result_as_image(
        self, event: AstrMessageEvent | None = None
    ) -> bool:
        """获取当前会话是否优先将搜索结果渲染为图片展示。

        Args:
            event: 可选的消息事件对象，用于读取当前会话的临时覆盖配置。

        Returns:
            开启图片渲染返回 True，纯文本展示返回 False。
        """
        if event is not None:
            uid = getattr(event, "unified_msg_origin", "")
            if uid in self.search_display_overrides:
                return self.search_display_overrides[uid]

        return self.get_group_value(
            "search_display_config",
            "render_search_result_as_image",
            True,
            lambda x: isinstance(x, bool),
        )

    def get_image_theme(self, event: AstrMessageEvent | None = None) -> str:
        """获取当前会话生效的搜索结果图片主题风格配置。

        Args:
            event: 可选的消息事件对象，用于读取当前会话的临时覆盖配置。

        Returns:
            主题配置字符串（'auto'、'light'、'dark' 等）。
        """
        if event is not None:
            uid = getattr(event, "unified_msg_origin", "")
            if uid in self.image_theme_overrides:
                return self.image_theme_overrides[uid]

        return self.get_group_value(
            "search_display_config",
            "image_theme",
            "auto",
            lambda x: (
                isinstance(x, str)
                and x.strip().lower()
                in ("auto", "light", "dark", "时间自动", "亮色", "暗色", "浅色", "深色")
            ),
        )

    def get_segment_duration(self) -> int:
        """获取语音切片单段最大时长（秒）。

        Returns:
            分段时长秒数，范围 30~300，默认 120。
        """
        return self.get_group_value(
            "audio_send_config",
            "segment_duration",
            120,
            lambda x: isinstance(x, int) and 30 <= x <= 300,
        )

    def get_send_interval(self) -> float:
        """获取多段语音切片之间的发送间隔（秒）。

        Returns:
            发送间隔秒数，范围 0~10，默认 1.0。
        """
        return float(
            self.get_group_value(
                "audio_send_config",
                "send_interval",
                1.0,
                lambda x: isinstance(x, (int, float)) and 0 <= x <= 10,
            )
        )

    def get_max_file_size(self) -> int:
        """获取单首歌曲允许下载的最大文件字节数。

        Returns:
            最大文件大小（字节），默认 80MB。
        """
        try:
            mb = self.get_group_value(
                "download_config",
                "max_file_size",
                80,
                lambda x: isinstance(x, (int, float)) and 10 <= x <= 200,
            )
            if not isinstance(mb, (int, float)):
                logger.warning(
                    f"{LOG_PREFIX}[配置] max_file_size 配置无效 ({mb})，已使用默认值 80MB 喵~"
                )
                mb = 80
            return int(float(mb) * 1024 * 1024)
        except Exception as e:
            logger.error(
                f"{LOG_PREFIX}[配置] 读取 max_file_size 出错: {e}，已使用默认值 80MB"
            )
            return 80 * 1024 * 1024

    def get_enable_cache(self) -> bool:
        """获取是否开启本地音频文件缓存。

        Returns:
            开启缓存返回 True，否则返回 False。
        """
        return self.get_group_value(
            "download_config",
            "enable_cache",
            False,
            lambda x: isinstance(x, bool),
        )

    def get_max_cache_size_bytes(self) -> int:
        """获取本地音频缓存目录的最大允许占用字节数。

        Returns:
            最大缓存空间（字节），默认 1GB。
        """
        max_cache_gb = self.get_group_value(
            "download_config",
            "max_cache_size",
            1,
            lambda x: isinstance(x, (int, float)) and x >= 1,
        )
        return int(float(max_cache_gb) * 1024 * 1024 * 1024)

    def get_daily_cleanup_time(self) -> tuple[int, int]:
        """获取每日定时清理缓存的目标时分。

        Returns:
            二元组 (小时, 分钟)，默认 (4, 30)。
        """
        daily_cleanup_time = self.get_group_value(
            "download_config",
            "daily_cleanup_time",
            "04:30",
            lambda x: (
                isinstance(x, str)
                and len(x.strip().split(":")) == 2
                and all(p.isdecimal() for p in x.strip().split(":"))
                and 0 <= int(x.strip().split(":")[0]) <= 23
                and 0 <= int(x.strip().split(":")[1]) <= 59
            ),
        )
        target_h, target_m = map(int, daily_cleanup_time.strip().split(":"))
        return target_h, target_m
