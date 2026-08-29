# mclaw CLI 迁移到 Go 的实施计划

> 结论先行：**新建顶层 `cli/` 作为独立 Go module，一次性替换 `src/movieclaw_cli`**。
> 迁移的第一步不是写 Go，而是**切断服务端对 CLI 包的运行时依赖**——那是当前
> 架构里唯一一处让「换语言」变成「动服务端」的耦合。
>
> 决策依据是本机实测（同一份 spec.json，544 KB / 279 端点）：
>
> | 形态 | 冷启动 | 体积 |
> |---|---|---|
> | **Go 单二进制（内嵌 spec）** | **6 ms** | **2.3 MB** |
> | uv + 独立 Python（现状） | 172 ms | 108 MB |
> | Nuitka onedir | 240 ms | 57 MB |
> | Nuitka onefile | 271 ms | 57 MB |
>
> 把 Python 编译成二进制**反而更慢**：Nuitka 加速的是 CPU 密集计算，而 mclaw 的
> 启动 100% 是 import 链与 JSON 解析。因此 `docs/design/device-auth.md` §6.4 里
> 「将来需要单二进制时补 Nuitka」那条判断作废——它解决不了问题。

---

## 0. 为什么现在做，以及什么不变

**做的理由**：CLI 的定位是「任何 agent 都能引用的工具」。172 ms × 一次任务几十次
调用是可感知的延迟；108 MB 装一个 HTTP 薄客户端对 NAS 用户不体面；Windows 一行
安装至今没有兑现（`install-cli.ps1` 从未存在）。这三条都指向同一个根因——分发形态。

**不变的东西**（迁移不重新设计，只换实现语言）：

- OpenAPI spec 是唯一事实源，命令树运行时由 spec 生成（`docs/design/cli.md` §1–3）；
- 退出码契约（0/1/2/3/4/5/6/7，§5.8）；
- 设备授权流程与凭证落盘位置（`docs/design/device-auth.md` §6）；
- 非 TTY 默认 JSON、`-o table|json|yaml`、stdout 只放数据；
- 精选命令的命令面与语义。

**耦合面盘点**（决定了迁移顺序）：

| 谁 | 依赖什么 | 处理 |
|---|---|---|
| `movieclaw_api/services/mclaw_tool.py` | `import movieclaw_cli`（load_baseline / iter_operations / is_generable / DOMAIN_HELP / CliError） | **Stage 0 先切断**：新增 `services/spec_catalog.py` 直接读 spec.json |
| `movieclaw_agent/tools/mclaw.py` | `sys.executable -m movieclaw_cli` 起子进程 | Stage 4 改为执行 mclaw 二进制 |
| `Dockerfile` | 现场导出 spec 到 `src/movieclaw_cli/data/` | Stage 4 增加 go-builder 阶段 |
| `scripts/build-release-artifacts.sh`、`app_update.py:_validate_layout` | 产物布局含 `backend/src/movieclaw_cli/data/spec.json` | Stage 4 改布局，产物带双架构二进制 |
| `tests/cli/*`（2428 行） | 测 Python 实现 | Stage 5 退役，行为契约由 Go 测试与共享快照承接 |

---

## 1. 架构落位

仓库已经是多语言 monorepo，约定是**一门语言一个顶层目录**：`src/` = Python、
`apps/` = 前端、`macos/` = Swift。Go 按同一约定新开顶层 `cli/`，module 根在
`cli/` 而不是仓库根——与 `macos/MovieClawTranscoder/Package.swift` 的隔离方式一致，
`go build ./...` 不会扫到无关目录。

