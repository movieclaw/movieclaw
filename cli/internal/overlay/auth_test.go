package overlay

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/yipengfei329/movieclaw/cli/internal/clierr"
	"github.com/yipengfei329/movieclaw/cli/internal/discover"
	"github.com/yipengfei329/movieclaw/cli/internal/output"
)

func quiet(t *testing.T) {
	t.Helper()
	previous := output.Stderr
	output.Stderr = discardWriter{}
	t.Cleanup(func() { output.Stderr = previous })
}

type discardWriter struct{}

func (discardWriter) Write(p []byte) (int, error) { return len(p), nil }

// TestProbeRejectsNonMovieclaw 是自动发现里最要紧的一道闸：局域网里的真
// Jellyfin 会应答同一句问询，直接拿它的地址去配对只会得到一串看不懂的 404。
func TestProbeRejectsNonMovieclaw(t *testing.T) {
	jellyfin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok","service":"jellyfin"}`))
	}))
	defer jellyfin.Close()
	if _, ok := probeMovieclaw(jellyfin.URL); ok {
		t.Error("真 Jellyfin 被当成了 movieclaw")
	}

	real := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok","service":"movieclaw"}`))
	}))
	defer real.Close()
	name, ok := probeMovieclaw(real.URL)
	if !ok || name != "movieclaw" {
		t.Errorf("movieclaw 没被认出来：name=%q ok=%v", name, ok)
	}
}

// TestProbeRejectsUnreachable 校验发现到但连不上的地址被丢掉——Docker 桥接
// 部署下服务端自报的可能是容器内网段，拿到手也用不了。
func TestProbeRejectsUnreachable(t *testing.T) {
	if _, ok := probeMovieclaw("http://192.0.2.1:3000"); ok {
		t.Error("连不上的地址不该通过确认")
	}
}

func TestChooseServerSingleAsksForConfirmation(t *testing.T) {
	quiet(t)
	found := []discover.Server{{Address: "http://192.168.1.10:3000", Name: "客厅 NAS"}}

	restore := stubYesNo(true)
	got, err := chooseServer(found)
	restore()
	if err != nil || got != found[0].Address {
		t.Fatalf("同意后应当返回该地址：got=%q err=%v", got, err)
	}

	restore = stubYesNo(false)
	_, err = chooseServer(found)
	restore()
	var cliErr *clierr.Error
	if !asCliError(err, &cliErr) || cliErr.ExitCode != clierr.Usage {
		t.Fatalf("拒绝后应当是用法错误（退出码 2）：%v", err)
	}
	if !strings.Contains(cliErr.Hint, "--server") {
		t.Errorf("取消时要告诉用户怎么指定另一台：%q", cliErr.Hint)
	}
}

func TestChooseServerMultipleAsksForIndex(t *testing.T) {
	quiet(t)
	found := []discover.Server{
		{Address: "http://192.168.1.10:3000", Name: "客厅 NAS"},
		{Address: "http://192.168.1.20:3000", Name: "书房"},
	}
	previous := askIndex
	askIndex = func(string, int) int { return 1 }
	got, err := chooseServer(found)
	askIndex = previous
	if err != nil || got != found[1].Address {
		t.Fatalf("应当返回选中的那台：got=%q err=%v", got, err)
	}

	askIndex = func(string, int) int { return -1 } // 输入不合法或直接回车
	_, err = chooseServer(found)
	askIndex = previous
	if err == nil {
		t.Fatal("没选出来时应当报错而不是随便挑一台")
	}
}

// TestDisplayNameFallsBack 校验服务器没配名字时不显示空白。
func TestDisplayNameFallsBack(t *testing.T) {
	if got := displayName(discover.Server{Address: "http://x"}); got != "movieclaw" {
		t.Errorf("缺省名不对：%q", got)
	}
}

func stubYesNo(answer bool) func() {
	previous := askYesNo
	askYesNo = func(string) bool { return answer }
	return func() { askYesNo = previous }
}

// TestResolveOrDiscoverPrefersExplicitServer 校验显式给了地址就不广播——
// 用户说了算，不该被局域网里另一台机器干扰。
func TestResolveOrDiscoverPrefersExplicitServer(t *testing.T) {
	quiet(t)
	called := false
	previous := discoverServers
	discoverServers = func() discovery { called = true; return discovery{} }
	defer func() { discoverServers = previous }()

	got, err := resolveOrDiscover(&Settings{Server: "http://192.168.9.9:3000"})
	if err != nil || got != "http://192.168.9.9:3000" {
		t.Fatalf("显式地址没被采用：got=%q err=%v", got, err)
	}
	if called {
		t.Error("显式给了 --server 还去广播了")
	}
}

