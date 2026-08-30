# 不声明 syntax 指令：本文件只用多阶段、COPY --from、$BUILDPLATFORM 这些
# BuildKit 内置前端就支持的特性。声明了反而要求每次构建先从 Docker Hub 拉
# frontend 镜像，网络不畅时整个构建会卡在第一步（国内部署者尤其常见）。
# =============================================================================
# movieclaw 单容器镜像：Next.js 前端 + FastAPI 后端 + NER 模型，一个容器跑全部。
#
# 设计要点：
#   - 前端 standalone 输出：只带被引用的依赖，不装完整 node_modules
#   - 后端只装运行依赖（从 pyproject 提取），源码按项目布局摆放（不 pip install
#     打包——启动迁移按「源码根目录」定位 alembic.ini，见 movieclaw_db/migrations.py）
#   - NER 模型从 GitHub Release 下载后烧进镜像，开箱即用，无需用户手动放置
#   - TMDB Key 通过构建参数烧进镜像（运行时可用环境变量覆盖）
#   - 对外只暴露一个端口（默认 3000，可用 MOVIECLAW_WEB_PORT 环境变量或应用内
#     「设置 → 应用设置」改），由容器内 nginx 前门接住：/api/v1 与 Jellyfin
#     命名空间直达后端，页面与静态资源交给 Next（docker/nginx.conf.template）
#   - 运行期数据全部落在 /app/data，挂载这一个卷即可持久化
#
# 构建（推荐用 scripts/build-image.sh，会自动带上 TMDB Key）：
#   docker build --build-arg TMDB_API_KEY=xxx -t movieclaw:latest .
#
# 国内网络加速（可选）：
#   --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
#   --build-arg NPM_REGISTRY=https://registry.npmmirror.com
#   --build-arg NER_MODEL_BASE=<GitHub Release 的镜像加速地址>
#   --build-arg SECONV_BASE=<Subtitle Edit Release 的镜像加速地址>
# =============================================================================

# ---------------------------------------------------------------------------
# 阶段 1：前端构建（含浏览器扩展 zip，供设置页下载）
# ---------------------------------------------------------------------------
# 前端产物是纯 JS（images.unoptimized 已去掉 sharp 原生依赖），跨架构通用，
# 因此固定在构建机原生架构上跑，交叉构建时不经过 QEMU 模拟（快一个数量级）。
FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS node-deps
ARG NPM_REGISTRY=https://registry.npmjs.org
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm install -g pnpm@10
WORKDIR /build
# 源码要在 install 之前就位：extension 的 postinstall（wxt prepare）依赖 entrypoints/ 源码
COPY pnpm-workspace.yaml pnpm-lock.yaml package.json ./
COPY apps ./apps
RUN pnpm config set registry "$NPM_REGISTRY" && pnpm install --frozen-lockfile

# 浏览器扩展单独成阶段（设置页「浏览器插件」提供下载）。
# 必须与前端构建隔离：两者都用 vite 系工具链，在同一工作目录里先后执行时，
# 扩展构建留下的产物会让随后的 next build 静默挂死（日志停在 "Creating an
# optimized production build"、CPU 掉到 0）。分阶段后各自从干净的 node-deps
# 出发，只把最终的 zip 交给前端，互不干扰。
FROM node-deps AS ext-builder
RUN pnpm ext:zip \
    && mkdir -p /out \
    && cp "$(ls -t apps/extension/.output/*-chrome.zip | head -1)" /out/movieclaw-extension.zip

FROM node-deps AS web-builder
# 限制静态生成并发 + 给 Node 明确堆上限：Docker 虚拟机通常核多内存少，
# 放任 Next 按核数开 worker 会把内存吃干（见 next.config.ts）
ENV NEXT_BUILD_CPUS=2 \
    NODE_OPTIONS=--max-old-space-size=4096
COPY --from=ext-builder /out/movieclaw-extension.zip apps/web/public/extension/movieclaw-extension.zip
# 输出重定向到文件、仅失败时回放：next build 的输出量很大，直接写 BuildKit
# 的日志管道会在管道拥塞时把进程永久挂起（表现为日志停在 "Creating an
# optimized production build"、CPU 掉到 0，可以挂十几个小时）。
RUN pnpm web:build > /tmp/web-build.log 2>&1 || (cat /tmp/web-build.log; exit 1)

