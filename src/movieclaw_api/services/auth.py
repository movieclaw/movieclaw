"""登录鉴权服务：超级管理员 + 成员（docs/design/member-management.md）。

职责边界（"原语用库、编排手写"）：
- 密码哈希 / 校验     → pwdlib（argon2，常量时间比较由库保证）
- 会话令牌签名 / 验签 → itsdangerous（Flask session 同款签名机制）
- 本模块只写编排：一次性初始化锁、登录限速、会话生命周期、Principal 装配。

★ 双身份体系
------------
超管仍是 ``auth.admin`` 配置域的一条配置（明确的产品决策：与成员体系互不
混用）；成员是 ``member`` 表的行。登录先匹配超管再查成员表；会话令牌通过
负载里的 ``k`` 字段区分——**没有 ``k`` 字段的旧令牌一律按超管解释**，升级
后已登录的管理员不掉线。

会话失效语义的差异（谁改密踢谁）：
- 超管改密 → 轮换全局签名密钥，所有端全部下线（密钥可能泄露时这是正确行为）；
- 成员改密/停用/重置 → 该成员 ``token_version+1``，只踢这一个人。

★ 一次性初始化锁（本模块最核心的安全保证）
------------------------------------------
建号接口 ``create_admin`` 必须做到"只要管理员已存在，任何请求都不可能再次建号"：

1. 进程内并发：模块级 ``asyncio.Lock`` 串行化所有建号请求，杜绝
   两个并发请求同时通过"账号不存在"检查的 TOCTOU 窗口。
2. 缓存绕过：锁内先 ``invalidate`` 再读，强制从数据库取最新状态，
   避免多进程部署（uvicorn --workers N）下本进程缓存过期导致误判。
3. 写后复核：落库后立即再次从数据库读回，若读到的用户名与本次写入不符，
   说明极端并发下被其他进程抢先，本次建号作废并报错。
   （2+3 无法做到跨进程严格原子，但把竞争窗口压缩到毫秒级，且要求攻击者
   与合法用户在同一毫秒内竞争——配合"首次部署即初始化"的使用方式已足够。）
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from itsdangerous import BadSignature, URLSafeSerializer
from pwdlib import PasswordHash

from movieclaw_api.exceptions import (
    AppException,
    BadRequestException,
    ConflictException,
    NotFoundException,
    UnauthorizedException,
)
from movieclaw_api.settings import (
    AdminAccountSetting,
    ApiTokenRecord,
    ApiTokensSetting,
    SessionSecretSetting,
    get_descriptor_by_model,
    get_setting_store,
)
from movieclaw_db.engine import get_database
from movieclaw_db.models.member import Member
from movieclaw_db.repositories.member_repo import MemberRepository

logger = logging.getLogger("movieclaw_api.auth")


# ---------------------------------------------------------------------------
# Principal：鉴权层产出、授权层消费的统一请求主体
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """请求主体。``require_login`` 的返回值，全站授权判定的唯一依据。

    - ``kind``：``admin``（超管会话）/ ``member``（成员会话）/
      ``pat``（CLI 长期令牌）/ ``agent``（Agent 工作区令牌）；
    - ``is_admin``：admin / pat / agent 均为 True——PAT 与 Agent 令牌只能由
      超管创建，等价管理员（PAT 创建接口已收口为管理员专属，防止成员提权）；
    - ``member``：kind == "member" 时携带已加载的成员行（验签时顺路查库拿到），
      供能力开关判定（allow_subscribe 等）免二次查库。
    - ``agent_session_id``：kind == "agent" 时携带令牌所属会话，供会话级
      自保护授权使用；其他主体恒为 None。
    - ``client_type``：仅令牌主体有值（worker / cli / manual）。它是**客户端
      形态**而非权限等级——权限完全来自 ``is_admin`` / ``member``，全站既有的
      授权依赖对令牌主体自动生效。它只用来把 Worker 令牌挡在业务接口之外
      （docs/design/device-auth.md §4.3）。

    ``__str__`` 返回与旧字符串身份一致的格式，日志归因处零改造。
    """

    kind: str
    name: str
    member_id: int | None = None
    is_admin: bool = True
    member: Member | None = None
    #: 仅 Agent 工作区短时令牌携带，用于阻止当前 Agent 停止承载自己的会话。
    agent_session_id: str | None = None
    #: 仅设备/手工令牌携带：worker | cli | manual。会话 Cookie 主体恒为 None。
    client_type: str | None = None

    def __str__(self) -> str:  # pragma: no cover - 纯格式化
        return self.name


# 会话 Cookie 名与有效期。签名令牌里带过期时间戳，轮换签名密钥即全端下线。
SESSION_COOKIE_NAME = "movieclaw_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
SESSION_TTL_REMEMBER_SECONDS = 30 * 24 * 3600
# itsdangerous 的签名域隔离标识：即使密钥泄漏复用，其他用途的签名也不能伪造会话
_SESSION_SALT = "movieclaw.session.v1"

# argon2（pwdlib 推荐配置）。verify 内部是常量时间比较。
_password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    """生成密码哈希（成员服务建号/重置密码复用同一套 argon2 配置）。"""
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码与哈希是否匹配（常量时间比较）。"""
    return _password_hash.verify(password, password_hash)


