// Package output 是 CLI 的输出层（docs/design/cli.md §5.2 / §8.3）。
//
// 契约：
//   - stdout 只放数据，stderr 放提示与错误；
//   - 非 TTY（Agent/管道）默认输出 JSON——服务端 data 字段原样，字段名即 API
//     schema，是脚本与 Agent 的稳定契约；TTY 下默认表格（人类副产品）；
//   - --quiet 抑制成功输出（配合退出码使用）。
//
// 表格渲染刻意保持极简（对齐分栏，不引第三方表格库）：表格是给人扫一眼的，
// 机器一律走 -o json。
package output

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/yipengfei329/movieclaw/cli/internal/jsonval"
	"golang.org/x/term"
	"gopkg.in/yaml.v3"
)

// Stdout / Stderr 是可替换的输出目标，测试据此捕获。
var (
	Stdout io.Writer = os.Stdout
	Stderr io.Writer = os.Stderr
	// isTTY 供测试覆盖；生产环境探测真实终端。
	isTTY = func() bool { return term.IsTerminal(int(os.Stdout.Fd())) }
)

// Format 解析最终输出格式：显式指定优先，否则 TTY 给表格、管道给 JSON。
func Format(explicit string) string {
	if explicit != "" {
		return explicit
	}
	if isTTY() {
		return "table"
	}
	return "json"
}

// Emit 按格式输出数据。quiet 时静默（调用方靠退出码判断结果）。
func Emit(data any, format string, quiet bool) error {
	if quiet {
		return nil
	}
	switch Format(format) {
	case "json":
		return emitJSON(data)
	case "yaml":
		var buf bytes.Buffer
		encoder := yaml.NewEncoder(&buf)
		encoder.SetIndent(2)
		if err := encoder.Encode(yamlValue(data)); err != nil {
			return err
		}
		if err := encoder.Close(); err != nil {
			return err
		}
		_, err := fmt.Fprint(Stdout, buf.String())
		return err
	default:
		return printTable(data)
	}
}

// Info 把过程提示写到 stderr——stdout 是数据通道，不能被提示污染。
func Info(format string, args ...any) {
	fmt.Fprintf(Stderr, format+"\n", args...)
}

// display 把一个单元格值渲染成人看的字符串。
func display(value any) string {
	switch v := value.(type) {
	case nil:
		return "-"
	case bool:
		if v {
			return "是"
		}
		return "否"
	case string:
		return v
	case json.Number:
		// 数字保留服务端给的字面量：39641 不写成 39641.0，长 ID 不丢精度
		return v.String()
	case *jsonval.Map, []any:
		encoded, err := marshalCompact(v)
		if err != nil {
			return fmt.Sprint(v)
		}
		text := string(encoded)
		if len([]rune(text)) <= 60 {
			return text
		}
		return string([]rune(text)[:57]) + "..."
	default:
		return fmt.Sprint(v)
	}
}

// width 是终端显示宽度：CJK 字符占两列，对齐时必须按显示宽度算。
func width(text string) int {
	total := 0
	for _, ch := range text {
		if ch > 0x2E7F {
			total += 2
		} else {
			total++
		}
	}
	return total
}

func pad(text string, w int) string {
	return text + strings.Repeat(" ", max(0, w-width(text)))
}

func printTable(data any) error {
	switch v := data.(type) {
	case []any:
		if len(v) == 0 {
			fmt.Fprintln(Stderr, "（空）")
			return nil
		}
		rows := make([]*jsonval.Map, 0, len(v))
		for _, item := range v {
			row, ok := item.(*jsonval.Map)
			if !ok {
				// 非对象数组按 JSON 原样输出
				return emitJSON(data)
			}
			rows = append(rows, row)
		}
		return printRows(rows)
	case *jsonval.Map:
		w := 0
		for _, key := range v.Keys() {
			w = max(w, width(key))
		}
		for _, key := range v.Keys() {
			fmt.Fprintf(Stdout, "%s  %s\n", pad(key, w), display(v.Get(key)))
		}
		return nil
	default:
		return emitJSON(data)
	}
}

