package overlay

import (
	"time"

	"github.com/spf13/pflag"
	"github.com/yipengfei329/movieclaw/cli/internal/flagx"
)

// Overrides 是允许写在子命令尾部的全局标志（kubectl 惯例：
// `mclaw subscriptions list -o json`）。根命令上的同名标志依然有效，
// 叶子上的取值优先。
type Overrides struct {
	Output  string
	Server  string
	Timeout time.Duration
	Quiet   bool
	Debug   bool
	Yes     bool
}

// Register 把覆盖标志注册到子命令。taken 里的名字已被 API 参数占用——
// API 参数优先，重名时不注册覆盖标志。
func (o *Overrides) Register(flags *pflag.FlagSet, taken map[string]bool) {
	if !taken["output"] {
		flags.StringVarP(&o.Output, "output", "o", "", "输出格式（覆盖全局设置）")
	}
	if !taken["server"] {
		flags.StringVar(&o.Server, "server", "", "服务器地址（覆盖全局设置）")
	}
	if !taken["timeout"] {
		flagx.Var(flags, &o.Timeout, "timeout", 0, "请求超时（秒，覆盖全局设置）")
	}
	if !taken["quiet"] {
		flags.BoolVar(&o.Quiet, "quiet", false, "成功时不输出数据")
	}
	if !taken["debug"] {
		flags.BoolVar(&o.Debug, "debug", false, "打印调试信息到 stderr")
	}
	if !taken["yes"] {
		flags.BoolVar(&o.Yes, "yes", false, "跳过确认提示")
	}
}

// Merge 把叶子上显式给出的覆盖值合并进全局 Settings，返回新副本。
func (o *Overrides) Merge(base *Settings, flags *pflag.FlagSet) *Settings {
	merged := *base
	if flags.Changed("output") && o.Output != "" {
		merged.Output = o.Output
	}
	if flags.Changed("server") && o.Server != "" {
		merged.Server = o.Server
	}
	if flags.Changed("timeout") && o.Timeout > 0 {
		merged.Timeout = o.Timeout
	}
	if flags.Changed("quiet") && o.Quiet {
		merged.Quiet = true
	}
	if flags.Changed("debug") && o.Debug {
		merged.Debug = true
	}
	if flags.Changed("yes") && o.Yes {
		merged.Yes = true
	}
	return &merged
}
