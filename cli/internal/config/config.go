// Package config 管理 CLI 的本地配置与凭证（docs/design/device-auth.md §6.2）。
//
// 凭证必须落在每用户的固定位置——这是「任何终端、任何应用触发都能连」的前提：
// 用户配一次，之后从 Dock 启动的 GUI 应用、cron、systemd 服务里跑 mclaw 都要能
// 自动带上授权。
//
// 两个文件、职责分离（配置可进 dotfiles 同步，凭证绝不）：
//
//	<配置目录>/config.toml   多上下文配置（服务器地址）
//	<配置目录>/credentials   设备令牌（JSON，0600，按服务器地址存）
//
// 配置目录按平台取（与 gh / gcloud 同款惯例）：
//
//	Linux / macOS   $XDG_CONFIG_HOME/movieclaw，缺省 ~/.config/movieclaw
//	Windows         %APPDATA%\movieclaw
//
// 凭证只在用户级，绝不放机器级：那份令牌等价管理员，全机器可读意味着本机任何
// 进程、任何其他用户都能拿到全权。机器级配置只允许放服务器地址，给「NAS 上装
// 一次全家都能用」兜底。
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"

	"github.com/yipengfei329/movieclaw/cli/internal/clierr"
)

// 环境变量名。环境变量是 Agent/CI 的主通道，可完全不落盘。
const (
	EnvServer    = "MOVIECLAW_SERVER"
	EnvToken     = "MOVIECLAW_TOKEN"
	EnvContext   = "MOVIECLAW_CONTEXT"
	EnvConfigDir = "MOVIECLAW_CONFIG_DIR"
)

var contextNameRe = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

// Dir 返回用户级配置目录。
//
// MOVIECLAW_CONFIG_DIR 是逃生舱：sudo、launchd、systemd、容器里的 $HOME 各不
// 相同，凭证会「凭空消失」；显式指定目录能一句话解决。
func Dir() string {
	if override := os.Getenv(EnvConfigDir); override != "" {
		return override
	}
	if runtime.GOOS == "windows" {
		if appData := os.Getenv("APPDATA"); appData != "" {
			return filepath.Join(appData, "movieclaw")
		}
	}
	if xdg := os.Getenv("XDG_CONFIG_HOME"); xdg != "" {
		return filepath.Join(xdg, "movieclaw")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		home = "."
	}
	return filepath.Join(home, ".config", "movieclaw")
}

// SystemConfigPath 返回机器级配置。只读、只允许放服务器地址，绝不放凭证。
func SystemConfigPath() string { return systemConfigPathFn() }

func defaultSystemConfigPath() string {
	if runtime.GOOS == "windows" {
		programData := os.Getenv("PROGRAMDATA")
		if programData == "" {
			programData = `C:\ProgramData`
		}
		return filepath.Join(programData, "movieclaw", "config.toml")
	}
	return "/etc/movieclaw/config.toml"
}

func configPath() string      { return filepath.Join(Dir(), "config.toml") }
func credentialsPath() string { return filepath.Join(Dir(), "credentials") }

// Config 是合并后的配置视图。
type Config struct {
	CurrentContext string
	Contexts       map[string]string // 上下文名 → 服务器地址
}

// Load 把用户级配置叠在机器级之上。
//
// 机器级只提供上下文与默认上下文的兜底（NAS 上管理员配一次，全家可用），
// 用户级的同名上下文覆盖它。凭证永远不从这里来。
func Load() (*Config, error) {
	merged := &Config{Contexts: map[string]string{}}
	for _, path := range []string{SystemConfigPath(), configPath()} {
		parsed, err := readTOML(path)
		if err != nil {
			return nil, err
		}
		if parsed == nil {
			continue
		}
		for name, server := range parsed.Contexts {
			merged.Contexts[name] = server
		}
		if parsed.CurrentContext != "" {
			merged.CurrentContext = parsed.CurrentContext
		}
	}
	return merged, nil
}

