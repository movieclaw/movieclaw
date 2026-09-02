"""show_media_cards：Agent 的「生成式 UI」工具——在会话页内联展示影音卡片。

设计（docs/design/agent-generative-ui.md，思路对齐 AG-UI 协议的 render-only
frontend tool）：

- 工具本身**不产出任何展示数据**：handler 只校验参数形状，返回固定的 ``ok``。
  卡片的封面、海报、评分、订阅/入库状态与播放入口全部由前端在拦截到这次
  tool_call 后，按参数里的编号调用产品既有接口实时加载——数据永远是最新的，
  也不会在转录里落下一份过期快照；
- 前端按**工具名**匹配渲染器（与 AG-UI / CopilotKit 一致），因此工具名带
  版本后缀 ``_v1``：参数契约一旦不兼容地变更就发 ``_v2``，旧会话转录里的
  ``_v1`` 调用仍能按旧渲染器绘制，不会出现「升级后历史会话里的卡片认不出来」；
- 参数字段名**与 mclaw 的输出字段一致**（title_ref / media_item_id /
  season_number / subscription_id …）：模型从 mclaw 结果里原样搬编号即可，
  不需要换算，也就不会搬错；
- 参数面刻意扁平：``component`` 决定画什么，``items`` 里每项只带编号。不用
  JSON Schema 的 oneOf 分支——不少 OpenAI 兼容端点对 oneOf 支持很差，扁平结构
  在各家模型上都能稳定生成；按 component 的字段校验放在 handler 里，错误文案
  写给模型看（它会据此自行修正）；
- 是否启用由装配方按通道显式打开（routes/agent.py::get_agent_tools 的
  ``generative_ui`` 开关，默认关）：只有网页会话有画卡片的界面，IM 通道
  （微信/Telegram/Discord）无法解析，带上只会让模型白调一次工具。
"""

from __future__ import annotations

import re
from typing import Any

from movieclaw_agent.toolkit import AgentTool
from movieclaw_llm import ToolDefinition

#: 工具名。版本号是前端渲染器的匹配键，参数契约不兼容变更时才递增。
#: 动词用 show（向用户展示）而不是 render：模型对「把结果展示给用户」这类意图
#: 的匹配更直接，render 偏前端工程术语。
TOOL_NAME = "show_media_cards_v1"

#: 可展示的组件（与 mclaw 的域/命令同名：library / titles / library items / subscriptions）
COMPONENTS = ("library", "title", "library_item", "subscription")

#: title_ref 的两种合法形态（与 movieclaw_api.services.title_discovery.parse_title_ref 同）
_TITLE_REF = re.compile(r"^(tmdb:(movie|tv):\d+|douban:[^:/\s]+)$")

_DESCRIPTION = """\
向用户展示可交互的影音卡片（生成式 UI）。你只需给出编号，会话页就会画出带封面/海报的\
卡片，用户可以直接在卡片上订阅、播放、进入详情。

什么时候用：只要用户的问题涉及影片/剧集、媒体库、库里的内容或订阅——例如问库里有什么、\
找片或要推荐、查某部片有没有、报告订阅进度、建议看什么——就主动用本工具把结果展示成\
卡片，再用文字讲结论。海报和一键操作比文字罗列片名直观得多，用户非常喜欢这种呈现；\
不要只用文字列片名或编号。编号必须来自本轮 mclaw 的真实结果，不要凭记忆编。

可展示的组件（component）及每项（items[]）的字段。字段名与 mclaw 输出一致，从结果里\
原样搬过来即可：
- library：媒体库卡片（封面拼贴、库名、类型与库存统计）。library_id ← library list 的 id。
- title：影片/剧集海报卡片（海报、评分、年份，自动标注已入库/已订阅，可一键订阅）。\
title_ref ← search titles / discover 结果的 title_ref（形如 tmdb:movie:123、\
tmdb:tv:456、douban:789）；手头只有 TMDB 编号时改传 tmdb_id + media_type（movie/tv）。
- library_item：库内条目播放卡片（剧照、一键播放、观看进度、片源规格）。\
media_item_id ← library items list / search library-items 的 media_item_id；\
剧集可加 season_number + episode_number 指定播放哪一集。
- subscription：订阅卡片（海报、追更范围、收录进度、自动续订/已收齐状态）。\
subscription_id ← subscriptions list 的 id。

同一组件的多项放进一次调用，不同组件分别调用；可用 title 给卡片组加一行小标题。\
卡片内容由界面实时加载，展示后不要再用文字复述海报、评分、状态这些卡片上已有的信息。"""

