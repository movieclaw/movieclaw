# movieclaw 设备授权设计（Mac Worker · mclaw CLI · 第三方 Agent）

> 目标：让**任何本机程序**——Mac 转码 Worker、mclaw CLI、用户自己的 Claude Code
> 或其他 Agent——用同一套流程接入一台用户自部署的 movieclaw，全程不需要抄写
> 密钥、不需要把管理员密码交给任何程序。
>
> 三条硬要求：
>
> 1. **密码和令牌都不进客户端的参数面**。密码只在浏览器里用；令牌只在进程之间
>    传递，不显示、不复制、不截图。
> 2. **每台设备一枚独立令牌**，可命名、可单独吊销、可归因，互不影响。
> 3. **一套机制服务所有客户端**。Worker 与 CLI 不允许各写一套认证。
>
> 结论先行：**实现一套「设备授权流程」——客户端出示短码、人在已登录的网页上
> 批准、令牌只回到发起进程**；令牌的权限继承批准者，落进现有 `ApiTokensSetting`
> 存储；
> Worker 的共享令牌与粘贴式配对码整个删除。
>
> 本文取代 `docs/design/cli.md` §8.1 的 PAT 方案与 `docs/design/remote-transcode.md`
> 中「网页生成配对码 → 粘贴进 Worker」的部分。产品内 Agent 的短时签名令牌
> （`docs/design/agent-cli-integration.md`）**不变**，只是接进同一个验签入口。

---

## 0. 现状盘点

代码基线 `8f940e5`。今天服务端一共有三套互不相干的凭证机制：

| 机制 | 落点 | 形态 | 问题 |
|---|---|---|---|
| 会话 Cookie | `services/auth.py:406-482` | itsdangerous 签名，7/30 天 | CLI 借用它，到期即断 |
| PAT | `services/auth.py:489-521` | sha256 落库，`mclaw_` 前缀 | 只能在网页手工建；无过期、无归属、无使用记录 |
| Worker 共享令牌 | `services/playback/remote_worker.py:508` | 设置里一个固定字符串 | 所有 Worker 共用一枚 |
| 产品内 Agent 令牌 | `services/auth.py:523-529` | 无状态签名，2 小时 | 设计正确，保留不动 |

三个必须解决的问题：

1. **共享令牌不可归因、不可单独吊销**。换台 Mac 要重发一次令牌，想踢掉某台机器
   只能改全局令牌——所有机器一起掉线。网页回答不了「现在有谁连着」。
2. **配对码本身就是凭据**。`PairingCode.swift` 的 `base64url({url, token})` 解开
   就是长期令牌，会留在剪贴板、聊天记录、截图里，且没有兑换与失效的概念。
3. **所有令牌都等价管理员**（`Principal.is_admin` 对 pat/agent 恒为 True）。一台放在
   书房的转码机，持有的凭证足以删掉整个媒体库。

Mac Worker 尚未正式发版，**不需要保留任何兼容分支**，共享令牌那条路径直接删除。

---

## 1. 总体设计

```
┌ 客户端（Worker / CLI / 任意 Agent）──────────────┐
│  ① 拿到地址 → GET /health 验证可达               │
│  ② POST /auth/device/authorize → 拿到 user_code   │
│  ③ 把 user_code 显示给人，同时开始轮询            │
│  ⑤ POST /auth/device/token → 拿到令牌，存本地     │
└───────────────┬──────────────────────────────────┘
                │ 人拿着 user_code 走到浏览器
┌ 浏览器（已登录 movieclaw 的那个人）──────────────┐
│  ④ /settings → 设备 → 看到待批准请求：            │
│     谁、什么类型、来自哪个 IP、要什么权限、码是多少 │
│     批准 → 服务端此刻才生成并落库令牌              │
└──────────────────────────────────────────────────┘
```

### 1.1 为什么是「客户端出码、网页批准」，而不是反过来

这是与现有 Worker 配对码最大的一处反向，理由有三条，缺一不可：

- **凭据路径唯一**。网页上出现的只有一段五分钟作废的短码，它不是凭据；真正的
  令牌通过 `device_code` 直接回到发起请求的那个进程。令牌从不显示在任何屏幕上，
  也就不会进剪贴板、聊天记录、截图或 Agent 的上下文。
- **人做的是有信息的决定**。审批卡上写着设备名、类型、来源 IP、将获得的权限，
  用户是在批准一台具体的机器，而不是复制一串看不懂的字符。真正的安全控制是
  **核对配对码**；来源 IP 是辅助线索，而且常常拿不到（见 §2.1）。
- **地址那一坎已经在第一步解决**。自部署产品最容易劝退用户的是「该填哪个地址」
  （`127.0.0.1`？内网 IP？反代域名？），把它单独作为第一步并当场验证，
  错了立刻知道，而不是保存之后表现为「连不上」。

业界同形态：GitHub CLI 的 device flow、Stripe CLI 的 `stripe login`、以及所有电视端
登录。它们共同的前提也正是这里成立的前提——设备有屏幕能显示短码，人另有一个
已登录的浏览器会话。

### 1.2 与现有两套配对机制的关系

产品里已有两处「配对」，本设计**复用其中一处的实现模型，替换另一处**：

- **复用** `services/im_channel.py:109-145` 的 `PairChallenge`：内存挑战 + TTL +
  尝试次数上限 + 终态留存期（`_CHALLENGE_LINGER_S`，让轮询方还能读到终态）。
  设备授权的挑战对象与它同构，直接照抄这套生命周期管理。
- **替换** `PairingCode.swift`：整文件删除。它解决的问题（不让用户手抄地址和
  令牌）被新流程更彻底地解决了——用户连令牌都不需要见到。

---

## 2. 协议

### 2.1 端点

四个端点，两个新增匿名端点 + 一组管理端点。全部走 `/api/v1`。

