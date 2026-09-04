"""OurBits 分类参数回归测试。

OurBits 的分类筛选只认重复的 ``cat[]=401&cat[]=402``，标准 NexusPHP 发出的
``cat401=1&cat402=1`` 会被站点忽略，带分类的搜索退化成全站结果。

两层保障：
1. 配置层：ourbits.yaml 绑定到 ``OurBitsSite``，且授权类型只声明 cookie。
2. 行为层：用 Mock HttpClient 验证 ``search`` / ``list_torrents`` 最终发出的
   请求参数是 ``cat[]`` 列表，且不再出现 ``cat4xx`` 这类键。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from movieclaw_tracker import load_all_sites
from movieclaw_tracker.models import SearchQuery, TorrentCategory
from movieclaw_tracker.registry import get_site_config
from movieclaw_tracker.sites.custom.ourbits import OurBitsSite


@pytest.fixture(scope="module", autouse=True)
def _load_configs() -> None:
    load_all_sites()


def test_ourbits_config_uses_custom_class() -> None:
    config = get_site_config("ourbits")
    assert config.site_class is OurBitsSite
    assert config.supported_auth_types == ("cookie",)


def _mock_site() -> tuple[OurBitsSite, AsyncMock]:
    config = get_site_config("ourbits")
    response = MagicMock()
    response.text = "<html><body></body></html>"
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    site = OurBitsSite(
        selectors=config.selectors,
        category_map=config.category_map,
        site_id=config.site_id,
        base_url=config.base_url,
        client=client,
        auth_manager=MagicMock(),
    )
    return site, client.get


async def test_search_sends_repeated_cat_array() -> None:
    site, get = _mock_site()
    await site.search(SearchQuery(keyword="test", categories=[TorrentCategory.MOVIE]))

    params = get.call_args.kwargs["params"]
    assert params["cat[]"] == ["401", "402"]
    assert not any(key.startswith("cat4") for key in params)


async def test_list_torrents_merges_multiple_categories() -> None:
    site, get = _mock_site()
    await site.list_torrents(categories=[TorrentCategory.MOVIE, TorrentCategory.DOCUMENTARY])

    params = get.call_args.kwargs["params"]
    assert params["cat[]"] == ["401", "402", "410"]


async def test_no_categories_sends_no_cat_param() -> None:
    site, get = _mock_site()
    await site.search(SearchQuery(keyword="test"))

    params = get.call_args.kwargs["params"]
    assert "cat[]" not in params