_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "component": {
            "type": "string",
            "enum": list(COMPONENTS),
            "description": "要展示的组件：library=媒体库卡片；title=影片/剧集海报卡片；"
            "library_item=库内条目播放卡片；subscription=订阅卡片",
        },
        "items": {
            "type": "array",
            "minItems": 1,
            "description": "要展示的条目列表，每项只需带对应组件要求的编号字段",
            "items": {
                "type": "object",
                "properties": {
                    "library_id": {
                        "type": "integer",
                        "description": "component=library：媒体库 id（library list 的 id）",
                    },
                    "title_ref": {
                        "type": "string",
                        "description": "component=title：影视条目稳定引用，原样取自 "
                        "search titles / discover 结果"
                        "（tmdb:movie:123 / tmdb:tv:456 / douban:789）",
                    },
                    "tmdb_id": {
                        "type": "integer",
                        "description": "component=title：没有 title_ref 时用 TMDB 编号"
                        "（与 media_type 成对）",
                    },
                    "media_type": {
                        "type": "string",
                        "enum": ["movie", "tv"],
                        "description": "component=title 且给了 tmdb_id 时必填：movie/tv",
                    },
                    "media_item_id": {
                        "type": "integer",
                        "description": "component=library_item：库内媒体条目 media_item_id",
                    },
                    "season_number": {
                        "type": "integer",
                        "description": "component=library_item 可选：剧集季号"
                        "（与 episode_number 成对）",
                    },
                    "episode_number": {
                        "type": "integer",
                        "description": "component=library_item 可选：剧集集号"
                        "（与 season_number 成对）",
                    },
                    "subscription_id": {
                        "type": "integer",
                        "description": "component=subscription：订阅 id"
                        "（subscriptions list 的 id）",
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

    JSON Schema 只能保证类型，「title 组件必须给 title_ref 或 tmdb_id+media_type」
    这类跨字段约束在这里做。不查编号是否真实存在——那是前端加载时的事：查不到的
    卡片会显示「未找到」，模型在调用前本就应先用 mclaw 查到真实编号。
    """
    if component not in COMPONENTS:
        raise ValueError(f"未知组件 {component!r}；可选：{', '.join(COMPONENTS)}")
    if not items:
        raise ValueError("items 不能为空")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{index}] 必须是对象")
        if component == "library":
            _positive_int(item, "library_id", index)
        elif component == "title":
            title_ref = item.get("title_ref")
            if title_ref is not None:
                if not isinstance(title_ref, str) or not _TITLE_REF.match(title_ref.strip()):
                    raise ValueError(
                        f"items[{index}].title_ref 格式无效（{title_ref!r}）；"
                        "请原样使用 search titles / discover 返回的 title_ref，"
                        "形如 tmdb:movie:123、tmdb:tv:456 或 douban:789"
                    )
            elif item.get("tmdb_id") is not None:
                _positive_int(item, "tmdb_id", index)
                if item.get("media_type") not in ("movie", "tv"):
                    raise ValueError(
                        f"items[{index}].media_type 必须是 movie 或 tv"
                        "（给了 tmdb_id 就必须说明是电影还是剧集，两者 TMDB 编号独立）"
                    )
            else:
                raise ValueError(
                    f"items[{index}] 缺少编号：title 组件需要 title_ref，或 tmdb_id + media_type"
                )
        elif component == "library_item":
            _positive_int(item, "media_item_id", index)
            season, episode = item.get("season_number"), item.get("episode_number")
            if (season is None) != (episode is None):
                raise ValueError(
                    f"items[{index}] 的 season_number 与 episode_number 必须同时给出或同时省略"
                )
            if season is not None:
                if not isinstance(season, int) or isinstance(season, bool) or season < 0:
                    raise ValueError(f"items[{index}].season_number 必须是非负整数（0 为特别篇）")
                _positive_int(item, "episode_number", index)
        else:  # subscription
            _positive_int(item, "subscription_id", index)


def make_media_ui_tool() -> AgentTool:
    """构建卡片展示工具。无运行时依赖：它不读任何数据，也不碰任何状态。"""

    async def handler(args: dict) -> str:
        validate_items(args["component"], args["items"])
        # 回执只表示「成功」：展示由前端按 tool_call 参数完成，与本文本无关；
        # 怎么用、用后别复述，全在 description 里说过，不在回执里重复占上下文
        return "ok"

    return AgentTool(
        definition=ToolDefinition(
            name=TOOL_NAME,
            description=_DESCRIPTION,
            parameters=_PARAMETERS,
        ),
        handler=handler,
    )