```
GET  /api/v1/health                                     匿名（已有）
     → {status, service, environment, spec_hash}
     用途：地址可达性验证。不新增接口，也**不给它加版本号**——它是匿名
     端点，为一句「已连接 movieclaw x.y.z」的文案而向未登录者公开精确
     版本，对自部署用户不是好交易；service 字段已经能回答「连上的是不是
     movieclaw」。

POST /api/v1/auth/device/authorize                      匿名 + 限流（新增）
     ← {client_type: "worker"|"cli",
        client_name: "Yi的Mac-mini"}
     客户端不声明权限：它只说自己是什么形态、叫什么名字。
     权限由批准者决定（§4）。
     → {user_code: "MCLW-7F3K",
        device_code: "<高熵不可猜>",
        verification_uri: "http://10.1.1.5:3000/settings/devices",
        interval: 2, expires_in: 300}

POST /api/v1/auth/device/token                          匿名 + 限流（新增）
     ← {device_code}
     → 202 {status: "pending"}                          尚未批准
       200 {token, granted_by, expires_at, client_name}  已批准，兑换一次即作废
                  granted_by 仅用于客户端回显「你现在是谁」，不是权限声明
       400 {code: "AUTHORIZATION_DENIED"}               人点了拒绝
       400 {code: "EXPIRED_TOKEN"}                      超过 expires_in
       429 {code: "SLOW_DOWN"}                          轮询过快

GET    /api/v1/auth/devices                             require_admin（新增）
DELETE /api/v1/auth/devices/{token_id}                  require_admin（新增）
POST   /api/v1/auth/devices/requests/{user_code}/approve   require_admin（新增）
POST   /api/v1/auth/devices/requests/{user_code}/deny       require_admin（新增）
GET    /api/v1/auth/devices/requests                    require_admin（新增）
```

`POST /api/v1/auth/tokens`（现有的手工建 PAT）**保留**：CI、脚本、以及任何不方便
走交互批准的场景仍然需要它。它与设备流写进同一份存储，在设备列表里一起显示，
只是 `client_type` 标为 `manual`。

### 2.2 时序

```
客户端                       服务端                         浏览器（管理员会话）
  │                            │                                   │
  ├─ GET /health ─────────────>│                                   │
  │<── 200 {version} ──────────┤                                   │
  │                            │                                   │
  ├─ POST device/authorize ───>│  生成 challenge（内存）             │
  │<── {user_code, device_code}┤  状态 pending，TTL 300s            │
  │                            │                                   │
  │  显示 user_code 给人        │                                   │
  │                            │<── GET devices/requests ──────────┤
  ├─ POST device/token ───────>│                                   │  显示审批卡
  │<── 202 pending ────────────┤                                   │
  │  （每 interval 秒重试）      │<── POST …/approve ────────────────┤  人点「批准」
  │                            │  ← 此刻才生成令牌明文并落库          │
  ├─ POST device/token ───────>│  challenge → approved，挂上令牌     │
  │<── 200 {token, granted_by} ┤  兑换后立即置 consumed              │
  │  写入钥匙串 / credentials   │                                   │
```

**关键约束：令牌在「批准」那一刻才生成，在「兑换」那一刻才交付，且只交付一次。**
兑换后挑战立刻进入 `consumed` 终态，重放同一个 `device_code` 返回 400。

### 2.3 轮询纪律

- 客户端按服务端下发的 `interval`（默认 2 秒）轮询，不得自行加快。
- 服务端对同一 `device_code` 的轮询间隔做检查，过快返回 `SLOW_DOWN` 且**不重置
  挑战**——只是让客户端退避，避免误把用户的正常操作判成攻击。
- 超过 `expires_in` 未获批准 → 挑战转 `expired`，客户端收到 400 后**停止轮询**并
  提示重新发起，不得静默重试。
- 终态（approved 已兑换 / denied / expired）在内存保留一段时间再清理，保证轮询方
  一定能读到确定的结论，而不是遇到「挑战不存在」这种含糊错误。

### 2.4 限流与防滥用

两个匿名端点是这次唯一新增的匿名攻击面，四道约束：

| 约束 | 值 | 理由 |
|---|---|---|
| `user_code` 熵 | 8 位 Base32 去混淆字符（去掉 `0/O/1/I`） | 人要念得出、抄得对；仅存活 5 分钟 |
| `device_code` 熵 | `secrets.token_urlsafe(32)` | 它是真正的兑换凭据，必须不可猜 |
| 单来源未决挑战数 | 上限 5（仅在来源可辨时按来源分桶） | 防止刷屏把审批页淹掉 |
| 未决挑战总数 | 上限 20 | 来源不可辨时唯一的闸；可辨时兜住「多个来源一起刷」 |
| 批准端点的码校验 | 沿用 `_PAIR_MAX_ATTEMPTS` 模式，错 5 次作废本轮 | 防止在审批页暴力猜码 |

`user_code` 的低熵是可接受的：它只能在**管理员已登录的浏览器里**被用来批准，
攻击者即使猜中也无法调用批准端点。真正需要高熵的是 `device_code`。

### 2.1 来源 IP：常常拿不到，那就如实说

链路是 `客户端 → docker 端口映射 → 容器内 nginx:3000 → uvicorn:8000`。

uvicorn 的 `ProxyHeadersMiddleware` 默认开着（`proxy_headers=True`，
`forwarded_allow_ips` 默认 `127.0.0.1`）。nginx 从 loopback 连上来、落在信任
名单里，中间件于是按 `X-Forwarded-For` 改写 `scope["client"]`。nginx 发的是
`$proxy_add_x_forwarded_for`（= 客户端自带的 XFF + nginx 看到的 `$remote_addr`），
中间件从右往左跳过可信项取第一个不可信的——拿到的正是**nginx 看到的那个地址**。
客户端伪造的条目永远在左边，取不到，所以这条路径不可伪造。

问题出在更下面一层：**桥接网络的容器经常根本看不到真实客户端地址**。Docker 会
把源地址 NAT 成网桥网关（最常见的 `172.17.0.1`），Docker Desktop 更是全部流量
都这样。这时每台设备看起来都一模一样。

这个值因此有两处不能用：

* **不能当身份线索。** 审批卡写着「如果这不是你刚发起的操作，选择拒绝」，
  让人照着一个对所有设备都相同的数字做安全判断，比不显示更糟。
* **不能当限流键。** 按它分桶等于全网共用一个计数桶，一台机器刷屏就把别人
  全锁在门外。

所以 `api/client_address.py` 先做判定：环回、未指定地址、以及**等于本机默认
网关**（读 `/proc/net/route`，网关字段是小端十六进制）的一律返回空串。上层
据此分流——界面显示「无法确定，请以配对码为准」，限流改走总数上限。

要真拿到地址，得让容器能直接看到客户端连接：用 host 网络，或者在外层还有一层
反代时，把该反代的地址加进 `FORWARDED_ALLOW_IPS` 环境变量（uvicorn 直接读它）。

---

## 3. 数据模型

### 3.1 `DeviceAuthChallenge`（内存，不落库）

放 `services/auth.py`，进程级字典管理，与 `PairChallenge` 同构：

