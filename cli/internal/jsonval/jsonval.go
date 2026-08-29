// Package jsonval 是读取「解析成 any 的 JSON」时的一组取值助手。
//
// spec 文档和 API 响应都是 map[string]any / []any 的动态结构，取一个字段要写
// 三行类型断言。这里把它们收成一处，生成层与等待循环共用，读的时候少一层噪音。
package jsonval

import (
	"encoding/json"
	"fmt"
	"strings"
)

// Str 安全取字符串字段；类型不符返回空串。
func Str(value any) string {
	if s, ok := value.(string); ok {
		return s
	}
	return ""
}

// Truthy 判断一个 JSON 值是否为真。布尔按原值，空串/nil 为假，其余为真。
func Truthy(value any) bool {
	switch v := value.(type) {
	case bool:
		return v
	case string:
		return v != ""
	case nil:
		return false
	default:
		return true
	}
}

// Plain 把 JSON 值渲染成不带引号的字面量（枚举候选、示例值、百分比用）。
func Plain(value any) string {
	switch v := value.(type) {
	case string:
		return v
	case json.Number:
		return v.String()
	case bool:
		if v {
			return "true"
		}
		return "false"
	case nil:
		return ""
	default:
		return fmt.Sprint(v)
	}
}

// At 按点分路径读取嵌套字段；任一层不是对象或不存在都返回 nil。
func At(value any, path string) any {
	if path == "" {
		return nil
	}
	for _, token := range strings.Split(path, ".") {
		obj, ok := value.(*Map)
		if !ok {
			return nil
		}
		value = obj.Get(token)
	}
	return value
}

// Int 取整数字段。JSON 数字可能落成 float64 或 json.Number，两种都收。
func Int(value any) int {
	switch v := value.(type) {
	case json.Number:
		n, err := v.Int64()
		if err != nil {
			return 0
		}
		return int(n)
	case float64:
		return int(v)
	case int:
		return v
	}
	return 0
}

// Float 取浮点字段（排序用）。
func Float(value any) float64 {
	switch v := value.(type) {
	case json.Number:
		n, err := v.Float64()
		if err != nil {
			return 0
		}
		return n
	case float64:
		return v
	case int:
		return float64(v)
	}
	return 0
}

// Object 把值当对象取；不是对象返回 nil（*Map 的取值方法对 nil 安全）。
func Object(value any) *Map {
	obj, _ := value.(*Map)
	return obj
}

// Array 把值当数组取；不是数组返回 nil。
func Array(value any) []any {
	arr, _ := value.([]any)
	return arr
}
