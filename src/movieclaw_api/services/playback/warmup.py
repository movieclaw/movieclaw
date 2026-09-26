"""起播预热（docs/design/web-player.md §6.10）。

「首次起播卡缓冲、重进就好」的另一半解法：决策要用的关键帧采样（三段共约
90 秒码流的读取）首次昂贵、之后有缓存。把它提前到**用户还在条目详情页看
简介**的那几秒里后台做掉，点播放时缓存直接命中。

**不预热内封字幕**：字幕抽取要通读整个容器（Remux 几十 GB，媒体库在 NFS
上还要全部走网络），而详情接口并不等于「用户马上要播」——列表巡检、脚本、
UI 测试都会批量打开详情。2026-09 NAS 实测：UI 测试逐个打开几十部电影的
详情，每部都起一次整文件通读，并发平分带宽、120 秒全部超时作废，一天白读
几百 GB。字幕改为与 Jellyfin 相同的按需抽取（播放器真正请求字幕时才抽，
见 services/media_extract），首播时字幕稍后出现，视频起播不受影响。

**只替「可能直通」的网页客户端采样**：关键帧密度只服务于网页播放器的直通档
（档 1/2，视频 ``copy``）。iOS App 的 MPV/系统播放器、Infuse 等全解码客户端
直读原文件，网页端能直放或注定转码的片子，都用不上它。详情接口本身不知道
客户端的解码能力，所以播放决策接口收到能力快照时按「身份 + User-Agent」
记一份（``remember_capability``），详情页预热查到这个客户端的能力、且用它
跑 ``needs_keyframe_probe``（纯判定，不读盘）结论为「可能直通」才采样；查
不到（没在这个浏览器上播过、App、脚本、UI 测试）就不预热，退回首播现场
采样。2026-09 实测：iOS 打开一部 15 GB 的 mkv 详情，预热白读 171 MB。

约束：

- **只预热文件数很少的条目**（电影/单文件）。剧集详情页猜不到用户要播
  哪一集，把几十个文件全预热是几 GB 的无谓 IO；剧集连播场景第二集起本来
  就有缓存与页缓存，收益也小。
- **同一条目并发触发只做一次**（防止详情页反复刷新叠加探测 IO）；探测
  自身的结果缓存（media_probe）保证真正的幂等。
- 关键帧探测放线程池；全部吞掉预热异常——预热失败的后果只是回到「首播
  现场探测」的旧行为，绝不能影响详情页本身。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import OrderedDict

from movieclaw_api.services.media_probe import probe_keyframe_interval
from movieclaw_db.models import LibraryFile
from movieclaw_playback.capability import ClientCapability
from movieclaw_playback.decide import needs_keyframe_probe
from movieclaw_playback.profile import media_profile_from_file

logger = logging.getLogger("movieclaw_api.playback.warmup")

#: 超过这个文件数的条目不预热（剧集整季详情页）。
_MAX_FILES = 4

#: 正在预热的条目 id 集合（进程内去重）。
_in_flight: set[int] = set()
# 保存任务句柄：用户真正开始播放时，详情页遗留的预热应立即退出。
_tasks: dict[int, asyncio.Task[None]] = {}

#: 记住的客户端能力上限（按「身份 + User-Agent」计，最久未用的先淘汰）。
_MAX_CLIENTS = 256
#: 客户端 → 最近一次播放决策上报的解码能力快照。
_capabilities: OrderedDict[str, ClientCapability] = OrderedDict()


def identity_of(principal) -> str:  # noqa: ANN001 — auth.Principal，免得预热模块依赖鉴权层
    """客户端身份的主体部分：主体类型 + 名字（不同类型的主体可能同名）。"""
    return f"{principal.kind}:{principal.name}"


def _client_key(identity: str, user_agent: str | None) -> str | None:
    # 没有 User-Agent 就分不清同一账号下的浏览器与 App，宁可不记、不预热。
    return f"{identity}\n{user_agent}" if user_agent else None


def remember_capability(
    identity: str, user_agent: str | None, capability: ClientCapability
) -> None:
    """播放决策接口调用：记下这个客户端的解码能力，供之后的详情页预热判断。"""
    key = _client_key(identity, user_agent)
    if key is None:
        return
    _capabilities[key] = capability
    _capabilities.move_to_end(key)
    while len(_capabilities) > _MAX_CLIENTS:
        _capabilities.popitem(last=False)


def _known_capability(identity: str, user_agent: str | None) -> ClientCapability | None:
    key = _client_key(identity, user_agent)
    return _capabilities.get(key) if key is not None else None


async def _warm_file(file: LibraryFile) -> None:
    """预热一个文件：关键帧采样放线程池（结果进 media_probe 的缓存）。"""
    interval = await asyncio.to_thread(
        probe_keyframe_interval, file.file_path, file.duration_seconds
    )
    logger.debug("起播预热完成：file_id=%s 关键帧间隔=%s", file.id, interval)


def schedule(
    media_item_id: int,
    files: list[LibraryFile],
    *,
    identity: str,
    user_agent: str | None,
) -> None:
    """后台预热一个条目的在位文件。

    跳过：文件多（剧集）、已在预热中、不认识这个客户端（没上报过解码能力）。
    """
    if not files or len(files) > _MAX_FILES:
        return
    capability = _known_capability(identity, user_agent)
    if capability is None:
        return
    if media_item_id in _in_flight:
        return
    _in_flight.add(media_item_id)

    async def run() -> None:
        try:
            # 延迟导入：plan 依赖设置存储等较重的模块，预热模块保持轻量可测。
            from movieclaw_api.services.playback.plan import load_policy

            policy = await load_policy()
            for file in files:
                # 原盘的关键帧密度来自 CLPI（不读码流），不需要预热；
                # 其余文件先用纯判定问一句「这个客户端放它会不会走直通」。
                if file.is_disc() or not needs_keyframe_probe(
                    media_profile_from_file(file), capability, policy
                ):
                    continue
                result = _warm_file(file)
                # 保留对旧版同步探测替身的兼容；正式实现是可取消的协程。
                if inspect.isawaitable(result):
                    await result
        except Exception:  # noqa: BLE001 — 预热失败绝不能影响详情页与播放
            logger.debug("起播预热失败：media_item_id=%s", media_item_id, exc_info=True)
        finally:
            _in_flight.discard(media_item_id)
            _tasks.pop(media_item_id, None)

    try:
        task = asyncio.get_running_loop().create_task(run())
    except RuntimeError:  # 没有事件循环（同步上下文）时跳过
        _in_flight.discard(media_item_id)
        return
    _tasks[media_item_id] = task


def cancel(media_item_id: int) -> None:
    """取消该条目仍在进行的详情页预热，避免与正式播放抢 IO/进程槽位。"""
    task = _tasks.get(media_item_id)
    if task is not None and not task.done():
        task.cancel()