```python
@dataclass(slots=True)
class DeviceAuthChallenge:
    """一次进行中的设备接入请求。

    生命周期：authorize（客户端发起）→ pending，人在网页上看到 →
    approve/deny → approved 时挂上刚生成的令牌明文 → 客户端兑换一次 →
    consumed。超时未批准转 expired。全程不落库：未获批准的请求
    不应该在磁盘上留下任何痕迹。
    """
    user_code: str
    device_code_hash: str          # 只存哈希，与 PAT 同口径
    client_type: str               # worker | cli | other
    client_name: str
    source_ip: str
    status: Literal["pending", "approved", "denied", "expired", "consumed"] = "pending"
    granted_token: str | None = None   # 仅 approved→consumed 之间短暂持有
    token_id: str | None = None
    expires_at: float = ...
    last_poll_at: float = 0.0
```

**未获批准的请求不落库**，这是刻意的：磁盘上不应该出现任何一条「有人试图接入」
的记录被当成凭据来源。进程重启则所有未决请求作废，客户端重新发起即可。

### 3.2 `ApiTokenRecord` 扩展

`settings/schemas.py:169`，补四个字段（全部给默认值，老记录零迁移）：

```python
class ApiTokenRecord(BaseModel):
    id: str
    name: str                      # 沿用：用户可见的名字
    token_hash: str
    created_at: str
    # ↓ 新增
    client_type: str = "manual"    # worker | cli | manual
    owner_kind: str = "admin"      # 谁批准的。v1 只有超管能批准，恒为 "admin"
    expires_at: str | None = None  # None = 长期有效
    last_used_at: str | None = None
```

**记录里存的是「谁批准的」，不是「能干什么」**——权限在验签时按批准者装配（§4）。
`owner_kind` 在 v1 恒为 `"admin"`，看似冗余，但它是**未来开放成员批准时唯一需要
分流的字段**；不预埋 `owner_member_id` 之类的成员字段，等真开放时再加，
那是一次纯加法（老记录默认 admin，语义天然正确）。老记录零迁移。

`last_used_at` 的写入频率要控制：**每次请求都写设置项等于每次请求一次磁盘写**。
采用「进程内累积、按分钟粒度落盘」——同一枚令牌一分钟内只写一次，
足够回答「这台机器还活着吗」，代价可忽略。

### 3.3 `Principal` 扩展

`services/auth.py:75`。**不新增权限字段**——令牌主体的 `is_admin` / `member`
直接按批准者装配，与会话 Cookie 主体走完全相同的赋值逻辑。只补一个
`client_type`，用于把 Worker 令牌挡在业务接口之外（§4.3）：

```python
@dataclass(frozen=True)
class Principal:
    kind: str                      # admin | member | pat | agent
    name: str
    member_id: int | None = None
    is_admin: bool = True
    member: Member | None = None
    agent_session_id: str | None = None
    client_type: str | None = None   # ← 新增：worker | cli | manual，仅令牌主体有值
```

`client_type` 是**客户端形态**，不是权限等级。权限完全来自 `is_admin` / `member`
这两个既有字段——全站的授权判定（`require_admin`、`require_search_capability`
等）一行都不用改，就能对令牌主体正确生效。

---

## 4. 权限：继承批准者

### 4.1 原则

**一枚设备令牌的权限，等于批准它的那个人的权限。**

不发明第二套权限词表。产品已经有一套完整且在用的权限体系——超管与成员两类
身份、成员的能力开关（`allow_subscribe` / `allow_search` /
`allow_direct_download`）、资源白名单（`all_libraries` / `all_sites` 与关联表）。
设备令牌是这个人把自己的权限**委派**给一台机器，不是另开一个平行的权限维度。

平行词表（`operate` / `admin` 之类）看起来更细，实则有两个硬伤：
它与成员能力开关是两个事实源，迟早漂移；而且它回答不了「一个只能订阅、
只能看 A 库的成员，他的 CLI 应该能做什么」——继承模型天然就答对了。

**v1 的批准者只有超管**（成员能否批准见 §4.5）。所以现阶段的实际形态是：
所有 CLI 令牌都是超管权限，Worker 令牌被形态上限压到只能转码。继承模型此刻
看起来像是「一个分支的 switch」，但它决定了数据怎么存、验签怎么装配——
选它是为了不在将来推翻重来，而不是为了今天多一档权限。

唯一的例外是**客户端形态上限**：

```
有效权限 = 批准者的当前权限  ∩  client_type 的能力上限
```

| client_type | 上限 | 说明 |
|---|---|---|
| `worker` | 仅转码四个端点 | 无论谁批准，都只能连转码 WS、读源、写产物 |
| `cli` / `manual` | 无上限 | 完全等同批准者本人在网页上的权限 |

Worker 的上限是必要的：它是一台长期在线、可能放在书房、可能被家人用的机器，
没有任何理由让它能碰订阅或媒体库。CLI 不设上限，是因为它就是这个人的手。

### 4.2 权限在验签时装配，不在签发时冻结

`verify_bearer_token`（`services/auth.py:531`）按 `owner_kind` 分流。v1 只有
一个分支：

```python
# owner_kind == "admin"（v1 唯一分支）
Principal(kind="pat", name=f"token:{record.name}", is_admin=True,
          client_type=record.client_type)
```

「在验签时装配」而不是「把权限写进令牌」，这个选择在 v1 就有一处实际收益：
`Principal.__str__` 仍返回 `token:<名字>`，访问日志里「是谁改的」照旧可答，
而权限判定走的是全站既有的 `is_admin` 路径——`require_admin`、
`require_search_capability` 这些依赖一行都不用改。

未来开放成员批准时，这里加一个成员分支即可，天然获得两个性质：能力开关
事后调整立刻对令牌生效（不需要吊销）、成员停用或改密（`token_version` +1）
时其令牌与会话一起失效。这是继承模型的长期价值，不是 v1 的卖点。

### 4.3 Worker 上限的执行点：默认拒绝

不遍历标注、不做端点分类。**在 `require_login` 里直接拒绝 `client_type == "worker"`
的主体**，只有转码专用依赖接受它：

```python
async def require_login(principal: Principal | None = Depends(optional_login)) -> Principal:
    if principal is None:
        raise UnauthorizedException("未登录，请先登录")
    if principal.client_type == "worker":
        # Worker 令牌只为转码链路签发，业务接口一律不认。白名单只有一处，
        # 即 require_transcode_worker，新增业务路由自动被挡住。
        raise ForbiddenException("转码 Worker 的凭证不能用于业务接口")
    return principal


async def require_transcode_worker(
    principal: Principal | None = Depends(optional_login),
) -> Principal:
    """转码数据面与控制面专用：只接受 worker 令牌。"""
    if principal is None or principal.client_type != "worker":
        raise UnauthorizedException("需要转码 Worker 凭证")
    return principal
```

