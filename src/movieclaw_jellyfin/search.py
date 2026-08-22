"""搜索匹配口径：逐字对齐 Jellyfin 服务端，不做任何本地增强。

协议依据（`jellyfin/jellyfin` @ `929834c`）：

- `src/Jellyfin.Extensions/StringExtensions.cs:158` `GetCleanValue()`
  —— 去变音符 → 转小写 → 标点/符号替换成空格 → 折叠连续空格并去首尾；
- `Jellyfin.Server.Implementations/Item/BaseItemRepository.TranslateQuery.cs:178`
  —— 命中条件是 `CleanName.Contains(cleanedTerm) || OriginalTitle LIKE %term%`，
  其中 `CleanName` 是入库时对 `Name` 预算的 `GetCleanValue()`；
- `Jellyfin.Server.Implementations/Item/PeopleRepository.cs:338`
  —— `/Persons` 的 `NameContains` **不走** GetCleanValue，是裸的
  `Name.ToUpper().Contains(term.ToUpper())`。两条路径口径不同是 Jellyfin
  自身的历史差异，这里照抄，不做统一。

有意不做的事（真 Jellyfin 也不做，做了就是偏离）：简繁折叠、拼音、别名匹配。
搜「三體」命不中「三体」、搜「santi」命不中「三体」都是对齐后的预期行为。

顺带说明一处 Jellyfin 的死代码：`TranslateQuery.cs:182` 会检查 cleaned term
里有没有 `% _ [ ] ^` 来决定走 LIKE 通配分支，但 `GetCleanValue()` 已经把这五个
字符全替换成空格了，该分支恒不成立。这里因此不实现通配语义。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

_SPACES = re.compile(r"\s+")


def _remove_diacritics(value: str) -> str:
    """去变音符：NFD 拆分后丢弃所有非间距记号（Unicode 类别 Mn）。

    等价于 .NET 侧 `Diacritics.Extensions.RemoveDiacritics`。Jellyfin 在这之前
    还剥了落单的代理项字符（`NonConformingUnicodeRegex`），Python 的 str 拿不到
    落单代理项，无需对应处理。
    """
    decomposed = unicodedata.normalize("NFD", value)
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return unicodedata.normalize("NFC", stripped)


# 库里的标题会被反复归一化（每次搜索一遍全部候选），而标题本身极少变动，
# 缓存住省掉重复的 NFD 拆分。上限按「万级条目 + 万级分集名」留量。
@lru_cache(maxsize=32768)
def clean_value(value: str | None) -> str:
    """`GetCleanValue()` 的等价实现。空/纯空白原样返回（对齐 C# 的提前返回）。"""
    if not value or not value.strip():
        return value or ""
    cleaned = _remove_diacritics(value).lower()
    # C# 是 [^\p{L}\p{N}\s] → " "。Python 的 re 没有 \p{...}，逐字符按 Unicode
    # 类别判定才能等价：\w 含下划线、又不含 Nl/No（如 ½、Ⅷ），用不得。
    cleaned = "".join(
        c if (c.isspace() or unicodedata.category(c)[0] in ("L", "N")) else " "
        for c in cleaned
    )
    return _SPACES.sub(" ", cleaned).strip()


@dataclass(frozen=True)
class SearchTerm:
    """预处理好的搜索词。一次请求算一次，避免在每条候选上重复归一化。"""

    raw: str
    cleaned: str
    lowered: str

    @property
    def empty(self) -> bool:
        return not self.cleaned and not self.lowered

    def matches(self, name: str | None, original_title: str | None = None) -> bool:
        """条目级命中判定（BaseItemRepository.TranslateQuery.cs:178-193）。

        ``name`` 比对的是它的 CleanName（这里现算，movieclaw 没有预存列）；
        ``original_title`` 走 LIKE，即大小写不敏感的裸子串。
        """
        if self.cleaned and name and self.cleaned in clean_value(name):
            return True
        return bool(
            self.lowered and original_title and self.lowered in original_title.lower()
        )


def parse_term(raw: str | None) -> SearchTerm | None:
    """把 query 里的 searchTerm 转成预处理形态；未传/空串返回 None。"""
    if not raw:
        return None
    return SearchTerm(raw=raw, cleaned=clean_value(raw), lowered=raw.lower())


def person_name_matches(name: str | None, term_raw: str) -> bool:
    """`/Persons` 的 NameContains 口径（PeopleRepository.cs:338-342）。

    注意这里**不过** GetCleanValue：真 Jellyfin 对人物就是裸的大写子串比对。
    """
    if not name or not term_raw:
        return False
    return term_raw.upper() in name.upper()
