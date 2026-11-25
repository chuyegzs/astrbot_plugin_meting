"""Pytest bootstrap for astrbot_plugin_meting unit tests.

Provides lightweight ``astrbot`` module stubs so running pytest from either the
plugin directory or a workspace venv stays fast and never triggers AstrBot's
import-time ``data/`` directory creation side effects.
"""

import logging
import sys
import types
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

if "astrbot" not in sys.modules:
    _STUB_MODULES: dict[str, dict[str, Any]] = {
        "astrbot": {},
        "astrbot.api": {"logger": logging.getLogger("astrbot_plugin_meting.test")},
        "astrbot.api.event": {"AstrMessageEvent": type("AstrMessageEvent", (), {})},
        "astrbot.api.message_components": {
            "Record": type("Record", (), {}),
            "File": type("File", (), {}),
            "Json": type("Json", (), {}),
        },
        "astrbot.core": {},
        "astrbot.core.config": {},
        "astrbot.core.config.default": {"VERSION": "4.29.4"},
        "astrbot.core.utils": {},
        "astrbot.core.utils.metrics": {
            "Metric": type(
                "Metric",
                (),
                {"get_installation_id": staticmethod(lambda: "test-install-id")},
            )
        },
    }
    for _mod_name, _attrs in _STUB_MODULES.items():
        _mod = types.ModuleType(_mod_name)
        _mod.__dict__.update(_attrs)
        sys.modules.setdefault(_mod_name, _mod)
