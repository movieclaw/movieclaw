package tree

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/spf13/cobra"
	"github.com/yipengfei329/movieclaw/cli/internal/spec"
)

// snapshotPath 是生成命令的清单快照：路由增删改必然反映为这里的 diff，
// 评审时一目了然；新端点没被生成器认出来也会在这里现形。
const snapshotPath = "../../testdata/command_tree_snapshot.txt"

func loadSpec(t *testing.T) spec.Spec {
	t.Helper()
	doc, err := spec.LoadBaseline()
	if err != nil {
		t.Fatalf("加载内置 spec 失败：%v", err)
	}
	return doc
}

// TestGeneratedPathsMatchSnapshot 校验生成范围（哪些端点该出命令）没有漂移。
func TestGeneratedPathsMatchSnapshot(t *testing.T) {
	want := readSnapshot(t)
	got := GeneratedCommandPaths(loadSpec(t))
	compare(t, "生成范围", want, got)
}

// TestBuiltTreeMatchesSnapshot 校验真正挂到 cobra 上的命令与快照一致。
//
// 与上一个测试的区别：这里走完整的建组逻辑，能抓到「组名冲突把命令吃掉」
// 这类只在挂载时才暴露的问题。
func TestBuiltTreeMatchesSnapshot(t *testing.T) {
	root := &cobra.Command{Use: "mclaw"}
	if err := Build(root, loadSpec(t)); err != nil {
		t.Fatalf("建树失败：%v", err)
	}
	var got []string
	walkLeaves(root, nil, &got)
	sort.Strings(got)
	compare(t, "已挂载命令", readSnapshot(t), got)
}

// TestOverlayCommandWins 校验同名让位：精选层已经占住的命令，生成层不覆盖。
//
// 精选层预建的组（search / library / session / logs / jobs）要能被生成命令并入，
// 所以「让位」只针对叶子命令本身，不阻断该前缀下的子命令挂载。
func TestOverlayCommandWins(t *testing.T) {
	doc := loadSpec(t)
	// 挑一条真实的生成命令，用一个同名精选命令占住它
	target := strings.Fields(GeneratedCommandPaths(doc)[0])
	root := &cobra.Command{Use: "mclaw"}
	parent := root
	for _, segment := range target[:len(target)-1] {
		next := &cobra.Command{Use: segment, Short: "精选组"}
		parent.AddCommand(next)
		parent = next
	}
	leaf := &cobra.Command{Use: target[len(target)-1], Short: "精选命令"}
	parent.AddCommand(leaf)

	if err := Build(root, doc); err != nil {
		t.Fatalf("建树失败：%v", err)
	}
	if leaf.Short != "精选命令" {
		t.Fatalf("精选命令 %s 被生成层覆盖", strings.Join(target, " "))
	}
	// 精选组仍然是同一个对象，且生成命令并入了它
	if got := childNamed(root, target[0]); got.Short != "精选组" {
		t.Fatalf("精选组 %s 被生成层替换了", target[0])
	}
}

// TestCommandCanAlsoBeGroup 锁住 Python 版做不到的那一处：`dl limits` 既是
// 可执行命令，又是 `dl limits set` 的父节点。两者少一个都算回归。
func TestCommandCanAlsoBeGroup(t *testing.T) {
	root := &cobra.Command{Use: "mclaw"}
	if err := Build(root, loadSpec(t)); err != nil {
		t.Fatalf("建树失败：%v", err)
	}
	for _, path := range [][]string{{"dl", "limits"}, {"watch", "entries"}} {
		cmd, _, err := root.Find(path)
		if err != nil {
			t.Fatalf("找不到 %v：%v", path, err)
		}
		if IsGroupOnly(cmd) {
			t.Errorf("%v 退化成了纯分组，命令本体丢失", path)
		}
		if !cmd.HasSubCommands() {
			t.Errorf("%v 没有子命令，子命令被丢弃", path)
		}
	}
}

// TestSampleHelpHasSummaryAndExample 抽样检查帮助文案：摘要、参数说明与示例
// 都要在，否则 Agent 只能靠猜。
func TestSampleHelpHasSummaryAndExample(t *testing.T) {
	root := &cobra.Command{Use: "mclaw"}
	if err := Build(root, loadSpec(t)); err != nil {
		t.Fatalf("建树失败：%v", err)
	}
	cmd, _, err := root.Find([]string{"library", "items", "list"})
	if err != nil || cmd == nil || cmd.Name() != "list" {
		t.Fatalf("找不到样本命令 library items list：%v", err)
	}
	if cmd.Short == "" {
		t.Error("样本命令缺少摘要")
	}
	if !strings.Contains(cmd.Long, "示例") {
		t.Errorf("样本命令的长帮助缺少示例段：\n%s", cmd.Long)
	}
	if cmd.Flags().Lookup("limit") == nil {
		t.Error("样本命令缺少 --limit 查询参数标志")
	}
}

func walkLeaves(cmd *cobra.Command, prefix []string, out *[]string) {
	for _, child := range cmd.Commands() {
		path := append(append([]string{}, prefix...), child.Name())
		// 一个节点可以既是命令又是组（`dl limits` / `dl limits set`），
		// 所以这两件事分开判断，不是 if/else。
		if !IsGroupOnly(child) {
			*out = append(*out, strings.Join(path, " "))
		}
		if child.HasSubCommands() {
			walkLeaves(child, path, out)
		}
	}
}

func readSnapshot(t *testing.T) []string {
	t.Helper()
	raw, err := os.ReadFile(filepath.FromSlash(snapshotPath))
	if err != nil {
		t.Fatalf("读取命令树快照失败：%v", err)
	}
	var lines []string
	for _, line := range strings.Split(string(raw), "\n") {
		if line = strings.TrimRight(line, "\r"); line != "" {
			lines = append(lines, line)
		}
	}
	return lines
}

func compare(t *testing.T, label string, want, got []string) {
	t.Helper()
	if len(want) == 0 {
		t.Fatalf("%s：快照为空", label)
	}
	wantSet := map[string]bool{}
	for _, item := range want {
		wantSet[item] = true
	}
	gotSet := map[string]bool{}
	for _, item := range got {
		gotSet[item] = true
	}
	for _, item := range got {
		if !wantSet[item] {
			t.Errorf("%s：多出命令 %q", label, item)
		}
	}
	for _, item := range want {
		if !gotSet[item] {
			t.Errorf("%s：缺少命令 %q", label, item)
		}
	}
}
