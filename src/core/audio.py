"""音频下载、本地缓存管理与 FFmpeg 语音转码切片模块。"""

import asyncio
import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp
import imageio_ffmpeg as ffmpeg

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Record

from ..utils.constants import (
    CACHE_DIR_NAME,
    CHUNK_SIZE,
    DOWNLOAD_TIMEOUT,
    FFMPEG_CONVERT_TIMEOUT,
    FFMPEG_INFO_TIMEOUT,
    LOG_PREFIX,
    TEMP_DIR_NAME,
    TEMP_FILE_PREFIX,
    AudioFormatError,
    DownloadError,
)
from ..utils.helpers import force_https, guess_file_extension, is_audio_content
from .api_client import MetingApiClient
from .config import PluginConfig
from .session import SessionManager


class AudioService:
    """负责歌曲音频下载、磁盘缓存控制、FFmpeg 探测与语音分段发送。"""

    def __init__(
        self,
        cfg: PluginConfig,
        api_client: MetingApiClient,
        session_mgr: SessionManager,
    ) -> None:
        """初始化音频服务。

        Args:
            cfg: 插件配置管理实例。
            api_client: MetingAPI 客户端实例。
            session_mgr: 会话与后台任务管理实例。
        """
        self.cfg = cfg
        self.api_client = api_client
        self.session_mgr = session_mgr
        try:
            self.ffmpeg_path: str = ffmpeg.get_ffmpeg_exe()
        except Exception as e:
            self.ffmpeg_path = shutil.which("ffmpeg") or ""
            if not self.ffmpeg_path:
                logger.warning(
                    f"{LOG_PREFIX}[音频] 未找到可用的 FFmpeg 可执行文件，语音切片功能将不可用: {e}"
                )
        self.download_semaphore = asyncio.Semaphore(3)
        self.temp_dir = Path(tempfile.gettempdir()) / TEMP_DIR_NAME
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.temp_dir / CACHE_DIR_NAME
        self._file_lock = threading.Lock()
        self._active_files: dict[str, int] = {}

    def _norm_path_key(self, filepath: str | Path) -> str:
        """生成跨平台规范化的文件路径锁键名。

        Args:
            filepath: 目标文件路径。

        Returns:
            规范化后的绝对路径字符串。
        """
        return os.path.normcase(os.path.abspath(str(filepath)))

    def acquire_file(self, filepath: str | Path) -> None:
        """为正在使用的音频文件增加引用计数锁，防止被清理任务误删。

        Args:
            filepath: 要锁定的文件路径。
        """
        key = self._norm_path_key(filepath)
        with self._file_lock:
            self._active_files[key] = self._active_files.get(key, 0) + 1

    def release_file(self, filepath: str | Path) -> None:
        """释放音频文件的引用计数锁。

        Args:
            filepath: 要释放的文件路径。
        """
        key = self._norm_path_key(filepath)
        with self._file_lock:
            count = self._active_files.get(key, 0) - 1
            if count <= 0:
                self._active_files.pop(key, None)
            else:
                self._active_files[key] = count

    def is_file_in_use(self, filepath: str | Path) -> bool:
        """检查指定文件当前是否正被下载或转码任务占用。

        Args:
            filepath: 待检查的文件路径。

        Returns:
            若引用计数大于 0 则返回 True，否则返回 False。
        """
        key = self._norm_path_key(filepath)
        with self._file_lock:
            return self._active_files.get(key, 0) > 0

    def get_file_ref_count(self, filepath: str | Path) -> int:
        """获取指定文件当前的引用计数。

        Args:
            filepath: 待检查的文件路径。

        Returns:
            当前引用计数整数。
        """
        key = self._norm_path_key(filepath)
        with self._file_lock:
            return self._active_files.get(key, 0)

    def cleanup_temp_files(self) -> None:
        """清理插件临时目录中超过 5 分钟且未被占用的临时音频文件。"""
        try:
            if not self.temp_dir.exists():
                return
            count = 0
            now = time.time()
            for filepath in self.temp_dir.iterdir():
                if filepath.name.startswith(TEMP_FILE_PREFIX) and filepath.is_file():
                    if self.is_file_in_use(filepath):
                        continue
                    try:
                        if now - filepath.stat().st_mtime > 300:
                            filepath.unlink(missing_ok=True)
                            count += 1
                    except Exception:
                        pass
            if count > 0:
                logger.debug(f"{LOG_PREFIX}[音频] 已清理 {count} 个残留临时文件喵~")
        except Exception as e:
            logger.error(f"{LOG_PREFIX}[音频] 清理临时文件时发生异常: {e}")

    def clear_all_cache(self) -> None:
        """清理缓存目录中所有未被占用的音乐缓存文件。"""
        try:
            if not self.cache_dir.exists():
                return
            count = 0
            for filepath in self.cache_dir.iterdir():
                if filepath.is_file():
                    if self.is_file_in_use(filepath):
                        continue
                    try:
                        filepath.unlink(missing_ok=True)
                        count += 1
                    except Exception:
                        pass
            if count > 0:
                logger.info(f"{LOG_PREFIX}[音频] 已清理 {count} 个音乐缓存文件喵~")
        except Exception as e:
            logger.error(f"{LOG_PREFIX}[音频] 清理音乐缓存文件时发生异常: {e}")

    async def enforce_cache_size(self) -> None:
        """检查本地缓存目录总大小，若超过配置上限则跳过占用中文件、按时间从旧到新清理至 50% 容量。"""
        try:
            max_bytes = self.cfg.get_max_cache_size_bytes()
            max_gb = max_bytes / (1024 * 1024 * 1024)

            def _prune() -> None:
                if not self.cache_dir.exists():
                    return
                total_size = 0
                files: list[tuple[Path, float, int]] = []
                for fp in self.cache_dir.iterdir():
                    if fp.is_file():
                        try:
                            stat = fp.stat()
                            total_size += stat.st_size
                            files.append((fp, stat.st_mtime, stat.st_size))
                        except OSError:
                            pass

                if total_size > max_bytes:
                    logger.info(
                        f"{LOG_PREFIX}[音频] 缓存占用 ({total_size / 1024 / 1024:.2f}MB) 超出上限 ({max_gb:.1f}GB)，开始瘦身清理..."
                    )
                    target_size = max_bytes * 0.5
                    files.sort(key=lambda item: item[1])
                    for fp, _mtime, size in files:
                        if self.is_file_in_use(fp):
                            logger.debug(
                                f"{LOG_PREFIX}[音频] 缓存文件正在使用中，跳过清理: {fp.name}"
                            )
                            continue
                        try:
                            fp.unlink(missing_ok=True)
                            total_size -= size
                            if total_size <= target_size:
                                break
                        except Exception as del_err:
                            logger.warning(
                                f"{LOG_PREFIX}[音频] 删除旧缓存文件失败 ({fp}): {del_err}"
                            )
                    logger.info(
                        f"{LOG_PREFIX}[音频] 缓存瘦身完成，当前剩余占用: {total_size / 1024 / 1024:.2f}MB"
                    )

            await asyncio.to_thread(_prune)
        except Exception as e:
            logger.error(f"{LOG_PREFIX}[音频] 执行缓存容量限制时出错: {e}")

    async def _run_ffmpeg(
        self,
        process: asyncio.subprocess.Process,
        timeout: int,
    ) -> bytes:
        """等待 FFmpeg 子进程执行完毕并实施超时保护。

        Args:
            process: 正在运行的 FFmpeg 子进程。
            timeout: 最大等待超时秒数。

        Returns:
            FFmpeg 标准错误输出字节流。

        Raises:
            asyncio.TimeoutError: 子进程在指定时间内未完成时抛出。
        """
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            return stderr
        except asyncio.TimeoutError:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            raise

    async def get_audio_info(self, file_path: str) -> float | None:
        """使用 FFmpeg 探测音频文件总时长。

        Args:
            file_path: 本地音频文件路径。

        Returns:
            音频时长（秒）；若非有效音频或探测超时则返回 None。
        """
        if not self.ffmpeg_path:
            return None
        process = await asyncio.create_subprocess_exec(
            self.ffmpeg_path,
            "-i",
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stderr = await self._run_ffmpeg(process, FFMPEG_INFO_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"{LOG_PREFIX}[音频] FFmpeg 探测音频时长超时: {file_path}")
            return None

        output = stderr.decode("utf-8", errors="ignore")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", output)
        if match:
            hours, minutes, seconds = map(float, match.groups())
            return hours * 3600 + minutes * 60 + seconds
        return None

    async def download_song(
        self,
        url: str,
        sender_id: str,
        source: str = "",
        song_id: str = "",
        br: int | None = None,
    ) -> tuple[str, float]:
        """下载歌曲音频文件、校验音频有效性，并按需写入本地缓存。

        Args:
            url: 歌曲音频下载 URL。
            sender_id: 点歌用户 ID。
            source: 歌曲来源平台。
            song_id: 歌曲唯一 ID。
            br: 目标音质码率（如 320、999、-1），为 None 时自动从 URL 或配置读取。

        Returns:
            二元组 (音频本地文件路径, 音频时长秒数)。

        Raises:
            DownloadError: 网络请求或文件写入失败时抛出。
            AudioFormatError: 响应类型或文件非有效音频时抛出。
        """
        if not self.ffmpeg_path:
            raise DownloadError("未找到可用的 FFmpeg 组件，无法处理语音切片")

        url = force_https(url)
        http_session = self.api_client.ensure_session()

        if br is None:
            try:
                br_vals = parse_qs(urlparse(url).query).get("br")
                br = int(br_vals[0]) if br_vals else self.cfg.get_send_music_info()
            except (ValueError, IndexError):
                br = self.cfg.get_send_music_info()

        safe_sender_id = "".join(c for c in sender_id if c.isalnum() or c in "._-")
        cache_enabled = self.cfg.get_enable_cache()

        url_hash = ""
        if cache_enabled:
            cache_key = f"{source}_{song_id}" if source and song_id else url
            url_hash = hashlib.md5(cache_key.encode()).hexdigest()
            target_rank = float("inf") if br == -1 else br

            def _find_cached_file() -> tuple[str, float] | None:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                for filepath in self.cache_dir.iterdir():
                    if filepath.name.startswith(f"{url_hash}_") and filepath.is_file():
                        try:
                            parts = filepath.stem.split("_")
                            if len(parts) >= 3:
                                cached_br = int(parts[1])
                                duration = float(parts[2])
                            else:
                                cached_br = 320
                                duration = float(parts[1])
                            cached_rank = float("inf") if cached_br == -1 else cached_br
                            if cached_rank >= target_rank:
                                self.acquire_file(filepath)
                                if not filepath.exists():
                                    self.release_file(filepath)
                                    continue
                                logger.info(
                                    f"{LOG_PREFIX}[音频] 命中本地缓存 (缓存音质={cached_br}, 需求音质={br}): {filepath}"
                                )
                                return str(filepath), duration
                            logger.info(
                                f"{LOG_PREFIX}[音频] 本地缓存音质 ({cached_br}) 低于目标音质 ({br})，准备重新下载覆盖: {filepath}"
                            )
                        except Exception as cache_err:
                            logger.warning(
                                f"{LOG_PREFIX}[音频] 解析缓存文件名失败 ({filepath.name}): {cache_err}"
                            )
                return None

            cached_hit = await asyncio.to_thread(_find_cached_file)
            if cached_hit is not None:
                return cached_hit

        download_success = False
        max_retries = 3
        retry_count = 0
        temp_file: str | None = None

        while retry_count < max_retries:
            try:
                async with self.download_semaphore:
                    logger.debug(
                        f"{LOG_PREFIX}[音频] 开始下载音频 (第 {retry_count + 1}/{max_retries} 次): {url}"
                    )

                    async with http_session.get(
                        url, allow_redirects=True, timeout=DOWNLOAD_TIMEOUT
                    ) as resp:
                        if resp.status != 200:
                            if resp.status >= 500:
                                raise aiohttp.ClientError(
                                    f"上游服务器错误，状态码: {resp.status}"
                                )
                            raise DownloadError(f"下载失败，状态码: {resp.status}")

                        content_type = resp.headers.get("Content-Type", "")
                        if not is_audio_content(content_type):
                            raise AudioFormatError(
                                f"不支持的 Content-Type: {content_type}"
                            )

                        file_ext = guess_file_extension(url, resp.headers)
                        max_file_size_bytes = self.cfg.get_max_file_size()
                        max_file_size_mb = max_file_size_bytes // (1024 * 1024)
                        total_size = 0
                        temp_path = self.temp_dir / (
                            f"{TEMP_FILE_PREFIX}{safe_sender_id}_{uuid.uuid4()}{file_ext}"
                        )
                        temp_file = str(temp_path)
                        self.acquire_file(temp_file)

                        await asyncio.to_thread(
                            self.temp_dir.mkdir, parents=True, exist_ok=True
                        )
                        f = await asyncio.to_thread(temp_path.open, "wb")
                        try:
                            async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                                await asyncio.to_thread(f.write, chunk)
                                total_size += len(chunk)
                                if total_size > max_file_size_bytes:
                                    raise DownloadError(
                                        f"音频文件过大，已超过 {max_file_size_mb} MB 限制"
                                    )
                        except aiohttp.ClientPayloadError as e:
                            logger.warning(
                                f"{LOG_PREFIX}[音频] 下载过程中连接意外断开: {e}"
                            )
                            raise
                        finally:
                            await asyncio.to_thread(f.close)

                        file_size_bytes = (
                            await asyncio.to_thread(temp_path.stat)
                        ).st_size
                        if file_size_bytes == 0:
                            raise DownloadError("下载的音频文件为空")

                        file_size_mb = file_size_bytes / (1024 * 1024)
                        logger.info(
                            f"{LOG_PREFIX}[音频] 歌曲下载完成: 大小={file_size_mb:.2f}MB, 路径={temp_file}"
                        )

                        duration = await self.get_audio_info(temp_file)
                        if duration is None or duration <= 0:
                            raise AudioFormatError("下载的文件不是有效音频")

                        logger.debug(
                            f"{LOG_PREFIX}[音频] 音频校验通过: 时长={duration:.2f}s"
                        )

                        if cache_enabled:
                            try:
                                cached_filename = (
                                    f"{url_hash}_{br}_{duration:.2f}{file_ext}"
                                )
                                cached_path = self.cache_dir / cached_filename

                                def _save_to_cache(
                                    src_path: str,
                                    dst_path: Path,
                                    prefix: str,
                                ) -> str:
                                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                                    for item in self.cache_dir.iterdir():
                                        if (
                                            item.name.startswith(prefix)
                                            and item != dst_path
                                            and item.is_file()
                                            and not self.is_file_in_use(item)
                                        ):
                                            try:
                                                item.unlink(missing_ok=True)
                                            except Exception:
                                                pass
                                    if (
                                        self.is_file_in_use(dst_path)
                                        and dst_path.exists()
                                    ):
                                        self.acquire_file(dst_path)
                                        self.release_file(src_path)
                                        try:
                                            Path(src_path).unlink(missing_ok=True)
                                        except Exception:
                                            pass
                                        return str(dst_path)
                                    self.acquire_file(dst_path)
                                    try:
                                        shutil.move(src_path, str(dst_path))
                                    except Exception:
                                        self.release_file(dst_path)
                                        raise
                                    self.release_file(src_path)
                                    return str(dst_path)

                                temp_file = await asyncio.to_thread(
                                    _save_to_cache,
                                    temp_file,
                                    cached_path,
                                    f"{url_hash}_",
                                )
                                logger.debug(
                                    f"{LOG_PREFIX}[音频] 已写入本地缓存: {temp_file}"
                                )
                                self.session_mgr.track_task(self.enforce_cache_size())
                            except Exception as e:
                                logger.warning(
                                    f"{LOG_PREFIX}[音频] 写入本地缓存失败: {e}"
                                )

                        download_success = True
                        return temp_file, duration

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                retry_count += 1
                err_msg = "请求超时" if isinstance(e, asyncio.TimeoutError) else str(e)
                logger.error(
                    f"{LOG_PREFIX}[音频] 下载网络异常 (第 {retry_count}/{max_retries} 次): {err_msg}"
                )
                if retry_count >= max_retries:
                    raise DownloadError(f"网络请求错误: {err_msg}") from e
                await asyncio.sleep(1)
            except (DownloadError, AudioFormatError, asyncio.CancelledError):
                raise
            except Exception as e:
                logger.error(
                    f"{LOG_PREFIX}[音频] 下载歌曲发生未知异常: {e}",
                    exc_info=True,
                )
                raise DownloadError(f"下载失败: {e}") from e
            finally:
                if not download_success and temp_file:
                    self.release_file(temp_file)
                    try:
                        await asyncio.to_thread(Path(temp_file).unlink, missing_ok=True)
                    except Exception:
                        pass
                    temp_file = None

        raise DownloadError("下载失败：已超过最大重试次数")

    async def split_and_send_audio(
        self,
        event: AstrMessageEvent,
        temp_file: str,
        session_id: str,
        duration: float,
    ) -> None:
        """将音频转码为 24kHz 单声道 WAV 并按配置时长自动切片发送语音消息。

        Args:
            event: 消息事件对象。
            temp_file: 已验证的本地音频文件路径。
            session_id: 当前会话唯一标识。
            duration: 音频总时长（秒）。
        """
        temp_files_to_cleanup: list[str] = []
        acquired_files: list[str] = []
        if Path(temp_file).parent != self.cache_dir:
            temp_files_to_cleanup.append(temp_file)

        try:
            if not self.ffmpeg_path:
                logger.error(f"{LOG_PREFIX}[音频] 未找到可用的 FFmpeg 可执行文件")
                await event.send(
                    event.plain_result(
                        "音频处理组件依赖加载失败惹qwq，请检查插件依赖喵！"
                    )
                )
                return

            audio_lock = await self.session_mgr.get_session_audio_lock(session_id)
            async with audio_lock:
                try:
                    logger.debug(
                        f"{LOG_PREFIX}[音频] 开始转码处理音频: path={temp_file}, duration={duration:.2f}s"
                    )

                    base_name = Path(temp_file).stem
                    if not base_name.startswith(TEMP_FILE_PREFIX):
                        base_name = f"{TEMP_FILE_PREFIX}{base_name}"
                    base_name = f"{base_name}_{uuid.uuid4().hex[:8]}"

                    processed_path = self.temp_dir / f"{base_name}_processed.wav"
                    processed_file = str(processed_path)
                    self.acquire_file(processed_file)
                    acquired_files.append(processed_file)
                    temp_files_to_cleanup.append(processed_file)

                    process = await asyncio.create_subprocess_exec(
                        self.ffmpeg_path,
                        "-i",
                        temp_file,
                        "-y",
                        "-ar",
                        "24000",
                        "-ac",
                        "1",
                        processed_file,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        await self._run_ffmpeg(process, FFMPEG_CONVERT_TIMEOUT)
                    except asyncio.TimeoutError:
                        logger.error(
                            f"{LOG_PREFIX}[音频] 音频转码超时，正在清理源文件: {temp_file}"
                        )
                        if temp_file and self.get_file_ref_count(temp_file) <= 1:
                            try:
                                await asyncio.to_thread(
                                    Path(temp_file).unlink, missing_ok=True
                                )
                            except Exception:
                                pass
                        await event.send(
                            event.plain_result(
                                f"音频转换超时惹，{self.cfg.get_bot_name()}唱不出来啦qwq"
                            )
                        )
                        return

                    processed_exists = await asyncio.to_thread(processed_path.exists)
                    if process.returncode != 0 or not processed_exists:
                        logger.error(
                            f"{LOG_PREFIX}[音频] 音频转码失败 (code={process.returncode})，正在清理损坏文件: {temp_file}"
                        )
                        if temp_file and self.get_file_ref_count(temp_file) <= 1:
                            try:
                                await asyncio.to_thread(
                                    Path(temp_file).unlink, missing_ok=True
                                )
                            except Exception:
                                pass
                        await event.send(
                            event.plain_result(
                                f"音频转换失败惹，{self.cfg.get_bot_name()}不会唱这首歌qwq"
                            )
                        )
                        return

                    segment_duration = self.cfg.get_segment_duration()
                    send_interval = self.cfg.get_send_interval()

                    # 允许末尾剩余 <= 7s 直接合并到当前段，避免产生过短的碎片语音
                    tolerance = 7.0 if segment_duration <= 293 else 0.0
                    if duration <= segment_duration + tolerance:
                        logger.debug(
                            f"{LOG_PREFIX}[音频] 时长未超分段阈值，直接整首发送语音"
                        )
                        await event.send(
                            event.chain_result([Record.fromFileSystem(processed_file)])
                        )
                        await event.send(event.plain_result("歌曲播放完啦喵~ 🎵"))
                        return

                    success_count = 0
                    start_time = 0.0
                    while start_time < duration:
                        remaining = duration - start_time
                        if remaining <= segment_duration + tolerance:
                            current_duration = remaining
                            is_final_segment = True
                        else:
                            current_duration = float(segment_duration)
                            is_final_segment = False

                        segment_path = (
                            self.temp_dir / f"{base_name}_segment_{int(start_time)}.wav"
                        )
                        segment_file = str(segment_path)
                        self.acquire_file(segment_file)
                        acquired_files.append(segment_file)
                        temp_files_to_cleanup.append(segment_file)

                        process = await asyncio.create_subprocess_exec(
                            self.ffmpeg_path,
                            "-ss",
                            str(start_time),
                            "-t",
                            str(current_duration),
                            "-i",
                            processed_file,
                            "-y",
                            "-c",
                            "copy",
                            segment_file,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        try:
                            await self._run_ffmpeg(process, FFMPEG_CONVERT_TIMEOUT)
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"{LOG_PREFIX}[音频] 切片提取超时: start={start_time}s, duration={current_duration}s"
                            )
                            if is_final_segment:
                                break
                            start_time += segment_duration
                            continue

                        if not await asyncio.to_thread(segment_path.exists):
                            if is_final_segment:
                                break
                            start_time += segment_duration
                            continue

                        try:
                            await event.send(
                                event.chain_result(
                                    [Record.fromFileSystem(segment_file)]
                                )
                            )
                            await asyncio.sleep(send_interval)
                            success_count += 1
                        except Exception as e:
                            logger.error(f"{LOG_PREFIX}[音频] 发送语音切片失败: {e}")
                            await event.send(
                                event.plain_result("发送语音片段时出现了一点小故障qwq")
                            )

                        try:
                            await asyncio.to_thread(
                                segment_path.unlink, missing_ok=True
                            )
                            temp_files_to_cleanup.remove(segment_file)
                        except Exception:
                            pass
                        finally:
                            self.release_file(segment_file)
                            if segment_file in acquired_files:
                                acquired_files.remove(segment_file)

                        if is_final_segment:
                            break
                        start_time += segment_duration

                    if success_count > 0:
                        await event.send(event.plain_result("歌曲播放完啦喵~ 🎵"))

                except asyncio.CancelledError:
                    logger.info(f"{LOG_PREFIX}[音频] 语音切片发送任务已取消")
                    raise
                except Exception as e:
                    logger.error(
                        f"{LOG_PREFIX}[音频] 分割音频时发生异常: {e}",
                        exc_info=True,
                    )
                    await event.send(
                        event.plain_result("音频处理出错惹，请稍后再试试叭qwq")
                    )
        finally:
            for f in temp_files_to_cleanup:
                try:
                    p = Path(f)
                    if await asyncio.to_thread(p.exists):
                        await asyncio.to_thread(p.unlink, missing_ok=True)
                except Exception:
                    pass
            for f in acquired_files:
                self.release_file(f)
