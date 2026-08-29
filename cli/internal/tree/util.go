package tree

import (
	"encoding/json"
	"fmt"
	"strings"
)

// str 安全取字符串字段。
func str(value any) string {
	if s, ok := value.(string); ok {
		return s
	}
	return ""
}

// truthy 判断 spec 里的布尔标记（JSON 里可能是 true 或字符串）。
func truthy(value any) bool {
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

// plain 把 JSON 值渲染成不带引号的字面量（枚举候选、示例值用）。
func plain(value any) string {
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

// apiPath 剥掉 spec 路径里的 /api/v1 前缀——http 层会再拼一次。
func apiPath(path string) string {
	const prefix = "/api/v1"
	return strings.TrimPrefix(path, prefix)
}
