"""标题排序键与首字母分档（海报墙的按标题排序 + A-Z 快速定位）。

为什么排序键在 Python 里算、不落库：

SQLite 对中文按 UTF-8 码点排序，「三体」排在「万里」前面纯属码点巧合，
对用户毫无意义——中文库必须按拼音排。而 SQLite 没有拼音排序规则，要在
SQL 里 ORDER BY 就得落一列排序键 + 加索引 + 在改名/重识别时维护同步。
但海报墙分页真正贵的是**聚合**（台账行、缺集判定、海报资产、序列化），
取一列标题排个序几乎不花钱：一万个条目也就是几百 KB 元组加一次 sorted()。
所以这里的取舍是——不加列、不迁移，把排序键算在内存里，等条目规模真的
大到「每次翻页排全部标题」有感了，再考虑落库。

首字母分档口径（与 Emby/Plex 的索引条一致）：
- 中文取拼音首字母（多音字/姓氏由 pypinyin 词库判定）；
- 拉丁字母取大写首字母；
- 数字、符号、以及拼不出首字母的（日文假名、韩文等）统一归入 ``#``。
"""

from __future__ import annotations

from functools import lru_cache

from pypinyin import Style, lazy_pinyin

# 落不进 A-Z 的条目统一归档到这一档（数字开头、符号开头、假名/谚文等）
OTHER_INITIAL = "#"

# A-Z + #：索引条的完整档位，顺序即排序顺序（# 排在最后，与主流媒体库一致）
INITIALS: list[str] = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + [OTHER_INITIAL]

_INITIAL_ORDER = {value: index for index, value in enumerate(INITIALS)}


# 缓存**必须无界**——容量上限是这段代码唯一踩过的性能陷阱，别改回定值。
#
# 海报墙每次翻页都要把整库标题按同一顺序排一遍。容量小于库内条目数时，
# LRU 每次淘汰的恰好是下一次马上要用的那一条，命中率**恒为零**（顺序扫描
# 的工作集大于缓存容量，是 LRU 最经典的失效形态）：曾经的 maxsize=8192 在
# 一个 9500 条目的库上实测 CacheInfo(hits=0, misses=9500) 轮轮如此，一次
# 翻页光拼音转换就要 424 ms，而缓存真正生效时是 2.2 ms——差 190 倍。
#
# 更麻烦的是它**阶跃式劣化**：条目数没过上限时一切正常，越过的那一刻整个
# 媒体库突然全线变卡，用户完全无从归因。
#
# 定义域是有界的（media_item 的标题集合），所以无界缓存不会无限增长：
# 实测 37,363 个条目的库占 2 MB。tests/api/test_library_sort_key.py 里有
# 守护测试钉住这个不变量。
@lru_cache(maxsize=None)
def title_sort_key(title: str) -> tuple[int, str]:
    """标题 → (首字母档位序号, 拼音串)。

    缓存到进程内：同一批标题在轮询里被反复排序，转换只在标题第一次出现时做。
    """
    cleaned = title.strip()
    if not cleaned:
        return _INITIAL_ORDER[OTHER_INITIAL], ""
    # 逐字转拼音后拼接：整串比较才排得对（"三国" 与 "三体" 要看第二个字）
    syllables = lazy_pinyin(cleaned, style=Style.NORMAL, errors=lambda item: item.lower())
    pinyin = "".join(syllables).lower()
    head = pinyin[:1].upper()
    initial = head if "A" <= head <= "Z" else OTHER_INITIAL
    return _INITIAL_ORDER[initial], pinyin


def title_initial(title: str) -> str:
    """标题的首字母档（A-Z 或 ``#``）——索引条分档与跳转都用它。"""
    return INITIALS[title_sort_key(title)[0]]
