package overlay

import (
	"fmt"
	"net/url"
	"os"
	"os/signal"
	"time"

	"github.com/spf13/cobra"
	"github.com/yipengfei329/movieclaw/cli/internal/api"
	"github.com/yipengfei329/movieclaw/cli/internal/clierr"
	"github.com/yipengfei329/movieclaw/cli/internal/flagx"
	"github.com/yipengfei329/movieclaw/cli/internal/jsonval"
	"github.com/yipengfei329/movieclaw/cli/internal/output"
)

// followTailLines 是 -f 每轮拉取的行数上限。取得比屏幕宽裕得多，让轮询间隔
// 内新增的行都能落在这一屏里；真超了会明说漏了多少行，不假装完整。
const followTailLines = 2000

// NewLogsGroup 构造 `mclaw logs` 组；生成层随后并入 logs days / logs read。
func NewLogsGroup() *cobra.Command {
	group := &cobra.Command{
		Use:          "logs",
		Short:        "系统日志查看与实时跟随",
		Long:         "系统日志查看与实时跟随",
		RunE:         func(cmd *cobra.Command, _ []string) error { return cmd.Help() },
		SilenceUsage: true,
	}
	group.AddCommand(newLogsTailCommand())
	return group
}

// newLogsTailCommand 构造 `mclaw logs tail`。
//
// 服务端只有「按天读全量/tail」的接口，follow 用轮询模拟：按行数差值只打印
// 新增部分（与页面「自动刷新」同语义）。
func newLogsTailCommand() *cobra.Command {
	var day string
	var lines int
	var follow bool
	var interval time.Duration

	cmd := &cobra.Command{
		Use:   "tail",
		Short: "查看系统日志尾部，-f 持续跟随",
		Long: `查看服务端日志。

示例：

    mclaw logs tail --lines 100

    mclaw logs tail -f                 # 类 tail -f，Ctrl-C 退出`,
		Args: cobra.NoArgs,
	}
	cmd.Flags().StringVar(&day, "day", "", "日期（YYYY-MM-DD）；缺省取最新一天")
	cmd.Flags().IntVar(&lines, "lines", 50, "初始输出的行数")
	cmd.Flags().BoolVarP(&follow, "follow", "f", false, "持续跟随新日志（Ctrl-C 退出）")
	flagx.Var(cmd.Flags(), &interval, "interval", 3*time.Second, "跟随的轮询间隔（秒）")

	return withOverrides(cmd, []string{"day", "lines", "follow", "interval"},
		func(s *Settings, _ *cobra.Command, _ []string) error {
			client, err := s.NewAPI()
			if err != nil {
				return err
			}
			autoDay := day == ""
			if autoDay {
				if day, err = latestLogDay(client); err != nil {
					return err
				}
			}
			data, err := readLogDay(client, day, lines)
			if err != nil {
				return err
			}
			for _, line := range jsonval.Array(data.Get("lines")) {
				fmt.Println(jsonval.Str(line))
			}
			if !follow {
				return nil
			}
			return followLogs(client, day, autoDay, interval, jsonval.Int(data.Get("total_lines")))
		})
}

// followLogs 轮询同一天的日志，按 total_lines 差值补打新增部分。
//
// 未显式指定日期时每轮复核最新日期，跨零点自动切到新一天的日志文件——
// 半夜盯着一个不再增长的文件是这类命令最典型的坑。
func followLogs(
	client *api.Client, day string, autoDay bool, interval time.Duration, seenTotal int,
) error {
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt)
	defer signal.Stop(sig)

	timer := time.NewTicker(interval)
	defer timer.Stop()
	for {
		select {
		case <-sig:
			output.Info("（已停止跟随）")
			return nil
		case <-timer.C:
		}
		if autoDay {
			current, err := latestLogDay(client)
			if err != nil {
				return err
			}
			if current != day {
				output.Info("（已跨天，切换到 %s 的日志）", current)
				day, seenTotal = current, 0
			}
		}
		data, err := readLogDay(client, day, followTailLines)
		if err != nil {
			var cliErr *clierr.Error
			if asCliError(err, &cliErr) && cliErr.ExitCode == clierr.Business {
				// 日志文件被超期清理等业务错误：如实说明后停止跟随，不算失败
				output.Info("（跟随中止：%s）", cliErr.Message)
				return nil
			}
			return err
		}
		total := jsonval.Int(data.Get("total_lines"))
		fetched := jsonval.Array(data.Get("lines"))
		if total <= seenTotal {
			continue
		}
		fresh := total - seenTotal
		if fresh > len(fetched) {
			output.Info("（两轮之间新增 %d 行，超出单次拉取上限，中间 %d 行未显示）",
				fresh, fresh-len(fetched))
			fresh = len(fetched)
		}
		for _, line := range fetched[len(fetched)-fresh:] {
			fmt.Println(jsonval.Str(line))
		}
		seenTotal = total
	}
}

func latestLogDay(client *api.Client) (string, error) {
	data, err := client.Request("GET", "/system/logs", nil, nil)
	if err != nil {
		return "", err
	}
	items := jsonval.Array(jsonval.At(data, "days"))
	if len(items) == 0 {
		return "", clierr.New("还没有任何日志文件").WithHint("服务运行后会按天产生日志")
	}
	if entry := jsonval.Object(items[0]); entry != nil {
		return jsonval.Str(entry.Get("day")), nil
	}
	return jsonval.Str(items[0]), nil
}

func readLogDay(client *api.Client, day string, tail int) (*jsonval.Map, error) {
	data, err := client.Request("GET", "/system/logs/"+day,
		url.Values{"tail": {fmt.Sprint(tail)}}, nil)
	if err != nil {
		return nil, err
	}
	return jsonval.Object(data), nil
}
