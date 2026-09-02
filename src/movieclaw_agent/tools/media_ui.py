"""render_media_cards：Agent 的「生成式 UI」工具——在会话页内联绘制影音卡片。

设计（docs/design/agent-generative-ui.md，思路对齐 AG-UI 协议的 render-only
frontend tool）：

- 工具本身**不产出任何展示数据**：handler 只校验参数形状，返回一句固定的中文
  回执喂给模型。卡片的封面、海报、评分、订阅/入库状态与播放入口全部由前端在
  拦截到这次 tool_call 后，按参数里的编号调用产品既有接口实时加载——数据永远
  是最新的，也不会在转录里落下一份过期快照；
- 前端按**工具名**匹配渲染器（与 AG-UI / CopilotKit 一致），因此工具名带
  版本后缀 ``_v1``：参数契约一旦不兼容地变更就发 ``_v2``，旧会话转录里的
  ``_v1`` 调用仍能按旧渲染器绘制，不会出现「升级后历史会话里的卡片认不出来」；
- 参数面刻意扁平：``component`` 决定画什么，``items`` 里每项只带编号。不用
  JSON Schema 的 oneOf 分支——不少 OpenAI 兼容端点对 oneOf 支持很差，扁平结构
  在各家模型上都能稳定生成；按 component 的字段校验放在 handler 里，错误文案
  写给模型看（它会据此自行修正）；
- 是否启用由装配方按通道显式打开（routes/agent.py::get_agent_tools 的
  ``generative_ui`` 开关，默认关）：只有网页会话有画卡片的界面，IM 通道
  （微信/Telegram/Discord）无法解析，带上只会让模型白调一次工具。
"""

from __future__ import annotations

from typing import Any

from movieclaw_agent.toolkit import AgentTool
from movieclaw_llm import ToolDefinition

#: 工具名。版本号是前端渲染器的匹配键，参数契约不兼容变更时才递增。
TOOL_NAME = "render_media_cards_v1"

#: 一次调用最多绘制的卡片数：再多会话页也放不下，模型应分组或挑重点。
MAX_ITEMS = 12

#: 可绘制的组件 → 面向用户的名称（回执文案用）
COMPONENT_LABELS = {
    "library": "媒体库卡片",
    "title": "影片海报卡片",
    "library_item": "库内条目播放卡片",
}

_DESCRIPTION = f"""\
在当前会话页内联绘制 MovieClaw 的影音卡片（生成式 UI）。你只需给出编号，界面会按编号\
实时加载封面、海报、名称、评分、已入库/已订阅状态与播放入口——这些信息不必、也不要再\
用文字复述一遍；卡片渲染后用简短文字补充卡片上没有的结论即可。

为什么要画：这是一款影音产品，用户对一部片子的第一印象来自海报而不是片名文字。一张卡片\
让用户一眼认出是哪部、评分如何、库里有没有、有没有订阅，并能直接在卡片上订阅或播放，\
省去他再去搜索、找入口的步骤；纯文字罗列片名和编号则显得单薄、难以浏览。凡是回答里\
涉及具体的媒体库、影片/剧集或库内条目，优先用卡片呈现，再用文字讲结论——用户的体验\
会明显更好。

可绘制的组件（component）与每项（items[]）的参数：
- library：媒体库卡片（封面拼贴 + 库名 + 类型与库存统计，点击进入该库）。\
每项传 library_id（来自 mclaw 的 library list）。
- title：影片/剧集海报卡片（海报、评分、年份、类型，自动标注已入库/已订阅，悬停即可\
一键订阅，点击进入详情页）。每项传 tmdb_id + media_type（movie/tv），或 douban_id。\
编号来自 search titles / discover 结果的 title_ref：tmdb:movie:123 → tmdb_id=123、\
media_type="movie"；douban:456 → douban_id="456"。
- library_item：库内条目播放卡片（剧照或海报 + 一键播放 + 观看进度 + 片源规格，\
点击进入条目页）。每项传 media_item_id（来自 library items list / search library-items），\
剧集可额外传 season + episode 指定播放哪一集。

使用时机：用户询问或你提到某个媒体库、找到了影片/推荐了片单、报告库里已有的内容或\
建议用户观看时，先用 mclaw 查证编号真实存在，再调用本工具展示。同一组件的多项合并在\
一次调用里（最多 {MAX_ITEMS} 项），不同组件分别调用。不要为了装饰而调用：只画与用户\
问题直接相关的条目。"""

