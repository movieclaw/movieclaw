// Package spec 负责 spec 的装载与版本偏斜刷新（docs/design/cli.md §2.1 / §3.1）。
//
// 三条装载通道，按优先级：
//
//  1. MOVIECLAW_SPEC_FILE 指定的文件——**镜像内走这条**：服务端在镜像构建时现场
//     导出 spec，二进制读它，保证与镜像内代码严格同版；
//  2. ~/.config/movieclaw/spec-cache/<server>.json——偏斜刷新写入的缓存；
//  3. //go:embed 的内置基线——远程独立安装的用户走这条，断网也有完整命令树。
//
// 这样既保住「单文件二进制」对外发行的卖点，也保住镜像内「spec 与代码零偏斜」的
// 既有保证。
package spec

import (
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/yipengfei329/movieclaw/cli/internal/clierr"
	"github.com/yipengfei329/movieclaw/cli/internal/config"
)

// EnvSpecFile 让部署方显式指定 spec 文件（镜像内由 entrypoint 设置）。
const EnvSpecFile = "MOVIECLAW_SPEC_FILE"

//go:embed data/spec.json
var baseline []byte

// ActiveHash 是本次进程装载的 spec 指纹（与响应头比对的基准）。
var ActiveHash string

// Spec 是解析后的 OpenAPI 文档。刻意保留为通用 map：生成层要读的字段很杂
// （schema、x-cli-* 扩展、$ref），定义成结构体反而处处要开洞。
type Spec map[string]any

// Hash 与 movieclaw_api.export_openapi.spec_hash 完全一致的指纹算法。
//
// 刻意复制而非共享实现：CLI 是独立发行的二进制，不应依赖服务端的可导入性；
// 两端一致性由测试守护。
func Hash(s Spec) (string, error) {
	canonical, err := canonicalJSON(map[string]any(s))
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:])[:16], nil
}

// canonicalJSON 复刻 Python 的 json.dumps(sort_keys=True, separators=(",", ":"),
// ensure_ascii=False)——键序、无空格、非 ASCII 不转义，三者都必须一致，
// 否则两端算出的指纹不同，偏斜检测会永远认为「有偏斜」。
func canonicalJSON(value any) ([]byte, error) {
	var b strings.Builder
	if err := writeCanonical(&b, value); err != nil {
		return nil, err
	}
	return []byte(b.String()), nil
}

func writeCanonical(b *strings.Builder, value any) error {
	switch v := value.(type) {
	case map[string]any:
		keys := make([]string, 0, len(v))
		for key := range v {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		b.WriteByte('{')
		for i, key := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := writeCanonicalString(b, key); err != nil {
				return err
			}
			b.WriteByte(':')
			if err := writeCanonical(b, v[key]); err != nil {
				return err
			}
		}
		b.WriteByte('}')
	case []any:
		b.WriteByte('[')
		for i, item := range v {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := writeCanonical(b, item); err != nil {
				return err
			}
		}
		b.WriteByte(']')
	case string:
		return writeCanonicalString(b, v)
	case nil:
		b.WriteString("null")
	case bool:
		if v {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case json.Number:
		b.WriteString(v.String())
	default:
		return fmt.Errorf("spec 里出现了预期之外的类型 %T", value)
	}
	return nil
}

// writeCanonicalString 复刻 Python json 的字符串转义：非 ASCII 原样输出，
// 控制字符与 " \ 按 JSON 规则转义。Go 的 encoding/json 会额外转义 < > &，
// 因此不能直接用它。
func writeCanonicalString(b *strings.Builder, s string) error {
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			if r < 0x20 {
				fmt.Fprintf(b, `\u%04x`, r)
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
	return nil
}

func parse(raw []byte) (Spec, error) {
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	// UseNumber：指纹算法要求数字原样保留，float64 往返会把 1 变成 1.0
	decoder.UseNumber()
	var parsed any
	if err := decoder.Decode(&parsed); err != nil {
		return nil, err
	}
	doc, ok := parsed.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("spec 顶层不是对象")
	}
	if _, hasPaths := doc["paths"]; !hasPaths {
		return nil, fmt.Errorf("内容不是 OpenAPI spec（缺 paths）")
	}
	return Spec(doc), nil
}

// LoadBaseline 读取内置基线 spec（或 MOVIECLAW_SPEC_FILE 指定的替代文件）。
func LoadBaseline() (Spec, error) {
	if path := os.Getenv(EnvSpecFile); path != "" {
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil, clierr.Usagef("%s 指定的 spec 读不到：%s（%v）", EnvSpecFile, path, err).
				WithHint("检查该路径，或去掉这个环境变量改用内置基线")
		}
		doc, err := parse(raw)
		if err != nil {
			return nil, clierr.Usagef("%s 指定的 spec 无法解析：%v", EnvSpecFile, err)
		}
		return doc, nil
	}
	doc, err := parse(baseline)
	if err != nil {
		return nil, clierr.Usagef("内置基线 spec 损坏：%v", err).
			WithHint("重新安装 movieclaw CLI")
	}
	return doc, nil
}