```
cli/
├── go.mod                       module github.com/yipengfei329/movieclaw/cli （go 1.24）
├── .goreleaser.yaml             多平台构建与分发
├── cmd/mclaw/main.go            唯一入口：装配 root 命令、全局标志、退出码收口
├── internal/
│   ├── clierr/                  ← core/errors.py       退出码与带 hint 的错误
│   ├── config/                  ← core/config.py       上下文、凭证、平台路径、权限自检
│   ├── api/                     ← core/http.py         认证注入、信封拆解、错误映射
│   ├── jsonval/                 （新）保序 JSON 对象与取值助手
│   ├── flagx/                   （新）接受「裸数字＝秒」的时长标志
│   ├── sse/                     ← core/sse.py          手写分帧、Last-Event-ID 续传
│   ├── output/                  ← core/output.py       table/json/yaml、TTY 判定
│   ├── wait/                    ← tree_builder 的两个等待循环（生成层与精选层共用）
│   ├── spec/                    ← gen/spec_loader.py   内置基线 + hash 偏斜刷新
│   │   └── data/spec.json       go:embed 的内置基线（scripts/export-spec.sh 生成）
│   ├── tree/                    ← gen/tree_builder.py  spec → cobra 命令树（最大一块）
│   └── overlay/                 ← overlay/*.py         精选命令
│       ├── auth.go  login/logout/status（设备配对）
│       ├── search.go download.go library.go session.go logs.go jobs.go
│       └── groups.go  DefaultCommandGroup 等价物
├── testdata/                    命令面快照（生成命令清单 + 装配后的整棵树）
└── e2e/                         对真服务器与协议桩的端到端验收脚本
```

两个包是移植过程中长出来的，Python 版没有对应物：

- **`jsonval`**：Go 的 map 无序、`encoding/json` 又按字典序输出，直接用
  `map[string]any` 会把服务端排好的字段顺序打乱（`name`、`kind` 被
  `auto_clear_missing` 挤到后面）。JSON 输出是 Agent 的稳定契约、表格列序是给
  人扫一眼的，两者都得按服务端给的顺序。顺带用 `json.Number` 解数字，长 ID
  不再退化成浮点近似值。
- **`flagx`**：`time.ParseDuration` 要求带单位，但既有契约是秒
  （`--timeout 30`、`--wait-timeout 3600` 已经写进脚本和 Agent 的用法）。
  两种都收。

依赖（刻意保持少，与 Python 版三个依赖的克制同口径）：

- `spf13/cobra` + `spf13/pflag`：命令树可程序化注册，这是动态生成的前提；
- `gopkg.in/yaml.v3`：`-o yaml`；
- `golang.org/x/term`：TTY 判定；
- HTTP、JSON、表格输出（`text/tabwriter`）全部用标准库，不引第三方客户端库。

---

## 2. spec 的分发：内嵌基线 + 磁盘覆盖

Python 版是「随包内置基线 + 运行时按 hash 刷新」。Go 版保持同一模型，只是基线的
承载方式变了：

```go
//go:embed data/spec.json
var baselineSpec []byte
```

装载优先级（`internal/spec`）：

1. `MOVIECLAW_SPEC_FILE` 指定的文件 —— **镜像内走这条**：服务端在镜像构建时
   现场导出 spec，二进制读它，保证与镜像内代码严格同版；
2. `~/.cache/movieclaw/spec-<server>.json` —— 偏斜刷新写入的缓存；
3. `//go:embed` 的内置基线 —— 远程独立安装的用户走这条，断网也有完整命令树。

这样既保住「单文件二进制」对外发行的卖点，也保住镜像内「spec 与代码零偏斜」的
既有保证，Dockerfile 的现场导出逻辑不用推翻。

---

## 3. 三个需要设计对齐的实现点

其余模块都是直译，只有这三处 Go 与 Python 的表达方式不同，需要先想清楚。

### 3.1 由 JSON Schema 动态构造 flag

click 是 `click.Option` 对象，cobra 是 `pflag.FlagSet`，两边都支持运行时构造。
映射表照搬 `docs/design/cli.md` §3.2，类型落到 pflag 的 `StringVar/IntVar/BoolVar/
Float64Var/StringSliceVar`；枚举用 `pflag.Value` 接口自定义类型做校验并在 help 里
列出候选；嵌套对象与数组走 `--<字段>-json`；整体替代走 `--input`。