// ResolveServer 按优先级解析目标服务器地址。
//
// 优先级（与 gcloud/kubectl 一致）：
//
//	--server 标志 > MOVIECLAW_SERVER > 用户级上下文 > 机器级上下文 > 报错
func ResolveServer(flagServer, flagContext string) (string, error) {
	if flagServer != "" {
		return strings.TrimRight(flagServer, "/"), nil
	}
	if env := os.Getenv(EnvServer); env != "" {
		return strings.TrimRight(env, "/"), nil
	}
	cfg, err := Load()
	if err != nil {
		return "", err
	}
	name := flagContext
	if name == "" {
		name = os.Getenv(EnvContext)
	}
	if name == "" {
		name = cfg.CurrentContext
	}
	if name != "" {
		server, ok := cfg.Contexts[name]
		if !ok {
			available := "（无）"
			if len(cfg.Contexts) > 0 {
				names := make([]string, 0, len(cfg.Contexts))
				for n := range cfg.Contexts {
					names = append(names, n)
				}
				available = strings.Join(names, ", ")
			}
			return "", clierr.Usagef("上下文不存在：%s", name).
				WithHint("可用上下文：%s；或用 mclaw login --server <地址> 新建", available)
		}
		return strings.TrimRight(server, "/"), nil
	}
	// 「我明明配对过了」是这套凭证机制最常见的投诉，根因通常是 $HOME 不同
	// （sudo / launchd / systemd / 容器）导致读的不是同一个配置目录。因此这里
	// 把找过的路径原样列出来，让人一眼看出读的是哪儿。
	return "", clierr.Usagef("未指定 movieclaw 服务器地址").
		WithHint("三种方式任选：mclaw login --server http://<主机>:3000 登录并记住；"+
			"或设置环境变量 MOVIECLAW_SERVER；或加 --server 标志。（已查找：%s、%s）",
			configPath(), SystemConfigPath()).
		Wrap(ErrNoServer)
}

// ErrNoServer 标记「哪儿都没配地址」这一种解析失败。
//
// 与「上下文写错了」区分开：前者可以退回局域网自动发现兜底，后者是用户明确
// 指了一个不存在的东西，猜一台机器给他反而更糟。
var ErrNoServer = errors.New("未指定服务器地址")

// SaveContext 把服务器记进上下文；首个上下文自动设为当前。
func SaveContext(server, name string) error {
	if name == "" {
		name = "default"
	}
	if !contextNameRe.MatchString(name) {
		return clierr.Usagef("上下文名不合法：%s", name).
			WithHint("只允许字母、数字、连字符与下划线（TOML 表名约束）")
	}
	// 只读回用户级：Load 读的是合并结果，直接回写会把机器级上下文抄进用户级
	user, err := readTOML(configPath())
	if err != nil {
		return err
	}
	if user == nil {
		user = &Config{Contexts: map[string]string{}}
	}
	if user.Contexts == nil {
		user.Contexts = map[string]string{}
	}
	user.Contexts[name] = server
	if user.CurrentContext == "" {
		user.CurrentContext = name
	}
	return writeTOML(configPath(), user)
}

// ---------------------------------------------------------------------------
// 设备令牌
// ---------------------------------------------------------------------------

type credentialEntry struct {
	Token string `json:"token"`
}

// LoadToken 读取该服务器的设备令牌。没有配对过则返回空串。
func LoadToken(server string) (string, error) {
	creds, err := readCredentials()
	if err != nil {
		return "", err
	}
	return creds[server].Token, nil
}

// SaveToken 写入令牌。
func SaveToken(server, token string) error {
	creds, err := readCredentials()
	if err != nil {
		return err
	}
	creds[server] = credentialEntry{Token: token}
	return writeCredentials(creds)
}

// DeleteToken 删除该服务器的令牌。
func DeleteToken(server string) error {
	creds, err := readCredentials()
	if err != nil {
		return err
	}
	if _, ok := creds[server]; !ok {
		return nil
	}
	delete(creds, server)
	return writeCredentials(creds)
}

// CredentialsPath 供 status 命令回显凭证来源。
func CredentialsPath() string { return credentialsPath() }

func readCredentials() (map[string]credentialEntry, error) {
	path := credentialsPath()
	info, err := os.Stat(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return map[string]credentialEntry{}, nil
		}
		return nil, clierr.Usagef("无法读取凭证文件：%s（%v）", path, err)
	}
	if err := checkPermissions(path, info); err != nil {
		return nil, err
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, clierr.Usagef("无法读取凭证文件：%s（%v）", path, err)
	}
	creds := map[string]credentialEntry{}
	// 文件被手工改坏时按「没有凭证」处理，用户重新配对即可
	if err := json.Unmarshal(raw, &creds); err != nil {
		return map[string]credentialEntry{}, nil
	}
	return creds, nil
}

// checkPermissions 拒绝加载权限过宽的凭证文件，并说清怎么修（同 ssh 对私钥）。
//
// 自部署用户遇到权限问题的第一反应是 chmod -R 777；那之后这枚等价管理员的令牌
// 就对本机所有用户可读了。静默继续用比报错危险得多。
func checkPermissions(path string, info os.FileInfo) error {
	if runtime.GOOS == "windows" {
		// NTFS ACL 不是 POSIX 位，这里不做判断
		return nil
	}
	mode := info.Mode().Perm()
	if mode&0o077 != 0 {
		return clierr.Usagef("凭证文件权限过宽：%s（%s）", path, mode.String()).
			WithHint("这枚令牌等价管理员，不能让同机其他用户读到。执行：chmod 600 %s", path)
	}
	return nil
}

