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
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---"):
        return {}
    end = normalized.find("\n---", 3)
    if end == -1:
        return {}
    try:
        data = yaml.safe_load(normalized[4:end])
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else {}


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


def build_skills_fragment(skills: list[Skill]) -> str | None:
    """渲染系统提示词的技能清单段；无技能时返回 None（整段不输出）。

    结构对齐 Agent Skills 标准的集成建议（pi 同款 XML 清单），指令行为
    中文：匹配才 read、相对路径以技能目录为锚、技能目录只读。
    """
    if not skills:
        return None
    lines = [
        "# 技能",
        "以下技能提供特定任务的专项指令。当用户请求与某技能的描述匹配时，"
        "先用 read 工具读取该技能文件的完整内容，再按其中的指令行动。"
        "技能文件里的相对路径以该技能所在目录（SKILL.md 的父目录）为锚，"
        "转换成绝对路径后再在工具调用中使用。技能目录是只读资料，不要修改其中的文件。",
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