// LoadActive 装载生效 spec：目标服务器有刷新缓存则优先，否则内置基线。
//
// 返回 (spec, 是否来自缓存)。缓存损坏时静默回退基线（下次偏斜检测会重新触发
// 刷新，自愈）。同时记录指纹供偏斜比对。
func LoadActive(server string) (Spec, bool, error) {
	if server != "" {
		path := cachePath(server)
		if raw, err := os.ReadFile(path); err == nil {
			if doc, err := parse(raw); err == nil {
				ActiveHash, _ = Hash(doc)
				return doc, true, nil
			}
			os.Remove(path)
		}
	}
	doc, err := LoadBaseline()
	if err != nil {
		return nil, false, err
	}
	ActiveHash, _ = Hash(doc)
	return doc, false, nil
}

func cacheDir() string { return filepath.Join(config.Dir(), "spec-cache") }

func cachePath(server string) string {
	sum := sha256.Sum256([]byte(strings.TrimRight(server, "/")))
	return filepath.Join(cacheDir(), hex.EncodeToString(sum[:])[:16]+".json")
}

func badMarkerPath(server string) string {
	return strings.TrimSuffix(cachePath(server), ".json") + ".bad"
}

// MarkBad 在缓存 spec 建树失败时调用：删掉坏缓存并登记指纹，阻止无限重拉，
// 等 CLI 升级后自然解除。
func MarkBad(server, hash string) {
	os.Remove(cachePath(server))
	if hash == "" {
		return
	}
	_ = os.MkdirAll(cacheDir(), 0o700)
	_ = os.WriteFile(badMarkerPath(server), []byte(hash), 0o600)
}

func knownBadHash(server string) string {
	raw, err := os.ReadFile(badMarkerPath(server))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(raw))
}

// Fetcher 抽象「拉取服务端 /spec」，便于测试注入。
type Fetcher func(server string) (Spec, error)

// MaybeRefresh 是命令执行完后的偏斜检查：服务端指纹 ≠ 本地装载指纹 → 拉取
// /spec 写缓存。
//
// 刷新只影响**下次调用**（本次命令树已构建完成）；失败静默跳过（下次再试），
// 不影响本次命令的结果与退出码。
func MaybeRefresh(seenHash, server string, fetch Fetcher, notify func(string)) {
	if seenHash == "" || server == "" || seenHash == ActiveHash {
		return
	}
	if seenHash == knownBadHash(server) {
		// 该版本的服务端 spec 已确认本版生成器处理不了：不再重复拉取，静默维持
		// 内置基线，直到服务端再次升级（指纹变化）或 CLI 升级
		return
	}
	doc, err := fetch(server)
	if err != nil || doc == nil {
		return
	}
	if _, ok := doc["paths"]; !ok {
		return
	}
	encoded, err := json.Marshal(map[string]any(doc))
	if err != nil {
		return
	}
	if err := os.MkdirAll(cacheDir(), 0o700); err != nil {
		return
	}
	if err := os.WriteFile(cachePath(server), encoded, 0o600); err != nil {
		return
	}
	if notify != nil {
		notify("提示：服务器接口目录已更新，命令目录将在下次调用时刷新生效")
	}
}

// GuessServerForStartup 在标志解析前尽力猜出目标服务器，用于选择缓存 spec。
//
// 只做无副作用的轻量解析：--server 标志 > 环境变量 > 配置文件当前上下文。
// 猜不出（或配置损坏）返回空串，走内置基线——功能不受影响。
func GuessServerForStartup(argv []string) string {
	context := ""
	for i, arg := range argv {
		switch {
		case arg == "--server" && i+1 < len(argv):
			return strings.TrimRight(argv[i+1], "/")
		case strings.HasPrefix(arg, "--server="):
			return strings.TrimRight(strings.TrimPrefix(arg, "--server="), "/")
		case arg == "--context" && i+1 < len(argv):
			context = argv[i+1]
		case strings.HasPrefix(arg, "--context="):
			context = strings.TrimPrefix(arg, "--context=")
		}
	}
	server, err := config.ResolveServer("", context)
	if err != nil {
		return ""
	}
	return server
}
