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
  用户是在批准一台具体的机器，而不是复制一串看不懂的字符。
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
     → {version, spec_hash}
     用途：地址可达性与版本验证。不新增接口。

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
| 单 IP 未决挑战数 | 上限 5 | 防止刷屏把审批页淹掉 |
| 批准端点的码校验 | 沿用 `_PAIR_MAX_ATTEMPTS` 模式，错 5 次作废本轮 | 防止在审批页暴力猜码 |

`user_code` 的低熵是可接受的：它只能在**管理员已登录的浏览器里**被用来批准，
攻击者即使猜中也无法调用批准端点。真正需要高熵的是 `device_code`。

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
    client_type: str = "manual"        # worker | cli | manual
    owner_kind: str = "admin"          # admin | member —— 谁批准的
    owner_member_id: int | None = None # owner_kind == "member" 时才有
    owner_token_version: int = 0       # 签发时批准者的 token_version（成员用）
    expires_at: str | None = None      # None = 长期有效
    last_used_at: str | None = None
```

**记录里存的是「谁批准的」，不是「能干什么」**——权限在每次验签时按批准者的
当前状态实时装配（§4）。老记录默认 `owner_kind="admin"`，语义与今天一致，零迁移。

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

### 4.2 权限在验签时实时装配，不在签发时冻结

`verify_bearer_token`（`services/auth.py:531`）按 `owner_kind` 分流，与
`verify_session_token` 用同一段装配逻辑：

```python
# owner_kind == "admin"
Principal(kind="pat", name=f"token:{record.name}", is_admin=True,
          client_type=record.client_type)

# owner_kind == "member"：查库拿当前成员行
member = await repo.get(record.owner_member_id)
if member is None or member.status != "active":
    raise UnauthorizedException("授权这枚令牌的账号已被停用，令牌随之失效")
if member.token_version != record.owner_token_version:
    raise UnauthorizedException("授权这枚令牌的账号已改密，请重新配对")
Principal(kind="member", name=f"token:{record.name}", member_id=member.id,
          is_admin=False, member=member, client_type=record.client_type)
