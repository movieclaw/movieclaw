# Docker 字幕运行时打包与多架构契约

本文是 MovieClaw 官方 Docker 镜像的字幕运行时事实源。目标不是“依赖能够
安装”，而是发布到 Docker Hub 的每个架构都能真实完成 PGS → SRT。

## 1. 支持矩阵

| Docker 平台 | seconv 官方资产 | 构建方式 | 发布状态 |
|---|---|---|---|
| `linux/amd64` | `SeConv-Linux-x64.tar.gz` | GitHub x64 原生 runner | 必须发布 |
| `linux/arm64` | `SeConv-Linux-ARM64.tar.gz` | GitHub ARM64 原生 runner | 必须发布 |

`docker compose` 不固定 `platform`，Docker 会从同一 manifest 自动选择与 NAS
匹配的镜像。32 位 ARM/x86、RISC-V 等架构没有 Subtitle Edit 5.1 官方 seconv
资产，也不在官方镜像支持范围；不能为了“能构建”而放入未经验证的自行编译产物。

这里的多平台指 Linux 容器的 amd64/arm64。源码直跑仍可使用应用层支持的
Windows/macOS seconv，但它们不属于 Docker 镜像。

## 2. 固定的上游产物

PGS 转换器固定为 Subtitle Edit `v5.1.0` 官方 Release：

| 架构 | SHA256 |
|---|---|
| amd64 / x64 | `3354dfeb4452b0dd8a11d8f3066c31a3116e2cbc137f271ac6b7e4f91896ba11` |
| arm64 | `ab58891abd54a1604fdf5956c340159bd2efbcaef8cf2c69d97fcc55b83b4718` |

下载阶段按 `$TARGETARCH` 选资产并在解包前校验 SHA256，`LICENSE` 与二进制一起
进入最终镜像。资产地址和摘要来自
[Subtitle Edit v5.1.0 Release](https://github.com/SubtitleEdit/subtitleedit/releases/tag/v5.1.0)。
完整性以 Release tag、资产名和摘要为准，不能只比较 `seconv --version` 的文本；
当前官方 Linux ARM64 资产的该命令仍显示 `5.0.0`。

## 3. 最终镜像中的字幕依赖

| 依赖 | 用途 | 缺失时的表现 |
|---|---|---|
| `ffmpeg` / `ffprobe` | 探测媒体、从容器抽取 PGS 为 `.sup` | 任务预检失败或无法抽轨 |
| seconv Linux 自包含产物 | 解析 PGS、编排 OCR、写 SRT | PGS 无法转换 |
| `libicu76` | seconv/.NET 的 Unicode 与区域数据（基础镜像 Debian 13/trixie 的 ICU 包名） | seconv 启动 FailFast |
| `fontconfig`、`fonts-dejavu-core` | SkiaSharp 字体解析与端到端合成探针 | 图形字幕路径延迟失败 |
| `libSkiaSharp.so`、`libHarfBuzzSharp.so` | seconv 随包的图像与字形库 | PGS 解析/渲染时动态加载失败 |
| Tesseract 及 traineddata | 实际 OCR | seconv 可启动但转换时报错 |

官方镜像承诺的 OCR 语言与应用 `pgs.OCR_LANGUAGE_LABELS` 一致：英语、简体中文、
繁体中文、日语、韩语、法语、德语、西班牙语、意大利语、葡萄牙语、俄语；对应
traineddata 为 `eng chi_sim chi_tra jpn kor fra deu spa ita por rus`。

不把 PaddleOCR 放进基础镜像：它会引入体积更大、跨架构 wheel 支持更敏感的
Python/推理栈，而当前可用性仍以 Tesseract 为稳定基线。源码部署者可以自行安装
PaddleOCR，应用的 `auto` 模式会在健康时优先使用它。

## 4. 构建阶段与架构边界

- Web 与 NER/SeConv 下载阶段固定在 `$BUILDPLATFORM`：这些产物架构无关，或只需
  根据 `$TARGETARCH` 下载，无需执行目标二进制。
- Python venv、Node 运行时与最终镜像按目标平台构建：其中含架构相关 wheel/
  二进制，不能从构建机架构直接复制。
- 最终镜像层会执行目标架构 seconv 与 Tesseract。原生 CI runner 直接执行；本地
  交叉构建必须先启用 binfmt/QEMU，`scripts/build-image.sh` 会提前检查并给出错误。

运行时依赖集合编号为 `docker/runtime-version`。本次字幕依赖落在 runtime 5；以后
更新 seconv、Tesseract、语言包、系统库或基础镜像时都必须继续 bump，保证应用内
更新不会把新代码安装到不具备依赖的旧镜像。

## 5. 三层发布门禁

1. **Dockerfile 构建层**：`movieclaw-subtitle-smoke-test` 检查架构、ffmpeg/
   ffprobe、全部 `.so` 的 `ldd`、11 个语言包；再用合成 SRT 生成 PGS、封装进
   MKV、通过 FFmpeg 抽出 SUP，并由 Tesseract OCR 回内容一致的 SRT。测试素材
   运行时生成，不携带媒体样本或版权内容。
2. **单架构 digest 层**：CI 推送 amd64/arm64 digest 后分别拉回，复验镜像架构、
   端到端探针，以及 MovieClaw Python 预检对 11 种语言均返回可用。
3. **manifest 层**：只有两个架构作业都成功才合并标签；合并后精确断言 manifest
   同时含 `linux/amd64` 和 `linux/arm64`。

任一层失败都不能发布 `latest` 或 `runtime-N`。

## 6. 本地验证

```bash
# 本机架构；构建完成后脚本会自动运行字幕探针
TMDB_API_KEY=... ./scripts/build-image.sh

# Apple Silicon 给常见 x86_64 NAS 交叉构建；需要 Docker 支持 linux/amd64
PLATFORM=linux/amd64 TMDB_API_KEY=... ./scripts/build-image.sh

# 对已有镜像单独复验
docker run --rm \
  --entrypoint /usr/local/bin/movieclaw-subtitle-smoke-test \
  movieclaw:latest
```

## 7. 更新依赖时的检查清单

- 更新 seconv：同时改版本 URL、两个资产名/摘要，并在两个原生架构跑探针。
- 增加 OCR 语言：同步更新应用语言映射、Debian traineddata 包、探针期望列表和测试；
  不能只给 UI 加选项。
- 改 Debian/Python 基础镜像：复核 ICU 包名以及 seconv 两个 `.so` 的 `ldd`。
- 改系统包或上述运行时契约：bump `docker/runtime-version`，随应用 Release 发布
  新的多架构镜像，并在 changelog 明确提示 Docker 用户重新拉取镜像。