这是**默认拒绝**而不是白名单枚举：新增的任何业务路由，只要照常挂
`require_login`，就自动把 Worker 令牌挡在外面，不需要记得给它标注什么。

配套一条守护测试（对标现有 `test_auth.py` 的全路由匿名扫描）：遍历全部路由，
持 worker 令牌访问，除转码四个端点外一律断言 4xx。

### 4.4 凭证的签发与吊销只认浏览器会话

`require_admin` 同时接受会话 Cookie 与 Bearer 令牌。若照原样用在凭证管理面上，
会开一个自我复制的口子：**一枚泄漏的设备令牌可以调 `/auth/tokens` 给自己造一枚
备份，也可以批准攻击者的机器接入**——那样吊销原来那枚就止不住损，而吊销是这套
设计唯一的事后止损手段（§8）。

因此新增 `require_admin_session`（`api/deps.py`）：在管理员之上再要求
`principal.kind == "admin"`，即「这是人在浏览器里操作」。挂在四类端点上：

```
POST   /auth/tokens                      创建令牌
GET    /auth/tokens                      列出令牌
DELETE /auth/tokens/{id}                 吊销令牌
GET    /auth/devices/requests            待批准列表
POST   /auth/devices/requests/{code}/approve|deny
```

Agent 工作区令牌同样被挡住，这正是想要的：Agent 不该能给自己续命。

连带结果：这些端点对 CLI 永远调不动，因此全部标 `x-cli-hidden`，命令树里
不再有 `mclaw auth tokens create/list/revoke`。用户签发凭证的入口收敛成一个
——网页的设备页。CLI 侧对应的入口是 `mclaw login`。

### 4.5 v1 不允许成员批准设备——这条取舍要写明白

**只有超管能进设备页、能按下批准。** 成员批准设备不在第一版范围内。

必须直说它的后果：**v1 没有「签发一枚受限的 CLI 令牌」这个能力**。除 Worker
被形态上限压到只能转码外，任何一枚 CLI 令牌都是超管全权，包括删除媒体文件
这类破坏性端点。

现阶段拦它的是三道非授权层的门槛：

1. CLI 自己的危险门槛——`x-cli-dangerous` 驱动的 `--yes` 强制确认，
   `destructive` 级还要先回显影响面（条目名、文件数、路径），
   见 `docs/design/cli.md` §5.6；
2. 产品内 Agent 工具描述里那条硬规约——删除媒体文件必须先用只读命令查清
   影响面、向用户复述并取得当轮明确同意（`docs/design/agent-cli-integration.md` §2）；
3. 设备列表里的可见性与一键吊销——出事之后能立刻切断，且能归因到具体机器。

这三道都是**操作纪律**，不是权限边界。它们挡得住误操作，挡不住一个恶意程序。
接受这个状态的前提是：设备令牌只签发给用户自己机器上、自己启动的程序，
批准页的文案必须把这一点讲透（§7）。

**未来打开成员批准时，收窄能力自然就有了**：成员管理页已经能逐人开关订阅、
搜索、一键下载，能划定可见媒体库与可用站点；用这样一个账号去批准，得到的
令牌恰好就是那个权限，且事后调开关令牌跟着变。届时需要的改动只有两处——
设备页按 `owner_member_id` 分流，验签加一个成员分支。整套模型不用推翻。

## 5. 客户端一：Mac Worker

### 5.1 配置面重构（七个控件 → 一个输入框）

现在的设置窗把配对码、地址、Token、Worker ID、ffmpeg 路径、最大并发、自动连接
七项平铺在一屏，其中四项用户既不知道填什么也不需要改，第一眼看见的还是
「Worker Token 请输入」。

重构后按 macOS 系统设置的语言组织——分组行（inset grouped）、每组下方一行脚注、
主按钮在右下：

**每个状态只显示它需要的那几行**，三态各不相同：

```
未授权                          等待批准                        已授权（稳态）
┌─── MovieClaw…设置 ───┐  ┌─── MovieClaw…设置 ───┐  ┌─── MovieClaw…设置 ───┐
│ 连接                  │  │ 授权                  │  │ 连接                  │
│ ┌──────────────────┐ │  │ ┌──────────────────┐ │  │ ┌──────────────────┐ │
│ │movieclaw 地址 […]│ │  │ │在浏览器中核对    │ │  │ │服务器  10.1.1.5:…│ │
│ └──────────────────┘ │  │ │   A1B2-C3D4      │ │  │ ├──────────────────┤ │
│ 请填局域网地址和端口。 │  │ │ 10.1.1.5:3000/…  │ │  │ │状态    ● 已连接  │ │
│ 转码要来回传输大量视频 │  │ ├──────────────────┤ │  │ └──────────────────┘ │
│ 分片，走公网或反向代理 │  │ │有效期   4 分 32 秒│ │  │ 授权                  │
│ 会明显变慢。          │  │ └──────────────────┘ │  │ ┌──────────────────┐ │
│                      │  │ 浏览器已打开设备页…   │  │ │身份    Yi的Mac-…  │ │
│ ▶ 高级设置            │  │                      │  │ ├──────────────────┤ │
│                      │  │                      │  │ │凭证 已存入钥匙串  │ │
│ 尚未连接              │  │                      │  │ └──────────────────┘ │
│  [在局域网中查找]     │  │           [取消]     │  │ ▶ 高级设置            │
│  [ 连接并配对 ]       │  │                      │  │   [断开并重新配置]    │
└──────────────────────┘  └──────────────────────┘  └──────────────────────┘
```

- **唯一必填项是地址**，**首次配置只需要一次点击**：一个「连接并配对」把保存
  设置、验证地址、发起接入请求三件事连着做完。
- 验证仍然先做且失败即停，地址错了当场知道；只是这个中间结论用一行进度文案
  交代，不再是必须点掉的关卡。「已连接」从来不是用户想要的终点，他要的是
  配对完成。
- **等批准时窗口里只剩配对码**，连地址框和高级设置都收起来——那一刻用户唯一
  要做的事就是核对那串字符。配对码用 `kern` 拉开字距，并给出有效期倒计时。
- **配好之后地址不可编辑**，变成一行只读的「服务器」。要改就点「断开并重新
  配置」回到第一步。这样「保存并测试连接」「重新配对」「清除配置」三个按钮
  收敛成一个：稳态下只有一件事可做，就是推倒重来。
- 「状态」行在稳态下显示的是**实时连接状态**（AppMain 用 `didSet` 把
  `WorkerStatus` 推给窗口）：授权还在不等于现在连着，Mac 睡了、网断了都还是
  已授权。