# 建号一次性锁（进程内串行化，详见模块 docstring）
_bootstrap_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# 登录限速：按用户名分桶的连续失败计数（按 IP 会被反代地址稀释）。
# 分桶的意义：多成员后一个人输错密码只锁他自己的用户名，不连坐全家；
# Jellyfin 侧登录复用同一入口，电视上输错也只锁对应用户名。成功登录清零。
# ---------------------------------------------------------------------------


class LoginThrottle:
    """连续失败达到阈值后强制等待，等待时间随失败次数翻倍，封顶 5 分钟。"""

    THRESHOLD = 5
    BASE_DELAY_SECONDS = 30
    MAX_DELAY_SECONDS = 300

    def __init__(self) -> None:
        self._failures = 0
        self._locked_until = 0.0

    def ensure_allowed(self) -> None:
        remaining = self._locked_until - time.monotonic()
        if remaining > 0:
            raise AppException(
                status_code=429,
                code="TOO_MANY_ATTEMPTS",
                message=f"登录失败次数过多，请 {int(remaining) + 1} 秒后再试",
            )

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.THRESHOLD:
            delay = min(
                self.BASE_DELAY_SECONDS * 2 ** (self._failures - self.THRESHOLD),
                self.MAX_DELAY_SECONDS,
            )
            self._locked_until = time.monotonic() + delay
            logger.warning("连续登录失败 %d 次，锁定 %d 秒", self._failures, delay)

    def reset(self) -> None:
        self._failures = 0
        self._locked_until = 0.0


# 桶上限：防止攻击者用海量随机用户名注水内存。淘汰规则是安全关键：
# 绝不能无差别 LRU——否则攻击者交替"猜一次目标账号 + 128 个随机用户名"
# 即可把目标账号的锁定桶挤出去，让按账号锁定形同虚设。因此：
# - 软上限内正常收；超软上限先淘汰"干净"的桶（无失败计数、未锁定）；
# - 有失败/锁定中的桶只有超过硬上限才淘汰最旧的（攻击者要顶到硬上限，
#   每个脏桶都得花真实的失败请求，成本翻数量级）。
_MAX_THROTTLE_BUCKETS = 128
_HARD_MAX_THROTTLE_BUCKETS = 4096
_throttles: OrderedDict[str, LoginThrottle] = OrderedDict()


def _bucket_dirty(bucket: LoginThrottle) -> bool:
    """桶是否携带安全状态（失败计数或仍在锁定期）——脏桶不轻易淘汰。"""
    return bucket._failures > 0 or bucket._locked_until > time.monotonic()


def _throttle_for(username: str) -> LoginThrottle:
    """取（或建）该用户名的限速桶。键做去空格 + 小写归一化。"""
    key = username.strip().lower()
    bucket = _throttles.get(key)
    if bucket is None:
        bucket = LoginThrottle()
        _throttles[key] = bucket
        if len(_throttles) > _MAX_THROTTLE_BUCKETS:
            # 先淘汰最旧的干净桶（成功登录过/从未失败的），锁定与计数不受影响
            for stale_key, stale in list(_throttles.items()):
                if stale_key != key and not _bucket_dirty(stale):
                    del _throttles[stale_key]
                    if len(_throttles) <= _MAX_THROTTLE_BUCKETS:
                        break
        while len(_throttles) > _HARD_MAX_THROTTLE_BUCKETS:
            # 硬上限兜底：全是脏桶时才淘汰最旧的，防内存无界
            _throttles.popitem(last=False)
    else:
        _throttles.move_to_end(key)
    return bucket


# ---------------------------------------------------------------------------
# 初始化状态与一次性建号
# ---------------------------------------------------------------------------


async def _load_admin_fresh() -> AdminAccountSetting:
    """绕过缓存、强制从数据库读取管理员账号（用于安全判定，不吃过期缓存）。"""
    store = get_setting_store()
    store.invalidate(get_descriptor_by_model(AdminAccountSetting).namespace)
    return await store.get(AdminAccountSetting)


async def is_admin_initialized() -> bool:
    """管理员是否已创建。每次都查库，保证多进程部署下状态实时准确。"""
    admin = await _load_admin_fresh()
    return bool(admin.password_hash)


