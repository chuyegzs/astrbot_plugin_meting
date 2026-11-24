"""会话状态、搜索结果隔离与 LLM 搜索缓存管理模块。"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from astrbot.api import logger

from ..utils.constants import LOG_PREFIX, MAX_SESSION_AGE
from .config import PluginConfig


class SessionData:
    """单个会话（群聊或私聊）的音源与搜歌缓存数据封装。"""

    def __init__(self, default_source: str) -> None:
        """初始化会话数据。

        Args:
            default_source: 该会话的初始默认音源。
        """
        self.source: str = default_source
        self.results: list[dict[str, Any]] = []
        self.timestamp: float = time.time()
        self.results_timestamp: float = 0.0
        self.user_results: dict[str, dict[str, Any]] = {}
        self.shared_msg_ids: list[Any] = []

    def update_timestamp(self) -> None:
        """刷新会话最后活跃时间戳。"""
        self.timestamp = time.time()


class SessionManager:
    """管理所有会话状态、音频并发锁、后台任务与 LLM 搜索缓存。"""

    def __init__(self, cfg: PluginConfig) -> None:
        """初始化会话管理器。

        Args:
            cfg: 插件配置管理实例。
        """
        self.cfg = cfg
        self.sessions: dict[str, SessionData] = {}
        self.sessions_lock = asyncio.Lock()
        self.session_audio_locks: dict[str, asyncio.Lock] = {}
        self.audio_locks_lock = asyncio.Lock()
        self.llm_search_cache: dict[str, dict[str, Any]] = {}
        self.bg_tasks: set[asyncio.Task[Any]] = set()

    def track_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """创建并跟踪后台异步任务，完成后自动从集合移除。

        Args:
            coro: 待执行的协程对象。

        Returns:
            创建的 asyncio.Task 实例。
        """
        task = asyncio.create_task(coro)
        self.bg_tasks.add(task)
        task.add_done_callback(self.bg_tasks.discard)
        return task

    async def get_session(self, session_id: str) -> SessionData:
        """获取或创建指定会话的状态对象。

        Args:
            session_id: 会话唯一标识（unified_msg_origin）。

        Returns:
            对应的 SessionData 实例。
        """
        async with self.sessions_lock:
            if session_id not in self.sessions:
                self.sessions[session_id] = SessionData(self.cfg.get_default_source())
            else:
                self.sessions[session_id].update_timestamp()
            return self.sessions[session_id]

    async def get_session_source(self, session_id: str) -> str:
        """获取指定会话当前生效的搜索音源。

        Args:
            session_id: 会话唯一标识。

        Returns:
            音源标识字符串。
        """
        session = await self.get_session(session_id)
        return session.source

    async def set_session_source(self, session_id: str, source: str) -> None:
        """更新指定会话的搜索音源并刷新活跃时间。

        Args:
            session_id: 会话唯一标识。
            source: 目标音源标识字符串。
        """
        async with self.sessions_lock:
            if session_id not in self.sessions:
                self.sessions[session_id] = SessionData(self.cfg.get_default_source())
            self.sessions[session_id].source = source
            self.sessions[session_id].update_timestamp()
            await self.cleanup_old_sessions_locked()

    async def get_session_audio_lock(self, session_id: str) -> asyncio.Lock:
        """获取会话级别的音频转码与分段发送互斥锁。

        Args:
            session_id: 会话唯一标识。

        Returns:
            该会话专属的 asyncio.Lock。
        """
        async with self.audio_locks_lock:
            if session_id not in self.session_audio_locks:
                self.session_audio_locks[session_id] = asyncio.Lock()
            return self.session_audio_locks[session_id]

    async def cleanup_old_sessions_locked(self) -> None:
        """清理过期会话与过期的 LLM 搜索缓存（须在持有 sessions_lock 时调用）。"""
        current_time = time.time()
        expired_sessions = [
            sid
            for sid, session in self.sessions.items()
            if current_time - session.timestamp > MAX_SESSION_AGE
        ]
        default_source = self.cfg.get_default_source()
        cleaned_count = 0
        for sid in expired_sessions:
            session = self.sessions.get(sid)
            if session is None:
                continue
            lock = self.session_audio_locks.get(sid)
            if lock is not None and not lock.locked():
                self.session_audio_locks.pop(sid, None)
            if session.source != default_source:
                if session.results or session.user_results or session.shared_msg_ids:
                    session.results = []
                    session.results_timestamp = 0.0
                    session.user_results.clear()
                    session.shared_msg_ids.clear()
                    cleaned_count += 1
            else:
                self.sessions.pop(sid, None)
                cleaned_count += 1
        if cleaned_count:
            logger.debug(f"{LOG_PREFIX}[会话] 已清理 {cleaned_count} 个过期会话缓存喵~")

        expired_llm_searches = [
            search_id
            for search_id, item in self.llm_search_cache.items()
            if current_time > item.get("deadline", 0)
        ]
        for search_id in expired_llm_searches:
            self.llm_search_cache.pop(search_id, None)

    async def set_session_results(
        self,
        session_id: str,
        results: list[dict[str, Any]],
        sender_id: str | None = None,
        msg_id: Any = None,
    ) -> float:
        """保存会话的搜索结果列表及关联消息 ID。

        Args:
            session_id: 会话唯一标识。
            results: 搜索结果列表。
            sender_id: 发起搜索的用户 ID（开启结果隔离时使用）。
            msg_id: 搜索结果消息 ID（用于后续自动撤回）。

        Returns:
            本次搜索结果记录的时间戳。
        """
        now = time.time()
        async with self.sessions_lock:
            if session_id not in self.sessions:
                self.sessions[session_id] = SessionData(self.cfg.get_default_source())

            session = self.sessions[session_id]
            restriction = self.cfg.get_search_result_restrictions()

            if restriction and sender_id:
                prev_msg_ids = list(
                    session.user_results.get(sender_id, {}).get("msg_ids", [])
                )
                if msg_id is not None and msg_id not in prev_msg_ids:
                    prev_msg_ids.append(msg_id)
                session.user_results[sender_id] = {
                    "results": results,
                    "timestamp": now,
                    "msg_ids": prev_msg_ids,
                }
                session.update_timestamp()
            else:
                session.results = results
                session.results_timestamp = now
                session.update_timestamp()
                if msg_id is not None and msg_id not in session.shared_msg_ids:
                    session.shared_msg_ids.append(msg_id)

            await self.cleanup_old_sessions_locked()
        return now

    async def get_session_results(
        self,
        session_id: str,
        sender_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """获取会话当前未过期的搜索结果列表。

        Args:
            session_id: 会话唯一标识。
            sender_id: 点歌用户 ID（开启结果隔离时用于匹配对应用户的搜歌记录）。

        Returns:
            未过期的歌曲字典列表；若无结果或已过期则返回空列表。
        """
        expiration_time = self.cfg.get_search_result_expiration_time()
        restriction = self.cfg.get_search_result_restrictions()

        async with self.sessions_lock:
            if session_id not in self.sessions:
                return []

            session = self.sessions[session_id]
            current_time = time.time()

            if restriction and sender_id and sender_id in session.user_results:
                user_data = session.user_results[sender_id]
                timestamp = user_data["timestamp"]

                if current_time - timestamp > expiration_time:
                    if not user_data.get("msg_ids"):
                        del session.user_results[sender_id]
                    else:
                        user_data["results"] = []
                    logger.debug(
                        f"{LOG_PREFIX}[会话] 用户 {sender_id} 在会话 {session_id} 的搜歌结果已过期"
                    )
                    return []
                return user_data["results"]

            if restriction:
                return []

            if (
                not session.results_timestamp
                or current_time - session.results_timestamp > expiration_time
            ):
                session.results = []
                logger.debug(
                    f"{LOG_PREFIX}[会话] 会话 {session_id} 的共享搜歌结果已过期"
                )
                return []

            return session.results

    async def pop_msgs_for_immediate_withdraw(
        self,
        session_id: str,
        sender_id: str,
    ) -> list[Any]:
        """在点歌成功后立即清除对应搜索缓存并取出待撤回的消息 ID 列表。

        Args:
            session_id: 会话唯一标识。
            sender_id: 点歌用户 ID。

        Returns:
            需要立即撤回的消息 ID 列表。
        """
        msgs_to_delete: list[Any] = []
        async with self.sessions_lock:
            if session_id in self.sessions:
                sess = self.sessions[session_id]
                if self.cfg.get_search_result_restrictions():
                    if sender_id in sess.user_results:
                        msgs_to_delete = list(
                            sess.user_results[sender_id].get("msg_ids", [])
                        )
                        del sess.user_results[sender_id]
                else:
                    msgs_to_delete = list(sess.shared_msg_ids)
                    sess.results = []
                    sess.results_timestamp = 0.0
                    sess.shared_msg_ids.clear()
        return msgs_to_delete

    async def clear_search_results_delayed(
        self,
        session_id: str,
        sender_id: str,
        delay: int,
        delete_callback: Callable[[Any], Awaitable[None]] | None = None,
        msg_id: Any = None,
        search_timestamp: float = 0.0,
    ) -> None:
        """延迟指定秒数后清理搜索结果缓存并触发消息撤回回调。

        Args:
            session_id: 会话唯一标识。
            sender_id: 搜索发起者 ID。
            delay: 延迟等待秒数。
            delete_callback: 撤回单条消息 ID 的异步回调函数。
            msg_id: 本次搜索产生的消息 ID。
            search_timestamp: 本次搜索记录的时间戳，防止误删后续新搜索的结果。
        """
        logger.debug(
            f"{LOG_PREFIX}[会话] 已安排会话 {session_id} (用户 {sender_id}) 的搜歌结果在 {delay}s 后自动清理"
        )
        await asyncio.sleep(delay)

        should_delete_msg = False
        async with self.sessions_lock:
            if session_id not in self.sessions:
                return
            sess = self.sessions[session_id]

            if self.cfg.get_search_result_restrictions():
                if sender_id in sess.user_results:
                    user_data = sess.user_results[sender_id]
                    msg_ids = user_data.get("msg_ids", [])
                    if msg_id is not None and msg_id in msg_ids:
                        msg_ids.remove(msg_id)
                        should_delete_msg = True
                    if (
                        search_timestamp
                        and user_data.get("timestamp") == search_timestamp
                    ) or (
                        not search_timestamp
                        and time.time() - user_data.get("timestamp", 0) >= delay - 0.5
                    ):
                        if not msg_ids:
                            del sess.user_results[sender_id]
                        else:
                            user_data["results"] = []
                        logger.debug(
                            f"{LOG_PREFIX}[会话] 用户 {sender_id} 在会话 {session_id} 的搜歌结果已超时清理"
                        )
            else:
                if msg_id is not None and msg_id in sess.shared_msg_ids:
                    sess.shared_msg_ids.remove(msg_id)
                    should_delete_msg = True
                if (
                    search_timestamp and sess.results_timestamp == search_timestamp
                ) or (
                    not search_timestamp
                    and sess.results_timestamp
                    and time.time() - sess.results_timestamp >= delay - 0.5
                ):
                    sess.results = []
                    sess.results_timestamp = 0.0
                    logger.debug(
                        f"{LOG_PREFIX}[会话] 会话 {session_id} 的共享搜歌结果已超时清理"
                    )

        if should_delete_msg and msg_id and delete_callback is not None:
            await delete_callback(msg_id)

    async def save_llm_search(
        self,
        search_id: str,
        songs: dict[int, dict[str, Any]],
        keyword: str,
        ttl: int = 60,
    ) -> tuple[int, int]:
        """缓存 LLM 搜索到的歌曲映射并启动定时过期清理。

        Args:
            search_id: 随机生成的搜索会话 ID。
            songs: 序号到歌曲字典的映射。
            keyword: 搜索关键词。
            ttl: 缓存有效秒数，默认 60 秒。

        Returns:
            二元组 (当前时间戳秒, 过期截止时间戳秒)。
        """
        now_ts = int(time.time())
        deadline_ts = now_ts + ttl
        async with self.sessions_lock:
            self.llm_search_cache[search_id] = {
                "songs": songs,
                "timestamp": now_ts,
                "deadline": deadline_ts,
                "keyword": keyword,
            }
        return now_ts, deadline_ts

    async def get_llm_search(self, search_id: str) -> dict[str, Any] | None:
        """读取未过期的 LLM 搜索缓存条目。

        Args:
            search_id: 搜索会话 ID。

        Returns:
            缓存条目字典；若不存在或已过有效期则返回 None。
        """
        async with self.sessions_lock:
            cache_entry = self.llm_search_cache.get(search_id)
            if cache_entry is not None and time.time() > cache_entry.get("deadline", 0):
                self.llm_search_cache.pop(search_id, None)
                return None
            return cache_entry

    async def clear_all(self) -> None:
        """取消所有后台任务并清空内存中的会话与缓存状态。"""
        for task in list(self.bg_tasks):
            task.cancel()
        if self.bg_tasks:
            await asyncio.gather(*self.bg_tasks, return_exceptions=True)
            self.bg_tasks.clear()

        self.sessions.clear()
        self.session_audio_locks.clear()
        self.llm_search_cache.clear()
