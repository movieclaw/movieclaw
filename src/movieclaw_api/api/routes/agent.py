from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_agent import AgentRunner, AgentStartParams, AgentTool, build_system_prompt, compact
from movieclaw_agent.tools import builtin_tools, make_mclaw_tool
from movieclaw_api.api.deps import require_login
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import (
    BadRequestException,
    NotFoundException,
    UpstreamServiceException,
)
from movieclaw_api.schemas.agent import (
    SessionCompactionEntryView,
    SessionContextCompactionView,
    SessionHandoffEntryView,
    SessionMessageAcceptedView,
    SessionMessageEntryView,
    SessionMessageView,
    SessionRenamePayload,
    SessionRetryPayload,
    SessionStartPayload,
    SessionSummary,
    SessionTranscriptView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services import auth as auth_service
from movieclaw_api.services.agent_runs import get_agent_run_registry
from movieclaw_api.services.agent_session_recorder import AgentSessionRecorder
from movieclaw_api.services.agent_sessions import (
    SessionCompactionEntry,
    SessionHandoffEntry,
    SessionMessageEntry,
    get_agent_session_store,
)
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.llm_config import acquire_llm_router
from movieclaw_api.services.mclaw_tool import render_service_map
from movieclaw_api.settings import AppServerSetting, get_setting_store
from movieclaw_db.engine import get_session
from movieclaw_db.repositories.agent_session_repo import (
    AgentSessionRepository,
    is_running,
)
from movieclaw_llm import ChatMessage, LlmRouter, ModelSettings

router = APIRouter(prefix="/sessions", tags=["session"])


def get_agent_tools(cli_env: dict[str, str]) -> list[AgentTool]:
    """Agent 的工具集：内置基础工具 + mclaw 产品操作工具。

    bash/read/write/edit 是纯工作区工具，**不携带**产品授权；mclaw 工具
    单独构建，令牌只注入它的子进程（每次运行的令牌不同，因此每次运行都
    重新构建工具集）。服务目录渲染自 CLI 内置 spec，与命令面严格同版。
    """
    workdir = Path(get_settings().agent_workspace_dir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    return [
        *builtin_tools(workdir),
        make_mclaw_tool(workdir, cli_env, render_service_map()),
    ]


# 前端页面路由表：模型拼可点击链接的唯一依据（(路径模式, 一行说明)）。
# 与 apps/web/app/(app) 的真实页面结构由守护测试保持同步（见
# tests/api/test_agent.py 的 test_page_routes_match_web_app_pages）——
# 前端加页面、改路径时测试会拦下，这张表不会悄悄过期。
_PAGE_ROUTES: list[tuple[str, str]] = [
    ("/", "首页"),
    ("/search", "站点资源搜索页"),
    ("/discover/{movie|tv}", "发现页（电影/剧集的榜单与分类浏览）"),
    (
        "/discover/{movie|tv}/collections/{tmdb|douban}/{片单ID}",
        "完整影视片单（片单引用来自 discover list-collections）",
    ),
    ("/discover/movie/top250", "豆瓣电影 Top250 榜单"),
    ("/discover/movie/high-score", "豆瓣高分电影榜单"),
    ("/discover/people/{TMDB 影人ID}", "发现页影人详情与完整作品列表"),
    ("/media/{movie|tv}/{TMDB ID}", "影片/剧集详情页"),
    ("/media/douban/{豆瓣ID}", "豆瓣词条详情页"),
    ("/subscriptions", "订阅列表"),
    ("/subscriptions/{订阅ID}", "订阅详情（ID 来自 subscriptions list/get）"),
    ("/library", "媒体库总览"),
    ("/library/{库ID}", "某个媒体库的内容（库 ID 来自 library list）"),
    (
        "/library/{库ID}/item/{条目ID}",
        "库内条目详情（条目 ID 来自 library items list）",
    ),
    ("/activity", "活动页（观看：实时播放/下载；任务：后台作业与下载任务）"),
    ("/sessions/{会话ID}", "AI 会话详情（ID 来自 session list/start）"),
    ("/people/{影人ID}", "影人档案（ID 来自 people 域）"),
    ("/settings", "设置页"),
]


async def _agent_system_prompt() -> str:
    """组装本次运行的系统提示词：通用正文 + 部署环境事实。

    日志目录是 API 层的配置（LOG_DIR），按 prompts.build_system_prompt 的
    设计走 extra_environment 传入——mclaw 已对 Agent 隐藏 logs 域（见
    services/mclaw_tool 的 _EXCLUDED_DOMAINS），排查问题靠这里给出的
    路径用 bash 直接检索。

    外部访问地址来自「设置 → 应用设置」（保存即生效），因此每次运行时
    现读设置存储，不做缓存；未配置时整块不输出——没有前缀，路由表也
    拼不出有效链接，避免模型给用户无效地址。
    """
    log_dir = Path(get_settings().log_dir).resolve()
    lines = [
        f"- 运行日志目录：{log_dir}，按天一个文件（movieclaw-YYYY-MM-DD.log）。"
        "排查问题时用 bash 的 grep/tail 直接检索日志。"
    ]
    external_url = (await get_setting_store().get(AppServerSetting)).external_url
    if external_url:
        routes = "\n".join(f"  {pattern}  {desc}" for pattern, desc in _PAGE_ROUTES)
        lines.append(
            f"- 本应用的外部访问地址：{external_url}。提到订阅、影片、库内条目等页面时，"
            "尽量附上可点击的 Markdown 链接：以外部访问地址为前缀，路径按下表拼接。"
            f"只拼表内路径，不要用容器内地址拼链接。\n{routes}"
        )
    return build_system_prompt("\n".join(lines))


async def _cli_env(session_id: str) -> dict[str, str]:
    """构造工作区里 mclaw CLI 的自动授权环境（docs/design/cli.md §6.2）。

    令牌按运行签发、短时效、无状态签名（改密轮换密钥即全体失效）；
    服务器地址走容器内回环直连。Agent 在工作区内执行 `mclaw ...` 即可
    直接操作本产品，全程零登录零交互。
    """
    settings = get_settings()
    token = await auth_service.issue_agent_token(session_id)
    return {
        "MOVIECLAW_SERVER": f"http://127.0.0.1:{settings.port}",
        "MOVIECLAW_TOKEN": token,
    }


async def _accept_user_message(
    payload: SessionStartPayload,
    identity: Principal,
    session: AsyncSession,
) -> ApiResponse[SessionMessageAcceptedView]:
    """持久化一条用户消息、创建后台运行并返回消息编号。

    ``run_id`` 只服务于进程内任务调度、心跳和事件日志，不进入公开协议。
    后续消息的上下文始终从服务端转录重建，调用方无需也不能注入历史。
    """
    # 递归硬闸（docs/design/agent-cli-integration.md §4）：Agent 工作区令牌
    # 不允许再发起新的 Agent 运行——工具层已有软闸，这里是绕过工具（curl 等）
    # 也拦得住的最后防线。放在一切校验/落盘之前。
    if identity.kind == "agent":
        raise BadRequestException("Agent 工作区内不能再发起新的 Agent 运行（禁止递归）")

    store = get_agent_session_store()
    repo = AgentSessionRepository(session)

    # 先组装路由器：内部会校验模型供应商是否已配置，未配置时抛 404。必须在任何
    # 会话记录落盘（转录文件 / 索引行）之前完成——否则校验失败时，前端虽然收到
    # 正确的错误提示，磁盘上却已残留一条空会话，下次刷新侧栏会冒出来。
    llm_router = await acquire_llm_router(session)

    if payload.session_id:
        session_id = payload.session_id
        row = await repo.get(session_id)
        if row is None:
            raise NotFoundException("Agent 会话不存在")
        if is_running(row):
            raise BadRequestException("该会话已有正在进行的运行，请先停止或等待完成")
        history = store.build_history(session_id)
        _, existing_entries = store.read(session_id)
        entry_count = len(existing_entries)
    else:
        header = store.create()
        session_id = header.session_id
        await repo.create(session_id, title=None)
        history = []
        entry_count = 0

    return await _launch_user_message(
        session_id=session_id,
        content=payload.content,
        model=payload.model,
        history=history,
        entry_count=entry_count,
        llm_router=llm_router,
    )


async def _launch_user_message(
    *,
    session_id: str,
    content: str,
    model: str,
    history: list[ChatMessage],
    entry_count: int,
    llm_router: LlmRouter,
    tools: list[AgentTool] | None = None,
    system_prompt: str | None = None,
) -> ApiResponse[SessionMessageAcceptedView]:
    """在已校验的会话链尾追加用户消息并启动后台处理。"""
    actual_tools = tools if tools is not None else get_agent_tools(await _cli_env(session_id))
    actual_system_prompt = system_prompt or await _agent_system_prompt()
    recorder = AgentSessionRecorder(
        get_agent_session_store(), session_id, entry_count=entry_count
    )
    message_id = await recorder.record_user_message(content)

    runner = AgentRunner(
        llm_router,
        tools=actual_tools,
        on_message=recorder.on_message,
        on_compaction=recorder.on_compaction,
    )
    params = AgentStartParams(
        input=content,
        history=history,
        model=model,
        system_prompt=actual_system_prompt,
    )
    run_id = get_agent_run_registry().start(
        runner,
        params,
        session_id=session_id,
        on_terminal=recorder.on_terminal,
    )
    await recorder.begin(run_id)
    return ok(
        SessionMessageAcceptedView(session_id=session_id, message_id=message_id),
        message="用户消息已提交",
    )


@router.post(
    "",
    response_model=ApiResponse[SessionMessageAcceptedView],
    status_code=202,
    summary="开始新会话，或向已有会话发送用户消息",
    operation_id="session.start",
)
async def start_session(
    payload: SessionStartPayload,
    identity: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SessionMessageAcceptedView]:
    """提交用户消息并异步启动模型处理。

    ``session_id`` 留空时创建新会话，传入时从该会话的服务端轨迹重建历史后
    继续；调用方不能自行注入历史。返回的 ``message_id`` 是新 user message
    的稳定编号。处理事件用 ``session.follow`` 订阅；已有消息正在处理时拒绝。
    持内部 ``agent:`` 令牌调用会被拒绝，防止 Agent 递归启动 Agent。
    """
    return await _accept_user_message(payload, identity, session)


@router.get(
    "",
    response_model=ApiResponse[list[SessionSummary]],
    summary="最近会话列表（按最后活跃时间倒序）",
    operation_id="session.list",
)
async def list_sessions(
    limit: int = Query(default=50, description="返回条数上限"),
    offset: int = Query(default=0, description="分页偏移（跳过前 N 条）"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[SessionSummary]]:
    """按最近活动时间倒序分页返回会话摘要。

    ``running`` 表示当前是否有消息正在处理；``entry_count`` 同时统计 message
    与 compaction。最多返回 200 条，公开协议不披露内部运行编号。
    """
    rows = await AgentSessionRepository(session).list_recent(
        limit=min(limit, 200), offset=max(offset, 0)
    )
    return ok([SessionSummary.from_model(row) for row in rows])


@router.get(
    "/{session_id}",
    response_model=ApiResponse[SessionTranscriptView],
    summary="读取会话的完整轨迹",
    operation_id="session.get-transcript",
)
async def get_session_transcript(
    session_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SessionTranscriptView]:
    """一次返回按写入顺序排列的完整 message/compaction 轨迹。

    结果不分页、不截断，长会话响应可能很大。user message 的 ``message_id``
    可作为 ``session.retry`` 的定位参数；compaction 记录不会隐藏更早的原始消息。
    handoff 记录只带来源信息，继承上下文的快照不在此重复下发（见
    ``SessionHandoffEntryView.replacement_history``）。
    """
    row = await AgentSessionRepository(session).get(session_id)
    if row is None:
        raise NotFoundException("Agent 会话不存在")
    _, entries = get_agent_session_store().read(session_id)
    return ok(
        SessionTranscriptView(
            session=SessionSummary.from_model(row),
            entries=[_entry_view(e, include_handoff_snapshot=False) for e in entries],
        )
    )


def _entry_view(
    entry: SessionMessageEntry | SessionCompactionEntry | SessionHandoffEntry,
    *,
    include_handoff_snapshot: bool = True,
) -> SessionMessageEntryView | SessionCompactionEntryView | SessionHandoffEntryView:
    """内部 JSONL entry → 公开的 message/compaction/handoff 判别联合。

    handoff 的 replacement_history 只在 fork 创建响应里下发一次；常规轨迹
    读取留空，避免每次打开分叉会话都重复传输整份继承上下文。
    """
    if isinstance(entry, SessionHandoffEntry):
        return SessionHandoffEntryView(
            handoff_id=entry.uuid,
            parent_id=entry.parent_uuid,
            timestamp=entry.timestamp,
            source_session_id=entry.source_session_id,
            source_leaf_id=entry.source_leaf_uuid,
            source_title=entry.source_title,
            replacement_history=(
                [SessionMessageView.from_model(message) for message in entry.replacement_history]
                if include_handoff_snapshot
                else []
            ),
        )
    if isinstance(entry, SessionCompactionEntry):
        return SessionCompactionEntryView(
            compaction_id=entry.uuid,
            parent_id=entry.parent_uuid,
            timestamp=entry.timestamp,
            summary=entry.summary,
            replacement_history=[
                SessionMessageView.from_model(message) for message in entry.replacement_history
            ],
            tokens_before=entry.tokens_before,
            tokens_after=entry.tokens_after,
        )
    return SessionMessageEntryView(
        message_id=entry.uuid,
        parent_id=entry.parent_uuid,
        timestamp=entry.timestamp,
        message=SessionMessageView.from_model(entry.message),
        model=entry.model,
        usage=entry.usage,
        finish_reason=entry.finish_reason,
    )


@router.post(
    "/{session_id}/fork",
    response_model=ApiResponse[SessionTranscriptView],
    status_code=201,
    summary="从已有上下文创建独立的新会话",
    operation_id="session.fork",
    openapi_extra={"x-cli-hidden": True},
)
async def fork_session(
    session_id: str,
    identity: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SessionTranscriptView]:
    """把源会话此刻的有效上下文快照到一个全新的会话编号。

    源会话保持不变；新会话不继承运行编号、心跳或事件流，源文件之后被删除也
    不影响新会话。运行中的源会话拒绝分叉，避免拿到一份仍在变化的半截快照。
    此操作不调用模型，新会话创建后等待用户提交第一条后续消息。

    运行状态检查与读取快照之间存在极小的竞态窗口（另一客户端可能恰好发起
    新一轮运行）；快照里悬空的 tool_call 会在写入新会话时修复为合法回执，
    因此即使踩中窗口，新会话拿到的仍是一份协议完整的上下文。
    """
    if identity.kind == "agent":
        raise BadRequestException("Agent 工作区内不能创建新的 Agent 会话（禁止递归）")

    store = get_agent_session_store()
    repo = AgentSessionRepository(session)
    source = await repo.get(session_id)
    if source is None:
        raise NotFoundException("Agent 会话不存在")
    if is_running(source):
        raise BadRequestException("源会话正在运行中，请先停止或等待完成后再创建新会话")

    source_title = source.title or source.last_prompt
    header, handoff = store.fork(session_id, source_title=source_title)
    target_title = (
        (source_title if source_title.startswith("续：") else f"续：{source_title}")[:80]
        if source_title
        else None
    )
    target = await repo.create(header.session_id, title=target_title)
    await repo.touch_after_append(
        header.session_id,
        leaf_uuid=handoff.uuid,
        entry_count=1,
    )
    # touch_after_append 在同一 AsyncSession 内提交后，重新读取确保响应携带最终索引。
    target = await repo.get(header.session_id) or target
    return ok(
        SessionTranscriptView(
            session=SessionSummary.from_model(target),
            entries=[_entry_view(handoff)],
        ),
        message="已创建续接会话",
    )


@router.patch(
    "/{session_id}",
    response_model=ApiResponse[SessionSummary],
    summary="重命名会话",
    operation_id="session.rename",
)
async def rename_session(
    session_id: str,
    payload: SessionRenamePayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SessionSummary]:
    """修改会话标题，不改变任何消息、模型上下文或完整轨迹。"""
    row = await AgentSessionRepository(session).rename(session_id, payload.title)
    if row is None:
        raise NotFoundException("Agent 会话不存在")
    return ok(SessionSummary.from_model(row), message="会话已重命名")


@router.post(
    "/{session_id}/compact-context",
    response_model=ApiResponse[SessionContextCompactionView],
    summary="手动压缩会话上下文",
    operation_id="session.compact-context",
)
async def compact_session_context(
    session_id: str,
    identity: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SessionContextCompactionView]:
    """压缩后续模型请求使用的上下文，不删除或截断完整轨迹。

    服务端保留预算内的用户原话，并生成一份历史交接摘要；结果作为 compaction
    entry 追加到完整轨迹，后续 ``start`` 从该压缩历史继续。此操作会调用模型，
    沿用会话最近一次使用的模型（没有记录时用默认模型）；运行中或空会话拒绝。
    """
    store = get_agent_session_store()
    repo = AgentSessionRepository(session)
    row = await repo.get(session_id)
    if row is None:
        raise NotFoundException("Agent 会话不存在")
    if is_running(row):
        raise BadRequestException("该会话正在运行中，请等待完成后再压缩")

    llm_router = await acquire_llm_router(session)
    history = store.build_history(session_id)
    if not history:
        raise BadRequestException("会话没有可压缩的内容")

    # 沿用会话最近一次运行的模型（转录里 assistant 行带 model 元数据）
    _, entries = store.read(session_id)
    model = next(
        (e.model for e in reversed(entries) if isinstance(e, SessionMessageEntry) and e.model),
        "",
    )

    messages = [ChatMessage(role="system", content=await _agent_system_prompt()), *history]
    result = await compact(llm_router, model, messages, ModelSettings())
    if result is None:
        raise UpstreamServiceException("压缩失败：模型未能生成摘要，请稍后重试")

    entry = store.append_compaction(session_id, result)
    await repo.touch_after_append(
        session_id,
        leaf_uuid=entry.uuid,
        entry_count=len(entries) + 1,
    )
    return ok(
        SessionContextCompactionView(
            summary=result.summary,
            tokens_before=result.tokens_before,
            tokens_after=result.tokens_after,
            compaction_id=entry.uuid,
        ),
        message="会话上下文已压缩",
    )


@router.post(
    "/{session_id}/retry",
    response_model=ApiResponse[SessionMessageAcceptedView],
    status_code=202,
    summary="重新提交指定用户消息（可替换问题内容）",
    operation_id="session.retry",
)
async def retry_session_message(
    session_id: str,
    payload: SessionRetryPayload,
    identity: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SessionMessageAcceptedView]:
    """从指定 user message 重新提问，并异步启动新的模型处理。

    ``content`` 留空时重提目标消息原文，传入时用新问题替换；目标消息及其后的
    现有 message/compaction 轨迹会被永久删除，新问题取得新的 ``message_id``。
    目标必须是 user message，运行中的会话拒绝。服务端会先校验会话、目标消息
    与模型配置，再改写事实源。CLI 与 Web 共用此一步重试入口。
    """
    if identity.kind == "agent":
        raise BadRequestException("Agent 工作区内不能再发起新的 Agent 运行（禁止递归）")

    store = get_agent_session_store()
    repo = AgentSessionRepository(session)
    row = await repo.get(session_id)
    if row is None:
        raise NotFoundException("Agent 会话不存在")
    if is_running(row):
        raise BadRequestException("该会话正在运行中，请先停止运行再重试")

    _, entries = store.read(session_id)
    target = next((entry for entry in entries if entry.uuid == payload.message_id), None)
    if target is None:
        raise NotFoundException("会话中没有这条记录，可能已被改写")
    if not isinstance(target, SessionMessageEntry) or target.message.role != "user":
        raise BadRequestException("只能重试用户消息")
    content = payload.content or target.message.text()

    # 供应商校验和运行所需上下文在删除轨迹前准备完成；下面才进入不可逆阶段。
    llm_router = await acquire_llm_router(session)
    system_prompt = await _agent_system_prompt()
    tools = get_agent_tools(await _cli_env(session_id))

    store.discard_from_user_message(session_id, payload.message_id)
    history = store.build_history(session_id)
    _, remaining_entries = store.read(session_id)
    summary = store.summarize(session_id)
    await repo.resync_after_discard(
        session_id,
        leaf_uuid=summary.leaf_uuid,
        entry_count=summary.entry_count,
        last_prompt=summary.last_prompt,
    )

    return await _launch_user_message(
        session_id=session_id,
        content=content,
        model=payload.model,
        history=history,
        entry_count=len(remaining_entries),
        llm_router=llm_router,
        tools=tools,
        system_prompt=system_prompt,
    )


@router.delete(
    "/{session_id}",
    response_model=ApiResponse[dict],
    summary="删除会话（转录文件与索引一并删除）",
    operation_id="session.delete",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def delete_session(
    session_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """永久删除会话的完整轨迹文件与索引记录；不可恢复。

    会话正在处理消息时拒绝，需先调用 ``session.stop`` 并等待取消终态。
    """
    repo = AgentSessionRepository(session)
    row = await repo.get(session_id)
    if row is None:
        raise NotFoundException("Agent 会话不存在")
    if is_running(row):
        raise BadRequestException("该会话正在运行中，请先停止运行再删除")
    get_agent_session_store().delete(session_id)
    await repo.delete(session_id)
    return ok({}, message="会话已删除")


@router.get(
    "/{session_id}/events",
    summary="跟随会话当前消息的处理事件（SSE，支持断线续传）",
    operation_id="session.follow",
    openapi_extra={
        "x-cli-stream": {"terminal_events": ["agent_done", "agent_error", "agent_cancelled"]},
    },
)
async def follow_session(
    session_id: str,
    last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """跟随会话当前或进程内保留的最近一次消息处理，直到运行进入终态。

    先回放游标后的事件，再实时推送新事件。SSE ``id`` 是本次处理内从 1
    开始的递增序号；首次订阅不传 ``Last-Event-ID`` 即回放全部，重连时传
    最后已处理的 id，只接收缺失事件。心跳不进入事件日志，也不推进游标。
    没有当前或尚在保留期内的最近一次处理时返回 404；持久记录应读取完整轨迹。
    """
    registry = get_agent_run_registry()
    cursor = last_event_id or 0
    # 在 StreamingResponse 建立前完成存在性和游标校验，确保 404/400 仍能以
    # 标准 JSON 错误返回，而不是已经发出 200 后才在生成器里异常断流。
    initial_events, initial_terminal = await registry.get_session_events(
        session_id,
        cursor,
        timeout_seconds=0,
    )

    async def event_source():
        nonlocal cursor
        events = initial_events
        terminal = initial_terminal
        while True:
            for stored in events:
                cursor = stored.sequence
                event = stored.event
                yield (
                    f"id: {stored.sequence}\n"
                    f"event: {event.type}\n"
                    f"data: {event.model_dump_json(exclude_none=True, exclude={'run_id'})}\n\n"
                )
            if terminal:
                return
            events, terminal = await registry.get_session_events(
                session_id,
                cursor,
                timeout_seconds=15,
            )
            if not events and not terminal:
                yield ": heartbeat\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            # SSE 反缓冲三件套（原理见 routes/search.py 的流式端点）
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/{session_id}/stop",
    response_model=ApiResponse[dict],
    summary="停止会话当前消息的模型处理",
    operation_id="session.stop",
)
async def stop_session(
    session_id: str,
    identity: Principal = Depends(require_login),
) -> ApiResponse[dict]:
    """请求取消当前或进程内保留的最近一次消息处理。

    同一次处理重复停止是幂等的，SSE 会以 ``agent_cancelled`` 事件收尾；没有
    尚在保留期内的处理时返回 404。内部 Agent 可以停止其他会话，但不能停止
    承载自己的当前会话。
    """
    if identity.kind == "agent" and identity.agent_session_id == session_id:
        raise BadRequestException("当前 Agent 不能停止承载自己的会话")
    await get_agent_run_registry().cancel_session(session_id)
    return ok({}, message="已请求停止会话")
