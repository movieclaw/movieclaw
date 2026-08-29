package sse

import (
	"strings"
	"testing"
)

func collect(t *testing.T, raw string) []Event {
	t.Helper()
	scanner := NewScanner(strings.NewReader(raw))
	var events []Event
	for {
		event, ok := scanner.Next()
		if !ok {
			break
		}
		events = append(events, event)
	}
	if err := scanner.Err(); err != nil {
		t.Fatalf("读流出错：%v", err)
	}
	return events
}

func TestParsesFrames(t *testing.T) {
	events := collect(t, "event: start\ndata: {\"a\":1}\nid: 7\n\n: 心跳\n\nevent: done\ndata: {}\n\n")
	if len(events) != 2 {
		t.Fatalf("期望 2 帧，实际 %d：%+v", len(events), events)
	}
	if events[0].Event != "start" || events[0].Data != `{"a":1}` || events[0].ID != 7 || !events[0].HasID {
		t.Errorf("首帧解析错误：%+v", events[0])
	}
	if events[1].Event != "done" || events[1].HasID {
		t.Errorf("末帧解析错误：%+v", events[1])
	}
}

// TestMultilineData 校验多行 data 按规范以换行拼接。
func TestMultilineData(t *testing.T) {
	events := collect(t, "data: 第一行\ndata: 第二行\n\n")
	if len(events) != 1 || events[0].Data != "第一行\n第二行" {
		t.Fatalf("多行 data 拼接错误：%+v", events)
	}
	if events[0].Event != "message" {
		t.Errorf("缺省事件名应为 message，实际 %q", events[0].Event)
	}
}

// TestUnterminatedFrame 校验流在末尾少一个空行时不丢最后一帧——
// 服务端被 kill 或反向代理截断时就是这个形态。
func TestUnterminatedFrame(t *testing.T) {
	events := collect(t, "event: site_result\ndata: {\"count\":3}\n")
	if len(events) != 1 || events[0].Event != "site_result" {
		t.Fatalf("末帧丢失：%+v", events)
	}
}
