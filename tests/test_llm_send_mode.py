"""Run with an AstrBot environment: python -m unittest discover -s tests -v."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
import wave

from main import MetingPlugin, Record, ffmpeg


class Event:
    def __init__(self, platform="aiocqhttp"):
        self.unified_msg_origin = f"{platform}:FriendMessage:test"
        self.messages = []
        self.records = []

    def get_sender_id(self):
        return "test"

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain

    async def send(self, result):
        self.messages.append(result)
        if isinstance(result, list):
            for component in result:
                if isinstance(component, Record):
                    path = component.file.removeprefix("file:///")
                    # Record files must exist and be valid while being sent.
                    with wave.open(path, "rb") as audio:
                        assert audio.getframerate() == 24000
                        assert audio.getnchannels() == 1
                        self.records.append((path, audio.getnframes() / 24000))


class SendModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = object.__new__(MetingPlugin)
        self.plugin.config = {"music_send_config": {"send_as_music": 0},
                              "audio_send_config": {"segment_duration": 30, "send_interval": 0}}
        self.plugin._send_overrides = {}
        self.plugin._ensure_initialized = AsyncMock()
        self.plugin._perform_search = AsyncMock(return_value=[{
            "name": "test", "artist": "test", "url": "https://example.com/test.wav", "id": "1"}])
        self.plugin._download_song = AsyncMock(return_value=("unused.wav", 1))
        self.plugin._split_and_send_audio = AsyncMock()
        self.plugin._send_as_file = AsyncMock()

    async def test_voice_override_is_per_call(self):
        event = Event()
        before = json.dumps(self.plugin.config)
        await self.plugin.astr_meting_music(event, "test", index=0, send_mode="voice")
        self.plugin._split_and_send_audio.assert_awaited_once()
        self.assertEqual(json.dumps(self.plugin.config), before)
        self.assertEqual(self.plugin._send_overrides, {})
        self.plugin.config["music_send_config"]["send_as_music"] = 2
        await self.plugin.astr_meting_music(event, "test", index=0)
        self.plugin._send_as_file.assert_awaited_once()

    async def test_global_and_session_voice(self):
        for global_mode, session_mode in [(1, None), (0, 1)]:
            with self.subTest(global_mode=global_mode, session_mode=session_mode):
                self.setUp()
                event = Event()
                self.plugin.config["music_send_config"]["send_as_music"] = global_mode
                if session_mode is not None:
                    self.plugin._send_overrides[event.unified_msg_origin] = session_mode
                await self.plugin.astr_meting_music(event, "test", index=0)
                self.plugin._split_and_send_audio.assert_awaited_once()

    async def test_file_and_non_qq_fallback(self):
        for platform, mode in [("aiocqhttp", "file"), ("webchat", "default"), ("webchat", "card")]:
            with self.subTest(platform=platform, mode=mode):
                self.setUp()
                await self.plugin.astr_meting_music(Event(platform), "test", index=0, send_mode=mode)
                self.plugin._send_as_file.assert_awaited_once()

    async def test_qq_card_and_failed_card_voice_fallback(self):
        from main import Json
        for success in [True, False]:
            with self.subTest(success=success):
                self.setUp()
                self.plugin.config["music_send_config"]["send_if_not_supported"] = 1
                response = AsyncMock()
                response.status = 200
                response.json.return_value = ({"code": 1, "data": {"config": {"token": "test"}}}
                                              if success else {"code": 0, "message": "test failure"})
                from unittest.mock import MagicMock
                self.plugin._http_session = MagicMock()
                self.plugin._http_session.get.return_value.__aenter__ = AsyncMock(return_value=response)
                event = Event()
                await self.plugin.astr_meting_music(event, "test", index=0, send_mode="card")
                if success:
                    self.assertTrue(any(isinstance(msg, list) and isinstance(msg[0], Json)
                                        for msg in event.messages))
                    self.plugin._download_song.assert_not_awaited()
                else:
                    self.plugin._split_and_send_audio.assert_awaited_once()

    def test_tool_schema_exposes_send_mode(self):
        from astrbot.core.provider.register import llm_tools
        tool = next(t for t in llm_tools.func_list if t.name == "astr_meting_music")
        self.assertEqual(tool.parameters["properties"]["send_mode"]["type"], "string")

    async def test_search_and_invalid_inputs_do_not_play(self):
        event = Event()
        result = await self.plugin.astr_meting_music(event, "test", send_mode="voice")
        self.assertEqual(json.loads(result)[0]["name"], "test")
        for invalid in ["audio", None, [], 1]:
            result = await self.plugin.astr_meting_music(event, "test", index=0, send_mode=invalid)
            self.assertIn("无效的发送格式", result)
        result = await self.plugin.astr_meting_music(event, "test", index=100, send_mode="voice")
        self.assertIn("超出搜索结果范围", result)
        self.plugin._download_song.assert_not_awaited()
        self.assertEqual(event.messages, [])

    async def test_real_ffmpeg_voice_and_cleanup(self):
        self.plugin._split_and_send_audio = MetingPlugin._split_and_send_audio.__get__(self.plugin)
        self.plugin._ffmpeg_path = ffmpeg.get_ffmpeg_exe()
        self.plugin._audio_locks_lock = asyncio.Lock()
        self.plugin._session_audio_locks = {}
        for duration, expected in [(1, 1), (39, 2)]:
            with self.subTest(duration=duration):
                handle, path = tempfile.mkstemp(prefix="astrbot_meting_plugin_test_", suffix=".wav")
                os.close(handle)
                with wave.open(path, "wb") as audio:
                    audio.setnchannels(2)
                    audio.setsampwidth(2)
                    audio.setframerate(8000)
                    audio.writeframes(b"\0" * (duration * 8000 * 4))
                self.plugin._download_song.return_value = (path, duration)
                event = Event()
                result = await self.plugin.astr_meting_music(event, "test", index=0, send_mode="voice")
                self.assertEqual(result, "点歌任务执行成功！")
                self.assertEqual(len(event.records), expected)
                self.assertAlmostEqual(sum(d for _, d in event.records), duration, places=1)
                self.assertFalse(Path(path).exists())
                self.assertTrue(all(not Path(p).exists() for p, _ in event.records))


if __name__ == "__main__":
    unittest.main()