```

三个白捡的性质：

- **能力开关是活的**。管理员事后关掉某成员的 `allow_search`，他那台机器上的
  CLI 立刻也搜不了——不需要吊销令牌，也不会出现「令牌里冻结的旧权限」。
- **停用即失效**。成员被停用或改密（`token_version` +1，见 `Member` 模型注释），
  他的全部设备令牌与会话一起失效，不需要额外的清理逻辑。
- **审计口径统一**。`Principal.__str__` 仍返回 `token:<名字>`，访问日志里
  「是谁改的」照旧可答。

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

### 4.4 想要一个受限的 CLI 令牌怎么办

**建一个成员账号，用它去批准。**

成员管理页里已经能逐人开关订阅、搜索、一键下载，能划定可见的媒体库与可用的
站点。想给某个第三方 Agent 一个「只能查、能订阅、看不到 A 库」的令牌，
就用这样一个成员账号登录网页去批准它——得到的令牌恰好就是这个权限，
而且以后调整这个成员的开关，令牌的权限跟着变。

这条路径不需要写任何新代码，而一套 `operate` / `admin` 的平行词表需要，
且能力还更弱。

**这里明确接受的取舍**：超管批准的 CLI 令牌是全权的，包括破坏性端点。
拦它的是 CLI 自己的门槛——`x-cli-dangerous` 驱动的 `--yes` 强制确认与
`destructive` 级的影响面回显（`docs/design/cli.md` §5.6），以及产品内 Agent
工具描述里那条「删除媒体文件必须先复述影响面并取得当轮同意」的硬规约。
如果用户要的是「连误操作也不可能」，正确答案是上面那句：用成员账号批准。

## 5. 客户端一：Mac Worker

### 5.1 配置面重构（七个控件 → 一个输入框）

现在的设置窗把配对码、地址、Token、Worker ID、ffmpeg 路径、最大并发、自动连接
七项平铺在一屏，其中四项用户既不知道填什么也不需要改，第一眼看见的还是
「Worker Token 请输入」。

重构后按 macOS 系统设置的语言组织——分组行（inset grouped）、每组下方一行脚注、
主按钮在右下：

```
┌──────────── MovieClaw Transcoder ────────────┐
│                                               │
│  连接                                          │
│  ┌─────────────────────────────────────────┐  │
│  │ movieclaw 地址   [http://10.1.1.5:3000] │  │
│  └─────────────────────────────────────────┘  │
│  请填写局域网地址和端口。转码要来回传输大量视频   │
│  分片，走公网或反向代理会明显变慢，也更容易中断。  │
│                                               │
│  ▸ 高级设置 (4)                                │
│                                               │
│              [在局域网中查找]  [ 验证连接 ]     │
└───────────────────────────────────────────────┘
```

- **唯一必填项是地址**，且必须先「验证连接」拿到确定结论（版本 + 往返延迟）
  才能进入下一步。地址错了当场知道。
- **Worker Token 输入框和配对码粘贴框彻底消失。**
- 高级设置默认折叠，内含四项且都有可用默认值：Worker 名称（取机器名）、
  ffmpeg（随应用安装）、最大并发（按 CPU 核数推导）、开机自动连接（默认开）。

可交互原型见本次评审的 demo。

### 5.2 状态机

```
未配置 ──填地址──> 验证中 ──失败──> 未配置（错误就地显示）
                     │成功
                     ↓
                  已连接·未授权 ──请求接入──> 等待批准（显示 user_code + 轮询）
                                                │批准        │拒绝/超时
                                                ↓            ↓
                                          已授权·运行中   未授权（可重试）
```

「已授权·运行中」是稳态：之后开机自动连接，用户不需要再打开这个窗口。

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
✓ 已连接 movieclaw 0.18.0

请在浏览器打开：http://10.1.1.5:3000/settings/devices
核对配对码：      MCLW-7F3K

⠋ 等待批准…（5 分钟内有效）
✓ 已授权：claude-code@Yi的Mac-mini（权限 operate）
  凭证已写入 ~/.config/movieclaw/credentials.json（0600）
```

- **`--password` 废弃**。密码只在浏览器里用。TTY 下也不再提供密码登录路径。
- **不需要也不接受 `--scope` 之类的参数**。CLI 得到什么权限，取决于是谁在网页上
  按下批准：超管批准就是全权，成员批准就是那个成员的权限（§4.4）。
- `client_name` 默认取 `<程序名>@<主机名>`，可用 `--name` 覆盖。
- 非 TTY（脚本 / CI）执行 `login` 直接以用法错误退出并提示：
  设备流需要人在浏览器确认，无人值守场景请在网页手工创建令牌后用
  `MOVIECLAW_TOKEN` 注入。

### 6.2 凭证的位置与优先级

要满足「任何终端、任何应用触发都能连」，凭证必须落在**每用户全局**的固定位置。
不是全机器共享——那份令牌等价管理员，全机器可读意味着本机任何进程都能拿到全权。

```
优先级：--flag  >  环境变量  >  用户级配置  >  机器级配置  >  报错

用户级（凭证只在这里）
  Linux/macOS  ~/.config/movieclaw/          目录 0700
  Windows      %APPDATA%\movieclaw\
    config.toml        [contexts.*] 服务器地址、默认上下文      0600
    credentials.json   按 server 分键的令牌                     0600

机器级（只放地址，绝不放凭证）
  Linux/macOS  /etc/movieclaw/config.toml
  Windows      %PROGRAMDATA%\movieclaw\config.toml
```

`core/config.py` 现在的 `credentials` 只存会话 Cookie（`config.py:138-159`），
改为按 server 分键存令牌；`core/http.py:51` 的凭证优先级相应改为
`MOVIECLAW_TOKEN` > credentials 里该 server 的令牌 > （删除 Cookie 通道）。

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

### 6.4 分发：CLI 必须先成为独立包

目前 `movieclaw_cli` 与服务端同属一个 `movieclaw` 发行包，依赖里有 fastapi、
onnxruntime、pillow、sqlmodel、openai 等几十个重依赖——远程用户装一个 CLI 要拖
几百 MB。而 CLI 本体的第三方依赖只有 **click、httpx、pyyaml** 三个，且零 import
服务端模块，拆包没有任何技术阻力。

```
主通道   curl -fsSL <install.sh> | sh
         → 装 uv（单个静态二进制）→ uv tool install movieclaw-cli
         → uv 自带独立 Python，用户什么都不用预装
         → symlink 进 /usr/local/bin，GUI 应用也能调到
次通道   uv tool install / pipx install movieclaw-cli （Python 用户与 CI）
第三     Docker 镜像内置（现状不变，docker exec 零安装）
```

选 uv 路线而非 PyInstaller 二进制的三个理由：用户视角完全等价；不需要维护五平台
构建矩阵和 onefile 的冷启动开销；绕过 macOS Gatekeeper 与 Windows SmartScreen 的
代码签名成本。真需要「一个文件拖过去就能用」时再补 Nuitka `--standalone`。

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
> 配对码 `MCLW-9QT2`
> **将获得：与你（`admin`）相同的权限。** 这台机器上的程序将能做你在网页上
> 能做的一切。想给它更小的权限，请改用一个受限的成员账号登录后再批准。
> ⚠ 请确认配对码与设备上显示的完全一致。如果这不是你刚发起的操作，选择拒绝。
> `[批准接入]` `[拒绝]`

「将获得」这一行必须写实：它是用户做决定的**唯一**依据，不能是含糊的技术名词。
命令行那条的措辞刻意点破全权的含义，并给出收窄的具体做法（§4.4）。

**已连接的设备**

> ● `Yi的Mac-mini` — 转码 Worker · 仅转码 · 刚刚活跃 `[吊销]`
> ○ `claude-code@MacBook` — 命令行 · 授权自 `admin` · 2 小时前 `[吊销]`
> ○ `家里人的 Mac` — 命令行 · 授权自成员 `xiaoyu` · 昨天 `[吊销]`
> ○ `nas-cron` — 手工令牌 · 授权自 `admin` · 从未使用 `[吊销]`

列表里显示「授权自谁」而不是抽象的权限名——这既是审计线索，也让用户一眼看出
哪些设备握着全权。吊销一台不影响其他设备；手工创建的 PAT 也在这个列表里，
入口统一。成员只能看到并吊销自己授权出去的设备，超管看到全部。

---

## 8. 安全分析

| 攻击面 | 缓解 |
|---|---|
| 局域网内他人抢先批准 | 批准需要管理员的浏览器会话，攻击者没有会话就点不了 |
| 猜 `user_code` 骗批准 | 码只在审批页可用，且审批页要管理员会话；错 5 次作废本轮 |
| 猜 `device_code` 直接兑换 | 32 字节高熵 + 只存哈希 + 5 分钟 TTL + 只能兑换一次 |
| 钓鱼式诱导批准 | 审批卡显示来源 IP 与设备名；文案明确要求核对码与「这不是你发起的就拒绝」 |
| 令牌泄漏 | 每设备独立、可单独吊销；Worker 令牌被业务接口默认拒绝；想要受限令牌用成员账号批准 |
| 匿名端点被刷 | 单 IP 未决挑战上限 + 轮询退避 + 挑战全程不落库 |
| 管理员改密 | 沿用现有密钥轮换机制：会话与 Agent 令牌一起失效；落库令牌按需在改密时一并清空（见开放问题） |

**明确不解决的**：本机上已经能读到 `~/.config/movieclaw/credentials.json` 的进程，
就等于持有该令牌。这是所有 CLI 的共同边界（gh、gcloud、aws 皆然），对策是文件
权限位与「用成员账号批准以收窄权限」，而不是加密——本机加密只能防到不会读文件的人。

---

## 9. 落点清单

| # | 改动 | 文件 | 性质 |
|---|---|---|---|
| 1 | `DeviceAuthChallenge` + 签发/批准/兑换 | `services/auth.py`（新增一节） | 核心 |
| 2 | 两个匿名端点 + 一组管理端点 | `api/routes/auth.py` | 核心 |
| 3 | `ApiTokenRecord` 四个新字段 + `last_used_at` 节流写入 | `settings/schemas.py:169` | 小 |
| 4 | `Principal.client_type` + 验签按批准者装配 + `require_login` 拒绝 worker | `services/auth.py:75,531`、`api/deps.py` | 中 |
| 5 | Worker 令牌隔离守护测试（除转码端点外一律 4xx） | `tests/api/test_auth.py` 同款模式 | 小 |
| 6 | Worker 握手改 Bearer；删共享令牌全链路 | `routes/transcode_worker.py:160`、`services/playback/remote_worker.py:508`、`settings/remote_transcode.py`、`schemas/transcode_worker.py` | 中 |
| 7 | 网页「设备」分区 | `apps/web/components/settings-view.tsx`（新分区组件） | 中 |
| 8 | Worker 设置窗重构；删 `PairingCode.swift` | `macos/…/SettingsWindowController.swift`、`ConfigurationStore.swift`、`AppMain.swift` | 中 |
| 9 | CLI `login` 改设备流，废弃 `--password` | `overlay/auth_cmds.py` | 中 |
| 10 | CLI 凭证层：全局路径、按 server 分键、原子写、权限自检 | `core/config.py`、`core/http.py` | 中 |
| 11 | 拆出独立 `movieclaw-cli` 发行包 + `install.sh` | `pyproject.toml`、`scripts/install.sh`（新） | 中 |

`docker/runtime-version` **需要 +1**：第 11 项改动了 pyproject 的包结构与依赖组织
（见 `CLAUDE.md` 发布规范第 2 条）。

---

## 10. 测试

1. **协议单元测试**：authorize→pending→approve→兑换 全链路；denied / expired /
   重放兑换 / SLOW_DOWN 五条异常路径各一例。
2. **默认拒绝守护**（扩展现有全路由扫描）：新增的两个匿名端点在白名单里显式登记；
   其余路由匿名一律 401。
3. **Worker 隔离守护**：遍历全部路由，持 worker 令牌访问，除转码四个端点外一律 4xx。
4. **权限继承**：成员批准的令牌，其 `allow_search` 关闭后调搜索接口返回 403；
   该成员被停用或改密（`token_version` +1）后，令牌立即 401。
5. **CLI 端到端**：真实 uvicorn + 真实 `mclaw login`，脚本模拟浏览器批准，
   断言凭证落盘位置与权限位（0600）、非 TTY 下 `login` 以用法错误退出。
6. **Worker 端到端**：Swift 侧对 authorize/token 两个端点的状态机测试；
   钥匙串读写；拒绝与超时路径的 UI 状态。
7. **人工 golden**：全新 Mac 上从下载应用到出现「已授权·运行中」，
   全程不打开终端、不抄任何字符串。

---

## 11. 实施路线

| 阶段 | 内容 | 验收 |
|---|---|---|
| **P0 协议与存储** | 落点 1–5：挑战模型、端点、令牌字段、验签装配与隔离守护测试 | 协议全路径测试通过；Worker 隔离与权限继承测试红/绿可验证 |
| **P1 网页审批页** | 落点 7 | 能看到待批准请求、批准、吊销；手工 PAT 一并显示 |
| **P2 两个客户端** | 落点 6、8、9、10（Worker 与 CLI 可并行） | 全新 Mac 走完 golden 流程；CLI 在 GUI 应用触发的终端里可用 |
| **P3 分发** | 落点 11 | 干净机器上一行安装命令跑通；`mclaw` 在非登录 shell 里可执行 |

P0 与 P1 之间不能并行——审批页要按端点的真实返回来写。P2 的两个客户端之间
没有依赖，可以并行。

---

## 12. 明确不做

- **不实现 OAuth Provider**（授权码、动态客户端注册、refresh token 轮换）。
  单管理员的自部署产品没有第三方开发者生态，OAuth 的复杂度全部用来解决
  不存在的问题。真需要被 MCP 客户端自动发现时，再在现有令牌之上补 RFC 9728
  的元数据端点。
- **不做令牌自动续期**。长期令牌 + 可吊销已经够用，续期只会增加状态。
- **不加密本地凭证文件**。见 §8 末尾。
- **mDNS 局域网自动发现暂不做**。它是让用户连地址都不用打的锦上添花
  （AirPlay、Plex、Jellyfin 同款机制，服务端约二十行 + Worker 侧 `NWBrowser`），
  但主流程没有它一样完整。排在 P3 之后，作为独立增强评估。

---

## 13. 开放问题

1. **超管改密时是否清空其名下的设备令牌**。成员这条路已经自洽——改密即
   `token_version` +1，他授权出去的令牌自动全失效（§4.2）。超管没有
   `token_version`，改密只轮换签名密钥，落库令牌不受影响。倾向：改密时给一个
   明确的选项「同时吊销我授权的所有设备」，默认不勾——改密的动机多半是密码
   本身泄漏而非设备失窃，静默踢掉所有 Worker 的观感很差。
2. **成员是否可以批准设备**。§4.4 的「用成员账号批准以收窄权限」要求成员能
   进入设备页并按下批准。这意味着设备页不能整体挂 `require_admin`，而要按
   「成员只见自己授权的设备」分流。倾向支持——它是 §4.4 那条路径成立的前提，
   实现成本只是一个按 `owner_member_id` 过滤的查询。
3. **令牌是否默认设过期**。默认长期有效最省事，但自部署用户的令牌泄漏是
   不可察觉的。倾向：设备流签发的令牌默认长期，但在设备列表里对
   「超过 90 天未使用」的令牌显示提示，把决定权交给用户。
