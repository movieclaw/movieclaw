"""validate_tool_call：校验通过 / 各类失败均返回可回喂模型的中文描述。"""

from movieclaw_llm import ToolCall, ToolDefinition, validate_tool_call

SEARCH_TOOL = ToolDefinition(
    name="search",
    description="搜索种子",
    parameters={
        "type": "object",
        "properties": {"q": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["q"],
    },
)


def test_valid_arguments_pass():
    args, err = validate_tool_call(
        [SEARCH_TOOL], ToolCall(id="1", name="search", arguments={"q": "沙丘", "limit": 5})
    )
    assert err is None
    assert args == {"q": "沙丘", "limit": 5}


def test_unknown_tool_reports_available_names():
    args, err = validate_tool_call([SEARCH_TOOL], ToolCall(id="1", name="downloadd"))
    assert args is None
    assert "downloadd" in err and "search" in err


def test_schema_violation_reports_field_path():
    args, err = validate_tool_call(
        [SEARCH_TOOL], ToolCall(id="1", name="search", arguments={"q": "x", "limit": "五"})
    )
    assert args is None
    assert "limit" in err


def test_parse_error_propagates():
    tc = ToolCall(id="1", name="search", raw_arguments="{broken", parse_error="不是合法 JSON")
    args, err = validate_tool_call([SEARCH_TOOL], tc)
    assert args is None
    assert "解析失败" in err


def test_harmony_residue_in_tool_name_reports_backend_hint(caplog):
    """工具名混入 Harmony 标记 → 错误信息引导用户排查推理后端，并打告警日志。"""
    tc = ToolCall(id="1", name="search<|channel|>commentary")
    with caplog.at_level("WARNING"):
        args, err = validate_tool_call([SEARCH_TOOL], tc)
    assert args is None
    assert "Harmony" in err and "推理后端" in err and "search" in err
    assert any("Harmony" in r.message for r in caplog.records)


def test_plain_unknown_tool_name_has_no_harmony_hint():
    """普通的幻觉工具名不应误报 Harmony 残留。"""
    _, err = validate_tool_call([SEARCH_TOOL], ToolCall(id="1", name="downloadd"))
    assert "Harmony" not in err
