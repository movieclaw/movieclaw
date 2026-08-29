// Package sse 是 SSE（Server-Sent Events）分帧器（docs/design/cli.md §8.3）。
//
// 一次实现、两处复用：种子搜索流（/search/torrents/stream）与 Agent 运行流
// （/sessions/{id}/events）。与前端刻意不用 EventSource 的理由相同：搜索流
// 断线不应自动重放整次搜索，Agent 流要用 Last-Event-ID 精确续传。
//
// 帧协议：`event:` / `data:` / `id:` 行组成一帧，空行分帧；`:` 开头是心跳
// 注释，跳过不产出事件。
package sse

import (
	"bufio"
	"io"
	"strconv"
	"strings"
)

// Event 是一帧 SSE 事件。Data 为原始字符串（多行 data 按规范以换行拼接）。
type Event struct {
	Event string
	Data  string
	// ID 是事件序号；缺省或不是整数时 HasID 为 false。
	ID    int
	HasID bool
}

// Scanner 从字节流里逐帧读事件。
type Scanner struct {
	reader *bufio.Scanner
	err    error
}

// NewScanner 包装一个响应体。
//
// 缓冲区上调到 1 MiB：一帧 Agent 事件可能带整段工具调用结果，
// bufio 默认的 64 KiB 会在真实会话里被撑爆。
func NewScanner(body io.Reader) *Scanner {
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	return &Scanner{reader: scanner}
}

// Next 读下一帧。流正常结束或出错时返回 false，错误由 Err 取回。
func (s *Scanner) Next() (Event, bool) {
	var event Event
	var data []string
	pending := false
	for s.reader.Scan() {
		line := strings.TrimRight(s.reader.Text(), "\r")
		if line == "" {
			if !pending {
				continue // 帧之间的空行
			}
			event.Data = strings.Join(data, "\n")
			if event.Event == "" {
				event.Event = "message"
			}
			return event, true
		}
		if strings.HasPrefix(line, ":") {
			continue // 心跳注释帧
		}
		field, value, _ := strings.Cut(line, ":")
		value = strings.TrimPrefix(value, " ")
		switch field {
		case "event":
			event.Event, pending = value, true
		case "data":
			data, pending = append(data, value), true
		case "id":
			if parsed, err := strconv.Atoi(value); err == nil {
				event.ID, event.HasID, pending = parsed, true, true
			}
		}
	}
	s.err = s.reader.Err()
	if pending {
		// 流在末尾没有空行就闭合：已读到的字段仍是一帧完整事件
		event.Data = strings.Join(data, "\n")
		if event.Event == "" {
			event.Event = "message"
		}
		return event, true
	}
	return Event{}, false
}

// Err 返回读取过程中的错误；正常读到流末尾时为 nil。
func (s *Scanner) Err() error { return s.err }