async def create_admin(username: str, password: str) -> AdminAccountSetting:
    """创建超级管理员（首次初始化，全生命周期只允许成功一次）。

    锁定策略见模块 docstring。已初始化时抛 409，错误信息刻意不区分
    "谁创建的/何时创建"，不给探测者任何额外信息。
    """
    async with _bootstrap_lock:
        current = await _load_admin_fresh()
        if current.password_hash:
            logger.warning("拒绝重复初始化：管理员账号已存在，来路请求被 409 拦截")
            raise ConflictException("系统已初始化，禁止重复创建管理员账号")

        account = AdminAccountSetting(
            username=username,
            password_hash=_password_hash.hash(password),
            nickname=username,  # 初始昵称即用户名，用户可在「个人信息」里随时改
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        await get_setting_store().set(account)

        # 写后复核：防御多进程并发下的抢先写入（详见模块 docstring 第 3 点）
        persisted = await _load_admin_fresh()
        if persisted.username != username or persisted.password_hash != account.password_hash:
            logger.error("初始化竞争检测：落库结果与本次写入不符，本次建号作废")
            raise ConflictException("系统已初始化，禁止重复创建管理员账号")

        logger.info("超级管理员账号已创建：%s", username)
        return account


# ---------------------------------------------------------------------------
# 登录认证
# ---------------------------------------------------------------------------


async def authenticate(username: str, password: str) -> AdminAccountSetting | Member:
    """校验用户名密码，返回命中的身份（超管配置或成员行）。

    匹配顺序：先超管、后成员表（用户名建号时已保证互斥，顺序只是实现细节）。
    失败计入该用户名的限速桶，成功清零。无论用户名是否存在都执行一次密码
    哈希校验，避免通过响应时间差探测用户名；错误信息也不区分"用户名不存在
    /密码错误"。

    管理员账号刻意绕过缓存直读数据库：离线重置入口
    （``python -m movieclaw_api.reset_password``）是另一个进程，改完密码不会
    通知到本进程的配置缓存——吃缓存的话用户会遇到"明明重置了却登录不上、
    非得重启服务"的困惑。登录本就是低频操作且已被限速，多一次按主键的
    SQLite 读取可忽略。
    """
    throttle = _throttle_for(username)
    throttle.ensure_allowed()

    admin = await _load_admin_fresh()
    if not admin.password_hash:
        raise BadRequestException("系统尚未初始化，请先完成首次引导创建管理员账号")

    if secrets.compare_digest(username.encode(), admin.username.encode()):
        if _password_hash.verify(password, admin.password_hash):
            throttle.reset()
            logger.info("管理员 %s 登录成功", admin.username)
            return admin
        throttle.record_failure()
        logger.warning("登录失败：用户名或密码错误（用户名输入：%s）", username)
        raise UnauthorizedException("用户名或密码错误")

    async with get_database().session() as session:
        repo = MemberRepository(session)
        member = await repo.get_by_username(username)
        # 成员不存在时也对超管哈希跑一次校验，抹平响应时间差
        target_hash = member.password_hash if member else admin.password_hash
        password_ok = _password_hash.verify(password, target_hash)
        if member is None or not password_ok:
            throttle.record_failure()
            logger.warning("登录失败：用户名或密码错误（用户名输入：%s）", username)
            raise UnauthorizedException("用户名或密码错误")
        if member.status != "active":
            # 密码对但账号被停用：不计失败（不是爆破），明确告知找管理员
            logger.warning("登录被拒：成员 %s 已被停用", member.username)
            raise UnauthorizedException("账号已被停用，请联系管理员")
        throttle.reset()
        await repo.touch_last_login(member)
        logger.info("成员 %s 登录成功", member.username)
        return member


async def get_admin_account() -> AdminAccountSetting:
    """读取管理员账号（走缓存即可，供展示类接口使用）。"""
    return await get_setting_store().get(AdminAccountSetting)


async def update_nickname(nickname: str) -> AdminAccountSetting:
    """修改展示昵称。昵称只影响界面展示，登录仍使用用户名。"""
    admin = await get_setting_store().get(AdminAccountSetting)
    if not admin.password_hash:
        raise BadRequestException("系统尚未初始化，无法修改昵称")
    admin.nickname = nickname
    await get_setting_store().set(admin)
    logger.info("管理员昵称已更新为：%s", nickname)
    return admin


async def change_password(old_password: str, new_password: str) -> None:
    """修改管理员密码（校验原密码），并强制全端下线。"""
    admin = await get_setting_store().get(AdminAccountSetting)
    if not admin.password_hash:
        raise BadRequestException("系统尚未初始化，无法修改密码")
    if not _password_hash.verify(old_password, admin.password_hash):
        raise UnauthorizedException("原密码错误")

    await reset_admin_password(new_password)
    logger.info("管理员密码已修改，所有登录会话与播放器凭据已强制下线")


async def reset_admin_password(new_password: str) -> AdminAccountSetting:
    """**不校验原密码**直接重写管理员密码，并轮换会话签名密钥、吊销播放器凭据。

    ⚠️ 安全红线：本函数是"忘记密码"的最后一道后门，绝不可挂到任何 HTTP 路由上
    （挂上去等于任何人都能改管理员密码）。它的唯一调用方是离线维护入口
    ``python -m movieclaw_api.reset_password``——那条路径要求调用者能直接访问
    ``data/`` 目录，与主密钥文件同一条信任边界：能碰数据目录的人本就能解密全部
    配置，再多一个改密能力不降低任何安全性。

    只覆写 ``password_hash`` 一个字段，用户名、昵称与其余所有配置域原样保留。
    """
    store = get_setting_store()
    # 绕开缓存取最新账号：离线入口是独立进程，缓存里可能是空的默认实例，
    # 直接用会把用户名/昵称写没
    admin = await _load_admin_fresh()
    if not admin.password_hash:
        raise BadRequestException("系统尚未初始化，请先在网页上完成首次引导创建管理员账号")

    admin.password_hash = _password_hash.hash(new_password)
    await store.set(admin)
    # 轮换签名密钥：已签发的会话令牌全部作废（重置密码的场景下，把可能已被
    # 他人持有的会话一并踢掉才是正确语义）
    await rotate_session_secret()
    await _drop_admin_jellyfin_devices()
    return admin


async def _drop_admin_jellyfin_devices() -> None:
    """管理员改密时吊销超管身份的 Jellyfin AccessToken。"""
    from sqlalchemy import delete as sa_delete

    from movieclaw_db.models import JellyfinDevice

    async with get_database().session() as session:
        await session.execute(sa_delete(JellyfinDevice).where(JellyfinDevice.member_id == 0))
        await session.commit()


# ---------------------------------------------------------------------------
# 会话令牌：签发 / 验签 / 轮换
# ---------------------------------------------------------------------------


async def _get_session_secret() -> str:
    """读取会话签名密钥；首次使用时自动生成并加密落库（用户无感）。"""
    store = get_setting_store()
    setting = await store.get(SessionSecretSetting)
    if not setting.secret:
        setting = SessionSecretSetting(secret=secrets.token_urlsafe(48))
        await store.set(setting)
        logger.info("已自动生成登录会话签名密钥")
    return setting.secret


async def get_signing_secret() -> str:
    """供其它签名用途复用的密钥（**必须配不同的 salt 做域隔离**）。

    目前的使用方：网页播放器的取流 URL 签名（``services/playback/signing.py``）。
    取流 URL 只能把凭据放查询参数——``<video src>`` 与原生 HLS 带不了自定义
    header。复用同一把密钥的附带好处是：轮换它（全端下线）会一并作废所有
    在途的取流 token。
    """
    return await _get_session_secret()


async def rotate_session_secret() -> None:
    """轮换签名密钥：所有已签发的会话令牌立即验签失败（全端下线）。"""
    await get_setting_store().set(SessionSecretSetting(secret=secrets.token_urlsafe(48)))


async def issue_session_token(username: str, *, remember: bool = False) -> tuple[str, int]:
    """签发**超管**会话令牌，返回 (令牌, 有效秒数)。负载不带 ``k`` 字段——
    与升级前的旧令牌同构，旧会话在新版本下继续有效。"""
    max_age = SESSION_TTL_REMEMBER_SECONDS if remember else SESSION_TTL_SECONDS
    serializer = URLSafeSerializer(await _get_session_secret(), salt=_SESSION_SALT)
    token = serializer.dumps({"u": username, "exp": int(time.time()) + max_age})
    return token, max_age


async def issue_member_session_token(member: Member, *, remember: bool = False) -> tuple[str, int]:
    """签发成员会话令牌。负载携带成员 id 与签发时的 ``token_version``——
    改密/停用把版本 +1，旧令牌验签通过但版本不匹配，即刻失效（只踢这个人）。"""
    max_age = SESSION_TTL_REMEMBER_SECONDS if remember else SESSION_TTL_SECONDS
    serializer = URLSafeSerializer(await _get_session_secret(), salt=_SESSION_SALT)
    token = serializer.dumps(
        {
            "u": member.username,
            "k": "member",
            "mid": member.id,
            "ver": member.token_version,
            "exp": int(time.time()) + max_age,
        }
    )
    return token, max_age


async def verify_session_token(token: str | None) -> Principal:
    """校验会话令牌并装配 Principal；无效/过期统一抛 401 提示重新登录。

    负载无 ``k`` 字段 → 超管（含升级前签发的旧令牌，向前兼容）；
    ``k == "member"`` → 查库校验成员仍存在、未停用、token_version 匹配——
    成员会话因此不是纯无状态，代价是一次按主键的行读取（SQLite 下可忽略），
    换来"停用/改密即刻生效"的正确语义。
    """
    if not token:
        raise UnauthorizedException("未登录，请先登录")

    serializer = URLSafeSerializer(await _get_session_secret(), salt=_SESSION_SALT)
    try:
        payload = serializer.loads(token)
    except BadSignature:
        raise UnauthorizedException("登录状态无效，请重新登录") from None

    if not isinstance(payload, dict) or int(payload.get("exp", 0)) < time.time():
        raise UnauthorizedException("登录已过期，请重新登录")

    if payload.get("k") != "member":
        return Principal(kind="admin", name=str(payload.get("u", "")), is_admin=True)

    async with get_database().session() as session:
        member = await MemberRepository(session).get(int(payload.get("mid", 0)))
    if member is None or member.status != "active":
        raise UnauthorizedException("登录状态已失效，请重新登录")
    if int(payload.get("ver", -1)) != member.token_version:
        raise UnauthorizedException("登录状态已失效，请重新登录")
    return Principal(
        kind="member",
        name=member.username,
        member_id=member.id,
        is_admin=False,
        member=member,
    )


# ---------------------------------------------------------------------------
# Bearer 令牌：CLI 长期令牌（PAT）+ 产品内 Agent 短时效令牌
# ---------------------------------------------------------------------------
# 两类令牌共用同一个验签入口 verify_bearer_token（docs/design/cli.md §6.2/§8.1）：
# - PAT：面向用户脚本/远程 CLI，落库只存 sha256 哈希，明文创建时返回一次，
#   可按 id 单独吊销；
# - Agent 令牌：产品内 AI 助手工作区专用，itsdangerous 签名（复用会话签名
#   密钥 + 独立 salt），无状态不落库，过期自动作废；管理员改密轮换签名
#   密钥时与会话一起全体失效（全局熔断免费获得）。

_AGENT_TOKEN_SALT = "movieclaw.agent-token.v1"
AGENT_TOKEN_TTL_SECONDS = 2 * 3600  # 一次 Agent 运行的令牌有效期上限
_PAT_PREFIX = "mclaw_"  # 令牌明文前缀：肉眼可辨认来源，误提交扫描器也好识别

#: 令牌「最近使用」的落盘节流间隔：精度够回答「这台机器还活着吗」，又不至于
#: 让每个请求都写一次设置项。
_TOKEN_TOUCH_INTERVAL_S = 60
#: token_id → 上次落盘时刻（time.monotonic）。纯进程内缓存，重启即重来。
_token_touched_at: dict[str, float] = {}

#: 配对码有效期：够人从设备走到浏览器，又不让未决请求长期挂着。
DEVICE_CODE_TTL_SECONDS = 300
#: 下发给客户端的轮询间隔（秒）。
DEVICE_POLL_INTERVAL_SECONDS = 2
#: 服务端判定「轮询过快」的下限，略小于下发值，容忍客户端的时钟与网络抖动。
_DEVICE_MIN_POLL_INTERVAL_S = 1.0
#: 终态挑战的留存期：客户端还在轮询，必须能读到确定结论而不是「挑战不存在」。
_DEVICE_LINGER_S = 120
#: 单来源 IP 的未决请求上限：防止刷屏把审批页淹掉。
#: 只在来源地址可用时才按它分桶——桥接网络里所有设备的源地址会被 NAT 成同一个
#: 网关地址（api/client_address.py），那时按地址分桶等于全网共用一个计数桶，
#: 一台机器刷屏就能把别人全锁在门外。
_DEVICE_MAX_PENDING_PER_IP = 5
#: 未决请求总数上限。来源地址不可用时它是唯一的闸；可用时它兜住「多个来源
#: 一起刷」的情况。审批页一次也展示不了这么多卡片。
_DEVICE_MAX_PENDING_TOTAL = 20
#: 配对码字母表：去掉 0/O/1/I 等易混淆字符，人要念得出、抄得对。
_USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
#: 允许的客户端形态。worker 有形态上限（只能转码），cli 没有。
_DEVICE_CLIENT_TYPES = ("worker", "cli")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_api_token(
    name: str,
    *,
    client_type: str = "manual",
    owner_kind: str = "admin",
) -> tuple[str, ApiTokenRecord]:
    """创建一枚 API 令牌，返回 (明文, 落库记录)。明文仅此一次，服务端不可再回显。

    ``client_type`` 决定这枚令牌能用在哪：``worker`` 只能走转码链路（业务接口
    在 ``require_login`` 里直接拒绝），``cli`` / ``manual`` 无形态上限。
    令牌不设过期（``expires_at`` 恒为 None）——失效只靠显式吊销，理由见
    docs/design/device-auth.md §8「凭证生命周期的独立性」。
    """
    store = get_setting_store()
    setting = await store.get(ApiTokensSetting)
    plaintext = _PAT_PREFIX + secrets.token_urlsafe(32)
    record = ApiTokenRecord(
        id=secrets.token_hex(8),
        name=name,
        token_hash=_hash_token(plaintext),
        created_at=_now_iso(),
        client_type=client_type,
        owner_kind=owner_kind,
    )
    setting.tokens.append(record)
    await store.set(setting)
    logger.info("已创建 API 令牌「%s」（id=%s，形态=%s）", name, record.id, client_type)
    return plaintext, record


async def list_api_tokens() -> list[ApiTokenRecord]:
    return (await get_setting_store().get(ApiTokensSetting)).tokens


async def revoke_api_token(token_id: str) -> bool:
    """按 id 吊销令牌；返回是否真的删掉了一枚。"""
    store = get_setting_store()
    setting = await store.get(ApiTokensSetting)
    remaining = [t for t in setting.tokens if t.id != token_id]
    if len(remaining) == len(setting.tokens):
        return False
    setting.tokens = remaining
    await store.set(setting)
    _token_touched_at.pop(token_id, None)
    logger.info("已吊销 API 令牌 id=%s", token_id)
    return True


async def _touch_api_token(token_id: str) -> None:
    """记录令牌的最近使用时间，按分钟粒度落盘。

    设备列表要能回答「这台机器还活着吗」，但每次请求都写设置项等于每次请求
    一次磁盘写。这里做进程内节流：同一枚令牌 60 秒内只落一次盘，精度对
    「最近活跃」这个用途绰绰有余。
    """
    now = time.monotonic()
    if now - _token_touched_at.get(token_id, 0.0) < _TOKEN_TOUCH_INTERVAL_S:
        return
    _token_touched_at[token_id] = now
    store = get_setting_store()
    setting = await store.get(ApiTokensSetting)
    for record in setting.tokens:
        if record.id == token_id:
            record.last_used_at = _now_iso()
            await store.set(setting)
            return


async def issue_agent_token(session_id: str) -> str:
    """为一次 Agent 运行签发短时效令牌（注入工作区环境变量 MOVIECLAW_TOKEN）。"""
    serializer = URLSafeSerializer(await _get_session_secret(), salt=_AGENT_TOKEN_SALT)
    return serializer.dumps(
        {"aud": "agent", "sid": session_id, "exp": int(time.time()) + AGENT_TOKEN_TTL_SECONDS}
    )


async def verify_bearer_token(token: str) -> Principal:
    """校验 Bearer 令牌（Agent 签名令牌或设备/手工令牌），装配 Principal。

    权限**在这里按批准者装配，而不是从令牌里读出来**——当前只有超管能批准
    设备，因此落库令牌一律 ``is_admin=True``；将来开放成员批准时在这里加一个
    成员分支即可，能力开关的事后调整会立刻对令牌生效
    （docs/design/device-auth.md §4.2）。
    """
    # 先试无状态的 Agent 签名令牌（无 IO），再查落库的设备/手工令牌
    serializer = URLSafeSerializer(await _get_session_secret(), salt=_AGENT_TOKEN_SALT)
    try:
        payload = serializer.loads(token)
    except BadSignature:
        payload = None
    if isinstance(payload, dict) and payload.get("aud") == "agent":
        if int(payload.get("exp", 0)) < time.time():
            raise UnauthorizedException("Agent 令牌已过期，请重新发起 Agent 运行")
        agent_session_id = str(payload.get("sid", ""))
        return Principal(
            kind="agent",
            name=f"agent:{agent_session_id}",
            is_admin=True,
            agent_session_id=agent_session_id,
        )

    provided_hash = _hash_token(token)
    for record in await list_api_tokens():
        if hmac.compare_digest(provided_hash, record.token_hash):
            await _touch_api_token(record.id)
            return Principal(
                kind="pat",
                name=f"token:{record.name}",
                is_admin=True,
                client_type=record.client_type,
            )
    raise UnauthorizedException("令牌无效或已吊销，请在网页「设备」页重新配对")


# ---------------------------------------------------------------------------
# 设备授权：客户端出示配对码，人在网页上批准
# ---------------------------------------------------------------------------
#
# 设计见 docs/design/device-auth.md §2/§3。三条不可动摇的性质：
#
# 1. **令牌只回到发起进程**。网页上出现的只有一段几分钟就作废的短码，它不是
#    凭据；真正的令牌通过 device_code 兑换，从不显示在任何屏幕上——也就不会
#    进剪贴板、聊天记录、截图或 Agent 的上下文。
# 2. **未获批准的请求不落库**。磁盘上不该出现「有人试图接入」的记录被当成
#    凭据来源。进程重启则未决请求全部作废，客户端重发即可。
# 3. **令牌在批准那一刻才生成，在兑换那一刻才交付，且只交付一次**。


@dataclass(slots=True)
class DeviceAuthChallenge:
    """一次进行中的设备接入请求（内存对象，不落库）。

    生命周期：authorize（客户端发起）→ pending，人在网页上看到 →
    approve / deny → approved 时短暂持有刚生成的令牌明文 → 客户端兑换一次
    → consumed。超时未批准转 expired。

    ``device_code`` 只存哈希：它是真正的兑换凭据，与令牌本体同等对待。
    """

    user_code: str
    device_code_hash: str
    client_type: str
    client_name: str
    source_ip: str
    status: Literal["pending", "approved", "denied", "expired", "consumed"] = "pending"
    #: 仅 approved → consumed 之间短暂持有；兑换后立即清空
    granted_token: str | None = None
    token_id: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    expires_at: float = field(default_factory=lambda: time.monotonic() + DEVICE_CODE_TTL_SECONDS)
    #: 终态进入时刻，用于 linger 清理
    settled_at: float | None = None
    last_poll_at: float = 0.0


@dataclass(frozen=True, slots=True)
class DeviceTokenResult:
    """兑换端点的结论。路由据此映射 HTTP 状态码，服务层不关心传输细节。"""

    status: Literal["pending", "granted", "denied", "expired", "slow_down"]
    token: str | None = None
    record: ApiTokenRecord | None = None


#: user_code → 挑战。进程级内存，刻意不落库（见上方设计注释第 2 条）。
_device_challenges: dict[str, DeviceAuthChallenge] = {}


def _new_user_code() -> str:
    """生成人能念出、能抄对的配对码，形如 ``MCLW-7F3K``。

    低熵是可接受的：它只能在**管理员已登录的浏览器里**用于批准，猜中也调不动
    批准端点。真正需要高熵的是 device_code。
    """
    body = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(4))
    return f"MCLW-{body}"


