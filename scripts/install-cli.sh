#!/bin/sh
# movieclaw CLI（mclaw）安装脚本（docs/design/device-auth.md §6.4）
#
#     curl -fsSL https://raw.githubusercontent.com/yipengfei329/movieclaw/main/scripts/install-cli.sh | sh
#
# 设计取舍：用 uv 装一个独立 Python 运行时，而不是分发 PyInstaller 二进制。
# 对用户完全等价（什么都不用预装），但省掉了五个平台的构建矩阵、onefile 的
# 冷启动开销，以及 macOS Gatekeeper / Windows SmartScreen 的代码签名成本。
#
# 装到默认 PATH 里的位置是刻意的：macOS 从 Dock 启动的应用、cron、systemd
# 都不读 ~/.zshrc，~/.local/bin 不在它们的 PATH 里。「任何终端、任何应用触发
# 都能连」的前提是 mclaw 首先得被找得到。
set -eu

PACKAGE="movieclaw-cli"
PYTHON_VERSION="3.12"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- 1) 准备 uv（单个静态二进制，无依赖） ---------------------------------
if command -v uv >/dev/null 2>&1; then
    say "✓ 已安装 uv"
else
    say "正在安装 uv（单文件二进制，用于管理独立的 Python 运行时）…"
    command -v curl >/dev/null 2>&1 || die "需要 curl，请先安装后重试"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 \
        || die "uv 安装失败，请检查网络后重试"
    # 安装脚本把 uv 放进 ~/.local/bin，但本次 shell 的 PATH 还没更新
    PATH="$HOME/.local/bin:$PATH"
    export PATH
    command -v uv >/dev/null 2>&1 || die "uv 安装后仍找不到，请重开终端后重试"
    say "✓ uv 安装完成"
fi

# --- 2) 安装 mclaw（uv 自带独立 Python，不碰系统 Python） -----------------
say "正在安装 $PACKAGE …"
uv tool install --force --python "$PYTHON_VERSION" "$PACKAGE" >/dev/null \
    || die "$PACKAGE 安装失败"

MCLAW="$(uv tool dir)/movieclaw-cli/bin/mclaw"
[ -x "$MCLAW" ] || MCLAW="$HOME/.local/bin/mclaw"
[ -x "$MCLAW" ] || die "安装完成但找不到 mclaw 可执行文件"
say "✓ $PACKAGE 安装完成"

# --- 3) 放进默认 PATH，让 GUI 应用与后台任务也能调用 ----------------------
# 不改 shell 配置文件：改了只对交互式终端生效，而这里恰恰要解决的就是
# 「非交互式环境找不到命令」。symlink 到默认 PATH 是唯一可靠的做法。
LINK_DIR=""
for candidate in /usr/local/bin /opt/homebrew/bin; do
    if [ -d "$candidate" ] && [ -w "$candidate" ]; then
        LINK_DIR="$candidate"
        break
    fi
done

if [ -n "$LINK_DIR" ]; then
    ln -sf "$MCLAW" "$LINK_DIR/mclaw"
    say "✓ 已链接到 $LINK_DIR/mclaw"
elif [ -d /usr/local/bin ] && command -v sudo >/dev/null 2>&1; then
    say "需要管理员权限把 mclaw 链接进 /usr/local/bin（让 GUI 应用与后台任务也能调用）"
    sudo ln -sf "$MCLAW" /usr/local/bin/mclaw && say "✓ 已链接到 /usr/local/bin/mclaw"
else
    say ""
    say "注意：没能把 mclaw 放进系统默认 PATH。终端里可以用："
    say "  $MCLAW"
    say "但从 Dock 启动的应用、cron 与 systemd 服务可能找不到它。手工链接："
    say "  sudo ln -sf $MCLAW /usr/local/bin/mclaw"
fi

say ""
say "下一步：把这台机器配对到你的 movieclaw"
say "  mclaw login --server http://<你的 movieclaw 地址>:3000"
say ""
say "命令会显示一段配对码，到网页「设置 → 设备」核对后批准即可。"
