// 凭证层：路径解析与四个工程陷阱（docs/design/device-auth.md §6.2/§6.3）。
//
// 「任何终端、任何应用触发都能连」的真正难点不在协议，在这四件事——配置目录
// 怎么定、$HOME 不一致怎么排障、权限过宽怎么办、并发写会不会写坏。每一个都会
// 表现成用户口中的「我明明配对过了」。
package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/yipengfei329/movieclaw/cli/internal/clierr"
)

const testServer = "http://10.1.1.5:3000"

// isolate 把配置目录指到临时目录，并清掉会干扰解析的环境变量。
func isolate(t *testing.T) string {
	t.Helper()
	home := filepath.Join(t.TempDir(), "cli-home")
	t.Setenv(EnvConfigDir, home)
	t.Setenv(EnvServer, "")
	t.Setenv(EnvContext, "")
	os.Unsetenv(EnvServer)
	os.Unsetenv(EnvContext)
	return home
}

// ---------------------------------------------------------------------------
// 配置目录：每用户固定位置
// ---------------------------------------------------------------------------

func TestDirPrefersExplicitOverride(t *testing.T) {
	// 逃生舱最优先：sudo / launchd / 容器里 $HOME 不同时，一句话解决
	home := isolate(t)
	if got := Dir(); got != home {
		t.Fatalf("Dir() = %s，期望 %s", got, home)
	}
}

func TestDirFollowsXDG(t *testing.T) {
	os.Unsetenv(EnvConfigDir)
	t.Setenv("XDG_CONFIG_HOME", "/tmp/xdg-test")
	want := filepath.Join("/tmp/xdg-test", "movieclaw")
	if got := Dir(); got != want {
		t.Fatalf("Dir() = %s，期望 %s", got, want)
	}
}

// ---------------------------------------------------------------------------
// 地址解析：机器级兜底 + 排障信息
// ---------------------------------------------------------------------------

func TestSystemConfigSuppliesServerWhenUserHasNone(t *testing.T) {
	// 机器级只提供地址，给「NAS 上管理员配一次，全家可用」兜底
	isolate(t)
	system := filepath.Join(t.TempDir(), "etc-movieclaw.toml")
	writeFile(t, system, "current_context = \"nas\"\n\n[contexts.nas]\nserver = \"http://nas.local:3000\"\n")
	restore := stubSystemConfig(system)
	defer restore()

	got, err := ResolveServer("", "")
	if err != nil {
		t.Fatalf("解析失败：%v", err)
	}
	if got != "http://nas.local:3000" {
		t.Fatalf("解析到 %s", got)
	}
}

func TestUserContextOverridesSystemOne(t *testing.T) {
	isolate(t)
	system := filepath.Join(t.TempDir(), "etc-movieclaw.toml")
	writeFile(t, system, "[contexts.default]\nserver = \"http://nas.local:3000\"\n")
	restore := stubSystemConfig(system)
	defer restore()

	if err := SaveContext(testServer, "default"); err != nil {
		t.Fatal(err)
	}
	got, err := ResolveServer("", "")
	if err != nil {
		t.Fatalf("解析失败：%v", err)
	}
	if got != testServer {
		t.Fatalf("用户级没有覆盖机器级，解析到 %s", got)
	}
}

func TestMissingServerErrorListsSearchedPaths(t *testing.T) {
	// 「我明明配对过了」十次里九次是读错了配置目录，所以要把路径打出来
	home := isolate(t)
	_, err := ResolveServer("", "")
	if err == nil {
		t.Fatal("没有配置时应当报错")
	}
	cliErr, ok := err.(*clierr.Error)
	if !ok || cliErr.ExitCode != clierr.Usage {
		t.Fatalf("退出码应为用法错误，实际 %v", err)
	}
	if !strings.Contains(cliErr.Hint, home) {
		t.Fatalf("提示里没有列出用户级配置路径：%s", cliErr.Hint)
	}
	if !strings.Contains(cliErr.Hint, SystemConfigPath()) {
		t.Fatalf("提示里没有列出机器级配置路径：%s", cliErr.Hint)
	}
}

func TestUnreadableSystemConfigDoesNotBreakCommands(t *testing.T) {
	// 机器级配置可能不可读（权限/挂载），当它不存在处理，不能让命令失败
	isolate(t)
	system := filepath.Join(t.TempDir(), "etc-movieclaw.toml")
	writeFile(t, system, "[contexts.nas]\nserver = \"http://nas:3000\"\n")
	if err := os.Chmod(system, 0o000); err != nil {
		t.Skip("当前环境无法制造不可读文件（可能以 root 运行）")
	}
	defer os.Chmod(system, 0o600)
	restore := stubSystemConfig(system)
	defer restore()

	if err := SaveContext(testServer, "default"); err != nil {
		t.Fatal(err)
	}
	got, err := ResolveServer("", "")
	if err != nil {
		t.Fatalf("机器级配置不可读时命令不该失败：%v", err)
	}
	if got != testServer {
		t.Fatalf("解析到 %s", got)
	}
}

