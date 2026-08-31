"""Agent 技能：发现与系统提示词清单渲染（设计见 docs/design/agent-skills.md）。

采用 pi 的「渐进披露」极简路线：这里只负责扫描技能目录、把每个技能的
name/description/SKILL.md 绝对路径渲染成 system prompt 里的清单段；技能
**正文从不在这里读取**——模型判断任务匹配后用现成的 read 工具自己加载，
改技能即时生效、正文不常驻上下文。

发现分两层（用户层优先，同名覆盖内置层）：
- 内置层：本包内 ``builtin-skills/`` 目录，随源码打包发版。路径从
  ``__file__`` 反推（与 prompts._SOURCE_ROOT 同一机制），应用内更新的
  overlay 生效时自动指向新版本的内置技能；
- 用户层：部署配置的技能目录（默认 data/agent-skills，由 API 层传入），
  管理员放目录即安装，同名时覆盖内置版并记 info 日志。

校验哲学与 pi 一致（warn-but-load）：唯一的硬性拒载条件是缺 description
（清单靠它触发，缺了等于不存在）；名字超长/含非法字符等只记警告照常加载。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

logger = logging.getLogger("movieclaw_agent.skills")

#: 内置技能层：随源码打包，__file__ 反推保证 overlay 更新后自动跟版
BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent / "builtin-skills"

#: Agent Skills 标准的元数据上限（超出只警告，不拒载）
_MAX_NAME_LENGTH = 64
_MAX_DESCRIPTION_LENGTH = 1024

#: 递归深度上限：symlink 已禁、真实目录树到不了这个深度，纯防御性常数
_MAX_SCAN_DEPTH = 16

#: 清单渲染结果的提醒阈值：超过只记警告不截断（P0 量级用不到预算机制）
_FRAGMENT_WARN_CHARS = 16_000

SkillScope = Literal["builtin", "user"]


@dataclass(frozen=True)
class Skill:
    """一个已发现的技能：只有清单所需的元数据，正文由模型按需 read。"""

    name: str
    description: str
    #: SKILL.md 的绝对路径——清单里的 location，模型 read 的目标
    file_path: Path
    #: 来源层级，仅用于日志与未来管理页区分，不进提示词清单
    scope: SkillScope


def scan_skills(root: Path, scope: SkillScope) -> list[Skill]:
    """扫描单个技能目录（pi 发现算法的简化版）。

    规则：目录含 SKILL.md 即技能根、不再深入；否则递归子目录（跳过 ``.``
    开头目录与 node_modules）；不跟随 symlink（防环兼防逃逸）；不支持根层
    裸 .md 单文件技能。目录项按名字排序遍历，产出确定性。
    """
    if not root.is_dir() or root.is_symlink():
        return []
    return _scan_dir(root, scope, depth=0)


def _scan_dir(directory: Path, scope: SkillScope, depth: int) -> list[Skill]:
    if depth > _MAX_SCAN_DEPTH:
        return []
    skill_file = directory / "SKILL.md"
    if skill_file.is_file() and not skill_file.is_symlink():
        skill = _load_skill(skill_file, scope)
        return [skill] if skill else []

    found: list[Skill] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        logger.warning("技能目录读取失败，已跳过：%s（%s）", directory, exc)
        return found
    for entry in entries:
        if entry.name.startswith(".") or entry.name == "node_modules":
            continue
        if entry.is_symlink() or not entry.is_dir():
            continue
        found.extend(_scan_dir(entry, scope, depth + 1))
    return found


def _load_skill(skill_file: Path, scope: SkillScope) -> Skill | None:
    """解析一个 SKILL.md 的 frontmatter，产出技能或 None（含中文日志）。"""
    try:
        text = skill_file.read_text(errors="replace")
    except OSError as exc:
        logger.warning("技能文件读取失败，已跳过：%s（%s）", skill_file, exc)
        return None

    frontmatter = _parse_frontmatter(text)
    if frontmatter is None:
        logger.warning("技能文件 frontmatter 无法解析，已跳过：%s", skill_file)
        return None

    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        logger.warning(
            "技能缺少 description（清单靠它触发），已跳过：%s。"
            "请在 SKILL.md 的 frontmatter 中补充 description 字段",
            skill_file,
        )
        return None
    description = description.strip()

    raw_name = frontmatter.get("name")
    has_name = isinstance(raw_name, str) and raw_name.strip()
    name = raw_name.strip() if has_name else skill_file.parent.name

    # 宽松校验：以下都只警告不拒载（兼容为其它 Agent 客户端编写的技能）
    if len(name) > _MAX_NAME_LENGTH:
        logger.warning("技能名超过 %d 字符：%s（%s）", _MAX_NAME_LENGTH, name, skill_file)
    elif not re.fullmatch(r"[a-z0-9-]+", name):
        logger.warning(
            "技能名不符合 Agent Skills 规范（仅小写字母/数字/连字符）：%s（%s）",
            name,
            skill_file,
        )
    if len(description) > _MAX_DESCRIPTION_LENGTH:
        logger.warning(
            "技能描述超过 %d 字符，会放大清单的常驻开销：%s", _MAX_DESCRIPTION_LENGTH, skill_file
        )

    return Skill(name=name, description=description, file_path=skill_file.resolve(), scope=scope)


def _parse_frontmatter(text: str) -> dict | None:
    """提取 YAML frontmatter；无 frontmatter 返回空 dict，解析失败返回 None。"""
    parsed = _split_frontmatter(text)
    return None if parsed is None else parsed[0]


def _split_frontmatter(text: str) -> tuple[dict, str] | None:
    """拆出 (frontmatter, 正文)；无 frontmatter 时正文即全文，解析失败返回 None。"""
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---"):
        return {}, normalized
    end = normalized.find("\n---", 3)
    if end == -1:
        return {}, normalized
    try:
        data = yaml.safe_load(normalized[4:end])
    except yaml.YAMLError:
        return None
    body = normalized[end + 4 :].lstrip("\n")
    return (data if isinstance(data, dict) else {}), body


def discover_skills(user_dir: Path, builtin_dir: Path | None = None) -> list[Skill]:
    """两层合并：先用户层、后内置层，同名（不分大小写）先到先得。

    用户技能覆盖内置技能是覆盖机制的正常用法（定制官方技能：复制到用户
    目录改），记 info 日志；同层内同名多半是配置失误，scan 产出的顺序内
    先到先得并记 warning。缺失的目录静默跳过。
    """
    merged: dict[str, Skill] = {}
    layered = [
        *scan_skills(user_dir, "user"),
        *scan_skills(builtin_dir or BUILTIN_SKILLS_DIR, "builtin"),
    ]
    for skill in layered:
        key = skill.name.lower()
        winner = merged.get(key)
        if winner is None:
            merged[key] = skill
        elif winner.scope == "user" and skill.scope == "builtin":
            logger.info(
                "用户技能「%s」已覆盖同名内置技能（%s 覆盖 %s）",
                winner.name,
                winner.file_path,
                skill.file_path,
            )
        else:
            logger.warning(
                "发现同名技能「%s」，仅保留先发现的一个（保留 %s，忽略 %s）",
                winner.name,
                winner.file_path,
                skill.file_path,
            )
    return list(merged.values())


def build_skills_fragment(skills: list[Skill], user_skills_dir: Path) -> str | None:
    """渲染系统提示词的技能清单段；无技能时返回 None（整段不输出）。

    结构对齐 Agent Skills 标准的集成建议（pi 同款 XML 清单），指令行为
    中文：匹配才 read、相对路径以技能目录为锚、已有技能只读。

    创建引导（pi 的做法是在自带文档里列出技能目录位置、模型读文档后自行
    写入；我们把位置直接写进指令行）：新建/修改技能一律进用户技能目录——
    内置目录随版本更新整体覆盖，写进去的东西升级就没了。
    """
    if not skills:
        return None
    lines = [
        "# 技能",
        "以下技能提供特定任务的专项指令。当用户请求与某技能的描述匹配时，"
        "先用 read 工具读取该技能文件的完整内容，再按其中的指令行动。"
        "技能文件里的相对路径以该技能所在目录（SKILL.md 的父目录）为锚，"
        "转换成绝对路径后再在工具调用中使用。已有技能目录是只读资料，不要修改其中的文件。",
        f"需要创建或修改技能时，一律写入用户技能目录 {user_skills_dir}"
        "（每个技能一个子目录，内含 SKILL.md，frontmatter 必须有 description）。"
        "系统内置技能随版本更新会被整体覆盖，绝不能往内置技能目录写入；"
        "要定制某个内置技能，先把它整个复制到用户技能目录再修改——"
        "同名的用户技能会自动覆盖内置版。新技能在下一轮对话自动生效，无需重启。",
        "",
        "<available_skills>",
    ]
    for skill in skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{_escape_xml(skill.name)}</name>")
        lines.append(f"    <description>{_escape_xml(skill.description)}</description>")
        lines.append(f"    <location>{_escape_xml(str(skill.file_path))}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    fragment = "\n".join(lines)
    if len(fragment) > _FRAGMENT_WARN_CHARS:
        logger.warning(
            "技能清单渲染后超过 %d 字符（当前 %d），会放大每次运行的常驻开销；"
            "建议精简技能描述或减少技能数量",
            _FRAGMENT_WARN_CHARS,
            len(fragment),
        )
    return fragment


def _escape_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# 显式调用：/skill:名字 占位符的服务端展开（docs/design/agent-skills.md §9）
# ---------------------------------------------------------------------------

#: token 语法取自 maka（行首或空白后 + /skill: + 名字），比 pi 的「整条消息
#: 开头」宽松——加号菜单把占位符插到光标处，前后都可能有文字。名字字符集
#: 放宽到大小写/点/下划线，容忍手误后仍能按小写匹配到技能。
SKILL_TOKEN_RE = re.compile(r"(?:^|(?<=\s))/skill:([A-Za-z0-9._-]+)")

#: 一条消息最多展开的技能数；超出的 token 原样保留
MAX_INVOCATIONS_PER_MESSAGE = 8

#: 单个技能正文注入上限：一次性进 user 消息没有 read 分页兜底，必须设限
MAX_INVOCATION_BODY_CHARS = 32_000

#: 已展开消息里技能块的识别正则（pi parseSkillBlock 同款思路，前端 TS 镜像
#: 一份）。只匹配消息开头的连续块——展开产物的固定形态。
_SKILL_BLOCK_RE = re.compile(r'^<skill name="([^"]*)" location="[^"]*">\n.*?\n</skill>\n*', re.S)


def expand_skill_invocations(content: str, skills: list[Skill]) -> str:
    """把消息里的 /skill:名字 占位符展开为技能正文块（pi 展开语义）。

    - 命中的 token 从文本剥除，技能正文现场读盘、剥 frontmatter、超限截断，
      包成 ``<skill name= location=>`` 块置于消息开头；
    - 未命中的 token 原样保留（pi 式透传：技能被删/名字敲错时模型看得见
      原文，能向用户解释，比静默吞掉或整条拒发更符合会话产品）；
    - 展开结果替换原文入转录：内容冻结在调用时刻，历史可复现；已展开文本
      不再含裸 token，retry 复用旧文本天然幂等。
    """
    matches = list(SKILL_TOKEN_RE.finditer(content))
    if not matches:
        return content

    by_name = {s.name.lower(): s for s in skills}
    # 首现序去重 + 上限；只展开能匹配到技能的名字
    requested: list[str] = []
    for m in matches:
        key = m.group(1).lower()
        if key in requested or key not in by_name:
            continue
        if len(requested) >= MAX_INVOCATIONS_PER_MESSAGE:
            logger.warning(
                "一条消息的技能调用超过 %d 个上限，多余的占位符原样保留",
                MAX_INVOCATIONS_PER_MESSAGE,
            )
            break
        requested.append(key)
    if not requested:
        return content

    blocks: list[str] = []
    expanded_names: set[str] = set()
    for key in requested:
        block = _render_invocation_block(by_name[key])
        if block is None:
            continue  # 读盘失败：token 保留在文本里，模型可见可解释
        blocks.append(block)
        expanded_names.add(key)
    if not blocks:
        return content

    remaining = _strip_tokens(content, expanded_names).strip()
    tail = remaining if remaining else "用户未附加说明，按上述技能指令执行。"
    return "\n\n".join([*blocks, tail])


def _render_invocation_block(skill: Skill) -> str | None:
    try:
        text = skill.file_path.read_text(errors="replace")
    except OSError as exc:
        logger.warning("显式调用读取技能文件失败：%s（%s）", skill.file_path, exc)
        return None
    parsed = _split_frontmatter(text)
    body = (text if parsed is None else parsed[1]).strip()
    if len(body) > MAX_INVOCATION_BODY_CHARS:
        body = (
            body[:MAX_INVOCATION_BODY_CHARS]
            + f"\n[技能正文超过 {MAX_INVOCATION_BODY_CHARS} 字符已截断，"
            f"完整内容可用 read 工具读取 {skill.file_path}]"
        )
    name = _sanitize_attribute(skill.name)
    location = _sanitize_attribute(str(skill.file_path))
    return (
        f'<skill name="{name}" location="{location}">\n'
        f"技能文件里的相对路径以 {skill.file_path.parent} 目录为锚。\n\n"
        f"{body}\n"
        "</skill>"
    )


def _strip_tokens(content: str, names: set[str]) -> str:
    """剥除已展开的 token（maka 的行内清理：被触碰的行折叠多余空白）。"""
    out: list[str] = []
    for line in content.split("\n"):
        touched = False

        def replace(m: re.Match[str]) -> str:
            nonlocal touched
            if m.group(1).lower() in names:
                touched = True
                return ""
            return m.group(0)

        stripped = SKILL_TOKEN_RE.sub(replace, line)
        if not touched:
            out.append(line)
            continue
        tidied = re.sub(r"[ \t]+", " ", stripped).strip()
        if tidied:
            out.append(tidied)
    return "\n".join(out)


def _sanitize_attribute(value: str) -> str:
    """XML 属性清洗（maka 同款思路）：剥控制字符，中和引号与尖括号。"""
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", value)
    return re.sub(r'["<>&]', "_", cleaned)


def strip_skill_blocks(text: str) -> tuple[list[str], str]:
    """从已展开的消息里拆出 (技能名列表, 剩余用户文本)。

    供列表预览（``[技能]`` 占位）与前端气泡 chip 渲染使用（TS 侧镜像同一
    正则）。非展开消息原样返回 ([], text)。
    """
    names: list[str] = []
    rest = text
    while True:
        m = _SKILL_BLOCK_RE.match(rest)
        if m is None:
            break
        names.append(m.group(1))
        rest = rest[m.end() :]
    return names, rest
