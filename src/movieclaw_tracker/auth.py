from __future__ import annotations

import abc
import html
import logging
import re
import urllib.parse
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING, Protocol, Union

from parsel import Selector

from movieclaw_tracker.exceptions import TrackerAuthError
from movieclaw_tracker.http import HttpClient
from movieclaw_tracker.models import AuthResult, AuthState

if TYPE_CHECKING:
    from movieclaw_tracker.selectors import LoginSelectors

logger = logging.getLogger("movieclaw_tracker.auth")

# Cookie 输入类型：支持字符串和字典两种常见格式
# - str : 从浏览器 DevTools / cURL 复制的原始字符串，如 "uid=1; pass=abc"
# - dict: 键值对字典，如 {"uid": "1", "pass": "abc"}
CookieInput = Union[str, "dict[str, str]"]


def parse_cookies(raw: CookieInput) -> dict[str, str]:
    """将各种常见 cookie 格式统一转换为内部使用的 ``dict[str, str]``。

    支持格式
    --------
    - **字符串**（最常用）：浏览器 DevTools → Application → Cookies 复制后的
      原始字符串，如 ``"c_secure_uid=123; c_secure_pass=abc; c_secure_ssl=yes"``
    - **字典**：已拆分好的键值对，如 ``{"c_secure_uid": "123"}``

    参数
    ----
    raw:
        任意上述格式的 cookie 输入。

    返回
    ----
    dict[str, str]
        可直接交给 httpx 使用的字典。
    """
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}

    # 字符串格式：用标准库 SimpleCookie 解析，能正确处理带引号的值及空格
    jar: SimpleCookie = SimpleCookie()
    jar.load(raw.strip())
    return {k: v.value for k, v in jar.items()}


# ---------------------------------------------------------------------------
# Cookie 持久化层
# ---------------------------------------------------------------------------


class CookieStore(Protocol):
    """Cookie 存储协议。可扩展为文件、Redis、DB 等后端。"""

    async def load(self, site_id: str) -> dict[str, str] | None: ...

    async def save(self, site_id: str, cookies: dict[str, str]) -> None: ...

    async def delete(self, site_id: str) -> None: ...


class MemoryCookieStore:
    """内存 Cookie 存储。进程重启后丢失。"""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, str]] = {}

    async def load(self, site_id: str) -> dict[str, str] | None:
        return self._data.get(site_id)

    async def save(self, site_id: str, cookies: dict[str, str]) -> None:
        self._data[site_id] = cookies
        logger.debug("Saved cookies for site=%s", site_id)

    async def delete(self, site_id: str) -> None:
        self._data.pop(site_id, None)
        logger.debug("Deleted cookies for site=%s", site_id)


# ---------------------------------------------------------------------------
# 验证码识别协议
# ---------------------------------------------------------------------------


class CaptchaSolver(Protocol):
    """验证码识别协议。

    不同的 OCR 服务（本地 ddddocr、远程打码平台等）实现此协议即可接入登录流程。
    仅需实现 solve 方法：接收验证码图片的原始字节，返回识别出的字符串。
    """

    async def solve(self, image_data: bytes) -> str:
        """识别验证码图片，返回文本结果。

        参数
        ----
        image_data:
            验证码图片的原始字节（通常为 JPEG 或 PNG）。

        返回
        ----
        str
            识别出的验证码文本。
        """
        ...


# ---------------------------------------------------------------------------
# 认证策略层
# ---------------------------------------------------------------------------


class AuthProvider(abc.ABC):
    """认证提供者。每种认证方式实现一个 Provider。"""

    @abc.abstractmethod
    async def authenticate(self, client: HttpClient) -> AuthResult:
        """执行认证流程。返回结果可能是成功、失败或需要用户干预。"""

    @abc.abstractmethod
    async def check(self, client: HttpClient) -> AuthState:
        """检查当前认证状态是否有效。"""

    def bind(  # noqa: B027 -- 默认空操作是刻意设计，仅需要站点上下文的 Provider 覆写
        self, *, base_url: str, login_selectors: LoginSelectors | None = None
    ) -> None:
        """由工厂函数调用，注入站点上下文（base_url、登录选择器等）。

        默认空操作。需要站点上下文的 Provider（如 CredentialAuthProvider）覆写此方法。
        """


