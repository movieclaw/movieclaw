---
name: release
description: 发布 movieclaw 新版本。当用户要求发版、发布新版本、打 tag、发布 NER 模型或发布 Docker 镜像时使用。涵盖版本号三处同步、应用/模型/镜像三种发布类型的完整流程与检查清单。
---

# movieclaw 发布规范

本项目有**三种发布类型**，先判定这次要发的是哪种（机制背景见
`docs/design/in-app-update.md`）：

| 类型 | 什么时候发 | 用户侧感知 |
|------|-----------|-----------|
| 应用 Release（`vX.Y.Z` tag） | 前后端代码变了（最常见） | 设置页一键更新，免拉镜像 |
| 模型 Release（`torrent-ner-vN` tag） | NER 模型重新训练 | 设置页一键更新模型 |
| Docker 镜像 | 随应用 Release 自动发（release.yml 调用 docker-image.yml 推 Docker Hub） | 仅 runtime bump 时用户必须重拉 |

## 一、应用 Release（日常发版）

### 1. 版本号三处同步（硬约束，构建脚本强制校验）

以下三处必须完全一致，否则 CI 构建直接失败、Release 不会创建：

- `pyproject.toml` 的 `[project] version`
- `src/movieclaw_api/__init__.py` 的 `__version__`
- git tag（去掉 `v` 前缀后的部分）

发版第一步就是把前两处 bump 到目标版本并提交合入主干。

### 2. 发版步骤

```
1. bump 两处版本号，并重导出基线 spec（OpenAPI spec 含应用版本号，漏了
   这步 CI 的 test_baseline_spec_matches_code 必挂）：`scripts/export-spec.sh`
   （一次写两处：服务端读的 `src/movieclaw_api/data/spec.json` 与 Go CLI
   内嵌的 `cli/internal/spec/data/spec.json`，两份漂移 pytest 和 go test 都红）
   → 提交 PR 合入 main（changelog 可同 PR 一起写，见下）
2. 以发版 PR 的 CI 全绿为准（CI 会跑 ruff / pytest / web lint / typecheck），
   无需在本地重跑全量测试——本地跑 pytest 还需先下载 NER 模型，且沙箱
   代理环境会造成与代码无关的误报
3. git tag vX.Y.Z && git push origin vX.Y.Z
   （推不了 tag 的环境——如远程会话，git 凭证只能推分支、也没有
   workflow_dispatch 的 API 权限，两条正门都够不着。走点火通道：
   `git push origin main:refs/heads/release-kick/vX.Y.Z`，
   release-kick.yml 会校验 tag 与 main 的 pyproject 版本一致后，
   以 GITHUB_TOKEN 代为触发 release.yml（在 main HEAD 上创建 tag），
   并自动删除点火分支。人工兜底：到 Actions → release 手动
   Run workflow 输入 tag）
4. release.yml 自动构建并上传 Release assets：
   app-web.tar.gz / app-backend.tar.gz / manifest.json（可选 manifest.json.sig）；
   产物上传成功后自动发布多架构 Docker 镜像到 Docker Hub
   （movieclaw/movieclaw，正式版打 vX.Y.Z + runtime-N + latest，
   预发布版只打 vX.Y.Z-… 不动 latest）
5. changelog：写 docs/changelog/vX.Y.Z.md 合入 main，release-notes.yml 会
   自动把它同步为 Release body（应用内更新界面原文展示）；先合 changelog
   后发 Release 也没关系，Release 创建后再触碰一次该文件即可触发同步。
   **撰写规则（保证不漏、不流水账）**：
   - 检索范围用 `git log --first-parent 上一tag..HEAD --oneline`，
     以合并 PR 为单位枚举区间内全部变更（含直接推 main 的提交），
     一条不落地过目；拿不准的条目再看该 PR 的具体 diff
   - 内容按「新功能 / 改进 / 修复」分组，写用户视角的中文（用户能感知
     什么变了、要不要做什么），纯内部重构/CI/文档类改动可合并一句带过
     或省略
   - runtime bump 的版本必须在 changelog 显著位置写明「需要更新
     Docker 镜像」及原因
6. 验证：到 GitHub Release 页确认三个产物齐全、body 是 changelog 而非
   自动生成的 PR 清单；有条件的话在一个 Docker 部署实例上走一遍
   「设置 → 应用」端到端更新
```

### 3. 预发布（beta/rc）

tag 带预发布段（如 `v0.3.0-beta.1`）时，release.yml 会自动标记 GitHub
prerelease，应用内更新的检查逻辑会跳过它——beta 不会被推给全量用户。
预发布数字段按数值比较（`beta.10` > `beta.9`），命名放心递增。

## 二、何时必须发 Docker 镜像（runtime 契约）

`docker/runtime-version` 是运行时依赖集合的版本代号（唯一事实源）。
**凡是改了以下任何一项，必须把它 +1**：

- `pyproject.toml` 的 dependencies
- Node 大版本、Dockerfile 里的系统包（ffmpeg 等）或基础镜像
- `docker/entrypoint.sh` 的行为契约（重启约定码、目录约定等）

CI 守卫（runtime-guard.yml）会拦截漏 bump 的 PR。bump 后发的应用
Release 其 manifest 会声明新的 `requires_runtime`，旧镜像用户在设置页
会看到「需升级 Docker 镜像」的明确提示，而不是坏掉的更新。

