"""render_media_cards 工具（docs/design/agent-generative-ui.md）：声明契约与参数校验。

工具不产出展示数据，测试只关心两件事：暴露给模型的声明是否稳定（名字带版本、
schema 能过 jsonschema 校验），以及跨字段约束的错误文案是否指向正确的字段。
"""

from __future__ import annotations

import asyncio

import pytest

from movieclaw_agent.tools.media_ui import (
    COMPONENT_LABELS,
    MAX_ITEMS,
    TOOL_NAME,
    make_media_ui_tool,
    validate_items,
)
from movieclaw_llm import ToolCall, validate_tool_call


def _run(args: dict) -> str:
    return asyncio.run(make_media_ui_tool().handler(args))


def test_tool_name_is_versioned() -> None:
    """工具名是前端渲染器的匹配键：必须带版本后缀，契约变更靠换版本号而非改字段。"""
    tool = make_media_ui_tool()
    assert tool.name == TOOL_NAME
    assert TOOL_NAME.endswith("_v1")


def test_description_teaches_every_component_and_id_source() -> None:
    """描述要告诉模型每个组件传什么编号、编号从哪来（title_ref 换算）。"""
    description = make_media_ui_tool().definition.description
    for component in COMPONENT_LABELS:
        assert f"- {component}：" in description
    for keyword in ("library_id", "tmdb_id", "media_type", "douban_id", "media_item_id"):
        assert keyword in description
    assert "tmdb:movie:123" in description and "douban:456" in description
    assert "不要再" in description  # 渲染后不复述卡片信息


def test_schema_accepts_valid_calls_via_runner_validation() -> None:
    """参数 schema 经 runner 同款的 jsonschema 校验路径能放行三种组件的合法调用。"""
    definition = make_media_ui_tool().definition
    for arguments in (
        {"component": "library", "items": [{"library_id": 1}]},
        {"component": "title", "items": [{"tmdb_id": 693134, "media_type": "movie"}]},
        {"component": "title", "items": [{"douban_id": "35267208"}], "title": "推荐"},
        {"component": "library_item", "items": [{"media_item_id": 7, "season": 1, "episode": 3}]},
    ):
        _, err = validate_tool_call(
            [definition], ToolCall(id="c1", name=TOOL_NAME, arguments=arguments)
        )
        assert err is None, err


def test_schema_rejects_unknown_component_and_empty_items() -> None:
    definition = make_media_ui_tool().definition
    for arguments in (
        {"component": "chart", "items": [{"library_id": 1}]},
        {"component": "library", "items": []},
        {"component": "library"},
    ):
        _, err = validate_tool_call(
            [definition], ToolCall(id="c1", name=TOOL_NAME, arguments=arguments)
        )
        assert err is not None


def test_handler_returns_fixed_receipt_with_count_and_label() -> None:
    """回执固定：告诉模型画了几张什么卡，且提醒不要复述——前端不读这段文本。"""
    output = _run({"component": "title", "items": [{"tmdb_id": 1, "media_type": "tv"}] * 3})
    assert output.startswith("已在会话页展示 3 张影片海报卡片")
    assert "无需再用文字复述" in output


@pytest.mark.parametrize(
    ("component", "item", "expected"),
    [
        ("library", {}, "library_id 必须是正整数"),
        ("library", {"library_id": 0}, "library_id 必须是正整数"),
        ("library", {"library_id": True}, "library_id 必须是正整数"),
        ("title", {}, "缺少编号"),
        ("title", {"tmdb_id": 5}, "media_type 必须是 movie 或 tv"),
        ("title", {"tmdb_id": 5, "media_type": "anime"}, "media_type 必须是 movie 或 tv"),
        ("title", {"douban_id": "  "}, "douban_id 必须是非空字符串"),
        ("library_item", {}, "media_item_id 必须是正整数"),
        ("library_item", {"media_item_id": 3, "season": 1}, "同时给出或同时省略"),
        ("library_item", {"media_item_id": 3, "season": -1, "episode": 1}, "season 必须是非负整数"),
        ("library_item", {"media_item_id": 3, "season": 1, "episode": 0}, "episode 必须是正整数"),
    ],
)
def test_cross_field_validation_points_at_the_offending_field(
    component: str, item: dict, expected: str
) -> None:
    with pytest.raises(ValueError, match=expected):
        _run({"component": component, "items": [item]})


def test_item_index_appears_in_error_for_batches() -> None:
    """多项里只有一项出错时，文案要带下标，模型才知道改哪一项。"""
    items = [{"library_id": 1}, {"library_id": 2}, {"library_id": "3"}]
    with pytest.raises(ValueError, match=r"items\[2\]\.library_id"):
        validate_items("library", items)


def test_too_many_items_rejected() -> None:
    with pytest.raises(ValueError, match=f"最多绘制 {MAX_ITEMS} 张"):
        validate_items("library", [{"library_id": i + 1} for i in range(MAX_ITEMS + 1)])


def test_douban_and_tmdb_items_can_mix_in_one_call() -> None:
    validate_items(
        "title",
        [{"tmdb_id": 10, "media_type": "movie"}, {"douban_id": "1292052"}],
    )


def test_special_season_zero_allowed() -> None:
    validate_items("library_item", [{"media_item_id": 9, "season": 0, "episode": 2}])
