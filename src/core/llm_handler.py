"""LLM 函数调用工具（astr_meting_music）的核心业务逻辑模块。"""

import asyncio
import json
import re
import secrets
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.config.default import VERSION

from ..utils.constants import LOG_PREFIX, PL_VERSION, SOURCE_DISPLAY
from ..utils.helpers import detect_platform, extract_song_meta
from .api_client import MetingApiClient
from .config import PluginConfig
from .sender import MusicSender
from .session import SessionManager


class LLMToolHandler:
    """处理 AI 大模型发起的音乐搜索与点歌请求，具备参数容错与纠偏能力。"""

    def __init__(
        self,
        cfg: PluginConfig,
        api_client: MetingApiClient,
        session_mgr: SessionManager,
        sender: MusicSender,
    ) -> None:
        """初始化 LLM 工具处理器。

        Args:
            cfg: 插件配置管理实例。
            api_client: MetingAPI 客户端实例。
            session_mgr: 会话与搜索缓存管理实例。
            sender: 音乐卡片与文件发送编排实例。
        """
        self.cfg = cfg
        self.api_client = api_client
        self.session_mgr = session_mgr
        self.sender = sender

    async def execute(
        self,
        event: AstrMessageEvent,
        keyword: str = "",
        source: str = "auto",
        index: int | float | str = -1,
    ) -> str:
        """执行 LLM 搜歌或点歌逻辑。

        Args:
            event: 消息事件对象。
            keyword: 搜索关键词（搜歌时必填，点歌时可留空）。
            source: 搜歌时填音源名（如 'auto'、'netease'、'tencent'）；点歌时填含 search_id 和 send_mode 的 JSON。
            index: 点歌时的目标歌曲非负整数序号（从 0 开始）；搜歌时保持 -1。

        Returns:
            返回给大模型的结构化 JSON 字符串或执行结果提示文本。
        """
        try:
            raw_source = source.strip() if isinstance(source, str) else ""
            if raw_source.startswith("```"):
                raw_source = re.sub(
                    r"^```(?:json)?\s*|\s*```$", "", raw_source, flags=re.IGNORECASE
                ).strip()

            is_json_source = False
            parsed_source_json: Any = None
            if isinstance(source, (dict, list)):
                is_json_source = True
                parsed_source_json = source
            elif raw_source.startswith(("{", "[")):
                is_json_source = True
                try:
                    parsed_source_json = json.loads(raw_source)
                except Exception as parse_err:
                    logger.warning(
                        f"{LOG_PREFIX}[LLM] source 参数包含非法 JSON: {source!r}, 错误: {parse_err}"
                    )
                    return (
                        "执行失败：source 参数中的 JSON 格式无效！若要搜索歌曲，source 请直接填写 "
                        "'auto'、'netease'、'tencent'、'kugou' 或 'kuwo'（禁止填 JSON）；"
                        '若要点歌，请提供合法的 JSON，例如：[{"search_id": "...", "send_mode": "card"}]。'
                    )

            # 分支一：搜索音乐
            if not is_json_source:
                kw = keyword.strip() if isinstance(keyword, str) else ""
                if not kw:
                    logger.warning(f"{LOG_PREFIX}[LLM] 搜索请求缺少 keyword 参数")
                    return "搜索失败：请提供要搜索的歌曲关键词 keyword。"

                src_clean = raw_source.lower() if raw_source else "auto"
                if src_clean != "auto" and src_clean not in SOURCE_DISPLAY:
                    logger.warning(f"{LOG_PREFIX}[LLM] 不支持的搜索音源: {source!r}")
                    return f"不支持的搜索音源：{source}。"

                target_sources = (
                    ["netease", "tencent"] if src_clean == "auto" else [src_clean]
                )
                search_tasks = [
                    self.api_client.perform_search(kw, src_item)
                    for src_item in target_sources
                ]
                raw_results_list = await asyncio.gather(*search_tasks)

                idx_counter = 0
                cached_songs: dict[int, dict[str, Any]] = {}
                sources_payload: dict[str, dict[str, Any]] = {}

                for src_item, res_list in zip(
                    target_sources, raw_results_list, strict=True
                ):
                    if res_list is None:
                        sources_payload[src_item] = {"status": -1, "data": []}
                        continue

                    formatted_items: list[dict[str, Any]] = []
                    for r in res_list:
                        if not isinstance(r, dict):
                            continue
                        song_copy = dict(r)
                        song_copy["source"] = r.get("source") or src_item
                        cached_songs[idx_counter] = song_copy

                        meta = extract_song_meta(
                            r, default_source=src_item, artist_sep="/"
                        )
                        entry: dict[str, Any] = {
                            "index": str(idx_counter),
                            "name": meta["title"],
                            "artist": meta["artist"],
                            "album": meta["album"],
                            "source": song_copy["source"],
                        }
                        for k, v in r.items():
                            if (
                                k
                                not in (
                                    "index",
                                    "name",
                                    "title",
                                    "artist",
                                    "author",
                                    "album",
                                    "url",
                                    "pic",
                                    "cover",
                                    "lrc",
                                    "source",
                                )
                                and v is not None
                            ):
                                entry[k] = v

                        formatted_items.append(entry)
                        idx_counter += 1

                    sources_payload[src_item] = {
                        "status": 0,
                        "data": formatted_items,
                    }

                if not cached_songs:
                    return f"未搜索到与“{kw}”相关的歌曲。"

                search_id = secrets.token_hex(9)
                now_ts, deadline_ts = await self.session_mgr.save_llm_search(
                    search_id=search_id,
                    songs=cached_songs,
                    keyword=kw,
                    ttl=60,
                )

                msg_platform, _is_qq = detect_platform(event)
                response_payload = [
                    {
                        "env": {
                            "astrbot_version": VERSION,
                            "meting_plugin_version": PL_VERSION,
                            "msg_platform": msg_platform,
                        },
                        "search_id": {
                            "id": search_id,
                            "time": now_ts,
                            "deadline": deadline_ts,
                        },
                        "sources": sources_payload,
                    }
                ]
                logger.info(
                    f"{LOG_PREFIX}[LLM] 搜索完成: keyword={kw!r}, source={src_clean}, search_id={search_id}, 结果数={len(cached_songs)}"
                )
                return json.dumps(response_payload, ensure_ascii=False)

            # 分支二：根据 search_id 发送/播放音乐
            play_conf: dict[str, Any] | None = None
            if isinstance(parsed_source_json, list) and parsed_source_json:
                if isinstance(parsed_source_json[0], dict):
                    play_conf = parsed_source_json[0]
            elif isinstance(parsed_source_json, dict):
                play_conf = parsed_source_json

            if not play_conf or "search_id" not in play_conf:
                logger.warning(
                    f"{LOG_PREFIX}[LLM] 模型在搜索阶段误将 source 传为 JSON: {source!r}"
                )
                return (
                    "执行失败：搜索歌曲时禁止将 source 写为 JSON！请将 source 设为 "
                    "'auto'、'netease'、'tencent'、'kugou' 或 'kuwo' 先进行搜索；"
                    "只有拿到搜索返回的 search_id 后，点歌时才在 source 中传入包含 search_id 和 send_mode 的 JSON。"
                )

            raw_search_id = play_conf.get("search_id")
            if isinstance(raw_search_id, dict):
                raw_search_id = raw_search_id.get("id", "")
            search_id = str(raw_search_id or "").strip()
            if not search_id or search_id.lower() in ("null", "none"):
                logger.warning(
                    f"{LOG_PREFIX}[LLM] 模型传入了空的 search_id: {source!r}"
                )
                return (
                    "执行失败：source JSON 中缺少有效的 search_id！如果你正在搜索歌曲，"
                    "请不要将 source 写为 JSON（应填 'auto'）；如果你正在点歌，请填入第一步搜索返回的 search_id。"
                )

            cache_entry = await self.session_mgr.get_llm_search(search_id)
            if not cache_entry:
                logger.warning(
                    f"{LOG_PREFIX}[LLM] search_id={search_id!r} 不存在或已过期"
                )
                return (
                    f"执行失败：未找到 search_id '{search_id}' 对应的搜索缓存"
                    "（可能已超过 60 秒有效期，或你在未搜索的情况下直接把 source 写成了 JSON）。"
                    "请先将 source 设为 'auto' 搜索歌曲获取最新的 search_id。"
                )

            send_mode = str(play_conf.get("send_mode", "")).strip().lower()
            if send_mode not in ("card", "file"):
                logger.warning(
                    f"{LOG_PREFIX}[LLM] 无效的 send_mode={send_mode!r} (search_id={search_id})"
                )
                return f"点歌失败：send_mode '{send_mode}' 无效，仅允许 'card'（音乐卡片，优先推荐）或 'file'（音乐文件）。"

            songs_map: dict[int, dict[str, Any]] = cache_entry["songs"]
            max_idx = len(songs_map) - 1
            parsed_index: int | None = None

            if isinstance(index, (bool, float)):
                parsed_index = None
            elif isinstance(index, int):
                parsed_index = index
            elif isinstance(index, str):
                s_idx = index.strip()
                if s_idx.isdecimal():
                    parsed_index = int(s_idx)

            if (
                parsed_index is None
                or parsed_index < 0
                or parsed_index not in songs_map
            ):
                logger.warning(
                    f"{LOG_PREFIX}[LLM] 模型传入了越界或无效序号 index={index!r}（有效范围 0~{max_idx}）"
                )
                return (
                    f"点歌失败：序号 index '{index}' 无效！"
                    f"请传入 0~{max_idx} 之间的非负整数（严禁传入负数、浮点数或超出范围的序号），"
                    f"并使用同一个 search_id '{search_id}' 重新选择调用。"
                )

            target_song = songs_map[parsed_index]
            br = self.cfg.get_send_music_info()
            song_meta = extract_song_meta(
                target_song, default_source="netease", br=br, artist_sep="/"
            )
            if not song_meta["raw_url"]:
                logger.warning(
                    f"{LOG_PREFIX}[LLM] 目标歌曲缺少播放地址: search_id={search_id}, index={parsed_index}"
                )
                return "点歌失败：无法获取该歌曲的播放地址，请尝试选择其它序号的歌曲。"

            title = song_meta["title"]
            artist = song_meta["artist"]

            if send_mode == "card":
                try:
                    await self.sender.send_music_card(event, song_meta)
                    logger.info(
                        f"{LOG_PREFIX}[LLM] 音乐卡片发送成功: [{parsed_index}] 《{title}》- {artist} (search_id={search_id})"
                    )
                    return (
                        f"点歌成功！已通过音乐卡片(card)向用户发送歌曲："
                        f"《{title}》- {artist}。"
                    )
                except Exception as card_err:
                    logger.warning(
                        f"{LOG_PREFIX}[LLM] 音乐卡片发送失败 (search_id={search_id}, index={parsed_index}): {card_err}"
                    )
                    return (
                        f"音乐卡片(card)发送失败或当前平台不支持（原因：{card_err}）。"
                        f"尚未向用户发送任何错误提示，请你使用同一个 search_id '{search_id}' 并将 send_mode 改为 'file' 重新调用本工具发送音乐文件。"
                    )

            try:
                await self.sender.send_as_file(event, song_meta)
                logger.info(
                    f"{LOG_PREFIX}[LLM] 音乐文件发送成功: [{parsed_index}] 《{title}》- {artist} (search_id={search_id})"
                )
                return (
                    f"点歌成功！已通过音乐文件(file)向用户发送歌曲："
                    f"《{title}》- {artist}。"
                )
            except Exception as file_err:
                logger.warning(
                    f"{LOG_PREFIX}[LLM] 音乐文件发送失败 (search_id={search_id}, index={parsed_index}): {file_err}"
                )
                return f"音乐文件(file)发送失败（原因：{file_err}），尚未向用户发送消息提醒，请向用户礼貌说明情况。"

        except Exception as e:
            logger.error(
                f"{LOG_PREFIX}[LLM] 音乐搜索/播放工具执行异常: {e}",
                exc_info=True,
            )
            return f"执行失败：{e}"
