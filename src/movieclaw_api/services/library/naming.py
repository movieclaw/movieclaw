"""命名模板渲染器：入库/整理/投递的目录与文件名唯一来源。

docs/design/scrape-customization.md §2.3。本模块存在的理由是**命名同源**：
``标题 (年份)`` 这一套规范名此前散落在四处各自拼字符串——

- ``config.derive_save_path``（投递给下载器的 save_path）
- ``config.derive_entry_dir``（监听导入的自定义目录落点）
- ``ingest``（下载完成后的入库落名）
- ``organize``（存量整理的目标路径）

模板化之后它们**必须**全部改读本模块：任何一处继续拼字符串，用户改了模板
就会出现"投递落 A 名、整理算 B 名"，strm 回流链路（docs/design/strm-workflow.md）
当场断裂——那条链路的全部衔接机制就是两端算出同一个名字。

模板语法刻意受控（否决自由模板引擎，见设计文档 §5）：只有花括号占位符与
字面文本，数字占位符支持 ``:02d`` 补零，没有条件与过滤器语法。字段缺失时
由 ``_collapse`` 统一收缩（空括号、重复分隔符、首尾分隔符），规则确定性、
有单测，不需要用户在模板里写条件。

层级结构固定为 ``条目目录[/季目录]/文件名``，模板只描述**一段**名字：
识别链的 ``entry_dirs``、条目删除、NFO 落点都依赖这个结构假设，开放层级
自定义会让它们全部失去判据。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from movieclaw_api.services.library.config import sanitize_folder_name

# 占位符：``{name}`` 或 ``{name:02d}``（补零仅对纯数字值生效）
_TOKEN = re.compile(r"\{(\w+)(?::0(\d)d)?\}")

# 可用占位符按可解析的上下文分组——在拿不到值的模板里放占位符只会渲染成空，
# 与其让用户事后发现名字少了一截，不如保存时就报错
_COMMON = frozenset({"title", "original_title", "year", "tmdb_id", "imdb_id"})
_FILE_ATTRS = frozenset({"resolution", "media_source", "release_group"})

ALLOWED_TOKENS: dict[str, frozenset[str]] = {
    "entry_dir": _COMMON,
    "movie_file": _COMMON | _FILE_ATTRS,
    "season_dir": _COMMON | frozenset({"season"}),
    "episode_file": _COMMON | _FILE_ATTRS | frozenset({"season", "episode", "episode_title"}),
}

# 模板字段的中文名（错误文案用，面向非开发者）
FIELD_LABELS = {
    "entry_dir": "条目目录",
    "movie_file": "电影文件名",
    "season_dir": "季目录",
    "episode_file": "剧集文件名",
}


@dataclass(frozen=True)
class NamingTemplates:
    """四个模板。默认值**逐字节等于**模板化之前的写死行为。"""

    entry_dir: str = "{title} ({year})"
    movie_file: str = "{title} ({year})"
    season_dir: str = "Season {season:02d}"
    episode_file: str = "{title} ({year}) - S{season:02d}E{episode:02d}"


DEFAULT_TEMPLATES = NamingTemplates()


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


# 一组括号（不嵌套）：占位符全空时整组丢弃，见 _drop_empty_groups
_BRACKET_GROUP = re.compile(r"[(\[【][^()\[\]【】]*[)\]】]")


def _token_value(context: Mapping[str, Any], name: str, pad: str | None) -> str:
    """占位符取值：缺失/空返回空串；值逐个过 sanitize（标题里的 ``/``
    不能凭空造出一层目录）；补零仅对纯数字生效。"""
    value = context.get(name)
    if value is None or value == "":
        return ""
    text = sanitize_folder_name(str(value))
    if pad and text.isdigit():
        text = text.zfill(int(pad))
    return text


def _drop_empty_groups(template: str, context: Mapping[str, Any]) -> str:
    """整组丢弃"占位符全空"的括号组——**含组内字面文本**。

    纯正则收尾只能删掉 ``()`` 这种空括号，删不掉 ``[tmdbid-]``：组里那截
    ``tmdbid-`` 是字面文本，占位符没值时它就成了残缺垃圾。而
    ``{title} ({year}) [tmdbid-{tmdb_id}]`` 正是 Emby/Jellyfin 最经典的
    目录写法，必须收干净。组内没有占位符的纯字面括号原样保留。
    """

    def _repl(match: re.Match[str]) -> str:
        group = match.group(0)
        tokens = _TOKEN.findall(group)
        if not tokens:
            return group
        if all(not _token_value(context, name, pad) for name, pad in tokens):
            return ""
        return group

    return _BRACKET_GROUP.sub(_repl, template)


def _collapse(text: str) -> str:
    """收尾收缩：括号内侧、重复分隔符、多余空白、首尾分隔符（幂等）。

    括号内侧单独收一道：``[{resolution} {release_group}]`` 只有分辨率有值时
    会剩下 ``[2160p ]``——整串 strip 够不着括号里面那个空格。
    """
    text = re.sub(r"([(\[【])[\s\-–]+", r"\1", text)
    text = re.sub(r"[\s\-–]+([)\]】])", r"\1", text)
    text = re.sub(r"(?:\s*-\s*){2,}", " - ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" -–.")


def render(template: str, context: Mapping[str, Any]) -> str:
    """按上下文渲染一段名字（**只是一段**，不含路径分隔符）。

    三步：先整组丢弃占位符全空的括号组，再替换占位符，最后收缩并整体
    过一次 ``sanitize_folder_name``（兜住模板字面文本里的保留字符）。
    """
    text = _drop_empty_groups(template, context)
    text = _TOKEN.sub(lambda m: _token_value(context, m.group(1), m.group(2)), text)
    return sanitize_folder_name(_collapse(text))


# ---------------------------------------------------------------------------
# 校验（保存时前置报错，中文文案）
# ---------------------------------------------------------------------------


def validate_template(field: str, template: str) -> str | None:
    """校验单个模板；通过返回 None，否则返回面向用户的中文错误。"""
    label = FIELD_LABELS.get(field, field)
    if not template.strip():
        return f"{label}模板不能为空"
    if "/" in template or "\\" in template:
        return f"{label}模板不能包含路径分隔符（目录层级是固定的，模板只描述一段名字）"

    allowed = ALLOWED_TOKENS[field]
    used = {m.group(1) for m in _TOKEN.finditer(template)}
    unknown = sorted(used - allowed)
    if unknown:
        return (
            f"{label}模板里有不可用的占位符：{'、'.join('{' + n + '}' for n in unknown)}"
            f"（该模板可用：{'、'.join('{' + n + '}' for n in sorted(allowed))}）"
        )

    if field in ("entry_dir", "movie_file") and not ({"title", "original_title"} & used):
        return f"{label}模板必须包含 {{title}} 或 {{original_title}}，否则不同影片会重名"
    if field == "season_dir" and "season" not in used:
        return "季目录模板必须包含 {season}，否则不同季的同集号文件会互相覆盖"
    if field == "episode_file" and not {"season", "episode"} <= used:
        return "剧集文件名模板必须包含 {season} 与 {episode}，否则同一部剧的多集会互相覆盖"
    return None


def validate_templates(templates: NamingTemplates) -> str | None:
    """校验四个模板，返回第一条错误；全部通过返回 None。"""
    for field in ALLOWED_TOKENS:
        error = validate_template(field, getattr(templates, field))
        if error is not None:
            return error
    return None


# ---------------------------------------------------------------------------
# 生效模板与上下文构造
# ---------------------------------------------------------------------------


def effective_templates(library: object | None = None) -> NamingTemplates:
    """当前生效的模板（内置默认 → 全局设置 → 库级覆盖，逐字段回落）。

    延迟导入 scrape_config：本模块被 settings 的校验器引用，模块级导入会
    绕成 settings → naming → scrape_config → settings 的环。
    """
    from movieclaw_api.services.scrape_config import effective_naming_templates

    return effective_naming_templates(library)


def item_context(item: Any) -> dict[str, Any]:
    """媒体条目 → 渲染上下文（只取模板可用的身份字段）。"""
    return {
        "title": item.title,
        "original_title": getattr(item, "original_title", None),
        "year": item.year,
        "tmdb_id": getattr(item, "tmdb_id", None),
        "imdb_id": getattr(item, "imdb_id", None),
    }


# ---------------------------------------------------------------------------
# 四个入口（四处调用点唯一允许的命名来源）
# ---------------------------------------------------------------------------


def entry_dir_name(
    *,
    title: str,
    year: int | None = None,
    templates: NamingTemplates | None = None,
    library: object | None = None,
    **extra: Any,
) -> str:
    """条目目录名。投递 save_path、监听导入落点、整理目标目录共用。"""
    tpl = templates or effective_templates(library)
    return render(tpl.entry_dir, {"title": title, "year": year, **extra})


def entry_dir_name_of(
    item: Any, templates: NamingTemplates | None = None, library: object | None = None
) -> str:
    """条目目录名（媒体条目版）。"""
    tpl = templates or effective_templates(library)
    return render(tpl.entry_dir, item_context(item))


def movie_file_name(
    item: Any,
    templates: NamingTemplates | None = None,
    library: object | None = None,
    **extra: Any,
) -> str:
    """电影正片文件名（不含扩展名）。"""
    tpl = templates or effective_templates(library)
    return render(tpl.movie_file, {**item_context(item), **extra})


def season_dir_name(
    season: int,
    item: Any | None = None,
    templates: NamingTemplates | None = None,
    library: object | None = None,
) -> str:
    """季目录名。"""
    tpl = templates or effective_templates(library)
    context: dict[str, Any] = dict(item_context(item)) if item is not None else {}
    context["season"] = season
    return render(tpl.season_dir, context)


def episode_file_name(
    item: Any,
    season: int,
    episode: int,
    templates: NamingTemplates | None = None,
    library: object | None = None,
    **extra: Any,
) -> str:
    """分集文件名（不含扩展名）。"""
    tpl = templates or effective_templates(library)
    return render(
        tpl.episode_file,
        {**item_context(item), "season": season, "episode": episode, **extra},
    )
