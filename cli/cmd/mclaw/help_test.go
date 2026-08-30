package main

import (
	"strings"
	"testing"
	"time"

	"github.com/movieclaw/movieclaw/cli/internal/config"
	"github.com/movieclaw/movieclaw/cli/internal/overlay"
)

// TestRootHelpDocumentsEnvironmentVariables 校验根帮助里列出了全部环境变量。
//
// 环境变量是无人值守环境唯一走得通的授权通道（那里没人能在浏览器里按批准），
// 但它此前只在两处错误提示里出现过——装在 NAS 上的用户不翻源码就不知道有这条路。
// 帮助里缺哪一个，哪一个就等于不存在。
func TestRootHelpDocumentsEnvironmentVariables(t *testing.T) {
	help := newRootCommand(&overlay.Settings{Timeout: 30 * time.Second}).Long

	for _, name := range []string{
		config.EnvServer, config.EnvToken, config.EnvContext, config.EnvConfigDir,
	} {
		if !strings.Contains(help, name) {
			t.Errorf("根帮助没有提到环境变量 %s", name)
		}
	}
}

// TestRootHelpExplainsPrecedenceAndUnattendedPath 校验帮助不止列了变量名，
// 还回答了用户真正会问的两个问题：谁覆盖谁，以及令牌从哪儿来。
func TestRootHelpExplainsPrecedenceAndUnattendedPath(t *testing.T) {
	help := newRootCommand(&overlay.Settings{Timeout: 30 * time.Second}).Long

	if !strings.Contains(help, "--server > MOVIECLAW_SERVER") {
		t.Error("没说明地址的优先级顺序")
	}
	if !strings.Contains(help, "手工创建令牌") {
		t.Error("没指出无人值守环境去哪儿拿令牌")
	}
	if !strings.Contains(help, "mclaw status") {
		t.Error("没指出排查当前凭证来源的命令")
	}
}
