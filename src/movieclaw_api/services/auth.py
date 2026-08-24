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
from dataclasses import dataclass
from datetime import UTC, datetime

from itsdangerous import BadSignature, URLSafeSerializer
from pwdlib import PasswordHash

from movieclaw_api.exceptions import (
    AppException,
    BadRequestException,
    ConflictException,
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

    ``__str__`` 返回与旧字符串身份一致的格式，日志归因处零改造。
    """

    kind: str
    name: str
    member_id: int | None = None
    is_admin: bool = True
    member: Member | None = None
    #: 仅 Agent 工作区短时令牌携带，用于阻止当前 Agent 停止承载自己的会话。
    agent_session_id: str | None = None

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


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_api_token(name: str) -> tuple[str, ApiTokenRecord]:
    """创建一枚 API 令牌，返回 (明文, 落库记录)。明文仅此一次，服务端不可再回显。"""
    store = get_setting_store()
    setting = await store.get(ApiTokensSetting)
    plaintext = _PAT_PREFIX + secrets.token_urlsafe(32)
    record = ApiTokenRecord(
        id=secrets.token_hex(8),
        name=name,
        token_hash=_hash_token(plaintext),
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    setting.tokens.append(record)
    await store.set(setting)
    logger.info("已创建 API 令牌「%s」（id=%s）", name, record.id)
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
    logger.info("已吊销 API 令牌 id=%s", token_id)
    return True


async def issue_agent_token(session_id: str) -> str:
    """为一次 Agent 运行签发短时效令牌（注入工作区环境变量 MOVIECLAW_TOKEN）。"""
    serializer = URLSafeSerializer(await _get_session_secret(), salt=_AGENT_TOKEN_SALT)
    return serializer.dumps(
        {"aud": "agent", "sid": session_id, "exp": int(time.time()) + AGENT_TOKEN_TTL_SECONDS}
    )


async def verify_bearer_token(token: str) -> Principal:
    """校验 Bearer 令牌（Agent 签名令牌或 PAT），装配 Principal。

    两类令牌都只能由超管创建（PAT 创建接口为管理员专属），因此 is_admin=True。
    """
    # 先试无状态的 Agent 签名令牌（无 IO），再查落库的 PAT
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
            return Principal(kind="pat", name=f"token:{record.name}", is_admin=True)
    raise UnauthorizedException("令牌无效或已吊销，请重新创建（mclaw auth tokens create）")


def reset_auth_state() -> None:
    """清空模块级可变状态（登录限速分桶）。仅供测试在用例间隔离。"""
    _throttles.clear()
