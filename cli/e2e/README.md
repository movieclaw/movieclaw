# mclaw 端到端验收

两套脚本，都要求先把二进制编出来：

```bash
go build -o /tmp/mclaw ./cmd/mclaw
```

## 1. 对着真服务器：`live.sh`

覆盖只读查询、三种输出格式、写操作与请求体（逐字段 / `--input` 文件 /
`--input -` 读 stdin）、七种退出码、帮助文案、长任务与作业等待。

```bash
# 先起一个后端并配好凭证（mclaw login，或注入 MOVIECLAW_TOKEN）
MCLAW=/tmp/mclaw MOVIECLAW_SERVER=http://127.0.0.1:8799 ./e2e/live.sh
```

跑之前需要一个可用凭证：要么 `MOVIECLAW_CONFIG_DIR` 指向已 `mclaw login`
过的目录，要么直接 `MOVIECLAW_TOKEN=<令牌>`。脚本会创建几个媒体库（名字带
随机后缀，可重复跑），不会删任何东西。

## 2. 对着协议桩：`sse.sh`

搜索流、会话流和 Job 等待在真环境要接 PT 站点和大模型，CI 里没有；协议
本身可以完整走一遍——分帧、断流续传、终态退出码、行号快照 → 下载、歧义
消解（退出码 7）。`stub.py` 就是这个桩。

```bash
python3 e2e/stub.py 8798 &
MCLAW=/tmp/mclaw ./e2e/sse.sh
```

桩刻意做了三个「坏」场景：站点失败、`done` 之前断流、会话流在终态前掉线，
分别对应 CLI 的「如实报告」「结果不完整不落快照」「Last-Event-ID 续传」。