# ---------------------------------------------------------------------------
# 阶段 2：后端运行依赖（只装 pyproject 的 dependencies，不装 dev 工具、不打包源码）
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS py-deps
ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /build
COPY pyproject.toml ./
# 构建会下载多个带原生 wheel 的大包；NAS/跨境链路偶发 TLS EOF 时，pip
# 默认 5 次短重试不够。显式放宽只影响构建容错，不改变依赖解析结果。
RUN python -c "import tomllib; deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; open('requirements.txt', 'w').write('\n'.join(deps))" \
    && python -m venv /venv \
    && /venv/bin/pip install --no-cache-dir --retries 10 --timeout 60 \
        -i "$PIP_INDEX_URL" -r requirements.txt

# ---------------------------------------------------------------------------
# 阶段 2.5：基线 spec 现场导出
# ---------------------------------------------------------------------------
# 现场导出而不是信任构建上下文里那份仓库产物：仓库产物可能过期（改了路由忘了
# 重新导出）或干脆没进上下文，两种情况镜像都照样构建成功，故障要等用户发第一
# 条对话才暴露。现场导出既堵死「缺失」，也保证 spec 与镜像内代码严格同版
# （偏斜检测的前提）。
#
# 它有两个消费方，都从这一份来：服务端运行期读它渲染 Agent 的工具描述，
# Go CLI 在下一阶段把它嵌进二进制。spec 与架构无关，固定在构建机原生架构上跑。
FROM --platform=$BUILDPLATFORM py-deps AS spec-export
WORKDIR /build
COPY src ./src
RUN PYTHONPATH=/build/src /venv/bin/python -m movieclaw_api.export_openapi -o /build/spec.json \
    && test -s /build/spec.json

# ---------------------------------------------------------------------------
# 阶段 2.6：mclaw CLI（Go）
# ---------------------------------------------------------------------------
# CLI 是独立的静态二进制：Agent 的 mclaw 工具执行它，用户也可以直接从 Release
# 下载同一份。CGO_ENABLED=0 保证不依赖运行镜像里的 glibc 版本。
# 基础镜像的 Go 版本必须 >= cli/go.mod 的 go 指令：golang 官方镜像里
# GOTOOLCHAIN=local，够不到的版本不会自动下载工具链，只会直接失败
# （go.mod 要 1.25.0 而镜像是 1.24 时，go mod download 就报错退出）。
FROM --platform=$BUILDPLATFORM golang:1.25-bookworm AS go-builder
ARG TARGETARCH
ARG GOPROXY=https://proxy.golang.org,direct
WORKDIR /build
COPY cli/go.mod cli/go.sum ./
RUN go mod download
COPY cli ./
# 内嵌 spec 用现场导出的那份，覆盖仓库里可能过期的副本
COPY --from=spec-export /build/spec.json ./internal/spec/data/spec.json
RUN CGO_ENABLED=0 GOOS=linux GOARCH=$TARGETARCH \
        go build -trimpath -ldflags="-s -w" -o /out/mclaw ./cmd/mclaw \
    # 同架构时顺手冒烟一次：spec 坏了、命令树建不起来，在这里就断，
    # 而不是等用户第一次跑 mclaw。交叉构建跑不了目标架构的二进制，跳过。
    && if [ "$(go env GOHOSTARCH)" = "$TARGETARCH" ]; then /out/mclaw --help > /dev/null; fi

# ---------------------------------------------------------------------------
# 阶段 3：NER 模型（从 GitHub Release 下载，烧进镜像作为默认模型）
# ---------------------------------------------------------------------------
# 模型文件与架构无关，同样跑在构建机原生架构上
FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS ner-model
ARG NER_MODEL_BASE=https://github.com/movieclaw/movieclaw/releases/download/torrent-ner-v3
ARG APT_MIRROR=""
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /model \
    && cd /model \
    && curl -fSL --retry 3 -O "$NER_MODEL_BASE/model.int8.onnx" \
    && curl -fSL --retry 3 -O "$NER_MODEL_BASE/tokenizer.json" \
    && curl -fSL --retry 3 -O "$NER_MODEL_BASE/labels.json" \
    # 记录内置模型的 Release tag（URL 末段；先去尾斜杠，防加速镜像地址
    # 以 / 结尾时写入空 tag），应用内模型更新据此比对版本
    && NER_BASE_TRIMMED="${NER_MODEL_BASE%/}" \
    && echo "${NER_BASE_TRIMMED##*/}" > /model/.release-tag

