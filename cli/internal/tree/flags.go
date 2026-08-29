package tree

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/spf13/pflag"
)

// enumValue 是带候选校验的字符串标志（对应 click.Choice）。
//
// 枚举必须在客户端就挡住：让用户拿到「这里只能填 a/b/c」比让服务端回一句
// 422 有用得多——「错误即帮助」。
type enumValue struct {
	value   string
	allowed []string
}

func (e *enumValue) String() string { return e.value }
func (e *enumValue) Type() string   { return "枚举" }

func (e *enumValue) Set(raw string) error {
	for _, candidate := range e.allowed {
		if raw == candidate {
			e.value = raw
			return nil
		}
	}
	return fmt.Errorf("只能是 %s 之一", strings.Join(e.allowed, " / "))
}

// paramValue 持有一个标志的取值与「是否被显式赋值」。
//
// pflag 没有「未提供」的天然表达（零值与显式传零值不可区分），而 API 的可选
// 参数必须区分——不传就不发送，让服务端用它自己的默认值。这里统一靠
// FlagSet.Changed 判定。
type paramValue struct {
	name    string // 标志名（不含 --）
	kind    string // string | integer | number | boolean | json
	strVal  string
	intVal  int64
	numVal  float64
	boolVal bool
	enum    *enumValue
}

// Value 返回该标志的实际取值（已按类型转换）。
func (p *paramValue) Value() any {
	switch {
	case p.enum != nil:
		return p.enum.value
	case p.kind == "integer":
		return p.intVal
	case p.kind == "number":
		return p.numVal
	case p.kind == "boolean":
		return p.boolVal
	default:
		return p.strVal
	}
}

// register 把该参数注册到 FlagSet。
func (p *paramValue) register(flags *pflag.FlagSet, schema Schema, usage string) {
	flagName := p.name
	if len(schema.Enum) > 0 {
		p.enum = &enumValue{allowed: schema.Enum}
		if def, ok := schema.Default.(string); ok {
			p.enum.value = def
		}
		flags.Var(p.enum, flagName, withCandidates(usage, schema.Enum))
		return
	}
	switch schema.Type {
	case "integer":
		flags.Int64Var(&p.intVal, flagName, int64(defaultFloat(schema)), usage)
		p.kind = "integer"
	case "number":
		flags.Float64Var(&p.numVal, flagName, defaultFloat(schema), usage)
		p.kind = "number"
	case "boolean":
		def, _ := schema.Default.(bool)
		flags.BoolVar(&p.boolVal, flagName, def, usage)
		p.kind = "boolean"
	default:
		def, _ := schema.Default.(string)
		flags.StringVar(&p.strVal, flagName, def, usage)
		p.kind = "string"
	}
}

func withCandidates(usage string, enum []string) string {
	candidates := "可选值：" + strings.Join(enum, " / ")
	if usage == "" {
		return candidates
	}
	return usage + "（" + candidates + "）"
}

func defaultFloat(schema Schema) float64 {
	switch v := schema.Default.(type) {
	case float64:
		return v
	case int:
		return float64(v)
	case string:
		if parsed, err := strconv.ParseFloat(v, 64); err == nil {
			return parsed
		}
	}
	if schema.Default != nil {
		// json.Number 走这里
		if s := fmt.Sprint(schema.Default); s != "" {
			if parsed, err := strconv.ParseFloat(s, 64); err == nil {
				return parsed
			}
		}
	}
	return 0
}

// convertPathArg 把位置参数按 schema 类型转换；转不了就报可读的用法错误。
func convertPathArg(raw string, schema Schema) (any, error) {
	switch schema.Type {
	case "integer":
		parsed, err := strconv.ParseInt(raw, 10, 64)
		if err != nil {
			return nil, fmt.Errorf("必须是整数，收到 %q", raw)
		}
		return parsed, nil
	case "number":
		parsed, err := strconv.ParseFloat(raw, 64)
		if err != nil {
			return nil, fmt.Errorf("必须是数字，收到 %q", raw)
		}
		return parsed, nil
	case "boolean":
		parsed, err := strconv.ParseBool(raw)
		if err != nil {
			return nil, fmt.Errorf("必须是 true 或 false，收到 %q", raw)
		}
		return parsed, nil
	default:
		if len(schema.Enum) > 0 {
			for _, candidate := range schema.Enum {
				if raw == candidate {
					return raw, nil
				}
			}
			return nil, fmt.Errorf("只能是 %s 之一，收到 %q", strings.Join(schema.Enum, " / "), raw)
		}
		return raw, nil
	}
}