def _purge_settled_challenges() -> None:
    """清掉过期的未决请求与留存期已到的终态请求。

    终态要留一会儿（``_DEVICE_LINGER_S``）：客户端还在轮询，必须让它读到
    「被拒绝」「已过期」这样的确定结论，而不是含糊的「挑战不存在」。
    """
    now = time.monotonic()
    for code, ch in list(_device_challenges.items()):
        if ch.status == "pending" and now >= ch.expires_at:
            ch.status = "expired"
            ch.settled_at = now
            continue
        if ch.settled_at is not None and now - ch.settled_at >= _DEVICE_LINGER_S:
            del _device_challenges[code]


def authorize_device(
    *, client_type: str, client_name: str, source_ip: str
) -> tuple[str, DeviceAuthChallenge]:
    """受理一次接入请求，返回 (device_code 明文, 挑战)。

    客户端**不声明权限**——它只说自己是什么形态、叫什么名字，权限由批准者决定。

    ``source_ip`` 允许为空串，表示取不到可用来区分设备的地址（桥接网络把源
    地址 NAT 掉了，见 ``api/client_address.py``）。空串不参与按来源分桶，
    只受总数上限约束。
    """
    if client_type not in _DEVICE_CLIENT_TYPES:
        raise BadRequestException(f"未知的客户端类型：{client_type}")
    _purge_settled_challenges()

    pending = [ch for ch in _device_challenges.values() if ch.status == "pending"]
    if len(pending) >= _DEVICE_MAX_PENDING_TOTAL:
        raise BadRequestException(
            "待批准的接入请求过多，请先在网页「设备」页处理或等待现有请求过期"
        )
    if source_ip:
        pending_from_ip = sum(1 for ch in pending if ch.source_ip == source_ip)
        if pending_from_ip >= _DEVICE_MAX_PENDING_PER_IP:
            raise BadRequestException(
                "同一来源的待批准请求过多，请先在网页「设备」页处理或等待现有请求过期"
            )

    while (user_code := _new_user_code()) in _device_challenges:  # pragma: no cover - 概率极低
        continue
    device_code = secrets.token_urlsafe(32)
    challenge = DeviceAuthChallenge(
        user_code=user_code,
        device_code_hash=_hash_token(device_code),
        client_type=client_type,
        client_name=client_name.strip() or "未命名设备",
        source_ip=source_ip,
    )
    _device_challenges[user_code] = challenge
    logger.info(
        "收到设备接入请求：%s（%s，来自 %s），配对码 %s",
        challenge.client_name,
        client_type,
        source_ip or "来源不可辨（容器网络已改写源地址）",
        user_code,
    )
    return device_code, challenge


