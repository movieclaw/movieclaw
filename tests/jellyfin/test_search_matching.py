"""搜索匹配口径单测：逐条锚定 Jellyfin 源码的行为，防止"顺手加个中文友好"。

对照物是 `jellyfin/jellyfin` @ `929834c`：
- `src/Jellyfin.Extensions/StringExtensions.cs:158` GetCleanValue
- `Jellyfin.Server.Implementations/Item/BaseItemRepository.TranslateQuery.cs:178`
- `Jellyfin.Server.Implementations/Item/PeopleRepository.cs:338`
"""

from __future__ import annotations

import pytest

from movieclaw_jellyfin.search import (
    clean_value,
    parse_term,
    person_name_matches,
)


class TestCleanValue:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # 去变音符（RemoveDiacritics）
            ("Aquí huele a muerto", "aqui huele a muerto"),
            ("Amélie", "amelie"),
            # 标点/符号 → 空格，再折叠空白并去首尾
            ("Spider-Man: No Way Home", "spider man no way home"),
            ("  Mission:   Impossible  ", "mission impossible"),
            ("WALL·E", "wall e"),
            # 下划线在 C# 的 [^\p{L}\p{N}\s] 里算标点，Python 的 \w 里不算，
            # 逐字符按 Unicode 类别判定才对得上
            ("foo_bar", "foo bar"),
            # 数字类保留（\p{N} 含 Nl/No，不只是 Nd）
            ("Iron Man 3", "iron man 3"),
            ("½", "½"),
            # CJK 不受影响，也不做任何简繁折叠
            ("三体", "三体"),
            ("三體", "三體"),
        ],
    )
    def test_clean_value(self, raw: str, expected: str) -> None:
        assert clean_value(raw) == expected

    def test_blank_returns_input(self) -> None:
        """空/纯空白原样返回，对齐 C# 的 IsNullOrWhiteSpace 提前返回。"""
        assert clean_value("") == ""
        assert clean_value("   ") == "   "
        assert clean_value(None) == ""


class TestSearchTermMatch:
    def test_matches_clean_name(self) -> None:
        term = parse_term("spider man")
        assert term is not None
        assert term.matches("Spider-Man: No Way Home")

    def test_term_punctuation_is_normalized_too(self) -> None:
        """搜索词自身也过 GetCleanValue，所以带不带连字符都命中。"""
        assert parse_term("spider-man").matches("Spider Man")
        assert parse_term("spider man").matches("Spider-Man")

    def test_matches_original_title_case_insensitively(self) -> None:
        """OriginalTitle 走 LIKE：裸子串、大小写不敏感、不过 GetCleanValue。"""
        assert parse_term("INCEPTION").matches("盗梦空间", "Inception")
        assert parse_term("cept").matches("盗梦空间", "Inception")

    def test_diacritics_folded_on_both_sides(self) -> None:
        assert parse_term("aqui").matches("Aquí huele a muerto")
        assert parse_term("aquí").matches("Aqui huele a muerto")

    def test_no_simplified_traditional_folding(self) -> None:
        """有意不做简繁折叠——真 Jellyfin 也不做，做了就是偏离。"""
        assert not parse_term("三體").matches("三体")
        assert not parse_term("三体").matches("三體")

    def test_no_pinyin(self) -> None:
        """有意不做拼音匹配。"""
        assert not parse_term("santi").matches("三体")

    def test_aliases_are_not_searched(self) -> None:
        """别名不参与匹配：Jellyfin 只看 Name(CleanName) 与 OriginalTitle。

        movieclaw 早期实现会连 media_item.aliases 一起比对，命中集因此是
        TMDB 别名表的函数（搜 The 能命中三百多条「The Proud General」之类）。
        这里锁死为「别名不参与」——匹配面由 matches() 的两个入参决定。
        """
        term = parse_term("Nirvana in Fire")
        assert term is not None
        assert not term.matches("琅琊榜", "琅琊榜")

    def test_percent_and_underscore_are_literal(self) -> None:
        """搜索词里的 LIKE 元字符不是通配符。

        Jellyfin 里那段 SearchWildcardTerms 分支是死代码：GetCleanValue 已把
        % _ [ ] ^ 全替换成空格，检查恒不成立。这里同样不实现通配语义。
        """
        assert not parse_term("a%c").matches("abc")
        assert not parse_term("a_c").matches("abc")

    def test_empty_term_parses_to_none(self) -> None:
        assert parse_term(None) is None
        assert parse_term("") is None


class TestPersonNameMatch:
    def test_plain_case_insensitive_substring(self) -> None:
        assert person_name_matches("Leonardo DiCaprio", "leonardo")
        assert person_name_matches("Leonardo DiCaprio", "DICAPRIO")
        assert person_name_matches("克里斯托弗·诺兰", "诺兰")

    def test_does_not_use_clean_value(self) -> None:
        """`/Persons` 的 NameContains 是裸的大写子串比对，不去变音符、不去标点。

        与条目搜索口径不同是 Jellyfin 自身的历史差异（两条路径分别落在
        PeopleRepository 与 BaseItemRepository），照抄不统一。
        """
        assert not person_name_matches("Amélie Nothomb", "amelie")
        assert person_name_matches("Amélie Nothomb", "amélie")
        # 标点保留：连字符不会被当成空格
        assert not person_name_matches("Jean-Luc Godard", "jean luc")
        assert person_name_matches("Jean-Luc Godard", "jean-luc")

    def test_empty_inputs(self) -> None:
        assert not person_name_matches(None, "x")
        assert not person_name_matches("x", "")
