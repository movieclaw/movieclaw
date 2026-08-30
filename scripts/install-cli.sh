#!/bin/sh
# movieclaw CLI（mclaw）安装脚本（docs/design/device-auth.md §6.4）
#
#     curl -fsSL https://raw.githubusercontent.com/yipengfei329/movieclaw/main/scripts/install-cli.sh | sh
#
# mclaw 是一个静态二进制：下载、解压、放进 PATH，就这三步。不需要 Python、
# 不需要 Node、不需要包管理器——CLI 要装在 NAS、软路由、同事的机器和 CI 里，
# 每多一个运行时前置就多一批装不上的人。
#
# 装到默认 PATH 里的位置是刻意的：macOS 从 Dock 启动的应用、cron、systemd
# 都不读 ~/.zshrc，~/.local/bin 不在它们的 PATH 里。「任何终端、任何应用触发
# 都能连」的前提是 mclaw 首先得被找得到。
set -eu

REPO="yipengfei329/movieclaw"
VERSION="${MOVIECLAW_CLI_VERSION:-latest}"
# 国内网络可用 MOVIECLAW_DOWNLOAD_BASE 换成 GitHub 加速镜像
BASE="${MOVIECLAW_DOWNLOAD_BASE:-https://github.com/$REPO/releases}"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || die "需要 curl，请先安装后重试"
command -v tar >/dev/null 2>&1 || die "需要 tar，请先安装后重试"

# --- 1) 认出平台 ----------------------------------------------------------
case "$(uname -s)" in
    Linux)  OS=linux ;;
    Darwin) OS=darwin ;;
    *) die "不支持的系统：$(uname -s)。Windows 请用 scripts/install-cli.ps1" ;;
esac
case "$(uname -m)" in
    x86_64|amd64)  ARCH=amd64 ;;
    aarch64|arm64) ARCH=arm64 ;;
    *) die "不支持的架构：$(uname -m)（仅 amd64/arm64）" ;;
esac

ASSET="mclaw_${OS}_${ARCH}.tar.gz"
if [ "$VERSION" = latest ]; then
    URL="$BASE/latest/download/$ASSET"
else
    URL="$BASE/download/$VERSION/$ASSET"
fi

# --- 2) 下载并校验 --------------------------------------------------------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
say "正在下载 mclaw（$OS/$ARCH）…"
curl -fsSL --retry 3 -o "$TMP/$ASSET" "$URL" \
    || die "下载失败：$URL（网络不通，或该版本没有这个平台的产物）"

# 校验和随 Release 一起发布；取不到就跳过——网络受限的用户不该因为多取一个
# 文件失败而装不上，但取得到就必须对得上。
if curl -fsSL --retry 2 -o "$TMP/checksums.txt" "${URL%/*}/checksums.txt" 2>/dev/null; then
    if command -v sha256sum >/dev/null 2>&1; then
        (cd "$TMP" && grep " $ASSET\$" checksums.txt | sha256sum -c - >/dev/null) \
            || die "校验和不匹配，下载的文件可能被篡改或损坏"
    elif command -v shasum >/dev/null 2>&1; then
        (cd "$TMP" && grep " $ASSET\$" checksums.txt | shasum -a 256 -c - >/dev/null) \
            || die "校验和不匹配，下载的文件可能被篡改或损坏"
    fi
    say "✓ 校验和通过"
fi

tar -xzf "$TMP/$ASSET" -C "$TMP" || die "解压失败"
[ -f "$TMP/mclaw" ] || die "压缩包里没有 mclaw 可执行文件"
chmod +x "$TMP/mclaw"

# --- 3) 放进默认 PATH，让 GUI 应用与后台任务也能调用 ----------------------
INSTALL_DIR=""
for candidate in /usr/local/bin /opt/homebrew/bin; do
    if [ -d "$candidate" ] && [ -w "$candidate" ]; then
        INSTALL_DIR="$candidate"
        break
    fi
done

if [ -n "$INSTALL_DIR" ]; then
    mv "$TMP/mclaw" "$INSTALL_DIR/mclaw"
    say "✓ 已安装到 $INSTALL_DIR/mclaw"
elif command -v sudo >/dev/null 2>&1 && [ -d /usr/local/bin ]; then
    say "需要管理员权限把 mclaw 装进 /usr/local/bin（让 GUI 应用与后台任务也能调用）"
    sudo mv "$TMP/mclaw" /usr/local/bin/mclaw
    say "✓ 已安装到 /usr/local/bin/mclaw"
else
    mkdir -p "$HOME/.local/bin"
    mv "$TMP/mclaw" "$HOME/.local/bin/mclaw"
    say ""
    say "已安装到 $HOME/.local/bin/mclaw，但那不在系统默认 PATH 里。"
    say "终端里可以直接用；从 Dock 启动的应用、cron 与 systemd 服务可能找不到它。"
    say "手工放进默认 PATH："
    say "  sudo mv $HOME/.local/bin/mclaw /usr/local/bin/mclaw"
fi

say ""
say "下一步：把这台机器配对到你的 movieclaw"
say "  mclaw login"
say ""
say "它会先在局域网里找一遍；找不到（跨网段、VPN，或服务端关了 Jellyfin"
say "兼容层）就自己给地址：mclaw login --server http://<你的 movieclaw 地址>:3000"
say ""
say "命令会显示一段配对码，到网页「设置 → 设备」核对后批准即可。"