def list_device_requests() -> list[DeviceAuthChallenge]:
    """列出仍待批准的请求（供网页展示），按发起时间从新到旧。"""
    _purge_settled_challenges()
    pending = [ch for ch in _device_challenges.values() if ch.status == "pending"]
    return sorted(pending, key=lambda ch: ch.created_at, reverse=True)


def _get_pending(user_code: str) -> DeviceAuthChallenge:
    _purge_settled_challenges()
    challenge = _device_challenges.get(user_code.strip().upper())
    if challenge is None:
        raise NotFoundException("配对请求不存在或已过期，请让设备重新发起")
    if challenge.status != "pending":
        raise BadRequestException("这条配对请求已经处理过了，请让设备重新发起")
    return challenge


async def approve_device_request(user_code: str) -> DeviceAuthChallenge:
    """批准一次接入请求：此刻才生成并落库令牌，等客户端来兑换。

    **先改状态再签发**：``create_api_token`` 要 await（读写设置项），如果放在
    状态变更之前，两个并发的批准请求会双双通过 ``_get_pending`` 的检查、
    给同一条请求签出两枚令牌，其中一枚永远没人兑换也没人知道它存在。
    签发失败则退回 pending，让用户能重试。
    """
    challenge = _get_pending(user_code)
    challenge.status = "approved"
    try:
        plaintext, record = await create_api_token(
            challenge.client_name, client_type=challenge.client_type
        )
    except Exception:
        challenge.status = "pending"
        raise
    challenge.granted_token = plaintext
    challenge.token_id = record.id
    challenge.settled_at = time.monotonic()
    logger.info("已批准设备接入：%s（令牌 id=%s）", challenge.client_name, record.id)
    return challenge


