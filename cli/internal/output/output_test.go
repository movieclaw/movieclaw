package output

import (
	"bytes"
	"strings"
	"testing"

	"github.com/yipengfei329/movieclaw/cli/internal/jsonval"
)

func capture(t *testing.T, data any, format string) string {
	t.Helper()
	var out bytes.Buffer
	old := Stdout
	Stdout = &out
	defer func() { Stdout = old }()
	if err := Emit(data, format, false); err != nil {
		t.Fatalf("输出失败：%v", err)
	}
	return out.String()
}

func decode(t *testing.T, raw string) any {
	t.Helper()
	value, err := jsonval.Decode([]byte(raw))
	if err != nil {
		t.Fatalf("解析失败：%v", err)
	}
	return value
}

// TestJSONKeepsServerOrder 是输出层的核心契约：JSON 按服务端字段顺序输出。
func TestJSONKeepsServerOrder(t *testing.T) {
	got := capture(t, decode(t, `{"name":"库","id":1,"auto_clear":false}`), "json")
	want := "{\n  \"name\": \"库\",\n  \"id\": 1,\n  \"auto_clear\": false\n}\n"
	if got != want {
		t.Errorf("JSON 输出不对：\n%q\n期望\n%q", got, want)
	}
}

// TestTableColumnsFollowFieldOrder 校验表格列序跟随字段顺序，且列宽按显示宽度对齐
// （CJK 占两列）。
func TestTableColumnsFollowFieldOrder(t *testing.T) {
	got := capture(t, decode(t, `[{"id":1,"name":"电影库"},{"id":2,"name":"剧"}]`), "table")
	lines := strings.Split(strings.TrimRight(got, "\n"), "\n")
	if lines[0] != "id  name  " {
		t.Errorf("表头不对：%q", lines[0])
	}
	if lines[1] != "1   电影库" {
		t.Errorf("首行不对：%q", lines[1])
	}
	// 「剧」显示宽度 2，要补到 6 列宽
	if lines[2] != "2   剧    " {
		t.Errorf("次行补白不对：%q", lines[2])
	}
}

// TestTableRendersNestedValueInline 校验嵌套对象在单元格里单行渲染，超长截断。
func TestTableRendersNestedValueInline(t *testing.T) {
	got := capture(t, decode(t, `{"stats":{"a":1,"b":[1,2]}}`), "table")
	if !strings.Contains(got, `{"a": 1, "b": [1, 2]}`) {
		t.Errorf("嵌套值渲染不对：%q", got)
	}
}

// TestYAMLKeepsOrderAndNumberTypes 校验 YAML 也按字段顺序走，且数字不带引号
// （json.Number 底层是字符串，直接交给 yaml 会输出成 '1'）。
func TestYAMLKeepsOrderAndNumberTypes(t *testing.T) {
	got := capture(t, decode(t, `{"name":"库","count":3,"ratio":0.5}`), "yaml")
	want := "name: 库\ncount: 3\nratio: 0.5\n"
	if got != want {
		t.Errorf("YAML 输出不对：\n%q\n期望\n%q", got, want)
	}
}

func TestQuietSuppressesOutput(t *testing.T) {
	var out bytes.Buffer
	old := Stdout
	Stdout = &out
	defer func() { Stdout = old }()
	if err := Emit(decode(t, `{"a":1}`), "json", true); err != nil {
		t.Fatalf("输出失败：%v", err)
	}
	if out.Len() != 0 {
		t.Errorf("--quiet 下不应有输出：%q", out.String())
	}
}

// TestJSONDoesNotEscapeHTML 校验 & < > 原样输出——种子标题里常有，
// 转成 & 既不好读，也让 grep 失效。
func TestJSONDoesNotEscapeHTML(t *testing.T) {
	got := capture(t, decode(t, `{"title":"A & B <c>"}`), "json")
	if !strings.Contains(got, "A & B <c>") {
		t.Errorf("HTML 字符被转义了：%s", got)
	}
}
