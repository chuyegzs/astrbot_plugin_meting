"""Core regression unit tests for astrbot_plugin_meting."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from src.core import audio as audio_mod
from src.core.config import PluginConfig
from src.core.llm_handler import LLMToolHandler
from src.core.session import SessionManager
from src.utils.constants import PL_VERSION
from src.utils.helpers import (
    detect_platform,
    extract_song_meta,
    force_https,
    guess_file_extension,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_metadata_and_conf_schema_valid() -> None:
    """Verify schema JSON parses cleanly and PL_VERSION is non-empty."""
    schema_path = PLUGIN_ROOT / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert "bot_name" in schema
    assert schema["bot_name"]["default"] == "猫猫"
    assert PL_VERSION and not PL_VERSION.startswith("v")


def test_force_https_preserves_private_hosts() -> None:
    """Private/local hosts keep http while public URLs upgrade to https."""
    assert force_https("http://127.0.0.1:3000/api") == "http://127.0.0.1:3000/api"
    assert force_https("http://localhost:8080/meting") == "http://localhost:8080/meting"
    assert force_https("http://192.168.1.10/api") == "http://192.168.1.10/api"
    assert force_https("http://100.64.0.5/api") == "http://100.64.0.5/api"
    assert (
        force_https("http://music.example.com/song.mp3")
        == "https://music.example.com/song.mp3"
    )
    assert force_https("//cdn.example.com/a.jpg") == "https://cdn.example.com/a.jpg"
    assert force_https("   ") == ""


def test_detect_platform_qq_and_fallback() -> None:
    """QQ card platforms match while non-card platforms and umo fallback behave safely."""
    qq_event = SimpleNamespace(
        unified_msg_origin="default:GroupMessage:123",
        get_platform_name=lambda: "aiocqhttp",
    )
    assert detect_platform(qq_event) == ("aiocqhttp", True)

    official_event = SimpleNamespace(
        unified_msg_origin="qq:GroupMessage:123",
        get_platform_name=lambda: "qqofficial",
    )
    assert detect_platform(official_event) == ("qqofficial", False)

    fallback_event = SimpleNamespace(unified_msg_origin="napcat:GroupMessage:123")
    assert detect_platform(fallback_event) == ("napcat", True)


def test_extract_song_meta_and_extension_guessing() -> None:
    """Song metadata normalizes artists, bitrate, IDs, and MIME/filename extensions."""
    meta = extract_song_meta(
        {
            "name": "坠落星空",
            "artist": ["小星星Aurora"],
            "album": "画一个星星一个你",
            "url": "https://api.example.com/?server=netease&type=url&id=1351520305",
        },
        default_source="netease",
        br=999,
        artist_sep=" / ",
    )
    assert meta["artist"] == "小星星Aurora"
    assert meta["song_id"] == "1351520305"
    assert meta["song_url"].endswith("&br=999")

    assert guess_file_extension("https://cdn.example.com/test.flac", {}) == ".flac"
    assert (
        guess_file_extension(
            "https://cdn.example.com/download",
            {"Content-Disposition": 'attachment; filename="demo.m4a"'},
        )
        == ".m4a"
    )
    assert (
        guess_file_extension(
            "https://cdn.example.com/download", {"Content-Type": "audio/mpeg"}
        )
        == ".mp3"
    )


def test_plugin_config_bot_name_and_validation() -> None:
    """Bot name trims whitespace and falls back cleanly; invalid config uses defaults."""
    assert PluginConfig().get_bot_name() == "猫猫"
    assert PluginConfig({"bot_name": ""}).get_bot_name() == "猫猫"
    assert PluginConfig({"bot_name": "   "}).get_bot_name() == "猫猫"
    assert PluginConfig({"bot_name": 123}).get_bot_name() == "猫猫"
    assert PluginConfig({"bot_name": " 小助手 "}).get_bot_name() == "小助手"

    bad_cfg = PluginConfig(
        {
            "api_config": {"api_url": "custom", "custom_api_url": "  "},
            "download_config": {"daily_cleanup_time": "25:99", "max_file_size": 999},
        }
    )
    assert bad_cfg.get_api_url().endswith("/")
    assert bad_cfg.get_daily_cleanup_time() == (4, 30)
    assert bad_cfg.get_max_file_size() == 80 * 1024 * 1024


def test_session_cleanup_preserves_custom_source_and_locked_lock() -> None:
    """Expired session cleanup keeps custom sources and never pops locked audio locks."""

    async def _run() -> None:
        cfg = PluginConfig({"search_config": {"default_source": "netease"}})
        mgr = SessionManager(cfg)
        await mgr.set_session_source("s1", "tencent")
        lock = await mgr.get_session_audio_lock("s1")
        mgr.sessions["s1"].timestamp = 0.0

        async with lock:
            async with mgr.sessions_lock:
                await mgr.cleanup_old_sessions_locked()

        assert mgr.sessions["s1"].source == "tencent"
        assert mgr.session_audio_locks["s1"] is lock

    asyncio.run(_run())


def test_session_result_isolation() -> None:
    """User-isolated search results do not overwrite or leak across users."""

    async def _run() -> None:
        cfg = PluginConfig({"search_config": {"search_result_restrictions": True}})
        mgr = SessionManager(cfg)
        await mgr.set_session_results("room1", [{"name": "Song A"}], sender_id="userA")
        await mgr.set_session_results("room1", [{"name": "Song B"}], sender_id="userB")

        assert (await mgr.get_session_results("room1", "userA")) == [{"name": "Song A"}]
        assert (await mgr.get_session_results("room1", "userB")) == [{"name": "Song B"}]
        assert (await mgr.get_session_results("room1", "userC")) == []

    asyncio.run(_run())


def test_llm_search_filters_urls_and_rejects_float_index() -> None:
    """LLM search omits raw URLs and system info, and rejects float/invalid indices."""

    async def _run() -> None:
        cfg = PluginConfig()
        session_mgr = SessionManager(cfg)
        api_client = SimpleNamespace(
            perform_search=AsyncMock(
                side_effect=[
                    [
                        {
                            "name": "坠落星空",
                            "artist": "小星星Aurora",
                            "album": "画一个星星一个你",
                            "url": "https://secret.example.com/a.mp3",
                            "pic": "https://secret.example.com/a.jpg",
                            "lrc": "https://secret.example.com/a.lrc",
                        }
                    ],
                    [],
                ]
            )
        )
        sender = SimpleNamespace(
            send_music_card=AsyncMock(),
            send_as_file=AsyncMock(),
        )
        handler = LLMToolHandler(
            cfg, cast(Any, api_client), session_mgr, cast(Any, sender)
        )
        event = cast(
            Any,
            SimpleNamespace(
                unified_msg_origin="napcat:GroupMessage:1",
                get_platform_name=lambda: "napcat",
            ),
        )

        search_raw = await handler.execute(event, keyword="坠落星空", source="auto")
        payload = json.loads(search_raw)[0]
        assert "system" not in payload["env"]
        song_entry = payload["sources"]["netease"]["data"][0]
        for hidden_key in ("url", "pic", "cover", "lrc"):
            assert hidden_key not in song_entry

        search_id = payload["search_id"]["id"]
        play_source = json.dumps([{"search_id": search_id, "send_mode": "card"}])

        float_resp = await handler.execute(event, source=play_source, index=0.0)
        assert "无效" in float_resp
        sender.send_music_card.assert_not_awaited()

        ok_resp = await handler.execute(event, source=play_source, index=0)
        assert "点歌成功" in ok_resp
        sender.send_music_card.assert_awaited_once()

    asyncio.run(_run())


def test_audio_ffmpeg_resolve_recovers_after_stale_path_and_ref_count(
    tmp_path: Path,
) -> None:
    """Stale FFmpeg path recovers via binaries scan; ref counting tracks active files."""
    fake_pkg = tmp_path / "fake_imageio_ffmpeg"
    bin_dir = fake_pkg / "binaries"
    bin_dir.mkdir(parents=True)
    exe_name = "ffmpeg-win-x86_64-v8.0.exe" if os.name == "nt" else "ffmpeg-linux-v8.0"
    fake_exe = bin_dir / exe_name
    fake_exe.write_bytes(b"fake-binary")
    if os.name != "nt":
        fake_exe.chmod(0o755)
    fake_init = fake_pkg / "__init__.py"
    fake_init.write_text("", encoding="utf-8")

    fake_ffmpeg_mod = SimpleNamespace(
        __file__=str(fake_init),
        get_ffmpeg_exe=lambda: str(bin_dir / "ffmpeg-old-deleted.exe"),
    )

    with (
        patch.object(audio_mod, "ffmpeg", fake_ffmpeg_mod),
        patch.dict(os.environ, {"IMAGEIO_FFMPEG_EXE": str(tmp_path / "missing.exe")}),
    ):
        svc = audio_mod.AudioService(PluginConfig(), cast(Any, None), cast(Any, None))
        assert svc.ffmpeg_path == str(fake_exe)

        # Simulate cached path becoming stale later and auto-recovering.
        svc.ffmpeg_path = str(bin_dir / "ffmpeg-gone.exe")
        assert svc._resolve_ffmpeg_path() == str(fake_exe)

        sample = tmp_path / "song.mp3"
        svc.acquire_file(sample)
        svc.acquire_file(sample)
        assert svc.is_file_in_use(sample)
        assert svc.get_file_ref_count(sample) == 2
        svc.release_file(sample)
        assert svc.get_file_ref_count(sample) == 1
        svc.release_file(sample)
        assert not svc.is_file_in_use(sample)
