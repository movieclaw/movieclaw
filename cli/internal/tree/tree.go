package tree

import (
	"sort"
	"strings"

	"github.com/spf13/cobra"
	"github.com/yipengfei329/movieclaw/cli/internal/spec"
)

// Build 把生成命令挂到根命令。已存在的同名命令（精选层）优先，不覆盖。
//
// operation_id 的点分段就是命令路径：`library.movies.list` → `mclaw library
// movies list`。中间段自动建组，组的帮助文案取自 domainHelp / commandGroupHelp。
//
// 一个段既是命令又是组是允许的（`mclaw dl limits` 读限速、`mclaw dl limits set`
// 改限速）。Python 版的 click 做不到这点，两种形态撞车时后来者被静默丢弃——
// `dl limits set` 与 `watch entries` 因此从未真正挂上去过，虽然命令树快照里
// 一直列着它们。这里按快照把两条命令补齐，命令面与文档一致。
func Build(root *cobra.Command, doc spec.Spec) error {
	all := IterOperations(doc)
	opsByID := make(map[string]Operation, len(all))
	for _, op := range all {
		if op.OperationID != "" {
			opsByID[op.OperationID] = op
		}
	}
	// synthetic 记录本次建树自动补出的中间组。只有它们可以在后续被同名的
	// 真命令顶替（顶替时继承已挂上的子命令）；精选层的命令一律不动。
	synthetic := map[*cobra.Command]bool{}
	for _, op := range all {
		if !op.Generable() {
			continue
		}
		segments := strings.Split(op.OperationID, ".")
		group := root
		for index, segment := range segments[:len(segments)-1] {
			existing := childNamed(group, segment)
			if existing == nil {
				prefix := strings.Join(segments[:index+1], ".")
				existing = newGroup(segment, prefix)
				synthetic[existing] = true
				group.AddCommand(existing)
			}
			group = existing
		}
		leaf := segments[len(segments)-1]
		cmd := makeCommand(op, opsByID)
		switch existing := childNamed(group, leaf); {
		case existing == nil:
			group.AddCommand(cmd)
		case synthetic[existing]:
			// 这个名字先前只是个自动补出的空壳组，现在有真命令了：
			// 把已挂上的子命令搬过去，再换掉空壳。
			cmd.AddCommand(existing.Commands()...)
			group.RemoveCommand(existing)
			delete(synthetic, existing)
			group.AddCommand(cmd)
		default:
			// 精选层占了同名命令，生成命令让位
		}
	}
	return nil
}

// groupAnnotation 标记「自动补出的中间组」。cobra 里组和命令是同一个类型，
// 没有这个标记就分不清 `mclaw dl limits`（真命令，也带子命令）和
// `mclaw dl`（纯粹的分组）。
const groupAnnotation = "movieclaw.group"

// IsGroupOnly 判断一条命令是不是纯分组（自身不可调用）。
func IsGroupOnly(cmd *cobra.Command) bool {
	return cmd.Annotations[groupAnnotation] != ""
}

// newGroup 造一个只用于分组的中间命令。
func newGroup(segment, prefix string) *cobra.Command {
	help := domainHelp[segment]
	if help == "" {
		help = commandGroupHelp[prefix]
	}
	return &cobra.Command{
		Use:   segment,
		Short: help,
		Long:  help,
		// 标记「这只是个组」：组名本身不是可调用的命令，快照校验据此区分
		Annotations: map[string]string{groupAnnotation: "1"},
		// 组本身不可执行：直接敲组名要看到子命令清单，而不是「运行成功」
		RunE:         func(cmd *cobra.Command, args []string) error { return cmd.Help() },
		SilenceUsage: true,
	}
}

// childNamed 按名字查子命令（含别名）。cobra 没有直接的按名查找。
func childNamed(parent *cobra.Command, name string) *cobra.Command {
	for _, child := range parent.Commands() {
		if child.Name() == name {
			return child
		}
		for _, alias := range child.Aliases {
			if alias == name {
				return child
			}
		}
	}
	return nil
}

// GeneratedCommandPaths 返回全部生成命令的完整命令路径（快照测试的数据源）。
func GeneratedCommandPaths(doc spec.Spec) []string {
	var paths []string
	for _, op := range IterOperations(doc) {
		if op.Generable() {
			paths = append(paths, strings.ReplaceAll(op.OperationID, ".", " "))
		}
	}
	sort.Strings(paths)
	return paths
}
