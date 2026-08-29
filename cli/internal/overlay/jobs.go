package overlay

import (
	"time"

	"github.com/spf13/cobra"
	"github.com/yipengfei329/movieclaw/cli/internal/flagx"
	"github.com/yipengfei329/movieclaw/cli/internal/output"
	"github.com/yipengfei329/movieclaw/cli/internal/wait"
)

// NewJobsGroup 构造 `mclaw jobs` 组；生成层随后并入 list / show / cancel 等命令。
func NewJobsGroup() *cobra.Command {
	group := &cobra.Command{
		Use:          "jobs",
		Short:        "后台作业查询、事件、等待、取消与重试",
		Long:         "后台作业查询、事件、等待、取消与重试",
		RunE:         func(cmd *cobra.Command, _ []string) error { return cmd.Help() },
		SilenceUsage: true,
	}
	group.AddCommand(newJobsWaitCommand())
	return group
}

// newJobsWaitCommand 构造 `mclaw jobs wait`：持续等待一个持久化任务到终态。
func newJobsWaitCommand() *cobra.Command {
	var waitTimeout time.Duration
	cmd := &cobra.Command{
		Use:   "wait <job_id>",
		Short: "等待后台任务完成（断点状态由服务端持久化）",
		Long: `等待任务到成功、失败、取消或需要人工处理；Ctrl-C 不会取消任务。

示例：

    mclaw jobs wait <job_id>`,
		Args: cobra.ExactArgs(1),
	}
	flagx.Var(cmd.Flags(), &waitTimeout, "wait-timeout", time.Hour,
		"最长等待秒数；超时只停止等待，不取消任务")
	return withOverrides(cmd, []string{"wait-timeout"},
		func(s *Settings, _ *cobra.Command, args []string) error {
			client, err := s.NewAPI()
			if err != nil {
				return err
			}
			if err := wait.Job(client, args[0], waitTimeout); err != nil {
				return err
			}
			// 等到终态后再取一次完整任务：stdout 给出的是结果，不是过程
			final, err := client.Request("GET", "/jobs/"+args[0], nil, nil)
			if err != nil {
				return err
			}
			return output.Emit(final, s.Output, s.Quiet)
		})
}
