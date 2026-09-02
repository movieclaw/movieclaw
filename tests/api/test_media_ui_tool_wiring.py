"""show_media_cards 工具的装配开关（docs/design/agent-generative-ui.md §2）。

卡片只有网页会话能画：开关默认关、网页会话显式开、IM/微信通道的受限工具集
永远不含它——三条各一个守护测试，新接通道忘了关也会被拦下。
"""

from __future__ import annotations

import inspect

from movieclaw_agent.tools.media_ui import TOOL_NAME
from movieclaw_api.api.routes import agent as agent_routes
from movieclaw_api.services import im_channel, weixin_channel


def _names(tools) -> set[str]:
    return {tool.name for tool in tools}


def test_generative_ui_is_off_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(tmp_path / "ws"))
    agent_routes.get_settings.cache_clear()
    try:
        assert TOOL_NAME not in _names(agent_routes.get_agent_tools({}))
        assert TOOL_NAME in _names(agent_routes.get_agent_tools({}, generative_ui=True))
    finally:
        agent_routes.get_settings.cache_clear()


def test_web_session_paths_opt_in_explicitly() -> None:
    """网页会话的两条装配路径（发消息 / 改写重问）都必须显式打开开关。"""
    source = inspect.getsource(agent_routes)
    calls = [line for line in source.splitlines() if "get_agent_tools(await _cli_env" in line]
    assert calls, "找不到网页会话的工具装配调用"
    assert all("generative_ui=True" in line for line in calls), calls


def test_im_and_weixin_channels_never_carry_the_tool() -> None:
    """IM/微信通道无法渲染卡片：受限工具集里不允许出现绘制工具（连 import 都不该有）。"""
    for module in (im_channel, weixin_channel):
        source = inspect.getsource(module)
        assert "make_media_ui_tool" not in source, module.__name__
        assert "generative_ui=True" not in source, module.__name__
