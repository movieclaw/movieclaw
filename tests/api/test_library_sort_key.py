"""标题排序键缓存的守护测试——钉住「缓存必须装得下整库」这个不变量。

为什么值得一个专门的测试文件：这个不变量一旦破掉，功能完全正常、测试全绿，
只有大库用户会突然遇到整个媒体库变卡，而且没有任何报错指向这里。用例本身
不测排序正确性（那是 test_library_wall_sort 的事），只测**缓存有没有真的
命中**——这是性能，但它是能被断言的性能。
"""

from __future__ import annotations

from movieclaw_api.services.library.sort_key import (
    INITIALS,
    title_initial,
    title_sort_key,
)

# 取一个明显大于历史上那个 8192 上限的规模：海报墙的真实压力就来自这里
_LIBRARY_SCALE = 12_000


def _fake_titles(count: int) -> list[str]:
    """构造 count 个互不相同的标题，中英混排（与真实媒体库的分布一致）。"""
    heads = ["长安", "流浪", "无间", "琅琊", "沙丘", "Dune", "Arrival", "Severance"]
    return [f"{heads[i % len(heads)]}{i}" for i in range(count)]


def test_排序键缓存能装下整库_不发生_lru_颠簸() -> None:
    """整库标题按同一顺序重排两轮，第二轮必须全部命中缓存。

    这正是海报墙翻页的真实访问形态：同样的标题、同样的顺序、反复排序。
    容量小于条目数时 LRU 每次淘汰的恰好是下一个要用的，命中数会是 0——
    历史上的 maxsize=8192 就是这样让 9500 条目的库每次翻页多花 400 ms。
    """
    titles = _fake_titles(_LIBRARY_SCALE)

    title_sort_key.cache_clear()
    sorted(titles, key=title_sort_key)          # 第一轮：全部 miss，理所当然
    first_round = title_sort_key.cache_info()
    assert first_round.hits == 0
    assert first_round.misses == _LIBRARY_SCALE

    sorted(titles, key=title_sort_key)          # 第二轮：必须全部命中
    second_round = title_sort_key.cache_info()
    assert second_round.misses == _LIBRARY_SCALE, (
        f"第二轮又发生了 {second_round.misses - _LIBRARY_SCALE} 次未命中："
        "缓存装不下整库，翻页会重算整库拼音"
    )
    assert second_round.hits == _LIBRARY_SCALE


def test_缓存容量无上限() -> None:
    """直接断言容量本身——比只测命中率更早、更明确地拦住改回定值的改动。"""
    assert title_sort_key.cache_info().maxsize is None, (
        "title_sort_key 的缓存容量被设了上限；条目数超过它之后命中率会归零，"
        "详见函数上方注释"
    )


def test_首字母分档仍然正确() -> None:
    """改缓存不该动语义：中文取拼音首字母，拉丁取大写首字母，其余归 #。"""
    assert title_initial("长安十二时辰") == "C"
    assert title_initial("Dune") == "D"
    assert title_initial("2001太空漫游") == "#"
    assert all(initial in INITIALS for initial in map(title_initial, _fake_titles(50)))
