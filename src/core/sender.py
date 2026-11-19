"""音乐播放编排、搜索结果图文渲染与消息撤回模块。"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File, Json

from ..utils.constants import (
    LOG_PREFIX,
    SOURCE_DISPLAY,
    AudioFormatError,
    DownloadError,
)
from ..utils.helpers import detect_platform, extract_song_meta, force_https
from .api_client import MetingApiClient
from .audio import AudioService
from .config import PluginConfig
from .session import SessionManager


class MusicSender:
    """负责音乐卡片/语音切片/文件三种模式的发送编排，以及搜索结果渲染与撤回。"""

    def __init__(
        self,
        cfg: PluginConfig,
        api_client: MetingApiClient,
        session_mgr: SessionManager,
        audio_service: AudioService,
        template_path: Path,
        html_render_fn: Callable[..., Awaitable[str]],
    ) -> None:
        """初始化音乐发送编排器。

        Args:
            cfg: 插件配置管理实例。
            api_client: MetingAPI 客户端实例。
            session_mgr: 会话管理实例。
            audio_service: 音频下载与转码切片服务实例。
            template_path: 搜索结果 HTML 模板文件路径。
            html_render_fn: Star 实例提供的 html_render 渲染方法。
        """
        self.cfg = cfg
        self.api_client = api_client
        self.session_mgr = session_mgr
        self.audio_service = audio_service
        self.template_path = template_path
        self.html_render_fn = html_render_fn
        self._search_template: str | None = None

    async def send_music_card(
        self,
        event: AstrMessageEvent,
        song_meta: dict[str, str],
    ) -> None:
        """请求签名接口并向当前会话发送 QQ 音乐 JSON Ark 卡片。

        Args:
            event: 消息事件对象。
            song_meta: 标准化歌曲元数据字典。

        Raises:
            Exception: 当签名请求失败或消息平台发送卡片异常时抛出。
        """
        ark_data = await self.api_client.sign_music_card(song_meta)
        json_card = Json(data=ark_data)
        logger.info(
            f"{LOG_PREFIX}[点歌] 音乐卡片签名成功，正在发送卡片: 《{song_meta['title']}》- {song_meta['artist']}"
        )
        logger.debug(f"{LOG_PREFIX}[点歌] 音乐卡片 Ark 数据: {json_card}")
        await event.send(event.chain_result([json_card]))

    async def send_as_file(
        self,
        event: AstrMessageEvent,
        song_meta: dict[str, str],
    ) -> None:
        """以音频文件直传方式向当前会话发送歌曲。

        Args:
            event: 消息事件对象。
            song_meta: 标准化歌曲元数据字典。
        """
        title = song_meta["title"]
        artist = song_meta["artist"]
        album = song_meta["album"]
        song_url = force_https(song_meta["song_url"])

        name = f"{title} - {artist}"
        if album and album != "未知专辑":
            name = f"{name} - [{album}]"

        safe_name = (
            "".join(c for c in name if c.isprintable() and c not in r'\/:*?"<>|').strip(
                " ."
            )[:120]
            or "music"
        )
        ext = await self.api_client.probe_song_ext(song_url)
        file_name = f"{safe_name}{ext}"
        await event.send(event.chain_result([File(name=file_name, url=song_url)]))

    async def play_song(
        self,
        event: AstrMessageEvent,
        song: dict[str, Any],
        session_id: str,
        force_card: bool = False,
    ) -> None:
        """根据配置、平台支持情况及降级策略播放指定歌曲。

        Args:
            event: 消息事件对象。
            song: 原始歌曲数据字典。
            session_id: 当前会话唯一标识。
            force_card: 是否强制使用音乐卡片模式发送。
        """
        default_source = await self.session_mgr.get_session_source(session_id)
        br = self.cfg.get_send_music_info()
        song_meta = extract_song_meta(
            song, default_source=default_source, br=br, artist_sep=" / "
        )

        if not song_meta["raw_url"]:
            await event.send(
                event.plain_result("获取歌曲播放地址失败惹，换一首试试叭qwq")
            )
            return

        send_val = 0 if force_card else self.cfg.get_send_as_music(event)
        fallback_val = self.cfg.get_send_if_not_supported()
        platform_name, is_qq = detect_platform(event)

        if send_val == 0 and not force_card and not is_qq:
            if fallback_val == 0:
                logger.info(
                    f"{LOG_PREFIX}[点歌] 消息来源非 QQ ({platform_name}) 且备选方式设为不发送，已取消发送"
                )
                await event.send(event.plain_result("当前平台不支持发送音乐卡片喔喵~"))
                return
            logger.info(
                f"{LOG_PREFIX}[点歌] 消息来源非 QQ ({platform_name})，自动切换为备选发送方式: {fallback_val}"
            )
            send_val = fallback_val

        # 模式 0：音乐卡片发送（失败时按需降级到备选方式）
        if send_val == 0:
            try:
                await self.send_music_card(event, song_meta)
                return
            except Exception as card_err:
                logger.error(f"{LOG_PREFIX}[点歌] 音乐卡片发送失败: {card_err}")
                if force_card or fallback_val == 0:
                    await event.send(
                        event.plain_result("制作或发送音乐卡片时出错惹qwq")
                    )
                    return
                logger.info(
                    f"{LOG_PREFIX}[点歌] 音乐卡片不可用，自动降级为备选发送方式: {fallback_val}"
                )
                send_val = fallback_val

        # 模式 1：下载音频并切片发送语音（固定 320 码率节省带宽）
        if send_val == 1:
            voice_meta = extract_song_meta(
                song, default_source=default_source, br=320, artist_sep=" / "
            )
            temp_file: str | None = None
            try:
                temp_file, duration = await self.audio_service.download_song(
                    force_https(voice_meta["song_url"]),
                    event.get_sender_id(),
                    voice_meta["source"],
                    voice_meta["song_id"],
                    br=320,
                )
                await event.send(
                    event.plain_result("猫猫正在努力录制歌曲语音，请稍等一下下喵~ 🎙️")
                )
                await self.audio_service.split_and_send_audio(
                    event, temp_file, session_id, duration
                )
            except asyncio.CancelledError:
                logger.info(f"{LOG_PREFIX}[点歌] 语音播放任务被取消")
                raise
            except DownloadError as e:
                logger.error(f"{LOG_PREFIX}[点歌] 歌曲下载失败: {e}")
                await event.send(event.plain_result(f"猫猫下载歌曲失败惹qwq: {e}"))
            except AudioFormatError as e:
                logger.error(f"{LOG_PREFIX}[点歌] 音频格式校验未通过: {e}")
                await event.send(event.plain_result(f"音频格式不支持诶qwq: {e}"))
            except Exception as e:
                logger.error(
                    f"{LOG_PREFIX}[点歌] 播放歌曲时发生异常: {e}", exc_info=True
                )
                await event.send(
                    event.plain_result("播音室出现了一点小故障，请稍后再试qwq")
                )
            finally:
                if temp_file:
                    self.audio_service.release_file(temp_file)

        # 模式 2：音频文件直传
        elif send_val == 2:
            try:
                await self.send_as_file(event, song_meta)
            except Exception as e:
                logger.error(f"{LOG_PREFIX}[点歌] 文件方式发送失败: {e}", exc_info=True)
                await event.send(
                    event.plain_result("发送音乐文件失败惹，请稍后再试试叭qwq")
                )

    async def delete_search_msg(
        self,
        event: AstrMessageEvent,
        msg_id: Any,
    ) -> None:
        """尝试通过底层协议端接口撤回指定的搜索结果消息。

        Args:
            event: 消息事件对象。
            msg_id: 待撤回的消息 ID。
        """
        if not msg_id:
            return
        _platform_name, is_qq = detect_platform(event)
        if not is_qq:
            return
        try:
            bot = getattr(event, "bot", None)
            if bot:
                logger.info(
                    f"{LOG_PREFIX}[会话] 正在撤回过期的搜索结果消息: msg_id={msg_id}"
                )
                if hasattr(bot, "delete_msg"):
                    await bot.delete_msg(message_id=msg_id)
                elif hasattr(bot, "api") and hasattr(bot.api, "call_action"):
                    await bot.api.call_action("delete_msg", message_id=msg_id)
        except Exception as e:
            logger.warning(
                f"{LOG_PREFIX}[会话] 撤回搜索结果消息失败 (msg_id={msg_id}): {e}"
            )

    async def send_search_result_msg(
        self,
        event: AstrMessageEvent,
        message: str,
        image_url: str | None = None,
    ) -> Any:
        """发送搜索结果（图片或文本），并在开启自动撤回时捕获返回的 message_id。

        Args:
            event: 消息事件对象。
            message: 文本格式的搜索结果内容。
            image_url: 可选的渲染后图片路径或 URL。

        Returns:
            协议端返回的 message_id（若不支持或未开启撤回则返回 None）。
        """
        payload = (
            [{"type": "image", "data": {"file": image_url}}]
            if image_url
            else [{"type": "text", "data": {"text": message}}]
        )
        _platform_name, is_qq = detect_platform(event)
        if is_qq and self.cfg.get_search_results_withdrawn_after_timeout() != -1:
            try:
                bot = getattr(event, "bot", None)
                if bot:
                    group_id = getattr(event.message_obj, "group_id", None)
                    ret = None
                    if group_id:
                        if hasattr(bot, "send_group_msg"):
                            ret = await bot.send_group_msg(
                                group_id=int(group_id), message=payload
                            )
                        elif hasattr(bot, "api") and hasattr(bot.api, "call_action"):
                            ret = await bot.api.call_action(
                                "send_group_msg",
                                group_id=int(group_id),
                                message=payload,
                            )
                    elif event.session_id and event.session_id.isdigit():
                        if hasattr(bot, "send_private_msg"):
                            ret = await bot.send_private_msg(
                                user_id=int(event.session_id), message=payload
                            )
                        elif hasattr(bot, "api") and hasattr(bot.api, "call_action"):
                            ret = await bot.api.call_action(
                                "send_private_msg",
                                user_id=int(event.session_id),
                                message=payload,
                            )

                    if ret is not None:
                        msg_id = None
                        if isinstance(ret, dict):
                            msg_id = ret.get("message_id")
                            if not msg_id and isinstance(ret.get("data"), dict):
                                msg_id = ret["data"].get("message_id")
                        return msg_id
            except Exception as e:
                logger.warning(
                    f"{LOG_PREFIX}[点歌] 直接调用协议接口发送搜歌结果失败，回退默认发送方式: {e}"
                )

        await event.send(
            event.image_result(image_url) if image_url else event.plain_result(message)
        )
        return None

    async def handle_search(
        self,
        event: AstrMessageEvent,
        keyword: str,
    ) -> Any:
        """执行搜歌、渲染图文列表、记录会话缓存并启动超时自动撤回任务。

        Args:
            event: 消息事件对象。
            keyword: 待搜索的歌曲关键词。

        Returns:
            若有直接返回的提示消息则返回 MessageEventResult，已异步发送完成则返回 None。
        """
        session_id = event.unified_msg_origin
        sender_id = event.get_sender_id()

        logger.info(
            f"{LOG_PREFIX}[点歌] 收到搜歌请求: keyword={keyword!r}, session={session_id}"
        )

        source = await self.session_mgr.get_session_source(session_id)
        results = await self.api_client.perform_search(keyword, source)

        if results is None:
            return event.plain_result("猫猫搜索歌曲时遇到网络小故障惹，请稍后再试qwq")

        if not results:
            return event.plain_result(f"猫猫没有找到与「{keyword}」相关的歌曲诶qwq")

        source_label = SOURCE_DISPLAY.get(source, source)
        message = f"🎵 猫猫为你找到的歌曲 ({source_label})\n"
        message += "━━━━━━━━━━━━━━\n"
        songs: list[dict[str, str]] = []

        for idx, song in enumerate(results, 1):
            meta = extract_song_meta(song, default_source=source, artist_sep=" / ")
            name = meta["title"]
            artist = meta["artist"]
            album = meta["album"]

            cover = meta["cover"]
            if cover.startswith(("http://", "https://")):
                if "picsize=" not in cover:
                    if source == "netease":
                        connector = "&" if "?" in cover else "?"
                        cover = f"{cover}{connector}picsize=320"
                    elif source == "tencent":
                        connector = "&" if "?" in cover else "?"
                        cover = f"{cover}{connector}picsize=300"
            else:
                cover = ""

            songs.append(
                {
                    "name": name,
                    "artist": artist,
                    "album": album if album != "未知专辑" else "",
                    "cover": cover,
                }
            )

            if album != "未知专辑":
                message += f"[{idx}] {name}  👤 {artist}  💿 {album}\n"
            else:
                message += f"[{idx}] {name}  👤 {artist}\n"

        message += "━━━━━━━━━━━━━━\n"
        message += '💡 小贴士：发送 "点歌 1" 就可以让猫猫放第一首歌啦喵~'

        if not self.cfg.get_render_search_result_as_image(event):
            msg_id = await self.send_search_result_msg(event, message)
        else:
            try:
                if self._search_template is None:
                    self._search_template = await asyncio.to_thread(
                        self.template_path.read_text, encoding="utf-8"
                    )
                render_songs = songs[:12]
                theme_setting = self.cfg.get_image_theme(event).strip().lower()
                if theme_setting in ("light", "亮色", "浅色"):
                    theme = "light"
                elif theme_setting in ("dark", "暗色", "深色"):
                    theme = "dark"
                else:
                    now_time = datetime.now().time()
                    theme = (
                        "light" if dtime(5, 59) <= now_time <= dtime(18, 59) else "dark"
                    )

                resolved_pairs = await asyncio.gather(
                    *(
                        self.api_client.resolve_cover_and_accent(
                            s["cover"],
                            theme,
                        )
                        for s in render_songs
                    )
                )
                for song_item, (resolved_cover, accent) in zip(
                    render_songs, resolved_pairs, strict=True
                ):
                    song_item["cover"] = resolved_cover
                    song_item["accent"] = accent
                has_any_album = any(bool(s.get("album")) for s in render_songs)

                image_url = await asyncio.wait_for(
                    self.html_render_fn(
                        self._search_template,
                        {
                            "songs": render_songs,
                            "source": source_label,
                            "has_any_album": has_any_album,
                            "theme": theme,
                        },
                        options={
                            "type": "png",
                            "scale": "device",
                            "device_scale_factor_level": "ultra",
                        },
                    ),
                    timeout=15,
                )
                msg_id = await self.send_search_result_msg(event, message, image_url)
                results = results[: len(render_songs)]
            except Exception as e:
                logger.warning(
                    f"{LOG_PREFIX}[点歌] 搜索结果图片渲染失败，已自动回退为文字列表: {e}"
                )
                msg_id = await self.send_search_result_msg(event, message)

        search_ts = await self.session_mgr.set_session_results(
            session_id, results, sender_id, msg_id
        )

        withdrawn_timeout = self.cfg.get_search_results_withdrawn_after_timeout()
        if withdrawn_timeout >= 0:
            expiration_time = self.cfg.get_search_result_expiration_time()
            delay = (
                min(withdrawn_timeout, expiration_time)
                if withdrawn_timeout > 0
                else expiration_time
            )
            self.session_mgr.track_task(
                self.session_mgr.clear_search_results_delayed(
                    session_id=session_id,
                    sender_id=sender_id,
                    delay=delay,
                    delete_callback=lambda mid: self.delete_search_msg(event, mid),
                    msg_id=msg_id,
                    search_timestamp=search_ts,
                )
            )
        return None
