"""show_media_cards 工具（docs/design/agent-generative-ui.md）：声明契约与参数校验。

工具不产出展示数据，测试只关心两件事：暴露给模型的声明是否稳定（名字带版本、
字段名与 mclaw 输出对齐、schema 能过 jsonschema 校验），以及跨字段约束的错误
文案是否指向正确的字段。
"""

from __future__ import annotations

import asyncio

import pytest

from movieclaw_agent.tools.media_ui import (
    COMPONENTS,
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
    """描述要说清：什么时候主动画、每个组件传什么编号、编号从哪条 mclaw 命令来。"""
    description = make_media_ui_tool().definition.description
    for component in COMPONENTS:
        assert f"- {component}：" in description
    # 字段名与 mclaw 输出一致，模型原样搬即可
    for keyword in (
        "library_id",
        "title_ref",
        "tmdb_id",
        "media_type",
        "media_item_id",
        "season_number",
        "episode_number",
        "subscription_id",
    ):
        assert keyword in description
    assert "tmdb:movie:123" in description and "douban:789" in description
    # 使用时机围绕用户问题：涉及影片/媒体库/订阅就主动画，用户喜欢
    assert "主动" in description and "订阅" in description and "用户非常喜欢" in description
    # 画完不复述、编号不能编
    assert "不要再用文字复述" in description and "不要凭记忆" in description


def test_schema_accepts_valid_calls_via_runner_validation() -> None:
    """参数 schema 经 runner 同款的 jsonschema 校验路径能放行四种组件的合法调用。"""
    definition = make_media_ui_tool().definition
    for arguments in (
        {"component": "library", "items": [{"library_id": 1}]},
        {"component": "title", "items": [{"title_ref": "tmdb:movie:693134"}]},
        {"component": "title", "items": [{"tmdb_id": 1399, "media_type": "tv"}], "title": "推荐"},
        {
            "component": "library_item",
            "items": [{"media_item_id": 7, "season_number": 1, "episode_number": 3}],
        },
        {"component": "subscription", "items": [{"subscription_id": 12}]},
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


def test_handler_returns_plain_ok() -> None:
    """回执只表示成功：展示由前端完成，回执不该占上下文。"""
    assert _run({"component": "title", "items": [{"title_ref": "douban:1292052"}] * 3}) == "ok"


@pytest.mark.parametrize(
    ("component", "item", "expected"),
    [
        ("library", {}, "library_id 必须是正整数"),
        ("library", {"library_id": 0}, "library_id 必须是正整数"),
        ("library", {"library_id": True}, "library_id 必须是正整数"),
        ("title", {}, "缺少编号"),
        ("title", {"title_ref": "693134"}, "title_ref 格式无效"),
        ("title", {"title_ref": "tmdb:anime:5"}, "title_ref 格式无效"),
        ("title", {"tmdb_id": 5}, "media_type 必须是 movie 或 tv"),
        ("title", {"tmdb_id": 5, "media_type": "anime"}, "media_type 必须是 movie 或 tv"),
        ("library_item", {}, "media_item_id 必须是正整数"),
        ("library_item", {"media_item_id": 3, "season_number": 1}, "同时给出或同时省略"),
        (
            "library_item",
            {"media_item_id": 3, "season_number": -1, "episode_number": 1},
            "season_number 必须是非负整数",
        ),
        (
            "library_item",
            {"media_item_id": 3, "season_number": 1, "episode_number": 0},
            "episode_number 必须是正整数",
        ),
        ("subscription", {}, "subscription_id 必须是正整数"),
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


def test_title_ref_and_tmdb_items_can_mix_in_one_call() -> None:
    validate_items(
        "title",
        [
            {"title_ref": "tmdb:movie:10"},
            {"title_ref": " douban:1292052 "},
            {"tmdb_id": 11, "media_type": "tv"},
        ],
    )


def test_special_season_zero_allowed() -> None:
    validate_items("library_item", [{"media_item_id": 9, "season_number": 0, "episode_number": 2}])
