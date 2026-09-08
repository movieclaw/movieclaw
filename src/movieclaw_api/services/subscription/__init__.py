"""订阅域子包：订阅 CRUD 与工单生长、匹配评估、投递、缺口搜索、对账、体检。

原先六个平级模块（subscription / subscription_matching / download_dispatch /
wanted_search / wanted_fulfillment / subscription_health）互相成环、靠函数内
延迟导入撑着——它们本就是一个概念包，这里补上目录边界：

- ``core``               订阅 CRUD、工单（wanted）生长与状态机
- ``matching``           被动匹配与主动搜索共用的评估管道（evaluate_and_dispatch）
- ``dispatch``           投递：认领工单 → 取种提交下载器 → 台账推进
- ``wanted_search``      主动缺口搜索（定时任务 + 即时踢）
- ``wanted_fulfillment`` 库存对账关单（入库/认领后关闭对应工单）
- ``health``             订阅链路体检（投递 → 转移 → 入库逐段预演）
- ``release_forecast``   从种子索引推导追新发布时间，并提供站点临时探测点
- ``replacement``        无进度种子的跨站搜索、试用晋升与安全清理

**包外只允许从本 ``__init__`` 导入**（下方显式导出的公共接口）；包内模块
之间用完整子模块路径互相引用。守护测试（tests/api/test_subscription_package.py）
会拦下绕过公共接口直捅子模块的外部 import。
"""

from movieclaw_api.services.subscription import wanted_search
from movieclaw_api.services.subscription.core import (
    FUTURE_GRACE,
    MOVIE_RELEASE_GRACE,
    SubscriptionService,
    expected_units,
    movie_schedule,
    recompute_subscription_status,
    schedule_for,
)
from movieclaw_api.services.subscription.dispatch import dispatch, preview_dispatch_route
from movieclaw_api.services.subscription.health import pipeline_health
from movieclaw_api.services.subscription.matching import (
    DISPATCH_RETRY_DELAY,
    MATCH_BATCH_SIZE,
    REFRESH_PER_TICK,
    evaluate_and_dispatch,
    load_match_context,
    units_text,
    upgrade_ready,
)
from movieclaw_api.services.subscription.release_forecast import (
    FORECAST_MIN_INTERVAL,
    FORECAST_VERSION,
    effective_forecast_probe_at,
    forecast_probe_times_by_site,
    next_forecast_probe_times_by_wanted,
    refresh_release_forecasts,
)
from movieclaw_api.services.subscription.replacement import (
    fail_trial,
    promote_trial,
    quality_not_lower,
    reconcile_pending_cleanup,
    replacement_backoff,
    request_replacement,
    run_replacement_search,
    try_replacement_candidates,
)
from movieclaw_api.services.subscription.upgrade import (
    run_upgrade_round,
    upgrade_attempt_wanted_rows,
    upgrading_counts,
)
from movieclaw_api.services.subscription.wanted_fulfillment import (
    close_fulfilled_wanted,
    reopen_unfulfilled_wanted,
)
from movieclaw_api.services.subscription.wanted_search import kick_search_soon, search_wanted

__all__ = [
    "DISPATCH_RETRY_DELAY",
    "FORECAST_MIN_INTERVAL",
    "FORECAST_VERSION",
    "FUTURE_GRACE",
    "MATCH_BATCH_SIZE",
    "MOVIE_RELEASE_GRACE",
    "REFRESH_PER_TICK",
    "SubscriptionService",
    "close_fulfilled_wanted",
    "reopen_unfulfilled_wanted",
    "dispatch",
    "effective_forecast_probe_at",
    "evaluate_and_dispatch",
    "expected_units",
    "fail_trial",
    "forecast_probe_times_by_site",
    "kick_search_soon",
    "load_match_context",
    "movie_schedule",
    "next_forecast_probe_times_by_wanted",
    "pipeline_health",
    "preview_dispatch_route",
    "promote_trial",
    "quality_not_lower",
    "reconcile_pending_cleanup",
    "run_upgrade_round",
    "upgrading_counts",
    "upgrade_attempt_wanted_rows",
    "upgrade_ready",
    "replacement_backoff",
    "recompute_subscription_status",
    "refresh_release_forecasts",
    "request_replacement",
    "run_replacement_search",
    "schedule_for",
    "search_wanted",
    "units_text",
    "try_replacement_candidates",
    "wanted_search",
]