// writeCredentials 落盘：0600 打开写临时文件，再原子替换。
//
// 两点都是必需的：先以 0600 创建而不是「写完再 chmod」，不留可读窗口；
// 临时文件 + rename 原子替换，因为多个 Agent 并发跑很正常，直接截断后写会在
// 中途留下一个损坏的凭证文件。
func writeCredentials(creds map[string]credentialEntry) error {
	path := credentialsPath()
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return clierr.Usagef("无法创建配置目录：%s（%v）", dir, err)
	}
	payload, err := json.MarshalIndent(creds, "", "  ")
	if err != nil {
		return clierr.New("凭证序列化失败：%v", err)
	}
	tmp, err := os.CreateTemp(dir, ".credentials-*")
	if err != nil {
		return clierr.Usagef("无法写入凭证：%v", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // 成功 rename 后这里是 no-op
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return clierr.Usagef("无法设置凭证文件权限：%v", err)
	}
	if _, err := tmp.Write(payload); err != nil {
		tmp.Close()
		return clierr.Usagef("无法写入凭证：%v", err)
	}
	if err := tmp.Close(); err != nil {
		return clierr.Usagef("无法写入凭证：%v", err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		return clierr.Usagef("无法写入凭证：%v", err)
	}
	return nil
}

// ---------------------------------------------------------------------------
// 极简 TOML 读写
// ---------------------------------------------------------------------------
//
// 配置结构固定且简单（current_context + [contexts.*].server），手写读写避免为
// 两个字段引入一个 TOML 库——与 Python 版手写序列化同口径。

func readTOML(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		// 机器级配置可能不可读（权限/挂载），当它不存在处理即可，不该让命令失败
		if path == SystemConfigPath() {
			return nil, nil
		}
		return nil, clierr.Usagef("无法读取配置文件：%s（%v）", path, err)
	}
	cfg := &Config{Contexts: map[string]string{}}
	section := ""
	for lineNo, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if strings.HasPrefix(line, "[") && strings.HasSuffix(line, "]") {
			section = strings.TrimSuffix(strings.TrimPrefix(line, "["), "]")
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			return nil, clierr.Usagef("配置文件格式错误：%s 第 %d 行", path, lineNo+1).
				WithHint("修正该文件或删除后重新执行 mclaw login")
		}
		key = strings.TrimSpace(key)
		unquoted, err := unquote(strings.TrimSpace(value))
		if err != nil {
			return nil, clierr.Usagef("配置文件格式错误：%s 第 %d 行（%v）", path, lineNo+1, err).
				WithHint("修正该文件或删除后重新执行 mclaw login")
		}
		switch {
		case section == "" && key == "current_context":
			cfg.CurrentContext = unquoted
		case strings.HasPrefix(section, "contexts.") && key == "server":
			cfg.Contexts[strings.TrimPrefix(section, "contexts.")] = unquoted
		}
	}
	return cfg, nil
}

func writeTOML(path string, cfg *Config) error {
	var b strings.Builder
	if cfg.CurrentContext != "" {
		fmt.Fprintf(&b, "current_context = %s\n\n", quote(cfg.CurrentContext))
	}
	names := make([]string, 0, len(cfg.Contexts))
	for name := range cfg.Contexts {
		names = append(names, name)
	}
	sortStrings(names)
	for _, name := range names {
		fmt.Fprintf(&b, "[contexts.%s]\nserver = %s\n\n", name, quote(cfg.Contexts[name]))
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return clierr.Usagef("无法创建配置目录：%v", err)
	}
	if err := os.WriteFile(path, []byte(b.String()), 0o600); err != nil {
		return clierr.Usagef("无法写入配置文件：%v", err)
	}
	return nil
}

// quote 用 JSON 字符串转义——它是合法的 TOML 基本字符串，含引号/反斜杠的值不会
// 写出损坏的配置文件。
func quote(value string) string {
	encoded, _ := json.Marshal(value)
	return string(encoded)
}

func unquote(value string) (string, error) {
	var out string
	if err := json.Unmarshal([]byte(value), &out); err != nil {
		return "", err
	}
	return out, nil
}

func sortStrings(values []string) {
	for i := 1; i < len(values); i++ {
		for j := i; j > 0 && values[j] < values[j-1]; j-- {
			values[j], values[j-1] = values[j-1], values[j]
		}
	}
}