- **Worker Token 输入框和配对码粘贴框彻底消失。**
- 高级设置默认折叠，内含四项且都有可用默认值：Worker 名称（取机器名）、
  ffmpeg（随应用安装）、最大并发（按 CPU 核数推导）、开机自动连接（默认开）。
- 视觉按 macOS 系统设置的 inset grouped 语言组织：分区小标题 + 圆角分组，
  组内每行「左标题 / 右取值」、行间发丝线，组下一行灰色脚注，动作栏在底部
  右侧、左端一句当前状态。尺寸照 demo 逐条对齐（行高 40、内边距 8/13、
  圆角 9、分区间距 20）。窗口高度随内容自适应。实现拆在 `SettingsViews.swift`
  （纯外观，含 `GroupView`/`SectionView`/`StatusDot`）与
  `SettingsWindowController.swift`（状态机）两个文件里；行是常驻的，
  `render()` 只决定谁 `isHidden`。

### 5.2 状态机

```
未授权 ──连接并配对──> 连接中 ──失败──> 失败（错误就地显示，可重试）
                        │成功（自动接着发起接入请求，不需要再点一次）
                        ↓
                     等待批准（显示 user_code + 倒计时 + 轮询）
                        │批准            │拒绝/超时/取消
                        ↓                ↓
                     已授权            失败 / 回到未授权
                        │断开并重新配置
                        ↓
                     未授权
```

「已授权」是稳态：之后开机自动连接，用户不需要再打开这个窗口。稳态的唯一出口
是「断开并重新配置」——它清掉本机地址与令牌，回到第一步。服务端那份授权记录
不会一起删，要彻底停用得去网页设备页吊销。

### 5.3 凭证存储

沿用现有 `KeychainStore.swift`（macOS 钥匙串），只是存的东西从「用户抄来的共享
令牌」变成「兑换得到的专属令牌」。令牌在 UI 上**不再有任何展示位**，
`LogSanitizer` 的打码逻辑保留。

### 5.4 握手改造

`routes/transcode_worker.py:160` 的 `X-Worker-Token` 头改为标准
`Authorization: Bearer`，改挂 `require_transcode_worker` 依赖（§4.3）——
只接受 `client_type == "worker"` 的主体。`remote_worker.py:508` 的 `verify_worker_token`、
`RemoteTranscodeSetting.worker_token` 字段、`schemas/transcode_worker.py` 里
worker_token 的三态语义**全部删除**。

---

## 6. 客户端二：mclaw CLI

### 6.1 login

```
$ mclaw login --server http://10.1.1.5:3000
✓ 已连接 movieclaw

请在浏览器打开：http://10.1.1.5:3000/settings/devices
核对配对码：      MCLW-7F3K

⠋ 等待批准…（5 分钟内有效）
✓ 已授权：claude-code@Yi的Mac-mini（权限 operate）
  凭证已写入 ~/.config/movieclaw/credentials（0600）
```

- **`--password` 废弃**。密码只在浏览器里用。TTY 下也不再提供密码登录路径。
- **不需要也不接受 `--scope` 之类的参数**。CLI 得到什么权限由批准者决定，
  v1 的批准者只有超管，所以拿到的是完全权限（§4.5）。
- `client_name` 默认取 `<程序名>@<主机名>`，可用 `--name` 覆盖。
- 非 TTY（脚本 / CI）执行 `login` 直接以用法错误退出并提示：
  设备流需要人在浏览器确认，无人值守场景请在网页手工创建令牌后用
  `MOVIECLAW_TOKEN` 注入。
- `logout` **只清本地凭证，不吊销服务端令牌**——吊销只认浏览器会话（§4.4），
  CLI 拿令牌调不动。命令输出必须把这一点说清楚，并指向网页的设备页，
  否则用户会以为 logout 等于停用了这台机器。

### 6.2 凭证的位置与优先级

要满足「任何终端、任何应用触发都能连」，凭证必须落在**每用户全局**的固定位置。
不是全机器共享——那份令牌等价管理员，全机器可读意味着本机任何进程都能拿到全权。

```
优先级：--flag  >  环境变量  >  用户级配置  >  机器级配置  >  报错

用户级（凭证只在这里）
  Linux/macOS  ~/.config/movieclaw/          目录 0700
  Windows      %APPDATA%\movieclaw\
    config.toml        [contexts.*] 服务器地址、默认上下文      0600
    credentials        按 server 分键的令牌（JSON）              0600

机器级（只放地址，绝不放凭证）
  Linux/macOS  /etc/movieclaw/config.toml
  Windows      %PROGRAMDATA%\movieclaw\config.toml
```

`core/config.py` 的 `credentials` 从存会话 Cookie 改为按 server 分键存令牌
（文件名不变——改名只会留下一个装着废弃 Cookie 的旧文件）；`core/http.py`
的凭证通道收敛为一条：`MOVIECLAW_TOKEN` > credentials 里该 server 的令牌，
Cookie 通道整个删除。

### 6.2.1 无人值守环境：环境变量授权

设备流的前提是「有人能在浏览器里按批准」。有一整类环境里没有那个人——NAS 上的
定时任务、CI、无界面容器——在那里跑 `mclaw login` 只会挂到超时。这些环境走的是
另一条路，两个环境变量：

```
MOVIECLAW_SERVER=http://192.168.1.10:3000
MOVIECLAW_TOKEN=mclaw_...
```

令牌在网页「设置 → 设备 → 手工创建令牌」里签发（`POST /auth/tokens`，
`client_type` 记为 `manual`），与设备流签出来的令牌进同一份存储、同一张设备列表、
同一个吊销入口——**签发的入口有两个，管理的入口只有一个**。

这条通道的三个性质是刻意的：

1. **完全不落盘**。`MOVIECLAW_TOKEN` 命中时 CLI 根本不去读凭证文件
   （`internal/api.TokenFor`），所以只读文件系统、没有 `$HOME`、`$HOME` 每次都变的
   容器里都能跑，也不会触发凭证文件的权限自检。产品内 Agent 的工作区走的正是
   这条路（`api/routes/agent.py`）。
2. **令牌不进参数面**。只认环境变量，不提供 `--token` 标志——一旦令牌能写在
   命令行上，它就会进 shell 历史、进 `ps` 输出、进 Agent 的上下文。
   这是 §6.1「密码只在浏览器里用」同一条原则的延伸。
3. **签发面不因此放宽**。手工创建仍然只认浏览器会话（`require_admin_session`，
   §4.4），令牌自己造不出备份。

**三处必须说破的相互作用**，否则用户会遇到查不出根因的现象：

