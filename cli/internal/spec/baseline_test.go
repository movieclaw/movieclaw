package spec

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"testing"
)

// serverBaseline 是服务端运行期读的那份基线 spec。
//
// 同一份 spec 有两个消费方：Go CLI 在构建期把它嵌进二进制（本包的
// data/spec.json），服务端运行期读它渲染 Agent 的工具描述。两处必须同版，
// 否则模型看到的服务目录和 CLI 实际能跑的命令会对不上。
const serverBaseline = "../../../src/movieclaw_api/data/spec.json"

// TestEmbeddedBaselineMatchesServerCopy 守住这两份文件不漂移。
// 重新导出用 scripts/export-spec.sh，它一次写两处。
func TestEmbeddedBaselineMatchesServerCopy(t *testing.T) {
	embedded, err := os.ReadFile("data/spec.json")
	if err != nil {
		t.Fatalf("读内嵌基线失败：%v", err)
	}
	server, err := os.ReadFile(serverBaseline)
	if err != nil {
		t.Skipf("跳过：读不到服务端基线（%v）", err)
	}
	if digest(embedded) != digest(server) {
		t.Fatalf("内嵌基线与服务端基线不一致，请重新导出：scripts/export-spec.sh\n"+
			"  cli/internal/spec/data/spec.json = %s\n  %s = %s",
			digest(embedded), serverBaseline, digest(server))
	}
}

func digest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:8])
}
