"""技能发现与清单渲染（docs/design/agent-skills.md §2.3 / §3.1）。

覆盖：单目录扫描规则（SKILL.md 短路 / 递归 / 缺 description 拒载 /
name 回退目录名 / 同层同名先到先得 / symlink 跳过）、两层合并（用户覆盖
内置 + info 日志 / 互不同名全量保留 / 缺失目录静默）、fragment 渲染
（XML 转义 / 空清单 None / 超限警告）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from movieclaw_agent.skills import (
    Skill,
    build_skills_fragment,
    discover_skills,
    scan_skills,
)


def write_skill(root: Path, rel_dir: str, description: str | None, name: str | None = None) -> Path:
    """生成一个技能目录并返回 SKILL.md 路径。description=None 表示不写该字段。"""
    skill_dir = root / rel_dir
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = ["---"]
    if name is not None:
        frontmatter.append(f"name: {name}")
    if description is not None:
        frontmatter.append(f"description: {description}")
    frontmatter.append("---")
    path = skill_dir / "SKILL.md"
    path.write_text("\n".join(frontmatter) + "\n\n# 正文\n")
    return path


# ---------------------------------------------------------------------------
# 单目录扫描
# ---------------------------------------------------------------------------


def test_skill_root_short_circuits_recursion(tmp_path: Path) -> None:
    """含 SKILL.md 的目录是技能根：references/ 里的 SKILL.md 不算新技能。"""
    write_skill(tmp_path, "writer", "写作技能")
    write_skill(tmp_path, "writer/references", "不该被发现")
    skills = scan_skills(tmp_path, "user")
    assert [s.name for s in skills] == ["writer"]


def test_recurses_into_grouping_folders(tmp_path: Path) -> None:
    write_skill(tmp_path, "group/inner/poster-wall", "海报墙策展")
    (tmp_path / ".hidden" / "skip").mkdir(parents=True)
    write_skill(tmp_path, ".hidden/skip", "隐藏目录应跳过")
    write_skill(tmp_path, "node_modules/dep", "依赖目录应跳过")
    skills = scan_skills(tmp_path, "user")
    assert [s.name for s in skills] == ["poster-wall"]


def test_missing_description_rejected(tmp_path: Path, caplog) -> None:
    write_skill(tmp_path, "no-desc", None)
    write_skill(tmp_path, "ok", "有描述")
    with caplog.at_level(logging.WARNING, logger="movieclaw_agent.skills"):
        skills = scan_skills(tmp_path, "user")
    assert [s.name for s in skills] == ["ok"]
    assert any("description" in r.message for r in caplog.records)


def test_name_falls_back_to_directory(tmp_path: Path) -> None:
    write_skill(tmp_path, "subtitle-workflow", "字幕流程")  # 无 name 字段
    write_skill(tmp_path, "custom", "自定义名", name="curation")
    by_dir = {s.file_path.parent.name: s for s in scan_skills(tmp_path, "user")}
    assert by_dir["subtitle-workflow"].name == "subtitle-workflow"
    assert by_dir["custom"].name == "curation"


def test_symlink_dirs_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write_skill(outside, "escaped", "不该被发现")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside / "escaped")
    write_skill(root, "real", "真实技能")
    assert [s.name for s in scan_skills(root, "user")] == ["real"]


def test_loose_markdown_not_supported(tmp_path: Path) -> None:
    (tmp_path / "loose.md").write_text("---\ndescription: 裸文件\n---\n正文")
    write_skill(tmp_path, "real", "真实技能")
    assert [s.name for s in scan_skills(tmp_path, "user")] == ["real"]


# ---------------------------------------------------------------------------
# 两层合并
# ---------------------------------------------------------------------------


def test_user_overrides_builtin_with_info_log(tmp_path: Path, caplog) -> None:
    user, builtin = tmp_path / "user", tmp_path / "builtin"
    user_path = write_skill(user, "writer", "用户版")
    write_skill(builtin, "writer", "内置版")
    with caplog.at_level(logging.INFO, logger="movieclaw_agent.skills"):
        skills = discover_skills(user, builtin)
    assert len(skills) == 1
    assert skills[0].scope == "user"
    assert skills[0].file_path == user_path.resolve()
    assert any("覆盖" in r.message and r.levelno == logging.INFO for r in caplog.records)


def test_distinct_names_from_both_layers_kept(tmp_path: Path) -> None:
    user, builtin = tmp_path / "user", tmp_path / "builtin"
    write_skill(user, "curation", "用户技能")
    write_skill(builtin, "writer", "内置技能")
    skills = discover_skills(user, builtin)
    assert {(s.name, s.scope) for s in skills} == {("curation", "user"), ("writer", "builtin")}


def test_missing_dirs_are_silent(tmp_path: Path) -> None:
    assert discover_skills(tmp_path / "absent", tmp_path / "also-absent") == []


def test_same_layer_duplicate_warns_first_wins(tmp_path: Path, caplog) -> None:
    user = tmp_path / "user"
    write_skill(user, "a-dir", "先发现", name="dup")
    write_skill(user, "b-dir", "后发现", name="dup")
    with caplog.at_level(logging.WARNING, logger="movieclaw_agent.skills"):
        skills = discover_skills(user, tmp_path / "absent")
    assert len(skills) == 1
    assert skills[0].description == "先发现"
    assert any("同名技能" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 清单渲染
# ---------------------------------------------------------------------------


def test_fragment_none_when_empty(tmp_path: Path) -> None:
    assert build_skills_fragment([], tmp_path) is None


def test_fragment_renders_and_escapes(tmp_path: Path) -> None:
    skill = Skill(
        name="a<b",
        description='描述 & "引号"',
        file_path=tmp_path / "a" / "SKILL.md",
        scope="builtin",
    )
    fragment = build_skills_fragment([skill], tmp_path / "user-skills")
    assert fragment is not None
    assert "<name>a&lt;b</name>" in fragment
    assert "&amp;" in fragment and "&quot;" in fragment
    assert f"<location>{tmp_path / 'a' / 'SKILL.md'}</location>" in fragment
    assert "read 工具" in fragment
    # 创建引导指向用户技能目录
    assert f"用户技能目录 {tmp_path / 'user-skills'}" in fragment
    # scope 不进清单
    assert "builtin" not in fragment


def test_fragment_warns_when_oversized(tmp_path: Path, caplog) -> None:
    skills = [
        Skill(
            name=f"skill-{i:03d}",
            description="很长的描述" * 40,
            file_path=tmp_path / f"s{i}" / "SKILL.md",
            scope="user",
        )
        for i in range(60)
    ]
    with caplog.at_level(logging.WARNING, logger="movieclaw_agent.skills"):
        fragment = build_skills_fragment(skills, tmp_path)
    assert fragment is not None  # 只警告不截断
    assert any("常驻开销" in r.message for r in caplog.records)
