package overlay

import (
	"net/url"

	"github.com/yipengfei329/movieclaw/cli/internal/api"
	"github.com/yipengfei329/movieclaw/cli/internal/clierr"
	"github.com/yipengfei329/movieclaw/cli/internal/jsonval"
	"github.com/yipengfei329/movieclaw/cli/internal/sse"
)

// streamEvents 订阅一个 SSE 端点，逐帧调用 handle。
//
// handle 返回 false 表示「拿到终态了，不用再读」，此时连接立刻关闭——
// Agent 事件流在终态之后还会挂着，不主动断开命令就不会退出。
//
// 流在没有终态的情况下自然闭合时返回 NETWORK 错误：调用方据此决定重连
// （Agent 流带 Last-Event-ID 续传）还是如实报告结果不完整（搜索流）。
func streamEvents(
	client *api.Client,
	path string,
	params url.Values,
	lastEventID string,
	handle func(sse.Event, *jsonval.Map) bool,
) error {
	body, err := client.Stream(path, params, lastEventID)
	if err != nil {
		return err
	}
	defer body.Close()

	scanner := sse.NewScanner(body)
	for {
		event, ok := scanner.Next()
		if !ok {
			break
		}
		var payload *jsonval.Map
		if event.Data != "" {
			// 单帧解析失败不该中断整条流：跳过这一帧继续读，
			// 后面的帧（尤其是终态帧）仍然有效。
			parsed, err := jsonval.Decode([]byte(event.Data))
			if err != nil {
				continue
			}
			payload = jsonval.Object(parsed)
		}
		if !handle(event, payload) {
			return nil
		}
	}
	if err := scanner.Err(); err != nil {
		return clierr.Networkf("事件流读取中断：%v", err).
			WithHint("网络可能不稳或服务已重启，可重试")
	}
	return clierr.Newf(clierr.Network, "事件流在终态前闭合")
}
