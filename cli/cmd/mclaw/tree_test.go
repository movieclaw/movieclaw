package main

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"
	"github.com/yipengfei329/movieclaw/cli/internal/overlay"
)

// fullTreeSnapshot 是「精选层 + 生成层」装配完成后的整棵命令树（含分组名），
// 由退役中的 Python CLI 导出。它是这次迁移唯一的命令面契约：一条不多、一条不少。
const fullTreeSnapshot = "../../testdata/full_command_tree.txt"

// TestAssembledTreeMatchesSnapshot 是整个迁移最要紧的一条断言：Go 版装配出来的
// 命令面与 Python 版逐条相同。命令名、分组结构、精选/生成的让位关系全在里面。
func TestAssembledTreeMatchesSnapshot(t *testing.T) {
	root := newRootCommand(&overlay.Settings{Timeout: 30 * time.Second})
	if err := assemble(root, &overlay.Settings{}); err != nil {
		t.Fatalf("装配命令树失败：%v", err)
	}
	var got []string
	walk(root, nil, &got)
	sort.Strings(got)

	raw, err := os.ReadFile(filepath.FromSlash(fullTreeSnapshot))
	if err != nil {
		t.Fatalf("读取命令树快照失败：%v", err)
	}
	var want []string
	for _, line := range strings.Split(string(raw), "\n") {
		if line = strings.TrimSpace(line); line != "" && !strings.HasPrefix(line, "#") {
			want = append(want, line)
		}
	}

	inWant := map[string]bool{}
	for _, item := range want {
		inWant[item] = true
	}
	inGot := map[string]bool{}
	for _, item := range got {
		inGot[item] = true
	}
	for _, item := range got {
		if !inWant[item] {
			t.Errorf("多出命令：%q", item)
		}
	}
	for _, item := range want {
		if !inGot[item] {
			t.Errorf("缺少命令：%q", item)
		}
	}
}

// walk 收集整棵树的命令路径，包含中间分组（分组名也是命令面的一部分：
// `mclaw library --help` 是用户探索命令的主要入口）。
func walk(cmd *cobra.Command, prefix []string, out *[]string) {
	for _, child := range cmd.Commands() {
		if child.Hidden {
			continue
		}
		path := append(append([]string{}, prefix...), child.Name())
		*out = append(*out, strings.Join(path, " "))
		walk(child, path, out)
	}
}