class CookieAuthProvider(AuthProvider):
    """用户直接提供 cookie 的认证模式，也是最简单的认证模式。

    接受任意常见的 cookie 格式，内部统一转换为 dict 再交给 HTTP 客户端。

    示例
    ----
    字符串格式（最常用，直接从浏览器复制）::

        CookieAuthProvider("c_secure_uid=123; c_secure_pass=abc")

    字典格式::

        CookieAuthProvider({"c_secure_uid": "123", "c_secure_pass": "abc"})
    """

    def __init__(self, cookies: CookieInput) -> None:
        self._cookies: dict[str, str] = parse_cookies(cookies)

    async def authenticate(self, client: HttpClient) -> AuthResult:
        client.cookies = self._cookies
        return AuthResult(
            success=True,
            state=AuthState.AUTHENTICATED,
            cookies=self._cookies,
        )

    async def check(self, client: HttpClient) -> AuthState:
        # CookieAuthProvider 无法主动验证有效性（没有站点信息）
        # 具体的有效性检查由上层在首次请求失败时触发
        return AuthState.AUTHENTICATED


class ApiKeyAuthProvider(AuthProvider):
    """API-Key 认证模式，用于走站点 API 而非网页的站点（如 M-Team）。

    与 cookie 不同，API-Key 通过 HTTP 请求头传递（默认 ``x-api-key``），
    认证时注入到 HttpClient 的默认请求头中，后续所有 API 请求自动携带。

    获取方式（以 M-Team 为例）
    ------------------------
    登录网页站点 → 控制台/实验室 → 密钥（API Key）中创建并复制。

    示例
    ----
    ::

        provider = ApiKeyAuthProvider("your-api-key")
        site = await create_site("mteam", auth_provider=provider)
        await site.authenticate()
    """

    def __init__(self, api_key: str, *, header_name: str = "x-api-key") -> None:
        self._api_key = api_key
        self._header_name = header_name

    async def authenticate(self, client: HttpClient) -> AuthResult:
        # 直接把 API-Key 写入默认请求头，无需网络请求即可完成"认证"
        client.set_header(self._header_name, self._api_key)
        return AuthResult(success=True, state=AuthState.AUTHENTICATED)

    async def check(self, client: HttpClient) -> AuthState:
        # 无法离线校验 Key 有效性，实际有效性由首次 API 请求的响应决定
        return AuthState.AUTHENTICATED