### 3.2 `mclaw search "沙丘2"` 的默认子命令

Python 版靠自定义 `click.Group.resolve_command`。cobra 的等价做法：父命令设
`TraverseChildren`，并在 `Args` 里判断首参是否命中已知子命令，未命中则把整串参数
转交默认子命令。需要覆盖首参是选项（`mclaw search --limit 5 沙丘`）的情形，
与 Python 版 `parse_args` 里那段垫子同源。

### 3.3 命令树必须与 Python 版逐字节一致

这是整个迁移最重要的一道验证。`tests/cli/command_tree_snapshot.txt` 是**语言中立
的产物**——它就是一份命令路径清单。因此：

> Go 版从同一份 spec 生成的命令树，必须与现有快照文件**逐字节相同**。

这一条测试独力覆盖了 681 行 `tree_builder.py` 的全部映射规则是否被正确移植。
迁移期间该文件不允许变更；Stage 5 删掉 Python CLI 后，它归 Go 测试所有。

---

## 4. 实施阶段

每个阶段自身可验证、可单独合入。前一阶段绿了才进下一阶段。

### Stage 0 —— 切断服务端对 CLI 包的依赖（不含任何 Go 代码）

服务端渲染 Agent 工具描述时 import 了 CLI 包。这层依赖本身就是设计错误：
`load_baseline` / `iter_operations` / `is_generable` 做的是**读 spec.json**，
与「命令行客户端」没有关系。

- 新增 `src/movieclaw_api/services/spec_catalog.py`：读 spec、迭代 operation、
  判定是否进命令树、给出域清单（约 80 行）；
- `services/mclaw_tool.py` 改为依赖它，删掉三处 `movieclaw_cli` import；
- `CliError` 的捕获改为本地异常类型。

**验收**：`grep -rn "movieclaw_cli" src/movieclaw_api src/movieclaw_agent` 只剩
子进程调用那一处；`tests/api/test_agent.py` 的工具描述快照测试不变绿。

这一步**即便 Go 迁移中途放弃也应当保留**——它单独就让架构更干净。

### Stage 1 —— Go 骨架与地基

`cli/` module、`cmd/mclaw`、`internal/{clierr,config,api,output,spec}`，以及
`mclaw status` / `mclaw login` / `mclaw logout` 三条精选命令。

**验收**：真实服务器上完成设备配对（含被拒绝、超时、非 TTY 三条路径）；
凭证落盘位置、0600 权限、权限过宽拒绝、原子写、地址解析失败时列出查找路径——
全部对齐 `device-auth.md` §6.2/§6.3；退出码 2/3/4 与 Python 版一致。

### Stage 2 —— 生成层

`internal/tree`：spec → 命令树、参数映射、help 渲染、`x-cli-*` 元数据消费。

**验收**：`go test` 生成的命令树与命令树快照逐字节相同；
`KNOWN_NON_GENERATED` 等价清单一致；抽样端点的 `--help` 含 summary、
参数说明与 `x-cli-examples`。

**实际结果**：216 条生成命令逐字节一致。另外发现并修掉一处 click 的固有限制：
一个名字在 click 里只能是组或命令二选一，`dl limits set` 与 `watch entries`
因此被各自的同名节点挤掉——快照里一直列着它们，但从未真正挂上去过。cobra
允许一个节点既可执行又带子命令，两条命令随之补齐。

### Stage 3 —— 精选层与流式

`internal/sse` + overlay 的 search / download / library / session / logs / jobs。
含长任务 `--wait` 轮询、危险确认（`--yes`、destructive 回显影响面）、
搜索结果快照落本地、歧义退出码 7。

**验收**：`tests/cli/test_p2_flows.py` 覆盖的行为逐条在 Go 侧有对应测试并通过；
「搜索 → 下载 → 订阅 → 扫描入库」在真实服务器上跑通。

