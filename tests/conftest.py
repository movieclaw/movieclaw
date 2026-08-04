"""测试全局配置：从仓库根目录的 .env 加载环境变量。

集成测试使用的真实站点 Cookie 通过环境变量传入，避免敏感凭据进入
git 历史。本地开发时复制 .env.example 为 .env 并填写即可，CI 环境
中保持环境变量为空，相关测试会自动跳过。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"

# 测试期间产生的运行日志统一写进临时目录：create_app 会装配按天落盘的
# 日志 Handler（见 core/logging.py），不隔离的话测试会在仓库 data/logs
# 下留下日志文件。先于 .env 加载设置，测试环境始终生效。
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="movieclaw-test-logs-"))


def _resolve_ner_model_dir() -> None:
    """git worktree 里跑测试时，把 NER 模型指向主仓库的 data 目录。

    模型文件是运行期数据（不入 git，从 Release 下载到 data/models/torrent-ner），
    而 worktree 只共享代码不共享 data/——不做这层回落，enrich 的片名/季集字段
    在 worktree 里全空，库扫描/enrich 语料等一批测试会稳定失败且原因很隐蔽。
    主仓库位置由 git 公共目录（.git 所在处）推导；主仓库也没有模型时保持原状，
    由 enrich 侧照常打"请下载模型"的警告。
    """
    if os.environ.get("MOVIECLAW_NER_DIR"):
        return  # 显式指定的优先
    local = _PROJECT_ROOT / "data/models/torrent-ner"
    if (local / "model.int8.onnx").is_file():
        # 当前检出自己就有模型（主仓库常态）：写成绝对路径，
        # 免得从其他 cwd 跑 pytest 时 enrich 的相对默认路径落空
        os.environ["MOVIECLAW_NER_DIR"] = str(local)
        return
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return
    candidate = (_PROJECT_ROOT / common).resolve().parent / "data/models/torrent-ner"
    if (candidate / "model.int8.onnx").is_file():
        os.environ["MOVIECLAW_NER_DIR"] = str(candidate)


_resolve_ner_model_dir()


def _load_env_file(path: Path) -> None:
    """加载简单的 .env 文件，将 KEY=VALUE 注入 os.environ。

    设计要点：
    - 已存在的环境变量优先，不会被 .env 覆盖（CI/手动 export 的值更权威）；
    - 仅支持 KEY=VALUE 形式，忽略空行与 # 开头的注释；
    - 自动剥离值两端的成对单/双引号，方便粘贴含分号的 cookie 串。

    避免新增依赖（python-dotenv），保持测试侧零依赖。
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_env_file(_ENV_FILE)


import pytest  # noqa: E402  须在环境变量装配之后导入


@pytest.fixture(autouse=True)
def _mute_instant_search_kick(monkeypatch):
    """默认打桩订阅写操作触发的即时缺口搜索（fire-and-forget）。

    service 层收口后，创建/调整/恢复/缺失重下都会踢一次 search_wanted；
    单测环境没有可用站点，放任它跑会写入"搜索失败"活动、污染断言。
    需要验证搜索行为的测试请显式 ``await search_wanted()``（管线测试即如此）；
    需要验证"踢了没踢"的测试可再次 monkeypatch 覆盖本桩。
    """
    monkeypatch.setattr(
        "movieclaw_api.services.subscription.wanted_search.kick_search_soon", lambda: None
    )


@pytest.fixture(autouse=True)
def _offline_tmdb_singleton(request, monkeypatch):
    """媒体身份层的 TmdbClient 单例在测试环境一律离线（不访问真实 TMDB）。

    背景：库路由决策（library/routing.gather_facts）等链路直接取
    ``get_tmdb_client()`` 进程级单例，测试往服务里注入的 fake client
    覆盖不到它。开发机 .env 里带 TMDB_API_KEY 时，订阅等服务级测试会
    经它发出**真实** TMDB 请求：单例连同 HTTP 连接池被创建在首个用例
    的事件循环上，循环随用例结束关闭，连接却还留在池里；之后第一个跑
    完整 lifespan 的测试在关停时 ``close_media_service()`` 去关这条
    死循环上的连接，偶发 ``RuntimeError: Event loop is closed``
    （teardown 报错，且与测试顺序/网络时机相关，极难排查）。

    这里按用例把单例换成独立的离线实例（503 快速失败，不重试）：
    既掐断单测的真实外网流量，也保证单例及其连接不跨事件循环存活。
    monkeypatch 会在用例结束时把单例槽位还原（通常是 None）。
    集成测试可能需要真实单例，跳过注入。
    """
    if request.node.get_closest_marker("integration"):
        yield
        return
    import httpx

    from movieclaw_api.services import media_discover
    from movieclaw_media.tmdb import TmdbClient

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"note": "测试环境不访问真实 TMDB"})

    monkeypatch.setattr(
        media_discover,
        "_tmdb_client",
        TmdbClient("test-offline-key", transport=httpx.MockTransport(handler)),
    )
    yield


@pytest.fixture(autouse=True)
def _offline_image_proxy(monkeypatch):
    """刮削管线的图片资产下载在测试环境一律快速失败（不访问外网图床）。

    管线对单张失败本就优雅降级（字段保持 NULL、下次刷新自愈），这里只是
    把"真实网络超时"换成即时异常，保证测试快速且不依赖外网。直接构造
    ImageProxy 实例（注入 MockTransport）的专项测试不经单例，不受影响。
    """

    class _Offline:
        async def fetch(self, url: str):
            raise RuntimeError("测试环境不访问外网图床")

    monkeypatch.setattr(
        "movieclaw_api.services.image_proxy.get_image_proxy", lambda: _Offline()
    )
