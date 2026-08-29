// Package tree 由 OpenAPI spec 构建命令树（docs/design/cli.md §3）。
//
// Stage 2 实现全量映射规则；当前是骨架。
package tree

import (
	"github.com/spf13/cobra"
	"github.com/yipengfei329/movieclaw/cli/internal/spec"
)

// Build 把 spec 里的操作挂成 root 的子命令。
func Build(root *cobra.Command, doc spec.Spec) error {
	_ = doc
	_ = root
	return nil
}
