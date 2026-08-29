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
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"

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
		encoded, err := json.MarshalIndent(data, "", "  ")
		if err != nil {
			return err
		}
		_, err = fmt.Fprintln(Stdout, string(encoded))
		return err
	case "yaml":
		encoded, err := yaml.Marshal(data)
		if err != nil {
			return err
		}
		_, err = fmt.Fprint(Stdout, string(encoded))
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
	case float64:
		// JSON 数字统一解成 float64；整数不显示小数点
		if v == float64(int64(v)) {
			return fmt.Sprintf("%d", int64(v))
		}
		return fmt.Sprintf("%g", v)
	case map[string]any, []any:
		encoded, err := json.Marshal(v)
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
		rows := make([]map[string]any, 0, len(v))
		for _, item := range v {
			row, ok := item.(map[string]any)
			if !ok {
				// 非对象数组按 JSON 原样输出
				return emitJSON(data)
			}
			rows = append(rows, row)
		}
		return printRows(rows)
	case map[string]any:
		keys := make([]string, 0, len(v))
		for key := range v {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		w := 0
		for _, key := range keys {
			w = max(w, width(key))
		}
		for _, key := range keys {
			fmt.Fprintf(Stdout, "%s  %s\n", pad(key, w), display(v[key]))
		}
		return nil
	default:
		return emitJSON(data)
	}
}

func printRows(rows []map[string]any) error {
	var columns []string
	seen := map[string]bool{}
	for _, row := range rows {
		keys := make([]string, 0, len(row))
		for key := range row {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
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
			cells[r][c] = display(row[col])
			widths[c] = max(widths[c], width(cells[r][c]))
		}
	}
	header := make([]string, len(columns))
	for i, col := range columns {
		header[i] = pad(col, widths[i])
	}
	fmt.Fprintln(Stdout, strings.TrimRight(strings.Join(header, "  "), " "))
	for _, row := range cells {
		padded := make([]string, len(row))
		for i, cell := range row {
			padded[i] = pad(cell, widths[i])
		}
		fmt.Fprintln(Stdout, strings.TrimRight(strings.Join(padded, "  "), " "))
	}
	fmt.Fprintf(Stderr, "（共 %d 条）\n", len(rows))
	return nil
}

func emitJSON(data any) error {
	encoded, err := json.MarshalIndent(data, "", "  ")
	if err != nil {
		return err
	}
	_, err = fmt.Fprintln(Stdout, string(encoded))
	return err
}
