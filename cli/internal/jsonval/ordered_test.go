package jsonval

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

const sample = `{"name":"沙丘","kind":"movie","stats":{"b":2,"a":1},"tags":[],"id":9007199254740993}`

// TestDecodePreservesOrder 是这个类型存在的理由：服务端排好的字段顺序要原样带到输出。
func TestDecodePreservesOrder(t *testing.T) {
	value, err := Decode([]byte(sample))
	if err != nil {
		t.Fatalf("解析失败：%v", err)
	}
	obj := Object(value)
	want := []string{"name", "kind", "stats", "tags", "id"}
	if got := obj.Keys(); strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("字段顺序错了：%v，期望 %v", got, want)
	}
	if keys := Object(obj.Get("stats")).Keys(); keys[0] != "b" {
		t.Errorf("嵌套对象也要保序，实际 %v", keys)
	}
}

// TestRoundTripKeepsBigIntAndEmptyArray 校验两处容易悄悄坏掉的地方：
// 超出 float64 精度的整数 ID，以及 [] 不能变成 null。
func TestRoundTripKeepsBigIntAndEmptyArray(t *testing.T) {
	value, err := Decode([]byte(sample))
	if err != nil {
		t.Fatalf("解析失败：%v", err)
	}
	encoded, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("序列化失败：%v", err)
	}
	if string(encoded) != sample {
		t.Errorf("往返不一致：\n实际 %s\n期望 %s", encoded, sample)
	}
}

// TestMarshalDoesNotEscapeHTML 校验种子标题里的 & < > 不被转义成 \u0026。
//
// 转义与否由最外层的 Encoder 决定：encoding/json 会把 MarshalJSON 的返回值再
// 过一遍 compact，外层开着 HTML 转义就会重新转回去。所以这里按输出层的用法
// 断言——Map 内部不转义，外层也关掉，两处都对了才成立。
func TestMarshalDoesNotEscapeHTML(t *testing.T) {
	value, err := Decode([]byte(`{"title":"A & B <c>"}`))
	if err != nil {
		t.Fatalf("解析失败：%v", err)
	}
	var buf bytes.Buffer
	encoder := json.NewEncoder(&buf)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		t.Fatalf("序列化失败：%v", err)
	}
	if !strings.Contains(buf.String(), "A & B <c>") {
		t.Errorf("HTML 字符被转义了：%s", buf.String())
	}
}

// TestAtWalksNestedPath 校验点分路径取值，以及缺失时不 panic。
func TestAtWalksNestedPath(t *testing.T) {
	value, _ := Decode([]byte(`{"job":{"progress":{"percent":42}}}`))
	if got := Int(At(value, "job.progress.percent")); got != 42 {
		t.Errorf("取值错误：%v", got)
	}
	if got := At(value, "job.missing.deep"); got != nil {
		t.Errorf("缺失路径应返回 nil，实际 %v", got)
	}
	if got := Object(nil).Get("任意"); got != nil {
		t.Errorf("nil 对象取值应返回 nil，实际 %v", got)
	}
}

// TestNewMapKeepsGivenOrder 校验 CLI 自己组装的输出（如搜索行视图）按给定顺序走。
func TestNewMapKeepsGivenOrder(t *testing.T) {
	m := NewMap("row", 1, "title", "沙丘", "seeders", 9)
	encoded, _ := json.Marshal(m)
	if want := `{"row":1,"title":"沙丘","seeders":9}`; string(encoded) != want {
		t.Errorf("顺序不对：%s", encoded)
	}
}

func TestDecodeRejectsTrailingContent(t *testing.T) {
	if _, err := Decode([]byte(`{"a":1} 多余`)); err == nil {
		t.Error("尾部有多余内容时应当报错")
	}
}
