"""AstrBot MetingAPI 点歌插件主入口模块。"""

import asyncio
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .src.core.api_client import MetingApiClient
from .src.core.audio import AudioService
from .src.core.config import PluginConfig
from .src.core.llm_handler import LLMToolHandler
from .src.core.sender import MusicSender
from .src.core.session import SessionManager
from .src.utils.constants import LOG_PREFIX, PL_VERSION


@register("astrbot_plugin_meting", "chuyegzs", "基于 MetingAPI 的点歌插件", PL_VERSION)
class MetingPlugin(Star):
    """基于 MetingAPI 的多音源点歌插件，支持音乐卡片、智能语音分段与文件直传。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        """初始化点歌插件并组装各核心业务组件。

        Args:
            context: AstrBot 插件运行上下文。
            config: 插件配置字典对象。
        """
        super().__init__(context)
        self.config = config
        self.cfg = PluginConfig(config)
        self.session_mgr = SessionManager(self.cfg)
        self.api_client = MetingApiClient(self.cfg)
        self.audio_service = AudioService(self.cfg, self.api_client, self.session_mgr)
        self.sender = MusicSender(
            cfg=self.cfg,
            api_client=self.api_client,
            session_mgr=self.session_mgr,
            audio_service=self.audio_service,
            template_path=Path(__file__).parent / "src" / "views" / "search.html",
            html_render_fn=self.html_render,
        )
        self.llm_handler = LLMToolHandler(
            cfg=self.cfg,
            api_client=self.api_client,
            session_mgr=self.session_mgr,
            sender=self.sender,
        )

        self._cleanup_task: asyncio.Task[Any] | None = None

    async def initialize(self) -> None:
        """AstrBot 框架加载插件时调用的异步初始化钩子。"""
        logger.info(f"{LOG_PREFIX}[系统] 猫猫正在初始化点歌插件...")
        self.api_client.ensure_session()
        await asyncio.to_thread(self.audio_service.clear_all_cache)

        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        logger.info(f"{LOG_PREFIX}[系统] 点歌插件初始化完成啦喵~")

    async def _periodic_cleanup(self) -> None:
        """后台循环任务：定期清理过期会话、残留临时文件及每日定时清理音乐缓存。"""
        last_cleanup_date: str | None = None
        while True:
            try:
                await asyncio.sleep(30)
                async with self.session_mgr.sessions_lock:
                    await self.session_mgr.cleanup_old_sessions_locked()

                await asyncio.to_thread(self.audio_service.cleanup_temp_files)

                now = time.localtime()
                target_h, target_m = self.cfg.get_daily_cleanup_time()
                current_date = f"{now.tm_year}-{now.tm_mon}-{now.tm_mday}"
                elapsed_mins = (now.tm_hour * 60 + now.tm_min) - (
                    target_h * 60 + target_m
                )
                if 0 <= elapsed_mins <= 5 and last_cleanup_date != current_date:
                    logger.info(
                        f"{LOG_PREFIX}[系统] 到达每日定时清理时间 {target_h:02d}:{target_m:02d}，开始清理音乐缓存..."
                    )
                    await asyncio.to_thread(self.audio_service.clear_all_cache)
                    last_cleanup_date = current_date

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"{LOG_PREFIX}[系统] 定期清理任务执行异常: {e}")

    async def _switch_source(
        self,
        event: AstrMessageEvent,
        source: str,
        source_name: str,
    ) -> Any:
        """切换当前会话默认音源并返回提示消息。

        Args:
            event: 消息事件对象。
            source: 目标音源内部标识。
            source_name: 目标音源展示名称。

        Returns:
            纯文本消息结果。
        """
        session_id = event.unified_msg_origin
        await self.session_mgr.set_session_source(session_id, source)
        return event.plain_result(
            f"好耶！猫猫已将当前音源切换为「{source_name}」啦喵~ 🎵"
        )

    async def _handle_specific_source_play(
        self,
        event: AstrMessageEvent,
        source: str,
        prefixes: list[str],
    ) -> None:
        """处理指定音源的快捷点歌指令（直接搜索并播放第一首结果）。

        Args:
            event: 消息事件对象。
            source: 目标音源标识。
            prefixes: 触发该指令的命令前缀列表。
        """
        kw = event.get_message_str().strip()
        for prefix in prefixes:
            if kw.startswith(prefix):
                kw = kw[len(prefix) :].strip()
                break

        if not kw:
            await event.send(
                event.plain_result(
                    f"请告诉猫猫要点播的歌名喔喵~ 例如：{prefixes[0]} 坠落星空"
                )
            )
            return

        results = await self.api_client.perform_search(kw, source)
        if results is None:
            await event.send(
                event.plain_result("猫猫搜索歌曲时遇到网络小故障惹，请稍后再试qwq")
            )
            return
        if not results:
            await event.send(
                event.plain_result(f"猫猫没有找到与「{kw}」相关的歌曲诶qwq")
            )
            return

        song = results[0]
        if "source" not in song:
            song["source"] = source

        await self.sender.play_song(event, song, event.unified_msg_origin)

    @filter.command("切换发送模式", alias={"切换点歌模式", "switch meting mode"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def switch_send_mode(
        self,
        event: AstrMessageEvent,
        mode: str = "",
    ) -> Any:
        """临时切换当前会话的音乐发送方式 [卡片/语音/文件/默认]。

        Args:
            event: 消息事件对象。
            mode: 目标发送模式名称或编号。

        Returns:
            切换结果提示消息。
        """
        event.stop_event()
        if not mode:
            message_str = event.get_message_str().strip()
            for prefix in ("切换发送模式", "切换点歌模式", "switch meting mode"):
                if message_str.startswith(prefix):
                    mode = message_str[len(prefix) :].strip()
                    break

        if not mode:
            return event.plain_result(
                "请指定要切换的模式喵：卡片(0)、语音(1)、文件(2) 或 默认(恢复)\n"
                "示例：/切换发送模式 语音"
            )

        mode_map = {
            "卡片": 0,
            "0": 0,
            "语音": 1,
            "1": 1,
            "文件": 2,
            "2": 2,
        }

        uid = event.unified_msg_origin
        if mode in ("默认", "恢复", "default", "-1"):
            self.cfg.send_overrides.pop(uid, None)
            return event.plain_result("已恢复当前会话的音乐发送模式为全局默认设置啦喵~")

        if mode in mode_map:
            val = mode_map[mode]
            self.cfg.send_overrides[uid] = val
            mode_name = {0: "音乐卡片", 1: "语音切片", 2: "文件发送"}[val]
            return event.plain_result(
                f"好喔！猫猫已临时将当前会话的发送模式切换为：「{mode_name}」喵~"
            )

        return event.plain_result(
            "猫猫不认识这个模式诶qwq! 可选模式：卡片、语音、文件、默认。"
        )

    @filter.command(
        "切换搜歌展示",
        alias={
            "切换搜歌模式",
            "mting-切换搜索展示",
            "mting-切换搜索模式",
            "mting-switch search display",
            "mting-switch search mode",
        },
    )
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def switch_search_display(
        self,
        event: AstrMessageEvent,
        mode: str = "",
    ) -> Any:
        """临时切换当前会话的搜索结果展示方式 [图片/文本/默认]。

        Args:
            event: 消息事件对象。
            mode: 目标展示方式（图片/文本/默认）。

        Returns:
            切换结果提示消息。
        """
        event.stop_event()
        if not mode:
            message_str = event.get_message_str().strip()
            for prefix in (
                "切换搜歌展示",
                "切换搜歌模式",
                "mting-切换搜索展示",
                "mting-切换搜索模式",
                "mting-switch search display",
                "mting-switch search mode",
            ):
                if message_str.startswith(prefix):
                    mode = message_str[len(prefix) :].strip()
                    break

        if not mode:
            return event.plain_result(
                "请指定要切换的搜歌展示方式喵：图片、文本 或 默认(恢复)\n"
                "示例：/切换搜歌展示 文本"
            )

        uid = event.unified_msg_origin
        clean_mode = mode.strip().lower()
        if clean_mode in ("默认", "恢复", "default", "-1"):
            self.cfg.search_display_overrides.pop(uid, None)
            return event.plain_result(
                "已恢复当前会话的搜歌结果展示方式为全局默认设置啦喵~"
            )

        if clean_mode in ("图片", "图", "image", "img", "1"):
            self.cfg.search_display_overrides[uid] = True
            return event.plain_result(
                "好喔！猫猫已将当前会话的搜歌结果展示方式切换为：「图片」喵~ 🖼️"
            )

        if clean_mode in ("文本", "文字", "text", "txt", "0"):
            self.cfg.search_display_overrides[uid] = False
            return event.plain_result(
                "好喔！猫猫已将当前会话的搜歌结果展示方式切换为：「文本」喵~ 📝"
            )

        return event.plain_result(
            "猫猫不认识这个展示方式诶qwq！可选方式：图片、文本、默认。"
        )

    @filter.command(
        "切换搜歌主题",
        alias={
            "mting-切换搜索主题",
            "mting-切换图片主题",
            "mting-切换主题",
            "mting-switch search theme",
            "mting-switch theme",
        },
    )
    async def switch_image_theme(
        self,
        event: AstrMessageEvent,
        theme: str = "",
    ) -> Any:
        """切换当前会话的搜歌结果图片主题 [浅色/深色/自动/默认]。

        Args:
            event: 消息事件对象。
            theme: 目标图片主题（浅色/深色/自动/默认）。

        Returns:
            切换结果提示消息。
        """
        event.stop_event()
        if not theme:
            message_str = event.get_message_str().strip()
            for prefix in (
                "切换搜歌主题",
                "mting-切换搜索主题",
                "mting-切换图片主题",
                "mting-切换主题",
                "mting-switch search theme",
                "mting-switch theme",
            ):
                if message_str.startswith(prefix):
                    theme = message_str[len(prefix) :].strip()
                    break

        if not theme:
            return event.plain_result(
                "请告诉猫猫要切换的图片主题喵：浅色、深色、自动 或 默认(恢复)\n"
                "示例：/切换搜歌主题 深色"
            )

        uid = event.unified_msg_origin
        clean_theme = theme.strip().lower()
        if clean_theme in ("默认", "恢复", "default", "-1"):
            self.cfg.image_theme_overrides.pop(uid, None)
            return event.plain_result("已恢复当前会话的搜歌图片主题为全局默认设置啦喵~")

        if clean_theme in ("浅色", "亮色", "日间", "白天", "白", "light", "1"):
            self.cfg.image_theme_overrides[uid] = "light"
            return event.plain_result(
                "好耶！猫猫已将当前会话的搜歌图片主题切换为：「浅色模式」啦喵~ ☀️"
            )

        if clean_theme in ("深色", "暗色", "夜间", "黑夜", "黑", "dark", "2"):
            self.cfg.image_theme_overrides[uid] = "dark"
            return event.plain_result(
                "好耶！猫猫已将当前会话的搜歌图片主题切换为：「深色模式」啦喵~ 🌙"
            )

        if clean_theme in ("自动", "时间自动", "auto", "0"):
            self.cfg.image_theme_overrides[uid] = "auto"
            return event.plain_result(
                "好耶！猫猫已将当前会话的搜歌图片主题切换为：「时间自动」啦喵~ 🕒"
            )

        return event.plain_result(
            "猫猫不认识这个主题诶qwq！可选主题：浅色、深色、自动、默认。"
        )

    @filter.command(
        "切换QQ音乐",
        alias={"切换腾讯音乐", "切换QQMusic", "switch tencent", "switch qqmusic"},
    )
    async def switch_tencent(self, event: AstrMessageEvent) -> Any:
        """切换当前会话的默认音源为 QQ 音乐。"""
        event.stop_event()
        return await self._switch_source(event, "tencent", "QQ音乐")

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
    async def switch_netease(self, event: AstrMessageEvent) -> Any:
        """切换当前会话的默认音源为网易云音乐。"""
        event.stop_event()
        return await self._switch_source(event, "netease", "网易云音乐")

    @filter.command("切换酷狗", alias={"切换酷狗音乐", "switch kugou"})
    async def switch_kugou(self, event: AstrMessageEvent) -> Any:
        """切换当前会话的默认音源为酷狗音乐。"""
        event.stop_event()
        return await self._switch_source(event, "kugou", "酷狗音乐")

    @filter.command("切换酷我", alias={"切换酷我音乐", "switch kuwo"})
    async def switch_kuwo(self, event: AstrMessageEvent) -> Any:
        """切换当前会话的默认音源为酷我音乐。"""
        event.stop_event()
        return await self._switch_source(event, "kuwo", "酷我音乐")

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
    async def play_netease_first_song(self, event: AstrMessageEvent) -> None:
        """在网易云音乐中搜索并直接播放第一首歌曲。"""
        event.stop_event()
        await self._handle_specific_source_play(
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
        )

    @filter.command(
        "腾讯点歌",
        alias={"QQ点歌", "QQ音乐点歌", "腾讯音乐点歌", "tencent play", "qq play"},
    )
    async def play_tencent_first_song(self, event: AstrMessageEvent) -> None:
        """在 QQ 音乐中搜索并直接播放第一首歌曲。"""
        event.stop_event()
        await self._handle_specific_source_play(
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
        )

    @filter.command("酷狗点歌", alias={"酷狗音乐点歌", "kugou play"})
    async def play_kugou_first_song(self, event: AstrMessageEvent) -> None:
        """在酷狗音乐中搜索并直接播放第一首歌曲。"""
        event.stop_event()
        await self._handle_specific_source_play(
            event, "kugou", ["酷狗音乐点歌", "酷狗点歌", "kugou play"]
        )

    @filter.command("酷我点歌", alias={"酷我音乐点歌", "kuwo play"})
    async def play_kuwo_first_song(self, event: AstrMessageEvent) -> None:
        """在酷我音乐中搜索并直接播放第一首歌曲。"""
        event.stop_event()
        await self._handle_specific_source_play(
            event, "kuwo", ["酷我音乐点歌", "酷我点歌", "kuwo play"]
        )

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
    async def show_commands(self, event: AstrMessageEvent) -> Any:
        """展示插件支持的所有点歌与音源切换指令说明。"""
        event.stop_event()
        commands = [
            "🐾 猫猫的 Meting 点歌指令指南 🎵",
            "========================",
            "【基础指令】",
            "• 搜歌 <歌名> - 搜索歌曲并展示列表",
            "• 点歌 <序号> - 播放搜歌列表里的指定歌曲",
            "• 点歌 <歌名> - 直接搜索并播放第一首歌",
            "",
            "【快捷点歌】(不改变会话默认音源)",
            "• 网易点歌 <歌名> - 网易云音乐快捷点歌",
            "• QQ点歌 <歌名> - QQ音乐快捷点歌",
            "• 酷狗点歌 <歌名> - 酷狗音乐快捷点歌",
            "• 酷我点歌 <歌名> - 酷我音乐快捷点歌",
            "",
            "【音源与模式切换】",
            "• 切换网易云 / 切换QQ音乐 / 切换酷狗 / 切换酷我",
            "• 切换搜歌主题 <浅色/深色/自动/默认>",
            "• 切换搜歌展示 <图片/文本/默认> (管理员)",
            "• 切换发送模式 <卡片/语音/文件/默认> (管理员)",
            "========================",
            f"初叶🍂 MetingAPI 点歌插件  v{PL_VERSION}",
            "========================",
            "Powered by NanoRocky",
        ]
        return event.plain_result("\n".join(commands))

    @filter.command("点歌", alias={"mting-play", "mting-play song"})
    async def play_song_cmd(self, event: AstrMessageEvent) -> Any:
        """通用点歌指令：支持按搜歌序号点播或直接按歌名搜索播放。"""
        event.stop_event()

        message_str = event.get_message_str().strip()
        session_id = event.unified_msg_origin
        sender_id = event.get_sender_id()

        arg = message_str
        for prefix in ("点歌", "mting-play song", "mting-play"):
            if arg.startswith(prefix):
                arg = arg[len(prefix) :].strip()
                break

        if not arg:
            return event.plain_result(
                "请告诉猫猫要点播的歌曲序号或歌名喔喵~ 例如：点歌 1 或 点歌 坠落星空"
            )

        unsigned_arg = arg[1:] if arg.startswith(("+", "-")) else arg
        if unsigned_arg.isdecimal() or (
            unsigned_arg.count(".") == 1 and unsigned_arg.replace(".", "").isdecimal()
        ):
            index = int(arg) if unsigned_arg.isdecimal() else None
            logger.info(
                f"{LOG_PREFIX}[点歌] 收到序号点歌请求: arg={arg!r}, session={session_id}"
            )

            results = await self.session_mgr.get_session_results(session_id, sender_id)
            if not results:
                return event.plain_result(
                    '还没有搜索记录或已经过期啦，请先发送 "搜歌 歌名" 让猫猫找找歌叭~'
                )

            if index is None or index < 1 or index > len(results):
                return event.plain_result(
                    f"序号超出范围啦qwq，请输入 1~{len(results)} 之间的整数喵~"
                )

            if self.cfg.get_search_results_withdrawn_after_timeout() == 0:
                msgs_to_delete = await self.session_mgr.pop_msgs_for_immediate_withdraw(
                    session_id, sender_id
                )
                logger.debug(
                    f"{LOG_PREFIX}[会话] 点歌成功，立即清理并撤回搜歌消息 (session={session_id})"
                )
                for mid in msgs_to_delete:
                    await self.sender.delete_search_msg(event, mid)

            song = results[index - 1]
            await self.sender.play_song(event, song, session_id)
            return None

        logger.info(
            f"{LOG_PREFIX}[点歌] 收到歌名直点请求: keyword={arg!r}, session={session_id}"
        )
        source = await self.session_mgr.get_session_source(session_id)
        results = await self.api_client.perform_search(arg, source)
        if results is None:
            return event.plain_result("猫猫搜索歌曲时遇到网络小故障惹，请稍后再试qwq")
        if not results:
            return event.plain_result(f"猫猫没有找到与「{arg}」相关的歌曲诶qwq")

        song = results[0]
        if "source" not in song:
            song["source"] = source

        await self.sender.play_song(event, song, session_id)
        return None

    @filter.command("搜歌", alias={"mting-search", "mting-search song"})
    async def search_song(self, event: AstrMessageEvent) -> Any:
        """在当前会话音源中搜索歌曲并展示结果列表。"""
        event.stop_event()

        keyword = event.get_message_str().strip()
        for prefix in ("搜歌", "mting-search song", "mting-search"):
            if keyword.startswith(prefix):
                keyword = keyword[len(prefix) :].strip()
                break

        if not keyword:
            return event.plain_result(
                "请告诉猫猫要搜索的歌曲名称喔喵~ 例如：搜歌 坠落星空"
            )

        return await self.sender.handle_search(event, keyword)

    @filter.llm_tool("astr_meting_music")
    async def astr_meting_music(
        self,
        event: AstrMessageEvent,
        keyword: str = "",
        source: str = "auto",
        index: int | float | str = -1,
    ) -> str:
        """这是一个音乐搜索与点歌函数。调用方法：

        【搜索音乐】
        - 当用户想找歌或点歌、且你手头还没有对应歌曲的有效 `search_id` 时调用。
        - 参数填法：
          1. `keyword`：必填，填写歌曲名或歌手名。
          2. `source`：填音源名称字符串。默认填 `"auto"`（同时搜索网易云 `netease` 和 QQ音乐 `tencent`）；若用户明确指定了平台，可填 `"netease"`、`"tencent"`、`"kugou"` 或 `"kuwo"`。注意：搜索时 `source` 绝对禁止填 JSON！
          3. `index`：搜索时忽略，保持 `-1` 即可。
        - 返回结果：包含环境信息 `env`、有效期 60 秒的 `search_id`（含 `id`、当前时间戳 `time`、过期时间戳 `deadline`）以及带全局唯一整数序号 `index`（从 `"0"` 开始）的歌曲列表 `sources`。

        【发送/播放音乐（点歌）】
        - 当已通过第一步拿到 `search_id` 且确定要播放哪一首时调用。
        - 参数填法：
          1. `source`：必填 JSON 字符串，格式为 `[{"search_id": "第一步返回的id", "send_mode": "card或file"}]`。
             - `send_mode` 仅允许 `"card"`（音乐卡片）或 `"file"`（音频文件）。
             - 请根据 `env.msg_platform` 判断并**优先使用 `"card"`**（如 `aiocqhttp`、`napcat`、`lagrange`、`llonebot` 等 QQ 协议平台）；若为非 QQ 平台（如 `telegram`、`discord`、`lark`、`webchat`、`wecom` 等）或尝试 `"card"` 失败，请使用 `"file"`。
          2. `index`：必填非负整数（如 `0`、`1`、`5`），对应搜索结果中目标歌曲的 `index` 字段（从 `0` 开始计数，严禁填 `-1` 或小数）。
          3. `keyword`：点歌时可留空或保持原关键词。

        Args:
            keyword (string): 搜索关键词（歌曲名、歌手名等）。搜索时必填；点歌时可留空。
            source (string): 搜索时填音源名（"auto"、"netease"、"tencent"、"kugou"、"kuwo"，默认 "auto"，严禁在搜索时填 JSON）；点歌时填 JSON 字符串，如 [{"search_id": "xxx", "send_mode": "card"}]。
            index (number): 点歌时的歌曲非负整数序号（从 0 开始，对应搜索结果的 index 字段，禁止填负数或浮点数）；搜索时保持 -1。
        """
        return await self.llm_handler.execute(
            event=event,
            keyword=keyword,
            source=source,
            index=index,
        )

    async def terminate(self) -> None:
        """插件卸载或重载时释放网络连接、后台任务与临时文件。"""
        logger.info(f"{LOG_PREFIX}[系统] 猫猫正在卸载并清理点歌插件资源...")
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        await self.session_mgr.clear_all()
        await self.api_client.close()

        await asyncio.to_thread(self.audio_service.cleanup_temp_files)
        await asyncio.to_thread(self.audio_service.clear_all_cache)
        logger.info(f"{LOG_PREFIX}[系统] 点歌插件资源清理完毕喵~")