**实际结果**：`cli/e2e/` 里落了两套可重复跑的验收脚本——`live.sh` 对真服务器
（47 项：只读查询、三种输出格式、三种传参形态、七种退出码、帮助文案、长任务），
`stub.py` + `sse.sh` 对协议桩（24 项：搜索流、会话流断线续传、Job 等待、
行号快照 → 下载、歧义消解退出码 7）。搜索流、会话流在真环境要接 PT 站点和
大模型，CI 里没有，所以协议本身用桩走完整。

### Stage 4 —— 集成进产品

| 改动 | 位置 |
|---|---|
| Dockerfile 增加 `go-builder` 阶段，二进制装到 `/usr/local/bin/mclaw` | `Dockerfile` |
| Agent 工具改执行二进制（`MOVIECLAW_CLI_BIN`，默认 `/usr/local/bin/mclaw`） | `src/movieclaw_agent/tools/mclaw.py` |
| 发版流程新增 cli 作业，由 GoReleaser 出五平台产物附到 Release | `.github/workflows/release.yml`、`cli/.goreleaser.yaml` |
| 基线 spec 搬到 `src/movieclaw_api/data/`，一次导出写两处 | `scripts/export-spec.sh`、`services/spec_catalog.py`、`app_update.py:_validate_layout` |
| CI 增加 Go lane（`go vet` / `golangci-lint` / `go test`），与既有 Swift lane 同构 | `.github/workflows/ci.yml` |

**Dockerfile 的阶段顺序**是这一步的关键：spec 必须先由 Python 侧导出，再进
go-builder 编译，否则内嵌基线与镜像代码不同版。

**验收**：镜像内 `mclaw --help` 可用；产品内 Agent 的 mclaw 工具跑通「我的订阅
有哪些」；`docker/runtime-version` +1（Dockerfile 的构建阶段与基础镜像变了）。

### Stage 5 —— 退役 Python CLI

删除 `src/movieclaw_cli/`、`packaging/cli/`、`tests/cli/`（快照文件迁到
`cli/testdata/`）、根 `pyproject.toml` 的 `mclaw` entry point 与 click 依赖。
`scripts/install-cli.sh` 改为下载 GoReleaser 产物，并**补上 `install-cli.ps1`**。

**验收**：`grep -rn "movieclaw_cli"` 只剩讲历史的注释；干净机器上一行安装跑通；
Windows 一行安装跑通；`pyproject.toml` 少掉 click 依赖（`runtime-version` 再 +1）。

Python 侧的命令面守护测试（命令树快照、豁免端点清单、域帮助覆盖、参数不与内置
标志重名、`x-cli` 标注）全部搬进 `cli/internal/tree/guards_test.go`；
基线 spec 的漂移守护搬到 `tests/api/test_spec_baseline.py`，并新增一条
「Go 内嵌副本与服务端副本逐字节一致」。

### Stage 6 —— 分发与验收

`.goreleaser.yaml`：darwin/linux/windows × amd64/arm64 六个目标，产出裸二进制
压缩包 + checksums；Homebrew tap 与 Scoop manifest 按需。

**人工 golden**（这是最终验收，需要你来做）：

1. 干净 macOS：一行安装 → `mclaw login` 配对 → `mclaw subscriptions list`；
2. 干净 Windows：一行安装 → 同上；
3. Docker 镜像内：产品内 Agent 完成一次真实任务；
4. Mac Worker 与 CLI 同时在线，网页设备页能分别吊销。

---

## 5. 工作量与风险

