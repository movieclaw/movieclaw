package tree

import (
	"strings"

	"github.com/yipengfei329/movieclaw/cli/internal/jsonval"
)

// 生成层高频读 spec 里的动态字段，这里给 jsonval 起短名，读起来不至于全是包前缀。
var (
	str    = jsonval.Str
	truthy = jsonval.Truthy
	plain  = jsonval.Plain
)

// apiPath 剥掉 spec 路径里的 /api/v1 前缀——http 层会再拼一次。
func apiPath(path string) string {
	const prefix = "/api/v1"
	return strings.TrimPrefix(path, prefix)
}