镜像发布本身无需额外操作——每次应用 Release 都会自动发布新镜像
（含 runtime-N 标签）。需要在发版之外单独补发镜像时，到 Actions →
docker-image 手动 Run workflow 输入标签即可。本地/离线构建仍可用
`TMDB_API_KEY=xxx ./scripts/build-image.sh`（详见脚本头注释；启用了
清单签名的话记得带 `--build-arg UPDATE_MANIFEST_PUBKEY=<公钥>`）。

## 三、模型 Release

1. 训练产出三件套：`model.int8.onnx`、`tokenizer.json`、`labels.json`
2. 生成更新清单（tag 必须与将要创建的 Release tag 一致，后端会强校验）：
   `./scripts/build-model-manifest.sh <模型目录> torrent-ner-vN`
3. 创建 tag 为 `torrent-ner-vN` 的 GitHub Release（N 递增），上传
   三件套 + `manifest.json`（+ 签名时的 `manifest.json.sig`）
4. **不要**把模型 Release 标成 latest 之外还需担心什么——应用更新检查
   按 tag 正则过滤，模型 Release 不会干扰应用更新
5. 没带 manifest.json 的模型 Release 无法被应用内安装（会提示用户），
   只能作为镜像构建的 `NER_MODEL_BASE` 来源

## 四、清单签名（可选，防 Release/加速镜像被篡改）

- 首次启用：`./scripts/gen-release-signing-key.sh` 生成密钥对；私钥配成
  仓库 Actions 机密 `RELEASE_SIGNING_KEY`（绝不入库），公钥烧进镜像
  （`UPDATE_MANIFEST_PUBKEY` 构建参数）或让用户配同名环境变量
- 启用后：应用发版自动签名；模型清单脚本在有 `RELEASE_SIGNING_KEY`
  环境变量时也会生成签名
- 注意：部署侧配置了公钥后，**所有**更新清单必须携带有效签名，包括
  模型 Release——启用后发的每个 Release 都不能漏传 `.sig`

## 五、硬件转码矩阵人工验收（改动播放/转码时必做）

CI 里没有显卡，`-m "not integration"` 的门禁**完全覆盖不到硬件转码**——
装配参数有单测、软件转码有集成测试，但「这块显卡上真能不能转」只有人能验。
凡是改了 `services/playback/`（尤其 `ffmpeg_args.py` / `hwprobe.py`）或换了
ffmpeg 版本，发版前按下表逐项过一遍。

每种硬件重复这几步：

1. 打开「设置 → 播放 → 查看检测详情」，确认该后端显示为可用；若不可用，
   照它给的中文提示改配置后按「重新检测」——这条提示本身也是被验收对象。
2. 播一部 **HEVC + DTS 的 MKV**（走档 2 音频单转）与一部 **4K HDR**
   （走档 3 硬件转码 + 色调映射），各拖几次进度条。
3. 记录「播放诊断」面板里的档位、是否硬件加速、掉帧数。
4. 播放中在宿主机确认 GPU 真的在动（`intel_gpu_top` / `nvidia-smi` /
   `radeontop`），别被「软件转码也能出画」骗过去。

| 硬件 | 自检可用 | 档 2 直通 | 档 3 转码 | HDR 转 SDR | 掉帧 <1% |
|---|---|---|---|---|---|
| Intel 核显（VAAPI/QSV） | ☐ | ☐ | ☐ | ☐ | ☐ |
| NVIDIA（NVENC） | ☐ | ☐ | ☐ | ☐ | ☐ |
| AMD（VAAPI） | ☐ | ☐ | ☐ | ☐ | ☐ |
| 无显卡（软件兜底） | — | ☐ | ☐ | 应明确拒绝 | ☐ |

另外三项与硬件无关但同样只能人验：

- [ ] **播放器真机走查**：内封 ASS 番剧（字体、特效、时间轴微调）、内封 SRT、
      外挂字幕、切下一集、续播点、进度条缩略图预览
- [ ] **iOS**：同局域网 iPhone 打开播放页，确认走原生 HLS 且全屏可用
- [ ] **Safari**：HEVC 直通不黑屏（`hvc1` 标签那条陷阱只有 Safari 能验）

## 六、发版检查清单

- [ ] 版本号三处一致（应用发版）
- [ ] bump 版本号后已跑 `scripts/export-spec.sh`（服务端与 Go CLI 两份 spec）
- [ ] 本次改动是否触碰运行时依赖？触碰了 → `docker/runtime-version` +1（镜像随发版自动发布）
- [ ] 数据库迁移向前兼容（alembic 迁移是单向的，用户回退靠自动备份）
- [ ] Release 产物齐全（CI 完成后到 Release 页核对）：应用更新三件套 +
      mclaw 五平台产物（`mclaw_{linux,darwin}_{amd64,arm64}.tar.gz`、
      `mclaw_windows_amd64.zip`、`checksums.txt`）
- [ ] 启用签名的仓库：`.sig` 已随产物上传
- [ ] changelog 已写入 `docs/changelog/vX.Y.Z.md` 并合入 main（release-notes.yml
      自动同步为 Release body，应用内更新界面会原文展示给用户）
- [ ] 改动了播放/转码 → 第五节的硬件矩阵人工验收已过（CI 覆盖不到）
