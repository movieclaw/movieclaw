"""预设注册表：yaml 加载与关键字段。"""

import pytest

from movieclaw_llm.exceptions import LlmRoutingError
from movieclaw_llm.providers import get_preset, list_presets


def test_builtin_presets_loaded():
    ids = {p.id for p in list_presets()}
    assert {"openai", "bailian", "deepseek", "kimi", "glm", "openai_compat"} <= ids


def test_official_channel_presets_have_fixed_endpoints():
    """官方直连渠道：端点固定（设置页据此隐藏端点输入框）、思考走 reasoning_content。"""
    expected = {
        "deepseek": "https://api.deepseek.com",
        "kimi": "https://api.moonshot.cn/v1",
        "glm": "https://open.bigmodel.cn/api/paas/v4",
    }
    for preset_id, base_url in expected.items():
        preset = get_preset(preset_id)
        assert preset.base_url == base_url
        assert preset.compat.thinking_field == "reasoning_content"
        assert not preset.requires_base_url
        assert preset.models, f"官方渠道「{preset_id}」的模型目录不应为空"


def test_bailian_preset_dialect():
    preset = get_preset("bailian")
    assert preset.protocol == "openai_chat"
    assert preset.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert preset.compat.thinking_field == "reasoning_content"
    flagship = next(m for m in preset.models if m.id == "qwen3.8-max")
    assert flagship.supports_thinking
    assert flagship.max_thinking_tokens == 262144
    assert flagship.max_output_tokens == 131072
    # 3.8 代起旗舰原生多模态；上代 qwen3.7-max 仍在售（多处测试以它为默认模型）
    assert flagship.modalities == ["text", "image", "video"]
    assert any(m.id == "qwen3.7-max" for m in preset.models)


def test_preset_model_ids_have_no_slash():
    """模型 id 含「/」会与路由 ref 的「实例名/模型id」语法冲突：resolve 会把
    id 前半段当实例名解析失败（如百炼的 ZHIPU/GLM-5.3，见 bailian.yaml 注释），
    这类模型不允许进预设目录。"""
    for preset in list_presets():
        for model in preset.models:
            assert "/" not in model.id, f"{preset.id} 目录里的 {model.id} 含保留字符「/」"


def test_unknown_preset_raises_chinese_error():
    with pytest.raises(LlmRoutingError, match="未知的供应商类型"):
        get_preset("nonexistent")