| 现象 | 根因 | 处理 |
|---|---|---|
| 配对成功了，身份却还是老的 | 环境变量优先级高于凭证文件，新令牌被完全遮蔽，且全程无报错 | `login` 在 `MOVIECLAW_TOKEN` 存在时直接以用法错误退出，让用户先 unset |
| `logout` 之后命令照样能跑 | 删的是凭证文件，环境变量还在 | `logout` 额外提示该 unset 哪个变量 |
| 401 之后照着提示去配对，白跑一趟 | 「请先执行 mclaw login」对环境变量凭证是错的下一步 | 401 的 hint 按凭证来源分流：环境变量在场时改为提示检查令牌本身或它是否已被吊销 |

排查当前到底用的是哪套地址和凭证，一律看 `mclaw status` 的 `credential` 一行——
它会如实回答是「环境变量 MOVIECLAW_TOKEN」还是某个凭证文件路径。

### 6.3 四个必须处理的工程陷阱

这些才是「任何应用触发都能连」的真实难点，每一个都会表现为「我明明登录过了」：

1. **PATH 不一致**。macOS 从 Dock 启动的 GUI 应用、Windows 服务、cron、systemd
   都不读 `~/.zshrc`，`~/.local/bin` 不在它们的 PATH 里。安装必须落进默认 PATH。
2. **HOME 不一致**。`sudo`、launchd、systemd、容器里的 `$HOME` 各不相同。对策是
   错误信息里**打印实际查找过的路径清单**，而不是干巴巴一句「未登录」。
3. **权限过宽**。目录 0700 / 文件 0600，加载时校验，过宽拒绝并提示（同 ssh 对
   私钥的做法）。自部署用户会 `chmod -R 777`。
4. **并发写**。多个 Agent 同时跑很正常，credentials 写必须是
   「临时文件 + `os.replace`」原子替换。

### 6.4 分发：一个静态二进制

CLI 是独立的 Go 二进制（`cli/`，见 `docs/design/cli-go-migration.md`）。装它
不需要 Python、Node 或任何包管理器——CLI 要装在 NAS、软路由、同事的机器和
CI 里，每多一个运行时前置就多一批装不上的人。

```
主通道   curl -fsSL <scripts/install-cli.sh> | sh      （Linux / macOS）
         irm <scripts/install-cli.ps1> | iex           （Windows）
         → 从 Release 下载对应平台的二进制、校验 sha256
         → 装进 /usr/local/bin（Windows 是用户级 PATH），
           GUI 应用与后台任务也能调到
次通道   直接从 Release 页下载 mclaw_<os>_<arch>.tar.gz 解压
第三     Docker 镜像内置（现状不变，docker exec 零安装）
```

命名规则 `mclaw_{os}_{arch}.{tar.gz|zip}` 是两个安装脚本与 `.goreleaser.yaml`
之间的契约，改动要三处同步。

### 6.5 第一次配对：地址从哪来

装好之后用户面对的第一个问题是「我的 movieclaw 在哪」。三条路，优先级从高到低：

```
mclaw login --server http://192.168.1.10:3000   # 知道地址，一步到位
mclaw login                                      # 不知道，先在局域网里找
```

不给 `--server` 且哪儿都没配过时，`login` 广播一次 UDP 7359（复用服务端已有的
Jellyfin 发现应答），把回应的地址列出来让用户确认：

```
未指定服务器地址，正在局域网内查找 movieclaw…
✓ 找到 MovieClaw（http://192.168.1.10:3000）
  使用这台吗？ [Y/n]
```

找到多台就列序号让选。**找到之后必须先打一次 `/health` 确认 `service == "movieclaw"`**：
局域网里的真 Jellyfin 会应答同一句问询，不确认就会拿着它的地址去配对，得到一串
看不懂的 404。

发现只是兜底，四种情况下没有结果，`--server` 因此永远不能取消：

1. 服务端的「Jellyfin 兼容层」开关被关掉（就不应答了）；
2. 桥接网络部署下服务端自报的是容器内地址——**这一种要单独报**
   （「找到了但连不上 http://172.17.0.2:3000」），并指向真正的修法：
   在网页填对外访问地址。咽下去只说「没找到」，用户会一直查错方向；
3. 跨网段、VPN、公网——广播出不去；
4. UDP 7359 被别的程序占了。

非 TTY 环境下完全不做发现：要选、要确认，零交互原则下没有意义
（无人值守场景走 `MOVIECLAW_TOKEN` 注入）。

明确指了一个不存在的上下文（`--context 打错的名字`）时**不退回发现**：
用户说的是明确的东西，猜一台机器给他比报错更糟。

**Mac Worker 用同一条通道**（`LANDiscovery.swift`），设置窗「连接」分组下方
有一个次要按钮「在局域网中查找」，首次打开且地址为空时自动跑一次。两处有意
的差异：

| | mclaw CLI | Mac Worker |
|---|---|---|
| 触发 | 没有地址时自动 | 按钮 + 地址为空时自动 |
| 拿到地址后 | 自己打 `/health` 确认是 movieclaw | **不确认**——紧接着的「验证连接」就是同一个检查，结论直接显示在窗口里 |
| 结果去向 | 确认后直接进入配对 | **只填进输入框**，由用户过目后自己点主按钮 |

Worker 不直接采用发现结果，是因为服务端优先返回的是用户为播放器配的「对外
访问地址」，那可能是反向代理域名——转码要来回传大量视频分片，走反代明显更慢。
这个判断必须留给人，所以窗口里的脚注一直提示「建议填内网直连地址」。

macOS 14 起往局域网广播需要用户授权一次，`Info.plist` 因此带
`NSLocalNetworkUsageDescription`；缺了它系统弹的框不会说明用途，查找会静默失效。

CLI 独立发版后「CLI 版本 ≠ 服务端版本」成为常态，`docs/design/cli.md` §2.1 的
`spec_hash` 偏斜检测从可选优化变成必需品。

---

## 7. 网页：已连接的设备

`apps/web/components/settings-view.tsx` 的设置分区里新增一个 `devices` 分区，
两块内容：

**待批准的请求**（有请求时才出现，置顶）

转码 Worker：

> 名称 `Yi的Mac-mini` · 类型 `转码 Worker` · 来源 `10.1.1.22（同一局域网）`
> 配对码 `MCLW-7F3K`
> **将获得：仅限转码。** 这台机器不能查看或修改你的订阅、媒体库和设置。
> ⚠ 请确认配对码与设备上显示的完全一致。如果这不是你刚发起的操作，选择拒绝。
> `[批准接入]` `[拒绝]`

命令行 / Agent：

