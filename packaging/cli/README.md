# movieclaw-cli

[movieclaw](https://github.com/yipengfei329/movieclaw) 的命令行客户端，命令名 `mclaw`。

面向 Agent 的薄客户端：命令树由服务端的 OpenAPI spec 动态生成，业务逻辑全部在
服务端。人类可用是副产品。

## 安装

```sh
curl -fsSL https://raw.githubusercontent.com/yipengfei329/movieclaw/main/scripts/install-cli.sh | sh
```

脚本会装一个独立的 Python 运行时，**不需要你的机器上预装 Python**。

已经有 Python 工具链时也可以：

```sh
uv tool install movieclaw-cli     # 或 pipx install movieclaw-cli
```

## 配对

```sh
mclaw login --server http://10.1.1.5:3000
```

命令会显示一段配对码，到 movieclaw 网页的「设置 → 设备」核对后批准即可。
令牌直接回到本机并存进 `~/.config/movieclaw/credentials`，全程不需要你看到或
抄写任何密钥，也不接受密码。

之后任何终端、任何 Agent 直接调用 `mclaw`，无需再登录：

```sh
mclaw --help
mclaw subscriptions list -o json
mclaw search torrents "沙丘2" --resolution 2160p
```

无人值守场景（CI、容器）走环境变量注入令牌，完全不落盘：

```sh
export MOVIECLAW_SERVER=http://10.1.1.5:3000
export MOVIECLAW_TOKEN=mclaw_...
```