_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "component": {
            "type": "string",
            "enum": list(COMPONENT_LABELS),
            "description": "要绘制的组件：library=媒体库卡片；title=影片/剧集海报卡片；"
            "library_item=库内条目播放卡片",
        },
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_ITEMS,
            "description": "要绘制的条目列表，每项只需带对应组件要求的编号字段",
            "items": {
                "type": "object",
                "properties": {
                    "library_id": {
                        "type": "integer",
                        "description": "component=library：媒体库 id",
                    },
                    "tmdb_id": {
                        "type": "integer",
                        "description": "component=title：TMDB 条目 id（与 media_type 成对）",
                    },
                    "media_type": {
                        "type": "string",
                        "enum": ["movie", "tv"],
                        "description": "component=title 且给了 tmdb_id 时必填：movie/tv",
                    },
                    "douban_id": {
                        "type": "string",
                        "description": "component=title：豆瓣条目 id（与 tmdb_id 二选一）",
                    },
                    "media_item_id": {
                        "type": "integer",
                        "description": "component=library_item：库内媒体条目 id",
                    },
                    "season": {
                        "type": "integer",
                        "description": "component=library_item 可选：剧集季号（与 episode 成对）",
                    },
                    "episode": {
                        "type": "integer",
                        "description": "component=library_item 可选：剧集集号（与 season 成对）",
                    },
                },
            },
        },
        "title": {
            "type": "string",
            "description": "可选：卡片组上方的一行小标题，如「你可能会喜欢」「电影库里已有」",
        },
    },
    "required": ["component", "items"],
}


def _positive_int(item: dict, key: str, index: int) -> int:
    value = item.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"items[{index}].{key} 必须是正整数，实际为 {value!r}")
    return value


def validate_items(component: str, items: list[dict]) -> None:
    """按组件校验每项携带的编号字段；错误文案面向模型，指明该改哪个字段。

    JSON Schema 只能保证类型，「title 组件必须给 tmdb_id+media_type 或 douban_id」
    这类跨字段约束在这里做。不查编号是否真实存在——那是前端加载时的事：查不到的
    卡片会显示「未找到」，模型在调用前本就应先用 mclaw 查证。
    """
    if component not in COMPONENT_LABELS:
        raise ValueError(f"未知组件 {component!r}；可选：{', '.join(COMPONENT_LABELS)}")
    if not items:
        raise ValueError("items 不能为空")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"一次最多绘制 {MAX_ITEMS} 张卡片，请挑重点或分批调用")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{index}] 必须是对象")
        if component == "library":
            _positive_int(item, "library_id", index)
        elif component == "title":
            tmdb_id, douban_id = item.get("tmdb_id"), item.get("douban_id")
            if tmdb_id is None and not douban_id:
                raise ValueError(
                    f"items[{index}] 缺少编号：title 组件需要 tmdb_id + media_type，或 douban_id"
                )
            if tmdb_id is not None:
                _positive_int(item, "tmdb_id", index)
                if item.get("media_type") not in ("movie", "tv"):
                    raise ValueError(
                        f"items[{index}].media_type 必须是 movie 或 tv"
                        "（给了 tmdb_id 就必须说明是电影还是剧集，两者 TMDB 编号独立）"
                    )
            elif not isinstance(douban_id, str) or not douban_id.strip():
                raise ValueError(f"items[{index}].douban_id 必须是非空字符串")
        else:  # library_item
            _positive_int(item, "media_item_id", index)
            season, episode = item.get("season"), item.get("episode")
            if (season is None) != (episode is None):
                raise ValueError(f"items[{index}] 的 season 与 episode 必须同时给出或同时省略")
            if season is not None:
                if not isinstance(season, int) or season < 0:
                    raise ValueError(f"items[{index}].season 必须是非负整数（0 为特别篇）")
                _positive_int(item, "episode", index)


def make_media_ui_tool() -> AgentTool:
    """构建卡片绘制工具。无运行时依赖：它不读任何数据，也不碰任何状态。"""

    async def handler(args: dict) -> str:
        component: str = args["component"]
        items: list[dict] = args["items"]
        validate_items(component, items)
        label = COMPONENT_LABELS[component]
        # 回执写给模型：告诉它「已经画出来了」以及接下来该怎么做（不要复述）。
        # 前端并不读这段文本——它按 tool_call 事件里的参数绘制，与本回执无关。
        return (
            f"已在会话页展示 {len(items)} 张{label}。"
            "卡片内容由界面实时加载，无需再用文字复述封面/海报/评分/状态等信息；"
            "可直接继续回答用户，或补充卡片上没有的结论。"
        )

    return AgentTool(
        definition=ToolDefinition(
            name=TOOL_NAME,
            description=_DESCRIPTION,
            parameters=_PARAMETERS,
        ),
        handler=handler,
    )