> 名称 `claude-code@Yi的Mac-mini` · 类型 `命令行` · 来源 `127.0.0.1（本机）`
>
> （桥接网络部署下这一行常常是「无法确定」，见 §2.1）
> 配对码 `MCLW-9QT2`
> **将获得：与你相同的完全权限。** 这台机器上的程序将能做你在网页上能做的
> 一切，包括删除媒体文件。只在你清楚这台机器上正在运行什么程序时才批准。
> ⚠ 请确认配对码与设备上显示的完全一致。如果这不是你刚发起的操作，选择拒绝。
> `[批准接入]` `[拒绝]`

「将获得」这一行必须写实：它是用户做决定的**唯一**依据，不能是含糊的技术名词。
命令行那条刻意把全权的含义点破到「包括删除媒体文件」——因为 v1 没有别的
收窄手段（§4.5），用户的知情就是唯一的那道闸。

**已连接的设备**

> ● `Yi的Mac-mini` — 转码 Worker · 仅转码 · 刚刚活跃 `[吊销]`
> ○ `claude-code@MacBook` — 命令行 · 完全权限 · 2 小时前 `[吊销]`
> ○ `nas-cron` — 手工令牌 · 完全权限 · 从未使用 `[吊销]`

「仅转码 / 完全权限」是给人看的实话，不是内部权限名。**吊销是 v1 唯一的
事后止损手段**，所以这个列表要好用：最近活跃时间必须准（`last_used_at`），
一台设备一行，一键吊销且不影响其他设备。手工创建的 PAT 也在这个列表里，
入口统一。

整个设备分区挂 `require_admin`——v1 只有超管能看、能批准、能吊销。

**手工创建令牌**（分区第三块，排在设备列表之后）

给 §6.2.1 那类没人能按批准的环境用。做成次要入口并主动劝退——收起态第一句话就是
「能打开浏览器的机器请直接运行 mclaw login，不必走这里」：手工令牌是全权且不过期的，
没有配对流「核对配对码」那道人工闸，不该和配对并列摆着让用户挑。

创建只要一个名字（日后在设备列表里认出它、决定要不要吊销），旁边是与审批卡同权的
「将获得」说明——同权就不能说得更轻，只是收尾换成这条路上真正要点破的两件事：
令牌不会自动过期，发出去只能靠吊销收回。

创建成功后给的是**可直接粘贴的两行环境变量**，不是一个裸令牌：

```
MOVIECLAW_SERVER=https://movieclaw.example.com
MOVIECLAW_TOKEN=mclaw_...
```

三个细节都是有代价的选择：

- **地址和令牌一起给**。用户接下来要做的事是「让那台机器连上这台 movieclaw」，
  两样缺一不可；只给令牌等于把找地址这一步留给用户，而地址恰恰是自部署里最容易
  填错的东西。
- **`KEY=value` 而不是 `export KEY=...`**。同一份文本要能同时用在 `.env`、
  `docker --env-file`、compose 的 `env_file` 和 shell 的 `source`。
- **地址取自「对外访问地址」；没配时回落当前浏览器地址，并当场说破这是猜的**
  （与 `_verification_uri` 同口径）。浏览器能打开不等于目标机器连得到——NAS 的
  定时任务、另一个网段的 CI 都可能不通。悄悄给一个可能不通的地址，用户只会看到
  mclaw 连接超时而查不到原因。

明文只在创建响应里出现一次（服务端只存哈希），所以这张卡必须让用户当场存走：
关闭前过一次确认，关闭后只能吊销重建。

---

## 8. 安全分析

| 攻击面 | 缓解 |
|---|---|
| 局域网内他人抢先批准 | 批准需要管理员的浏览器会话，攻击者没有会话就点不了 |
| 猜 `user_code` 骗批准 | 码只在审批页可用，且审批页要管理员会话；错 5 次作废本轮 |
| 猜 `device_code` 直接兑换 | 32 字节高熵 + 只存哈希 + 5 分钟 TTL + 只能兑换一次 |
| 钓鱼式诱导批准 | 文案要求核对码与「这不是你发起的就拒绝」；审批卡显示设备名与来源 IP，但来源拿不到时如实说「无法确定」而不是补一个占位地址（§2.1） |
| 令牌泄漏 | 每设备独立、可单独吊销、可归因；Worker 令牌被业务接口默认拒绝。CLI 令牌是全权的（§4.5），止损手段是设备列表里的一键吊销 |
| 泄漏令牌自我复制 | 签发与吊销只认浏览器会话（§4.4）：令牌既造不出备份，也拉不进别的机器，吊销才真的能止损 |
| 匿名端点被刷 | 未决挑战总数上限（+ 来源可辨时的单来源上限）+ 轮询退避 + 挑战全程不落库 |
| 管理员改密 | 会话与 Agent 令牌按现有机制失效；**设备令牌不受影响**——见下方「凭证生命周期的独立性」 |

**凭证生命周期的独立性（明确决定）**：改密**不吊销**任何设备令牌，
设备令牌只在用户按下「吊销」时失效。

密码回答的是「谁能登录网页」，设备令牌回答的是「哪台机器还该连着」，
这是两条独立的凭证线。改密的动机通常是例行更换或密码本身泄漏，与「书房那台
Mac 还该不该转码」无关；把两者绑在一起的代价很具体——用户改完密码，转码停了、
CLI 全部报 401，而他不会把这两件事联系起来，只会认为改密把东西弄坏了。

代价也说清楚：如果攻击者拿到的是**密码**并借此批准了一台设备，那么改密之后
那台设备依然连着。这正是设备列表存在的意义——改密之后应当去看一眼那份列表，
把不认识的设备吊销掉。因此列表要能回答「这是什么、什么时候活跃过」，
并把吊销做成一次点击（§7）。安全兜底靠**可见 + 可吊销**，不靠连坐。

**明确不解决的**：本机上已经能读到 `~/.config/movieclaw/credentials` 的进程，
就等于持有该令牌。这是所有 CLI 的共同边界（gh、gcloud、aws 皆然），对策是文件
权限位、形态上限与可吊销，而不是加密——本机加密只能防到不会读文件的人。

---

## 9. 落点清单

