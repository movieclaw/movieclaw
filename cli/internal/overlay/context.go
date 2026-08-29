// Package overlay 承载精选命令（docs/design/cli.md §7）：跨接口的工作流。
//
// 准入标准：需要客户端编排或本地状态才收进精选层；单接口的便利包装一律不收
// （那是生成层 + x-cli 元数据该解决的事）。
package overlay

import (
	"time"

	"github.com/spf13/cobra"
	"github.com/yipengfei329/movieclaw/cli/internal/api"
	"github.com/yipengfei329/movieclaw/cli/internal/config"
)

// Settings 是一次调用的全局标志集合，经 cobra 的 context 传给所有命令。
type Settings struct {
	Server  string
	Context string
	Output  string
	Timeout time.Duration
	Debug   bool
	Quiet   bool
	Yes     bool
}

type settingsKey struct{}

// WithSettings 把全局标志挂到 context 上。
func WithSettings(cmd *cobra.Command, s *Settings) {
	cmd.SetContext(contextWith(cmd, s))
}

// SettingsOf 取回全局标志；根命令一定挂过，取不到说明装配有误。
func SettingsOf(cmd *cobra.Command) *Settings {
	if s, ok := cmd.Context().Value(settingsKey{}).(*Settings); ok {
		return s
	}
	return &Settings{Timeout: 30 * time.Second}
}

// ResolveServer 按优先级解析目标服务器地址。
func (s *Settings) ResolveServer() (string, error) {
	return config.ResolveServer(s.Server, s.Context)
}

// NewAPI 为解析出的服务器构造客户端。
func (s *Settings) NewAPI() (*api.Client, error) {
	server, err := s.ResolveServer()
	if err != nil {
		return nil, err
	}
	return s.NewAPIFor(server)
}

// NewAPIFor 为指定服务器构造客户端（login 已知目标时用它，跳过上下文解析）。
func (s *Settings) NewAPIFor(server string) (*api.Client, error) {
	return api.New(server, s.Timeout, s.Debug)
}