# ---------------------------------------------------------------------------
# 阶段 4：PGS 转换器（按目标架构选 Subtitle Edit seconv 官方产物）
# ---------------------------------------------------------------------------
# 这里只下载/校验/解包，不执行目标架构二进制，因此固定在构建机架构上跑，
# 交叉构建 amd64/arm64 时不需要 QEMU。SHA256 锁定 v5.1.0 官方 Release，
# 防下载镜像或上游资产被替换；LICENSE 随二进制一起进入最终镜像。
FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS seconv-dist
ARG TARGETARCH
ARG SECONV_BASE=https://github.com/SubtitleEdit/subtitleedit/releases/download/v5.1.0
ARG APT_MIRROR=""
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN case "$TARGETARCH" in \
        amd64) SECONV_ARCH=x64; SECONV_SHA=3354dfeb4452b0dd8a11d8f3066c31a3116e2cbc137f271ac6b7e4f91896ba11 ;; \
        arm64) SECONV_ARCH=ARM64; SECONV_SHA=ab58891abd54a1604fdf5956c340159bd2efbcaef8cf2c69d97fcc55b83b4718 ;; \
        *) echo "不支持的 seconv 目标架构：$TARGETARCH（仅 amd64/arm64）" >&2; exit 1 ;; \
    esac \
    && curl -fSL --retry 3 -o /tmp/seconv.tar.gz \
        "$SECONV_BASE/SeConv-Linux-$SECONV_ARCH.tar.gz" \
    && echo "$SECONV_SHA  /tmp/seconv.tar.gz" | sha256sum -c - \
    && mkdir -p /out \
    && tar -xzf /tmp/seconv.tar.gz -C /out \
    && chmod +x /out/seconv \
    && test -s /out/LICENSE

# ---------------------------------------------------------------------------
# 阶段 5：目标架构的 node 二进制来源
# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim AS node-dist

# ---------------------------------------------------------------------------
# 阶段 6：运行镜像
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