def deny_device_request(user_code: str) -> DeviceAuthChallenge:
    """拒绝一次接入请求。不生成任何令牌，也不在磁盘上留痕。"""
    challenge = _get_pending(user_code)
    challenge.status = "denied"
    challenge.settled_at = time.monotonic()
    logger.info(
        "已拒绝设备接入：%s（来自 %s）",
        challenge.client_name,
        challenge.source_ip or "来源不可辨",
    )
    return challenge


async def redeem_device_code(device_code: str) -> DeviceTokenResult:
    """客户端轮询兑换。兑换成功即作废，重放同一个 device_code 不再返回令牌。"""
    _purge_settled_challenges()
    provided_hash = _hash_token(device_code)
    challenge = next(
        (
            ch
            for ch in _device_challenges.values()
            if hmac.compare_digest(ch.device_code_hash, provided_hash)
        ),
        None,
    )
    if challenge is None:
        # 已过期被清理、或根本不存在——对客户端是同一件事：停止轮询，重新发起
        return DeviceTokenResult(status="expired")

    now = time.monotonic()
    if challenge.status == "pending":
        # 轮询过快只让客户端退避，**不作废挑战**：正常用户的重试不该被当成攻击
        if now - challenge.last_poll_at < _DEVICE_MIN_POLL_INTERVAL_S:
            return DeviceTokenResult(status="slow_down")
        challenge.last_poll_at = now
        return DeviceTokenResult(status="pending")

    if challenge.status == "approved":
        token = challenge.granted_token
        record = next(
            (r for r in await list_api_tokens() if r.id == challenge.token_id),
            None,
        )
        if token is None or record is None:  # 令牌在兑换前被吊销
            challenge.status = "denied"
            challenge.settled_at = now
            return DeviceTokenResult(status="denied")
        challenge.status = "consumed"
        challenge.granted_token = None
        challenge.settled_at = now
        logger.info("设备已完成配对：%s", challenge.client_name)
        return DeviceTokenResult(status="granted", token=token, record=record)

    if challenge.status == "denied":
        return DeviceTokenResult(status="denied")
    return DeviceTokenResult(status="expired")


def reset_auth_state() -> None:
    """清空模块级可变状态（登录限速分桶、设备挑战、令牌活跃缓存）。

    仅供测试在用例间隔离——这些状态都在进程内存里，生产环境靠重启自然清零。
    """
    _throttles.clear()
    _device_challenges.clear()
    _token_touched_at.clear()
