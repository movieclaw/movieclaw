package spec

import (
	"os"
	"os/exec"
	"strings"
	"testing"
)

// TestHashMatchesPythonExporter 是整个偏斜检测机制的前提。
//
// Go 与 Python 必须对同一份 spec 算出同一个指纹，否则 CLI 会永远认为「服务端
// spec 变了」，每次调用都白拉一遍 /spec。两边的实现是独立写的（CLI 是独立发行
// 的二进制，不该依赖服务端可导入），所以一致性只能靠这条测试守。
func TestHashMatchesPythonExporter(t *testing.T) {
	doc, err := LoadBaseline()
	if err != nil {
		t.Fatalf("装载基线 spec 失败：%v", err)
	}
	got, err := Hash(doc)
	if err != nil {
		t.Fatalf("计算指纹失败：%v", err)
	}

	python := findPython(t)
	cmd := exec.Command(python, "-c",
		"import json,sys;from movieclaw_api.export_openapi import spec_hash;"+
			"print(spec_hash(json.load(open(sys.argv[1]))))",
		"data/spec.json")
	cmd.Dir = "."
	cmd.Env = append(os.Environ(), "PYTHONPATH=../../../src")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Skipf("跳过跨语言比对（Python 环境不可用）：%v\n%s", err, out)
	}
	want := strings.TrimSpace(string(out))
	if got != want {
		t.Fatalf("指纹与 Python 导出器不一致：\n  Go     = %s\n  Python = %s", got, want)
	}
}

func findPython(t *testing.T) string {
	t.Helper()
	for _, candidate := range []string{"../../../.venv/bin/python", "python3"} {
		if path, err := exec.LookPath(candidate); err == nil {
			return path
		}
	}
	t.Skip("找不到 Python 解释器，跳过跨语言比对")
	return ""
}

func TestLoadBaselineHasPaths(t *testing.T) {
	doc, err := LoadBaseline()
	if err != nil {
		t.Fatalf("装载基线失败：%v", err)
	}
	paths, ok := doc["paths"].(map[string]any)
	if !ok || len(paths) == 0 {
		t.Fatal("基线 spec 里没有 paths")
	}
}

func TestSpecFileOverrideWins(t *testing.T) {
	// 镜像内靠这条：服务端现场导出的 spec 覆盖内嵌基线，保证与代码严格同版
	tmp := t.TempDir() + "/override.json"
	if err := os.WriteFile(tmp, []byte(`{"paths":{"/x":{"get":{"operationId":"x.y"}}}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(EnvSpecFile, tmp)
	doc, err := LoadBaseline()
	if err != nil {
		t.Fatalf("装载覆盖文件失败：%v", err)
	}
	paths := doc["paths"].(map[string]any)
	if len(paths) != 1 {
		t.Fatalf("读到的不是覆盖文件，paths 数量 = %d", len(paths))
	}
}

func TestMissingOverrideFileIsActionable(t *testing.T) {
	t.Setenv(EnvSpecFile, "/nonexistent/spec.json")
	_, err := LoadBaseline()
	if err == nil {
		t.Fatal("覆盖文件不存在时应当报错")
	}
	if !strings.Contains(err.Error(), "读不到") {
		t.Fatalf("错误信息不够可操作：%v", err)
	}
}