# - libstdc++6：onnxruntime / tokenizers 的 manylinux wheel 依赖（slim 基础镜像不带）
# - jellyfin-ffmpeg7：介质规格探测（ffprobe）、PGS 轨道抽取、网页播放器的转码与
#   直通。**不用 Debian 自带的 ffmpeg**：硬件加速覆盖不全（QSV/RKMPP/AMF 基本
#   没有），更关键的是没有 GPU 色调映射路径——软件 zscale+tonemap 转 4K HDR 是
#   幻灯片级性能。jellyfin-ffmpeg 维护着一批上游因优先级不同未合入的补丁，恰是
#   媒体库刚需：Intel VPP / CUDA / Metal tone-map、Dolby Vision 透传进 HLS、
#   libx265 fMP4 HLS 的 Safari 兼容修复、Atmos 透传、PGS 在硬件滤镜里叠加。
#   （详见 docs/design/web-player.md §5.3。改动这个包必须 bump runtime-version。）
# - tesseract-ocr + 常用字幕语言：seconv 的跨架构保底 OCR。覆盖简繁中、英、
#   日、韩、法、德、西、意、葡、俄；语言数据约增加 22MB（镜像展开后）。
#   预检仍会按 PGS 语言检测，未知语言不会回退到英语生成乱码
# - fontconfig + DejaVu：seconv 随附的 libSkiaSharp.so 依赖 Fontconfig；内置一套
#   跨架构字体也让构建期可以真正生成 PGS，再 OCR 回 SRT 验证完整调用链
# - libicu72：seconv 自包含 .NET 运行时仍需要 ICU 提供区域/Unicode 数据；
#   缺失时进程会在加载字幕格式类型前直接 FailFast
# - nginx：容器内统一前门。播放器取流/整文件下载直达后端而不经 Node 反代
#   （Node 每 GB 多烧约 10 个 CPU 秒），同时在前后端重启窗口代答占位页。
#   只要核心包（proxy/gzip/map/rewrite 都是静态内置模块），不装推荐的动态模块
ARG APT_MIRROR=""
# Jellyfin 官方 apt 源。用源而不是硬编码 .deb 直链：apt 自己按目标架构取包，
# 交叉构建 amd64/arm64 不用维护两份文件名。国内网络可用 JELLYFIN_REPO 换镜像。
ARG JELLYFIN_REPO=https://repo.jellyfin.org
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && install -d -m 0755 /etc/apt/keyrings \
    # 签名 key 优先从镜像站取，取不到回退官方站：常见加速镜像（如南京大学）
    # 只同步 debian/ 仓库本体、不带顶层 key 文件；key 只有 3KB，官方站再慢
    # 也拖得动（2026-08-23 NAS 实测：官方站 deb 只有 5KB/s，key 2 秒到手）
    && { curl -fsSL --retry 3 -o /tmp/jellyfin_team.gpg.key "$JELLYFIN_REPO/jellyfin_team.gpg.key" \
        || curl -fsSL --retry 3 -o /tmp/jellyfin_team.gpg.key https://repo.jellyfin.org/jellyfin_team.gpg.key; } \
    && gpg --dearmor -o /etc/apt/keyrings/jellyfin.gpg < /tmp/jellyfin_team.gpg.key \
    && rm /tmp/jellyfin_team.gpg.key \
    && echo "deb [signed-by=/etc/apt/keyrings/jellyfin.gpg] $JELLYFIN_REPO/debian bookworm main" \
        > /etc/apt/sources.list.d/jellyfin.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        libstdc++6 jellyfin-ffmpeg7 fontconfig fonts-dejavu-core libicu72 nginx \
        tesseract-ocr \
        tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
        tesseract-ocr-jpn tesseract-ocr-kor \
        tesseract-ocr-fra tesseract-ocr-deu tesseract-ocr-spa \
        tesseract-ocr-ita tesseract-ocr-por tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

# jellyfin-ffmpeg 装在自己的目录里，不占用 /usr/bin/ffmpeg。放进 PATH 即可让
# 所有调用点（media_probe、字幕抽取、转码会话、硬件自检）按名字找到它，
# 不必逐处改成绝对路径。必须早于下面的构建期冒烟测试。
ENV PATH="/usr/lib/jellyfin-ffmpeg:${PATH}"

# PGS OCR：每个多架构镜像只带与自身匹配的一份 seconv，不在运行时下载。
COPY --from=seconv-dist /out /opt/movieclaw/seconv
COPY docker/subtitle-smoke-test.sh /usr/local/bin/movieclaw-subtitle-smoke-test
# 构建期就在目标架构执行真实 PGS → SRT：架构、延迟加载的动态库、字体、
# ffmpeg 或任一承诺的语言包不完整时，直接阻断镜像产出。
RUN chmod +x /usr/local/bin/movieclaw-subtitle-smoke-test \
    && /usr/local/bin/movieclaw-subtitle-smoke-test

# Node 运行时：只拷贝 node 二进制（跑 Next standalone server 足够），不装 npm。
# 注意必须取自目标架构的 node 镜像（web-builder 是构建机架构，二进制不通用）。
COPY --from=node-dist /usr/local/bin/node /usr/local/bin/node

WORKDIR /app

# 后端：venv + 源码布局（src / alembic / alembic.ini 的相对位置必须保持）
COPY --from=py-deps /venv /venv
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./

# 基线 spec（阶段 2.5 现场导出）：Agent 组装 mclaw 工具时要读它渲染服务目录，
# 缺了就是每次对话都 500，属运行期硬依赖。
COPY --from=spec-export /build/spec.json ./src/movieclaw_api/data/spec.json

# mclaw CLI：Agent 的 mclaw 工具执行它（tools/mclaw.py 默认找这个路径）。
# 用户也可以 docker cp 出来当本机 CLI 用，或直接从 Release 下载同一份。
COPY --from=go-builder /out/mclaw /usr/local/bin/mclaw

# 前端：standalone 产物 + 静态资源 + public（standalone 不自动包含后两者）
COPY --from=web-builder /build/apps/web/.next/standalone ./web
COPY --from=web-builder /build/apps/web/.next/static ./web/apps/web/.next/static
COPY --from=web-builder /build/apps/web/public ./web/apps/web/public
# 图片优化已关闭（images.unoptimized），sharp 永不加载；但 Next 只要能解析到就会
# 把它塞进 standalone——它是构建机架构的原生二进制，在目标架构上是错的，删掉。
RUN rm -rf ./web/node_modules/.pnpm/@img* ./web/node_modules/.pnpm/sharp@* \
    ./web/node_modules/@img ./web/node_modules/sharp

# NER 模型：镜像内只读目录，不占用户的 data 卷；MOVIECLAW_NER_DIR 指过来
COPY --from=ner-model /model ./models/torrent-ner

COPY docker/entrypoint.sh /entrypoint.sh
# 对外端口的解析脚本：entrypoint 与下面的 HEALTHCHECK 共用同一份逻辑，
# 保证「nginx 监听的口」和「健康检查探的口」永远一致（见脚本内注释）。
COPY docker/resolve-web-port.sh /resolve-web-port.sh
RUN chmod +x /entrypoint.sh /resolve-web-port.sh
# nginx 前门的配置模板（entrypoint 启动时渲染端口后拉起）与启动期占位页
COPY docker/nginx.conf.template /etc/movieclaw/nginx.conf.template
COPY docker/starting.html /usr/share/movieclaw/starting.html
# 构建期就校验一次渲染后的配置：模板写错不该等到用户容器起不来才发现
RUN mkdir -p /run/movieclaw \
    && sed -e "s|__WEB_PORT__|3000|g" -e "s|__ASSETS_DIR__|/usr/share/movieclaw|g" \
        /etc/movieclaw/nginx.conf.template > /run/movieclaw/nginx.conf \
    && nginx -t -c /run/movieclaw/nginx.conf

# 运行时版本（依赖集合的代号，docker/runtime-version 是唯一事实源）：
# entrypoint 据此判断 data 卷上的应用内更新 overlay 与本镜像是否兼容，
# Release 产物的 manifest.requires_runtime 也取自同一个文件（构建脚本读取）。
# 凡是改动 pyproject dependencies、Node 大版本、系统包或 entrypoint 契约，
# 必须 bump docker/runtime-version 并发布新镜像。
COPY docker/runtime-version /etc/movieclaw-runtime

# TMDB API Key 在构建时烧入镜像（部署者可用同名环境变量覆盖）
ARG TMDB_API_KEY=""
ENV TMDB_API_KEY=${TMDB_API_KEY}

# 更新清单签名公钥（可选，base64 的 Ed25519 公钥）：烧入后应用内更新强制
# 验签（manifest.json.sig），防 Release/加速镜像被篡改。留空则不校验。
# 密钥对由 scripts/gen-release-signing-key.sh 生成。
ARG UPDATE_MANIFEST_PUBKEY=""
ENV UPDATE_MANIFEST_PUBKEY=${UPDATE_MANIFEST_PUBKEY}

ENV PATH="/venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    NODE_ENV=production \
    # 生产镜像关闭 uvicorn 热重载（本地开发默认值是开）
    APP_RELOAD=false \
    # 发布镜像默认真实投递订阅（代码默认 dry-run 是开发期的保护）
    SUBSCRIPTION_DISPATCH_DRY_RUN=false \
    MOVIECLAW_NER_DIR=/app/models/torrent-ner \
    MOVIECLAW_SECONV_PATH=/opt/movieclaw/seconv/seconv

# 运行期数据（SQLite、日志、缓存、上传、密钥）全部落在这个目录
VOLUME /app/data

# 默认对外端口。改了端口（MOVIECLAW_WEB_PORT 或应用内设置）时这行不会跟着变，
# 它只是镜像元数据；bridge 部署真正决定映射的是 compose 的 ports。
EXPOSE 3000

# 走对外端口打后端健康接口：验证 nginx 前门与 FastAPI（Next 进程由 entrypoint
# 的看门狗单独探测，它挂了容器会主动退出交给 restart 策略）。
# 端口不能写死——用户改过端口后仍要探到实际监听的那个口，否则容器会被判成
# unhealthy。这里与 entrypoint 共用 /resolve-web-port.sh 的解析结果。
HEALTHCHECK --interval=30s --timeout=5s --start-period=6m --retries=3 \
    CMD PORT="$(/resolve-web-port.sh | cut -d' ' -f1)" node -e "fetch('http://127.0.0.1:' + process.env.PORT + '/api/v1/health').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"

ENTRYPOINT ["/entrypoint.sh"]