| # | 改动 | 文件 | 性质 |
|---|---|---|---|
| 1 | `DeviceAuthChallenge` + 签发/批准/兑换 | `services/auth.py`（新增一节） | 核心 |
| 2 | 两个匿名端点 + 一组管理端点 | `api/routes/auth.py` | 核心 |
| 3 | `ApiTokenRecord` 四个新字段 + `last_used_at` 节流写入 | `settings/schemas.py:169` | 小 |
| 4 | `Principal.client_type` + 验签按批准者装配 + `require_login` 拒绝 worker | `services/auth.py:75,531`、`api/deps.py` | 中 |
| 4b | `require_admin_session`：凭证签发面收归浏览器会话，并对 CLI 隐藏 | `api/deps.py`、`api/routes/auth.py` | 小 |
| 5 | Worker 令牌隔离守护测试（除转码端点外一律 4xx） | `tests/api/test_auth.py` 同款模式 | 小 |
| 6 | Worker 握手改 Bearer；删共享令牌全链路 | `routes/transcode_worker.py:160`、`services/playback/remote_worker.py:508`、`settings/remote_transcode.py`、`schemas/transcode_worker.py` | 中 |
| 7 | 网页「设备」分区 | `apps/web/components/settings-view.tsx`（新分区组件） | 中 |
| 7b | 手工创建令牌入口 + 环境变量片段 | `apps/web/components/devices-section.tsx`、`lib/api/devices.ts`、`lib/devices-display.ts` | 中 |
| 8 | Worker 设置窗重构；删 `PairingCode.swift` | `macos/…/SettingsWindowController.swift`、`ConfigurationStore.swift`、`AppMain.swift` | 中 |
| 9 | CLI `login` 改设备流，废弃 `--password` | `cli/internal/overlay/auth.go` | 中 |
| 9b | 环境变量授权的可发现性与三处相互作用（§6.2.1） | `cli/cmd/mclaw/main.go`、`cli/internal/overlay/auth.go`、`cli/internal/api/api.go` | 小 |
| 10 | CLI 凭证层：全局路径、按 server 分键、原子写、权限自检 | `cli/internal/config/config.go` | 中 |
| 11 | 安装脚本：Linux/macOS 与 Windows 各一份 | `scripts/install-cli.sh`、`scripts/install-cli.ps1` | 中 |

第 9、10 两项最初写在 Python CLI 里，随后整体迁到 Go
（`docs/design/cli-go-migration.md`），表里给的是迁移后的位置。

---

## 10. 测试

1. **协议单元测试**：authorize→pending→approve→兑换 全链路；denied / expired /
   重放兑换 / SLOW_DOWN 五条异常路径各一例。
2. **默认拒绝守护**（扩展现有全路由扫描）：新增的两个匿名端点在白名单里显式登记；
   其余路由匿名一律 401。
3. **Worker 隔离守护**：遍历全部路由，持 worker 令牌访问，除转码四个端点外一律 4xx。
4. **批准者收口**：设备页全部端点对成员会话返回 403；批准端点只接受超管会话。
5. **令牌不能自我复制**：持设备令牌调 `/auth/tokens` 的增删查与设备批准端点
   一律 403，同一枚令牌调业务接口仍然 200（证明拦的是凭证管理面而非令牌本身）。
6. **CLI 端到端**：真实 uvicorn + 真实 `mclaw login`，脚本模拟浏览器批准，
   断言凭证落盘位置与权限位（0600）、非 TTY 下 `login` 以用法错误退出。
7. **Worker 端到端**：Swift 侧对 authorize/token 两个端点的状态机测试；
   钥匙串读写；拒绝与超时路径的 UI 状态。
8. **人工 golden**：全新 Mac 上从下载应用到出现「已授权·运行中」，
   全程不打开终端、不抄任何字符串。

---

## 11. 实施路线

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0 协议与存储** | 落点 1–5：挑战模型、端点、令牌字段、验签装配与隔离守护测试 | ✅ 已完成 |
| **P1 网页审批页** | 落点 7 | ✅ 已完成 |
| **P2 两个客户端** | 落点 6、8、9、10 | ✅ 已完成 |
| **P3 分发** | 落点 11 | ✅ 已完成 |

P0 与 P1 之间不能并行——审批页要按端点的真实返回来写。P2 的两个客户端之间
没有依赖，可以并行。

### 尚未验证的部分

Mac Worker 的改动（`DevicePairing.swift`、设置窗重写、握手改 Bearer）**没有编译
验证过**——开发环境没有 Swift 工具链，只做了代码审查与逻辑推演。Swift 侧的单元
测试（信封解析、四种轮询结论的映射）也随之未执行。合并前需要在一台 Mac 上：

1. `swift build` 与 `swift test` 通过；
2. 全新配置走一遍 golden 流程——填地址 → 连接并配对 → 网页批准 →
   菜单栏显示已连接，全程不打开终端、不抄任何字符串；
3. 网页「设置 → 设备」里能看到这台 Mac，吊销后 Worker 应当掉线。

其余部分（服务端、网页、CLI）在本仓库的测试里跑通了，含 CLI 用 pty 起真实
子进程走完的端到端配对。

---

## 12. 明确不做

- **不实现 OAuth Provider**（授权码、动态客户端注册、refresh token 轮换）。
  单管理员的自部署产品没有第三方开发者生态，OAuth 的复杂度全部用来解决
  不存在的问题。真需要被 MCP 客户端自动发现时，再在现有令牌之上补 RFC 9728
  的元数据端点。
- **不做令牌自动续期**。长期令牌 + 可吊销已经够用，续期只会增加状态。
- **不加密本地凭证文件**。见 §8 末尾。
- **改密不连带吊销设备令牌**。理由见 §8「凭证生命周期的独立性」：两条独立的
  凭证线，连坐的代价是用户在一次不相关的操作后遭遇一片失联。吊销必须是显式动作。
- **成员不能批准设备**。设备页整体只对超管开放。代价是 v1 签不出受限令牌
  （§4.5 已写明后果与现有的三道操作纪律）。模型本身留好了口子，将来打开
  只需两处加法，不需要推翻。
- **不为发现新开协议或新开端口**。`mclaw login` 的局域网自动发现改为复用服务端
  **已经在应答**的那条通道：UDP 7359 上的 Jellyfin 发现协议
  （`src/movieclaw_jellyfin/udp.py`）。客户端广播一句 `who is JellyfinServer?`，
  服务端回的 `Address` 就是 web 端口的完整地址。代价是客户端约 130 行 Go，
  服务端零改动（详见 §6.5）——比另起 mDNS 或自定义应答器低一个数量级。

---

## 13. 已定的其余细节

**令牌默认长期有效，不设过期。** `ApiTokenRecord.expires_at` 字段保留但 v1 恒为
`None`——它存在是为了将来「临时授权一台机器 24 小时」这类需求能直接加上，
不是给 v1 用的默认策略。设备列表里对「超过 90 天未使用」的令牌给一行提示，
把清理的决定权交给用户；提示不等于自动失效。

理由与 §8「凭证生命周期的独立性」一致：自动过期是另一种形式的连坐——用户
某天发现转码停了，而他什么都没做。可见 + 可吊销是这套设计一以贯之的兜底方式。
