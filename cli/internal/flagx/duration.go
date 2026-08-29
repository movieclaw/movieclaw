// Package flagx 是几个自定义的命令行取值类型。
package flagx

import (
	"fmt"
	"strconv"
	"time"

	"github.com/spf13/pflag"
)

// Duration 是接受「裸数字＝秒」的时长标志。
//
// Go 的 time.ParseDuration 要求带单位（`30s`），但 CLI 的既有契约是秒：
// `--timeout 30`、`--wait-timeout 3600` 这样的写法已经写进脚本和 Agent 的
// 用法里了。两种都收：有单位按 Go 的规则解析（`90s`、`1h30m` 也就跟着能用），
// 没单位当秒。
type Duration struct {
	value *time.Duration
}

// Var 在 flags 上注册一个时长标志。
func Var(flags *pflag.FlagSet, target *time.Duration, name string, value time.Duration, usage string) {
	*target = value
	flags.Var(&Duration{value: target}, name, usage)
}

func (d *Duration) String() string {
	if d.value == nil {
		return "0"
	}
	return d.value.String()
}

// Type 决定 --help 里标志名后面显示什么。写「秒数」而不是 duration：
// 用户看到的第一提示应该是最常用的那种写法。
func (d *Duration) Type() string { return "秒数" }

func (d *Duration) Set(raw string) error {
	if seconds, err := strconv.ParseFloat(raw, 64); err == nil {
		if seconds < 0 {
			return fmt.Errorf("不能是负数：%s", raw)
		}
		*d.value = time.Duration(seconds * float64(time.Second))
		return nil
	}
	parsed, err := time.ParseDuration(raw)
	if err != nil {
		return fmt.Errorf("必须是秒数（如 30）或带单位的时长（如 90s、5m、1h），收到 %q", raw)
	}
	if parsed < 0 {
		return fmt.Errorf("不能是负数：%s", raw)
	}
	*d.value = parsed
	return nil
}
