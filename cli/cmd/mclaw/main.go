// Command mclaw 是 movieclaw 的命令行客户端。
//
// 命令面两层（docs/design/cli.md §1）：
//   - 精选命令（overlay）：login / logout / status / search / download 等跨接口
//     工作流，先注册；
//   - 生成命令（gen）：由 spec（内置基线，或偏斜刷新后的服务器缓存）动态构建，
//     后挂载，同名让位于精选层。缓存 spec 建树失败自动回退内置基线。
package main

import (
	"fmt"
	"net/http"
	"os"
	"time"

	"github.com/spf13/cobra"
	"github.com/yipengfei329/movieclaw/cli/internal/api"
	"github.com/yipengfei329/movieclaw/cli/internal/clierr"
	"github.com/yipengfei329/movieclaw/cli/internal/flagx"
	"github.com/yipengfei329/movieclaw/cli/internal/jsonval"
	"github.com/yipengfei329/movieclaw/cli/internal/output"
	"github.com/yipengfei329/movieclaw/cli/internal/overlay"
	"github.com/yipengfei329/movieclaw/cli/internal/spec"
	"github.com/yipengfei329/movieclaw/cli/internal/tree"
)

const rootLong = `movieclaw 命令行工具：发现电影/剧集、搜索和下载 PT 资源、订阅追更、管理本地媒体库；覆盖页面的主要业务流程和管理设置。

探索方式：mclaw <域> --help 看该域全部命令，mclaw <域> <命令> --help 看参数与示例。
机器输出：加 -o json（非终端环境默认即 JSON）。`

func main() {
	os.Exit(run())
}

func run() int {
	settings := &overlay.Settings{Timeout: 30 * time.Second}
	root := newRootCommand(settings)

	// 装配失败（如 spec 损坏）不能裸抛：统一走中文错误格式
	assembleErr := assemble(root, settings)

	exitCode := 0
	if assembleErr != nil {
		exitCode = report(assembleErr)
	} else {
		root.SetArgs(os.Args[1:])
		if err := root.Execute(); err != nil {
			exitCode = report(err)
		}
	}

	// 版本偏斜检查：服务端 spec 指纹变了就拉新缓存（下次调用生效）。
	// 放在退出码结算之后，绝不影响本次命令的结果。
	spec.MaybeRefresh(api.LastSeenSpecHash, api.LastSeenServer, func(server string) (spec.Spec, error) {
		client, err := api.New(server, settings.Timeout, settings.Debug)
		if err != nil {
			return nil, err
		}
		data, err := client.Request(http.MethodGet, "/spec", nil, nil)
		if err != nil {
			return nil, err
		}
		// 生成层读的是标准库形态的 map（内置基线 spec 也走那条路），
		// 这里把 api 层的保序对象还原回去
		doc, ok := jsonval.Plainify(data).(map[string]any)
		if !ok {
			return nil, fmt.Errorf("响应不是 OpenAPI spec")
		}
		return spec.Spec(doc), nil
	}, func(message string) { output.Info("%s", message) })

	return exitCode
}

func newRootCommand(settings *overlay.Settings) *cobra.Command {
	root := &cobra.Command{
		Use:           "mclaw",
		Short:         "movieclaw 命令行工具",
		Long:          rootLong,
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	flags := root.PersistentFlags()
	flags.StringVar(&settings.Server, "server", "",
		"movieclaw 服务器地址（优先级最高，覆盖上下文与环境变量）")
	flags.StringVar(&settings.Context, "context", "",
		"使用指定上下文（见 ~/.config/movieclaw/config.toml）")
	flags.StringVarP(&settings.Output, "output", "o", "",
		"输出格式；缺省 TTY 为 table、管道/Agent 为 json")
	flagx.Var(flags, &settings.Timeout, "timeout", 30*time.Second, "请求超时（秒；也接受 90s、5m 这种带单位的写法）")
	flags.BoolVar(&settings.Debug, "debug", false, "打印请求/响应调试信息到 stderr（凭证自动打码）")
	flags.BoolVar(&settings.Quiet, "quiet", false, "成功时不输出数据（配合退出码使用）")
	flags.BoolVar(&settings.Yes, "yes", false, "跳过确认提示（破坏性操作需要显式给出）")

	// 关掉 cobra 自带的 help / completion 子命令：命令面越小模型的选择越稳
	// （docs/design/cli.md §1「less is more」），补全另行提供。
	root.SetHelpCommand(&cobra.Command{Hidden: true, Use: "no-help"})
	root.CompletionOptions.DisableDefaultCmd = true
	overlay.WithSettings(root, settings)
	return root
}

// assemble 装配命令树：精选命令先注册（同名让位机制），生成命令后挂载
// （缓存 spec 建树失败回退内置基线）。
func assemble(root *cobra.Command, settings *overlay.Settings) error {
	overlay.Register(root)

	server := spec.GuessServerForStartup(os.Args[1:])
	doc, fromCache, err := spec.LoadActive(server)
	if err != nil {
		return err
	}
	if err := tree.Build(root, doc); err != nil {
		if !fromCache {
			return err
		}
		// 刷新缓存里的新 spec 含本版生成器不认识的形态：回退内置基线，并登记
		// 坏指纹——否则每次调用结束后偏斜检查都会重拉一遍同一份坏 spec
		badHash := spec.ActiveHash
		output.Info("提示：服务器接口目录较新，部分命令暂不可用，已回退内置命令目录；建议升级 CLI")
		doc, _, err = spec.LoadActive("")
		if err != nil {
			return err
		}
		if err := tree.Build(root, doc); err != nil {
			return err
		}
		if server != "" {
			spec.MarkBad(server, badHash)
		}
	}
	_ = settings
	return nil
}

// report 把错误按统一格式写到 stderr 并返回退出码。
func report(err error) int {
	var cliErr *clierr.Error
	if ok := asCli(err, &cliErr); ok {
		prefix := "错误"
		if cliErr.Code != "" {
			prefix = fmt.Sprintf("错误[%s]", cliErr.Code)
		}
		fmt.Fprintf(os.Stderr, "%s：%s\n", prefix, cliErr.Message)
		if cliErr.Hint != "" {
			fmt.Fprintf(os.Stderr, "提示：%s\n", cliErr.Hint)
		}
		if cliErr.Details != nil {
			fmt.Fprintf(os.Stderr, "详情：%v\n", cliErr.Details)
		}
		return int(cliErr.ExitCode)
	}
	// cobra 的参数解析错误 → 退出码 2（用法错误）
	fmt.Fprintf(os.Stderr, "错误：%v\n", err)
	return int(clierr.Usage)
}