| 模块 | Python 行数 | Go 预估 | 风险 |
|---|---|---|---|
| tree（生成层） | 681 | ~1100 | **高**——映射规则最多；由快照逐字节比对兜底 |
| overlay/search + download | 483 | ~700 | 中——SSE 聚合与结果快照 |
| config | 255 | ~350 | 低——逻辑简单但边界多（已有 12 项测试可照抄） |
| session（SSE） | 245 | ~350 | 中——Last-Event-ID 续传与退避 |
| api | 239 | ~350 | 低 |
| 其余 overlay + core | ~800 | ~1100 | 低 |
| 测试 | 2428 | ~2000 | —— |
| **合计** | 2998 + 2428 | **约 4000 + 2000** | |

**主要风险与对策**：

1. **映射规则移植出错** → 命令树快照逐字节比对，一处不同即红；
2. **退出码/输出契约漂移** → 把 `test_exit_codes.py` 与 `test_p1/p2_flows.py`
   的断言逐条移植为 Go 表驱动测试，它们是 Agent 依赖的机器接口；
3. **镜像内 spec 与二进制不同版** → Stage 4 的阶段顺序 + `/health` 的
   `spec_hash` 偏斜检测（已有机制）；
4. **中途卡住** → Stage 0 独立有价值；Stage 1–3 期间 Python CLI 仍在服役，
   任何阶段中止都不影响现有功能。真正的不可逆点是 Stage 5。

---

## 6. 明确不做

- **不趁机改命令面**。迁移期间命令名、参数名、输出结构、退出码一律不动——
  否则出问题时无法判断是移植 bug 还是设计变更。要改，迁移完成后单独提。
- **不做 Go 版的 keyring**。凭证仍是 0600 文件，与 `device-auth.md` §6.2 一致。
- **不把服务端也 Go 化**。这次只换客户端。
- **不保留 Python CLI 做双轨**。两份实现必然漂移，且 Agent 到底调哪个会变成
  一个说不清的问题。Stage 5 一次删干净。

---

## 7. 迁移期发现并修掉的问题

移植不是逐行翻译，对着两版逐条比对输出反而暴露了原实现里的几处问题。记在这里，
因为它们都不是「Go 特有」的，将来读代码的人会想知道为什么这么写：

| 问题 | 影响 | 处理 |
|---|---|---|
| `dl limits set`、`watch entries` 从未挂上命令树 | 两条命令在快照里、在文档里，但敲了报「未知命令」 | cobra 允许一个节点既可执行又带子命令，补齐 |
| `Stream` 的注释写着「防半开连接永久挂死」，代码只设了 `Timeout: 0` | NAT 超时、反代重启、服务假死时命令永久挂着，既不出结果也不报错 | 按读空闲计时的 `idleGuard`，120 秒无数据就取消请求 |
| 搜索流在 `done` 之前断掉时报的是笼统的断流错误 | 调用方不知道收到几条、快照有没有落 | 改报「结果不完整（仅收到 N 条，未落快照）」 |
| `require_admin` 接受 Bearer 令牌（Stage 0 之前就存在） | 泄漏的设备令牌可以自我复制：再签一枚令牌、批准攻击者的机器 | 新增 `require_admin_session`，凭证签发面收归浏览器会话 |

**一个需要长期记住的后果**：mclaw 是 COPY 进镜像的二进制，应用包里没有它，
所以它和 `nginx.conf.template`、`entrypoint.sh` 同属「只存在于镜像里的文件」——
改了 `cli/` 就要 bump `docker/runtime-version` 并发新镜像，否则老镜像用户的
Agent 拿不到新命令。`runtime-guard.yml` 已把 `cli/**` 纳入监控（排除
`_test.go`、`e2e/`、`testdata/` 这些不进镜像的部分）。好在不会坏：spec 偏斜
检测会让旧 CLI 从服务端拉新的接口目录，只是拿不到精选命令与修复。

对着真服务器逐条比对退役中的 Python CLI 是这次迁移性价比最高的一步：32 条命令的
JSON 输出、12 条命令的表格输出逐字节一致，YAML 因两边 emitter 风格不同
（PyYAML 序列不缩进、偏好单引号）比数据等价。**字段顺序**这个问题就是这样发现的，
靠读代码不可能看出来。