// TestResolveOrDiscoverFallsBackToLAN 校验哪儿都没配时退回自动发现。
func TestResolveOrDiscoverFallsBackToLAN(t *testing.T) {
	quiet(t)
	t.Setenv("MOVIECLAW_CONFIG_DIR", t.TempDir())
	t.Setenv("MOVIECLAW_SERVER", "")

	previous := discoverServers
	discoverServers = func() discovery {
		return discovery{Confirmed: []discover.Server{
			{Address: "http://192.168.1.10:3000", Name: "客厅 NAS"},
		}}
	}
	defer func() { discoverServers = previous }()
	restore := stubYesNo(true)
	defer restore()

	got, err := resolveOrDiscover(&Settings{})
	if err != nil || got != "http://192.168.1.10:3000" {
		t.Fatalf("没退回自动发现：got=%q err=%v", got, err)
	}
}

// TestResolveOrDiscoverExplainsBothCauses 校验一台都没找到时，报错要同时说清
// 「怎么手工给地址」和「为什么可能找不到」——只说一半用户就得自己猜。
func TestResolveOrDiscoverExplainsBothCauses(t *testing.T) {
	quiet(t)
	t.Setenv("MOVIECLAW_CONFIG_DIR", t.TempDir())
	t.Setenv("MOVIECLAW_SERVER", "")

	previous := discoverServers
	discoverServers = func() discovery { return discovery{} }
	defer func() { discoverServers = previous }()

	_, err := resolveOrDiscover(&Settings{})
	var cliErr *clierr.Error
	if !asCliError(err, &cliErr) {
		t.Fatalf("应当是 CLI 错误：%v", err)
	}
	if !strings.Contains(cliErr.Message, "局域网内也没有找到") {
		t.Errorf("没说明自动查找也失败了：%q", cliErr.Message)
	}
	for _, want := range []string{"--server", "MOVIECLAW_SERVER", "Jellyfin 兼容层"} {
		if !strings.Contains(cliErr.Hint, want) {
			t.Errorf("提示里缺少 %q：%s", want, cliErr.Hint)
		}
	}
}

// TestResolveOrDiscoverKeepsContextError 校验用户明确指了个不存在的上下文时
// 直接报错，不去猜一台机器给他。
func TestResolveOrDiscoverKeepsContextError(t *testing.T) {
	quiet(t)
	t.Setenv("MOVIECLAW_CONFIG_DIR", t.TempDir())
	t.Setenv("MOVIECLAW_SERVER", "")

	called := false
	previous := discoverServers
	discoverServers = func() discovery { called = true; return discovery{} }
	defer func() { discoverServers = previous }()

	_, err := resolveOrDiscover(&Settings{Context: "打错的上下文名"})
	if err == nil || !strings.Contains(err.Error(), "上下文不存在") {
		t.Fatalf("应当透出上下文错误：%v", err)
	}
	if called {
		t.Error("上下文写错时不该退回广播——猜一台给他比报错更糟")
	}
}

// TestUnreachableDiscoveryIsReportedNotSwallowed 校验「应答了但连不上」这一种
// 结果被单独说明：桥接部署下最常见，咽下去只报「没找到」会让用户查错方向。
func TestUnreachableDiscoveryIsReportedNotSwallowed(t *testing.T) {
	quiet(t)
	t.Setenv("MOVIECLAW_CONFIG_DIR", t.TempDir())
	t.Setenv("MOVIECLAW_SERVER", "")

	previous := discoverServers
	discoverServers = func() discovery {
		return discovery{Unreachable: []string{"http://172.17.0.2:3000"}}
	}
	defer func() { discoverServers = previous }()

	_, err := resolveOrDiscover(&Settings{})
	var cliErr *clierr.Error
	if !asCliError(err, &cliErr) {
		t.Fatalf("应当是 CLI 错误：%v", err)
	}
	if !strings.Contains(cliErr.Message, "172.17.0.2") {
		t.Errorf("没把连不上的地址报出来：%q", cliErr.Message)
	}
	if !strings.Contains(cliErr.Hint, "对外访问地址") {
		t.Errorf("没指向真正的修法：%q", cliErr.Hint)
	}
}