func printRows(rows []*jsonval.Map) error {
	// 列序取各行字段的出现顺序（服务端 schema 的顺序），不排序
	var columns []string
	seen := map[string]bool{}
	for _, row := range rows {
		for _, key := range row.Keys() {
			if !seen[key] {
				seen[key] = true
				columns = append(columns, key)
			}
		}
	}
	cells := make([][]string, len(rows))
	widths := make([]int, len(columns))
	for i, col := range columns {
		widths[i] = width(col)
	}
	for r, row := range rows {
		cells[r] = make([]string, len(columns))
		for c, col := range columns {
			cells[r][c] = display(row.Get(col))
			widths[c] = max(widths[c], width(cells[r][c]))
		}
	}
	header := make([]string, len(columns))
	for i, col := range columns {
		header[i] = pad(col, widths[i])
	}
	fmt.Fprintln(Stdout, strings.Join(header, "  "))
	for _, row := range cells {
		padded := make([]string, len(row))
		for i, cell := range row {
			padded[i] = pad(cell, widths[i])
		}
		fmt.Fprintln(Stdout, strings.Join(padded, "  "))
	}
	fmt.Fprintf(Stderr, "（共 %d 条）\n", len(rows))
	return nil
}

func emitJSON(data any) error {
	var buf bytes.Buffer
	encoder := json.NewEncoder(&buf)
	// 不转义 < > &：输出要喂给 jq 和人眼，把种子标题里的 & 变成 \u0026 只会碍事
	encoder.SetEscapeHTML(false)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(data); err != nil {
		return err
	}
	_, err := fmt.Fprint(Stdout, buf.String())
	return err
}

// marshalCompact 是表格单元格里嵌套对象/数组的单行渲染。
//
// 分隔符用 `, ` 和 `: `（而非 encoding/json 的无空格形态）：单元格会被截到
// 60 列，带空格的版本在这个宽度里明显更好读，也和其余输出的观感一致。
func marshalCompact(value any) ([]byte, error) {
	var buf bytes.Buffer
	if err := writeCompact(&buf, value); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func writeCompact(buf *bytes.Buffer, value any) error {
	switch v := value.(type) {
	case *jsonval.Map:
		buf.WriteByte('{')
		for i, key := range v.Keys() {
			if i > 0 {
				buf.WriteString(", ")
			}
			if err := writeCompact(buf, key); err != nil {
				return err
			}
			buf.WriteString(": ")
			if err := writeCompact(buf, v.Get(key)); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
		return nil
	case []any:
		buf.WriteByte('[')
		for i, item := range v {
			if i > 0 {
				buf.WriteString(", ")
			}
			if err := writeCompact(buf, item); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
		return nil
	default:
		encoder := json.NewEncoder(buf)
		encoder.SetEscapeHTML(false)
		if err := encoder.Encode(value); err != nil {
			return err
		}
		buf.Truncate(buf.Len() - 1) // 去掉 Encode 追加的换行
		return nil
	}
}

// yamlValue 把保序对象转成 yaml.Node，让 YAML 输出也按服务端字段顺序走
// （yaml.v3 对 Go map 会排序，那会和 JSON、表格三种格式各说一套）。
func yamlValue(value any) any {
	switch v := value.(type) {
	case *jsonval.Map:
		node := &yaml.Node{Kind: yaml.MappingNode}
		for _, key := range v.Keys() {
			keyNode := &yaml.Node{}
			if err := keyNode.Encode(key); err != nil {
				return value
			}
			valueNode := &yaml.Node{}
			if err := valueNode.Encode(yamlValue(v.Get(key))); err != nil {
				return value
			}
			node.Content = append(node.Content, keyNode, valueNode)
		}
		return node
	case []any:
		converted := make([]any, len(v))
		for i, item := range v {
			converted[i] = yamlValue(item)
		}
		return converted
	case json.Number:
		// json.Number 底层是 string，直接交给 yaml 会输出成带引号的字符串
		if n, err := v.Int64(); err == nil {
			return n
		}
		if f, err := v.Float64(); err == nil {
			return f
		}
		return v.String()
	default:
		return value
	}
}
