"""OurBits（我堡 / ourbits.club）自定义站点适配器。

OurBits 基于 NexusPHP 框架，列表解析、分页、用户资料全部沿用标准实现，
只有一处与框架默认行为不同，需要覆盖：

1. **分类筛选参数是重复的数组形式**
   标准 NexusPHP 每个分类一个独立参数（``?cat401=1&cat402=1``），而
   OurBits 只认重复的 ``cat[]``（``?cat[]=401&cat[]=402``）。站点对
   ``cat401=1`` 这种写法直接忽略，带分类的搜索和列表会退化成全站结果。
   因此重写 ``_apply_category_params``，把所有站点分类 ID 收进一个列表交给
   ``cat[]``，由 HttpClient 展开为重复参数。
"""

from __future__ import annotations

from typing import Any

from movieclaw_tracker.frameworks.nexusphp import NexusPHPSite
from movieclaw_tracker.models import TorrentCategory


class OurBitsSite(NexusPHPSite):
    """OurBits 站点：仅重写分类参数的拼法。

    选择器、分类映射、分页偏移等仍由 ``sites/configs/ourbits.yaml`` 配置，
    不在此重写。
    """

    def _apply_category_params(
        self,
        params: dict[str, Any],
        categories: list[TorrentCategory] | None,
    ) -> None:
        if not categories or not self.category_map:
            return

        site_ids: list[str] = []
        for category in categories:
            site_ids.extend(self.category_map.get(category, []))
        if site_ids:
            params["cat[]"] = site_ids
