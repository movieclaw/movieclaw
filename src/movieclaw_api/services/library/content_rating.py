"""分级串 → 年龄下限（docs/design/library-filtering.md F5）。

TMDB 的 ``content_rating`` 是**各国自己的一套符号**（美国 PG-13、英国 15、
德国 FSK 16、日本 R15+…），彼此不可比。要把它当成"儿童看不看得了"的判据，
必须先折算成一个共同刻度——这里选**年龄**，因为各国的分级本来就是按年龄定的，
折算过程不引入新概念。

三条分寸：

1. **只认得出来的才认。** 表里没有的串一律当"未知"（返回 None），
   由调用方按 ``allow_unrated`` 决定给不给看。猜一个数字比说不知道危险：
   把 ``R18+`` 猜成 18 是对的，把某国的 ``T`` 猜成 0 就可能把成人片放给小孩。
2. **同一符号在不同体系里含义冲突时，取更严的那个。** 例如 ``PG``
   在美国是"建议家长陪同"（约 8 岁），在英国也是 8——不冲突；而裸数字
   （``12``/``16``）在多数体系里就是年龄本身，直接用。
3. **不做地区推断。** 我们不知道这条分级来自哪个国家（``_parse_certification``
   按优先级取了第一个有数据的地区就丢掉了国家码），所以表按符号查，
   不按 (国家, 符号) 查。这是现状带来的精度上限，写在这里而不是假装没有。
"""

from __future__ import annotations

import re

#: 分级符号 → 建议年龄下限。键一律大写、去空白后比对。
#:
#: 覆盖的体系：美国电影（MPA）、美国电视（TV Parental Guidelines）、英国（BBFC）、
#: 德国（FSK）、法国（CNC）、日本（映伦）、韩国（KMRB）、澳洲（ACB）。
#: 中国大陆没有分级制度，所以中文片多数是未分级——那正是 ``allow_unrated``
#: 存在的原因。
_RATING_AGES: dict[str, int] = {
    # —— 美国电影（MPA）——
    "G": 0,
    "PG": 8,
    "PG-13": 13,
    "R": 17,
    "NC-17": 18,
    # —— 美国电视 ——
    "TV-Y": 0,
    "TV-Y7": 7,
    "TV-Y7-FV": 7,
    "TV-G": 0,
    "TV-PG": 8,
    "TV-14": 14,
    "TV-MA": 17,
    # —— 英国（BBFC）。U=Universal，12A=影院陪同版 ——
    "U": 0,
    "UC": 0,
    "12A": 12,
    "15": 15,
    "18": 18,
    "R18": 18,
    # —— 德国（FSK）：数字体系，走下面的裸数字分支 ——
    # —— 法国（CNC）：TP=tous publics ——
    "TP": 0,
    "T": 0,
    # —— 日本（映伦）——
    "PG12": 12,
    "R15+": 15,
    "R18+": 18,
    # —— 韩国（KMRB）——
    "ALL": 0,
    "19": 19,
    # —— 澳洲（ACB）：M=成熟观众建议，MA15+ 起为法定限制 ——
    "M": 15,
    "MA15+": 15,
    "R18+AU": 18,
    "X18+": 18,
    # —— 常见的"未分级"写法：明确当作未知，不要因为它是个已知字符串就放行 ——
}

#: 裸数字分级（德国 FSK、韩国、北欧、荷兰…）：数字本身就是年龄
_BARE_AGE = re.compile(r"^(\d{1,2})\+?$")


def rating_age(raw: str | None) -> int | None:
    """一条分级串 → 年龄下限；认不出来返回 ``None``（**不猜**）。"""
    if not raw:
        return None
    key = re.sub(r"\s+", "", raw).upper()
    # 少数来源会带体系前缀（FSK16 / KMRB12）：剥掉之后就是裸数字
    key = re.sub(r"^(FSK|KMRB|CNC|BBFC)", "", key)
    if key in _RATING_AGES:
        return _RATING_AGES[key]
    match = _BARE_AGE.match(key)
    if match:
        age = int(match.group(1))
        # 21 岁以上的"分级"多半是别的东西（年份、集数），不当年龄用
        return age if age <= 21 else None
    return None


def ratings_at_or_below(age: int) -> list[str]:
    """年龄上限 → 允许的分级串全集（含各种写法）。

    查询侧要的是"哪些 ``content_rating`` 的值是允许的"，而不是"这个值折算成
    几岁"——**折算放在 Python 里做一次，SQL 只做一次 IN**。反过来（把折算写进
    SQL 的 CASE）既难读又没法走索引。分级取值就那么几十个，全集很小。
    """
    allowed = [key for key, value in _RATING_AGES.items() if value <= age]
    # 裸数字：0..age 的每一个都可能出现在库里（FSK 0/6/12/16/18、韩国 12/15/19…）
    allowed.extend(str(n) for n in range(age + 1))
    allowed.extend(f"{n}+" for n in range(age + 1))
    return allowed
