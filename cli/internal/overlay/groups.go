package overlay

import "github.com/spf13/cobra"

// withOverrides 给精选命令挂上与生成命令同一套全局覆盖标志（`-o json`、
// `--server`、`--yes` 等可以写在命令尾部），并把合并后的 Settings 交给 run。
//
// taken 里的名字已被该命令自己的参数占用，重名时命令自己的参数优先。
func withOverrides(
	cmd *cobra.Command,
	taken []string,
	run func(*Settings, *cobra.Command, []string) error,
) *cobra.Command {
	occupied := map[string]bool{}
	for _, name := range taken {
		occupied[name] = true
	}
	o := &Overrides{}
	o.Register(cmd.Flags(), occupied)
	cmd.SilenceUsage = true
	cmd.RunE = func(c *cobra.Command, args []string) error {
		return run(o.Merge(SettingsOf(c), c.Flags()), c, args)
	}
	return cmd
}

// newDefaultCommandGroup 造一个「首参不是子命令名就转交给默认子命令」的组。
//
// 用于 `mclaw search "沙丘2"`（等价 `mclaw search torrents "沙丘2"`）与
// `mclaw search history list` 在同一个组下共存：搜索是最高频的入口，
// 多敲一个 torrents 是纯粹的摩擦。
//
// 极端情况（关键词恰好叫 history、presets 等子命令名）用显式形态兜底：
// `mclaw search torrents history`。
func newDefaultCommandGroup(name, help, defaultCommand string) *cobra.Command {
	group := &cobra.Command{
		Use:   name,
		Short: help,
		Long:  help,
		// 首参可能是 `--limit` 这样的标志（`mclaw search --limit 5 沙丘`），
		// 那不是组自己的标志，交给默认子命令去解析，组这一层不碰。
		DisableFlagParsing: true,
		SilenceUsage:       true,
	}
	group.RunE = func(cmd *cobra.Command, args []string) error {
		// 关掉标志解析后 -h/--help 也要自己认，否则组的帮助就打不出来了
		if len(args) == 0 || args[0] == "-h" || args[0] == "--help" {
			return cmd.Help()
		}
		target, _, err := cmd.Find([]string{defaultCommand})
		if err != nil {
			return err
		}
		target.SetArgs(args)
		return target.ExecuteContext(cmd.Context())
	}
	return group
}