func TestUnknownContextIsActionable(t *testing.T) {
	isolate(t)
	if err := SaveContext(testServer, "default"); err != nil {
		t.Fatal(err)
	}
	_, err := ResolveServer("", "nope")
	if err == nil {
		t.Fatal("不存在的上下文应当报错")
	}
	if !strings.Contains(err.(*clierr.Error).Hint, "default") {
		t.Fatalf("提示里应列出可用上下文：%s", err.(*clierr.Error).Hint)
	}
}

// ---------------------------------------------------------------------------
// 令牌读写：权限、原子性
// ---------------------------------------------------------------------------

func TestTokenRoundTripAndFileIsOwnerOnly(t *testing.T) {
	home := isolate(t)
	if err := SaveToken(testServer, "mclaw_abc"); err != nil {
		t.Fatal(err)
	}
	got, err := LoadToken(testServer)
	if err != nil {
		t.Fatal(err)
	}
	if got != "mclaw_abc" {
		t.Fatalf("读回 %q", got)
	}
	info, err := os.Stat(filepath.Join(home, "credentials"))
	if err != nil {
		t.Fatal(err)
	}
	if mode := info.Mode().Perm(); mode&0o077 != 0 {
		t.Fatalf("凭证文件权限过宽：%s", mode)
	}
}

func TestTokensAreKeyedByServer(t *testing.T) {
	// 多服务器各存各的：切上下文不该串号
	isolate(t)
	other := "http://other:3000"
	if err := SaveToken(testServer, "mclaw_a"); err != nil {
		t.Fatal(err)
	}
	if err := SaveToken(other, "mclaw_b"); err != nil {
		t.Fatal(err)
	}
	if err := DeleteToken(testServer); err != nil {
		t.Fatal(err)
	}
	if token, _ := LoadToken(testServer); token != "" {
		t.Fatalf("删除后仍读到 %q", token)
	}
	if token, _ := LoadToken(other); token != "mclaw_b" {
		t.Fatalf("误删了另一个服务器的令牌，读到 %q", token)
	}
}

func TestOverPermissiveCredentialsAreRefused(t *testing.T) {
	// 自部署用户会 chmod -R 777；静默继续用比报错危险得多
	home := isolate(t)
	if err := SaveToken(testServer, "mclaw_abc"); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(home, "credentials")
	if err := os.Chmod(path, 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := LoadToken(testServer)
	if err == nil {
		t.Fatal("权限过宽时应当拒绝加载")
	}
	cliErr := err.(*clierr.Error)
	if cliErr.ExitCode != clierr.Usage {
		t.Fatalf("退出码应为用法错误，实际 %d", cliErr.ExitCode)
	}
	if !strings.Contains(cliErr.Hint, "chmod 600") {
		t.Fatalf("提示里应给出可执行的修复命令：%s", cliErr.Hint)
	}
}

func TestConcurrentWritesNeverLeaveABrokenFile(t *testing.T) {
	// 多个 Agent 并发跑是常态：写必须原子，不能留下半截 JSON
	home := isolate(t)
	if err := SaveToken(testServer, "mclaw_seed"); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for i := 0; i < 32; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_ = SaveToken("http://host-"+string(rune('a'+i%26))+":3000", "mclaw_x")
		}(i)
	}
	wg.Wait()

	raw, err := os.ReadFile(filepath.Join(home, "credentials"))
	if err != nil {
		t.Fatal(err)
	}
	var parsed map[string]credentialEntry
	if err := json.Unmarshal(raw, &parsed); err != nil {
		t.Fatalf("并发写留下了损坏的凭证文件：%v", err)
	}
	entries, err := os.ReadDir(home)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".credentials-") {
			t.Fatalf("残留了临时文件：%s", entry.Name())
		}
	}
}

func TestCorruptCredentialsFileDoesNotCrash(t *testing.T) {
	// 凭证文件被手工改坏时按「没有凭证」处理，用户重新配对即可
	home := isolate(t)
	if err := os.MkdirAll(home, 0o700); err != nil {
		t.Fatal(err)
	}
	writeFileMode(t, filepath.Join(home, "credentials"), "{ 这不是 JSON", 0o600)
	token, err := LoadToken(testServer)
	if err != nil {
		t.Fatalf("损坏的凭证文件不该让命令失败：%v", err)
	}
	if token != "" {
		t.Fatalf("不该读出令牌：%q", token)
	}
}

func TestContextNameIsValidated(t *testing.T) {
	isolate(t)
	if err := SaveContext(testServer, "bad name!"); err == nil {
		t.Fatal("非法上下文名应当被拒绝")
	}
}

// ---------------------------------------------------------------------------

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	writeFileMode(t, path, content, 0o600)
}

func writeFileMode(t *testing.T, path, content string, mode os.FileMode) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), mode); err != nil {
		t.Fatal(err)
	}
}