class CredentialAuthProvider(AuthProvider):
    """用户名/密码登录认证。

    完整流程：
    1. GET 登录页，检测是否存在验证码
    2. 若有验证码，下载图片并调用 CaptchaSolver 识别
    3. POST 登录表单（用户名 + 密码 + 可选验证码）
    4. 检查响应页面中是否出现 logout 链接判定登录成功
    5. 验证码识别失败时自动重试（最多 MAX_CAPTCHA_RETRIES 次）

    使用示例::

        provider = CredentialAuthProvider(username="user", password="pass")
        # base_url 和 login_selectors 由 create_site 工厂函数通过 bind() 自动注入

    带验证码识别::

        provider = CredentialAuthProvider(
            username="user",
            password="pass",
            captcha_solver=my_solver,  # 实现 CaptchaSolver 协议的实例
        )
    """

    MAX_CAPTCHA_RETRIES: int = 3

    def __init__(
        self,
        *,
        username: str,
        password: str,
        captcha_solver: CaptchaSolver | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._captcha_solver = captcha_solver
        # 以下由 bind() 注入
        self._base_url: str | None = None
        self._selectors: LoginSelectors | None = None

    def bind(self, *, base_url: str, login_selectors: LoginSelectors | None = None) -> None:
        """接收站点上下文。由 create_site 工厂函数自动调用。"""
        self._base_url = base_url.rstrip("/")
        if login_selectors is not None:
            self._selectors = login_selectors

    @property
    def _login_selectors(self) -> LoginSelectors:
        """获取登录选择器，未绑定时使用默认值。"""
        if self._selectors is not None:
            return self._selectors
        from movieclaw_tracker.selectors import LoginSelectors

        return LoginSelectors()

    def _url(self, path: str) -> str:
        """拼接完整 URL。"""
        if self._base_url is None:
            raise TrackerAuthError(
                "CredentialAuthProvider 尚未绑定站点，请通过 create_site 创建或手动调用 bind()",
            )
        return f"{self._base_url}/{path.lstrip('/')}"

    @staticmethod
    def _form_action(form: Selector | None, form_url: str) -> str:
        """解析同源登录表单 action；缺失或空值时回退到登录页 URL。"""
        action = form.css("::attr(action)").get("").strip() if form else ""
        post_url = urllib.parse.urljoin(form_url, action or form_url)
        form_origin = urllib.parse.urlsplit(form_url)
        post_origin = urllib.parse.urlsplit(post_url)

        def normalized_origin(
            parts: urllib.parse.SplitResult,
        ) -> tuple[str, str | None, int | None]:
            default_port = {"http": 80, "https": 443}.get(parts.scheme)
            return parts.scheme, parts.hostname, parts.port or default_port

        if normalized_origin(form_origin) != normalized_origin(post_origin):
            raise TrackerAuthError("登录表单 action 指向跨站地址，已拒绝提交凭据")
        return post_url

    @staticmethod
    def _login_form(doc: Selector, sel: LoginSelectors) -> Selector | None:
        """选择同时包含配置用户名和密码控件的登录表单。"""
        for form in doc.css("form"):
            field_names = {node.attrib.get("name", "") for node in form.css("[name]")}
            if sel.username_field in field_names and sel.password_field in field_names:
                return form
        return None

    @staticmethod
    def _set_form_field(form_data: list[tuple[str, str]], name: str, value: str) -> None:
        """覆盖同名表单字段，同时保留其他字段的原始顺序和重复值。"""
        form_data[:] = [(key, current) for key, current in form_data if key != name]
        form_data.append((name, value))

    async def authenticate(self, client: HttpClient) -> AuthResult:
        """执行用户名/密码登录流程。"""
        sel = self._login_selectors
        login_url = self._url(sel.login_path)

        # 清空 cookies，确保干净的登录状态
        client.cookies = {}

        for attempt in range(1, self.MAX_CAPTCHA_RETRIES + 1):
            # 1. GET 登录页
            login_page_res = await client.raw_get(login_url)
            login_page_html = login_page_res.text
            doc = Selector(text=login_page_html)

            # 2. 检测验证码
            captcha_text: str | None = None
            captcha_hash: str | None = None
            captcha_match = re.search(sel.captcha_url_re, login_page_html)

            if captcha_match:
                logger.info("检测到验证码（第 %d 次尝试）", attempt)

                if self._captcha_solver is None:
                    # 没有验证码识别器，返回需要用户干预的状态
                    captcha_url = html.unescape(captcha_match.group(1))
                    if not captcha_url.startswith(("http://", "https://")):
                        captcha_url = self._url(captcha_url)
                    return AuthResult(
                        success=False,
                        state=AuthState.NEEDS_CAPTCHA,
                        message="登录页存在验证码，但未配置验证码识别器",
                        captcha_image_url=captcha_url,
                    )

                # 下载验证码图片
                captcha_rel_url = html.unescape(captcha_match.group(1))
                if captcha_rel_url.startswith(("http://", "https://")):
                    captcha_img_url = captcha_rel_url
                else:
                    captcha_img_url = self._url(captcha_rel_url)
                img_res = await client.raw_get(captcha_img_url)
                captcha_text = await self._captcha_solver.solve(img_res.content)
                logger.info("验证码识别结果：%s", captcha_text)

                # 提取 imagehash 隐藏字段
                hash_input = doc.css(f'input[name="{sel.captcha_hash_field}"]::attr(value)')
                captcha_hash = hash_input.get()

                # 也尝试从验证码 URL 的查询参数中提取 imagehash
                if not captcha_hash:
                    query_params = urllib.parse.parse_qs(
                        urllib.parse.urlparse(captcha_rel_url).query
                    )
                    hash_values = query_params.get(sel.captcha_hash_field, [])
                    if hash_values:
                        captcha_hash = hash_values[0]

            # 3. 构造登录表单
            # NexusPHP 站点通常把 logout 等参数放在 hidden input 中；
            # 保留重复字段并忽略 disabled 控件，与浏览器 successful controls 语义一致。
            form = self._login_form(doc, sel)
            form_data: list[tuple[str, str]] = []
            for input_el in form.css("input[type='hidden'][name]:not([disabled])") if form else ():
                form_data.append((input_el.attrib["name"], input_el.attrib.get("value", "")))

            self._set_form_field(form_data, sel.username_field, self._username)
            self._set_form_field(form_data, sel.password_field, self._password)
            for select_name, default_value in sel.select_defaults:
                if form and form.css(f"select[name='{select_name}']"):
                    self._set_form_field(form_data, select_name, default_value)

            # 验证码相关字段
            if captcha_text:
                self._set_form_field(form_data, sel.captcha_field, captcha_text)
            if captcha_hash:
                self._set_form_field(form_data, sel.captcha_hash_field, captcha_hash)

            # 站点特殊的额外表单字段
            for key, value in sel.extra_form_data:
                self._set_form_field(form_data, key, value)

            # 4. POST 登录。httpx.AsyncClient 不支持以键值对列表作为 data；
            # 先编码为标准表单字节，既保留重复字段，也避免生成同步请求流。
            headers = {
                "Referer": login_url,
                "Content-Type": "application/x-www-form-urlencoded",
            }
            post_url = self._form_action(form, login_url)
            form_body = urllib.parse.urlencode(form_data, doseq=True).encode("utf-8")
            res = await client.raw_post(post_url, content=form_body, headers=headers)
            response_text = res.text

            # 5. 判定登录结果
            result_doc = Selector(text=response_text)

            # 成功：页面中有 logout 链接，或重定向到了首页
            if result_doc.css(sel.success_css).get():
                cookies = self._extract_cookies(client)
                logger.info("登录成功，获取到 Cookies")
                return AuthResult(
                    success=True,
                    state=AuthState.AUTHENTICATED,
                    cookies=cookies,
                )

            # 检查是否为验证码错误（可重试）
            error_el = result_doc.css(sel.error_css)
            error_text = error_el.css("::text").get("") if error_el else ""

            if captcha_match and self._is_captcha_error(error_text):
                logger.warning(
                    "验证码错误（第 %d/%d 次），将重试",
                    attempt,
                    self.MAX_CAPTCHA_RETRIES,
                )
                if attempt < self.MAX_CAPTCHA_RETRIES:
                    continue
                # 已达最大重试次数
                raise TrackerAuthError(
                    f"验证码识别连续失败 {self.MAX_CAPTCHA_RETRIES} 次，登录中止",
                )

            # 其他错误（用户名/密码错误等），不可重试
            raise TrackerAuthError(
                f"登录失败：{error_text or '用户名或密码错误'}",
            )

        # 理论上不会到这里，但做防御
        raise TrackerAuthError("登录失败：超出最大重试次数")

    async def check(self, client: HttpClient) -> AuthState:
        """通过访问站点首页检查当前会话是否仍然有效。"""
        sel = self._login_selectors
        try:
            res = await client.raw_get(self._url(""))
            doc = Selector(text=res.text)
            if doc.css(sel.success_css).get():
                return AuthState.AUTHENTICATED
            return AuthState.EXPIRED
        except Exception:
            logger.warning("检查登录状态时发生异常", exc_info=True)
            return AuthState.EXPIRED

    @staticmethod
    def _extract_cookies(client: HttpClient) -> dict[str, str]:
        """从 HTTP 客户端中提取当前所有 cookie 为字典。"""
        return {name: value for name, value in client.cookies.items()}

    @staticmethod
    def _is_captcha_error(error_text: str) -> bool:
        """判断错误信息是否为验证码相关错误。"""
        captcha_keywords = ("验证码", "captcha", "imagestring", "imagecode", "code")
        lower = error_text.lower()
        return any(kw in lower for kw in captcha_keywords)


# ---------------------------------------------------------------------------
# 认证编排层
# ---------------------------------------------------------------------------


class AuthManager:
    """编排认证流程：优先用缓存 cookie → 过期则用 Provider 重新认证 → 持久化。"""

    def __init__(
        self,
        *,
        provider: AuthProvider,
        store: CookieStore,
        site_id: str,
    ) -> None:
        self._provider = provider
        self._store = store
        self._site_id = site_id

    async def authenticate(self, client: HttpClient) -> AuthResult:
        # 1. 尝试从 store 加载已保存的 cookies
        saved = await self._store.load(self._site_id)
        if saved:
            client.cookies = saved
            state = await self._provider.check(client)
            if state == AuthState.AUTHENTICATED:
                logger.debug("Loaded cached cookies for site=%s", self._site_id)
                return AuthResult(success=True, state=state, cookies=saved)
            logger.info("Cached cookies expired for site=%s, re-authenticating", self._site_id)

        # 2. 缓存无效或不存在，执行完整认证
        result = await self._provider.authenticate(client)

        # 3. 成功则持久化
        if result.success and result.cookies:
            await self._store.save(self._site_id, result.cookies)

        return result

    async def check_auth(self, client: HttpClient) -> bool:
        state = await self._provider.check(client)
        return state == AuthState.AUTHENTICATED

    async def deauthenticate(self, client: HttpClient) -> None:
        await self._store.delete(self._site_id)
        client.cookies = {}
        logger.info("Deauthenticated site=%s", self._site_id)
