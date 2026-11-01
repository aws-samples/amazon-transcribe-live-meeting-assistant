# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""An MCP client gets each key-frame picture in a summary as its alt text."""

import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

HERE = Path(__file__).resolve().parent


def _load():
    # Loaded from its file under its own name: test_jsonrpc replaces the
    # ``tools`` package in sys.modules, so an ordinary import could get that.
    helper = types.ModuleType("tools.url_helper")
    helper.get_meeting_url = lambda meeting_id: f"https://example.com/#/calls/{meeting_id}"
    sys.modules.setdefault("tools", types.ModuleType("tools"))
    sys.modules["tools.url_helper"] = helper
    spec = importlib.util.spec_from_file_location(
        "mcp_get_summary_under_test", HERE / "tools" / "get_summary.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


get_summary = _load()


def test_key_frame_pictures_become_their_alt_text():
    text = '{"SLIDES PRESENTED": "**[00:30] Budget**\\n\\n![Slide at 00:30](keyframe:0000030000-ab12cd)"}'
    assert get_summary.keyframe_images_as_text(text) == (
        '{"SLIDES PRESENTED": "**[00:30] Budget**\\n\\n(Slide at 00:30)"}'
    )


def test_other_images_and_links_are_left_alone():
    text = "![chart](https://example.com/c.png) [keyframe:x](keyframe:x)"
    assert get_summary.keyframe_images_as_text(text) == text
    assert get_summary.keyframe_images_as_text(None) is None
