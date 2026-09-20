"""跨通道主动推送:把系统事件文本(可附配图)扇出到所有已绑定的 IM 通道。

调用方(订阅投递/入库对账等)只管 fire-and-forget:``notify_channels(text)``
内部起后台任务,任何通道失败都只记日志,绝不影响业务主链路。

配图链路:业务方传 ``image_url``(TMDB 图床完整 URL),这里经 ImageCache
取图(本地缓存命中免回源,回源复用图片代理的 SSRF 防护与代理出口),字节
随出站信封下发;Telegram/Discord 图文一条发出,微信 adapter 无发图能力,
发送泵自动退回纯文本。取图失败同样退纯文本——图可以丢,推送不能断。

配图选横版剧照而非竖版海报:IM 客户端的图片气泡宽度随文案长短伸缩,
竖图会跟着缩放导致不同推送显示大小不一;横图天然顶满气泡最大宽度,
渲染规格恒定(Telegram 实测结论,2026-08)。

微信通道的绑定用户与 TG/Discord 一样从 channel_account.bound_user_id 取,
出站统一经各账号 dispatcher 的发送泵(顺序与限流集中一处)。
"""

from __future__ import annotations

import asyncio
import logging

from movieclaw_channel.types import OutboundEnvelope, ReplyContext
from movieclaw_db.engine import get_database
from movieclaw_db.models.channel_account import ChannelAccountStatus
from movieclaw_db.repositories.channel_account_repo import ChannelAccountRepository

logger = logging.getLogger("movieclaw_api.channel_push")

#: 在飞的推送任务:事件循环只持任务的弱引用,不留强引用的话
#: 任务可能在执行中途被 GC 回收,推送就会无声丢失
_push_tasks: set[asyncio.Task[None]] = set()


def tmdb_push_image_url(backdrop_path: str | None, poster_path: str | None) -> str | None:
    """推送配图 URL:优先横版剧照(w780,渲染规格恒定),无剧照回落海报(w500)。"""
    from movieclaw_api.core.config import get_settings

    base = get_settings().tmdb_image_base_url.rstrip("/")
    if backdrop_path:
        return f"{base}/w780{backdrop_path}"
    if poster_path:
        return f"{base}/w500{poster_path}"
    return None


async def push_to_all_channels(text: str, photo: bytes | None = None) -> int:
    """推送到所有通道(微信 + Telegram + Discord + 飞书),返回入队的账号数。"""
    count = 0

    # IM 通道(telegram/discord/飞书):服务内存里有现成的推送地址簿
    try:
        from movieclaw_api.services.im_channel import get_im_channels

        count += await get_im_channels().push_text(text, photo=photo)
    except RuntimeError:
        pass  # 服务未初始化(启动早期/测试)

    # 微信通道:从库里取绑定用户,经运行中的 dispatcher 入队
    # (photo 照常带上,微信 adapter 无发图能力时发送泵自动退纯文本)
    try:
        from movieclaw_api.services.weixin_channel import get_weixin_channel
        from movieclaw_channel.weixin.adapter import CHANNEL_ID as WEIXIN_CHANNEL_ID

        service = get_weixin_channel()
        async with get_database().session() as session:
            rows = await ChannelAccountRepository(session).list_by_channel(WEIXIN_CHANNEL_ID)
        for row in rows:
            bound = (row.bound_user_id or "").strip()
            if not bound or row.status != ChannelAccountStatus.ACTIVE:
                continue
            dispatcher = service.manager.get_dispatcher(WEIXIN_CHANNEL_ID, row.account_id)
            if dispatcher is None:
                continue
            reply = ReplyContext(
                channel_id=WEIXIN_CHANNEL_ID, account_id=row.account_id, user_id=bound
            )
            await dispatcher.push_outbound(
                OutboundEnvelope(reply=reply, text=text, origin="push", photo=photo)
            )
            count += 1
    except RuntimeError:
        pass

    return count


async def _fetch_image(image_url: str) -> bytes | None:
    """经 ImageCache 取配图字节;任何失败返回 None(推送退纯文本)。"""
    try:
        from movieclaw_api.services.image_cache import get_image_cache

        cached = await get_image_cache().get_or_fetch(image_url)
        return await asyncio.to_thread(cached.path.read_bytes)
    except Exception:  # noqa: BLE001 -- 配图取不到只降级,不能拦住文本推送
        logger.info("推送配图获取失败,将发纯文本:%s", image_url)
        return None


def notify_channels(text: str, event: str = "", image_url: str | None = None) -> None:
    """业务侧唯一入口:后台推送,失败只记日志,不阻塞、不抛错。

    ``event`` 对应 ChannelPushSetting 的开关字段(``push_<event>``):
    dispatch=投递下载,imported=入库完成。空 event(测试推送)不受开关限制。
    ``image_url`` 可选,给了就尝试图文推送(取图/发图失败自动退纯文本)。
    """

    async def _run() -> None:
        try:
            if event:
                from movieclaw_api.settings import ChannelPushSetting, get_setting_store

                setting = await get_setting_store().get(ChannelPushSetting)
                if not getattr(setting, f"push_{event}", True):
                    logger.debug("推送事件 %s 已被用户关闭,跳过", event)
                    return
            photo = await _fetch_image(image_url) if image_url else None
            sent = await push_to_all_channels(text, photo=photo)
            if sent:
                logger.info("已推送到 %d 个通道账号:%s", sent, text[:60])
        except Exception:  # noqa: BLE001 -- 推送绝不能影响业务主链路
            logger.exception("通道推送失败(已忽略)")

    try:
        task = asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        logger.debug("无运行中的事件循环,推送已跳过")
        return
    _push_tasks.add(task)
    task.add_done_callback(_push_tasks.discard)
