package overlay

import (
	"fmt"
	"net/http"
	"os"
	"time"

	"github.com/spf13/cobra"
	"github.com/yipengfei329/movieclaw/cli/internal/clierr"
	"github.com/yipengfei329/movieclaw/cli/internal/config"
	"github.com/yipengfei329/movieclaw/cli/internal/jsonval"
	"github.com/yipengfei329/movieclaw/cli/internal/output"
	"github.com/yipengfei329/movieclaw/cli/internal/spec"
	"golang.org/x/term"
)

// maxWaitSeconds 是轮询兑换的兜底上限。服务端会下发 expires_in，这里只防
// 「服务端给了个离谱的大数」导致命令挂死——CLI 的零交互原则不允许无限等待。
const maxWaitSeconds = 15 * 60

// stdinIsTTY 供测试覆盖。
var stdinIsTTY = func() bool { return term.IsTerminal(int(os.Stdin.Fd())) }

// sleep 供测试跳过真实等待。
var sleep = time.Sleep

// NewLoginCommand 构造 `mclaw login`。
//
// 走设备授权流程（docs/design/device-auth.md §6.1）：CLI 出示一段配对码，人在
// 浏览器里核对并批准，令牌通过兑换直接回到本进程——**从不显示在屏幕上，也就不会
// 进剪贴板、shell 历史或 Agent 的上下文**。
//
// 刻意没有密码登录：密码只在浏览器里用。这条不是洁癖——一旦 CLI 接受
// --password，第三方 Agent 替用户跑命令时管理员密码就会进模型上下文。
func NewLoginCommand() *cobra.Command {
	var serverFlag, nameFlag string
	cmd := &cobra.Command{
		Use:   "login",
		Short: "配对本机并保存授权",
		Long: `把这台机器配对到 movieclaw，并保存授权。

示例：

    mclaw login --server http://192.168.1.10:3000

命令会显示一段配对码，请在浏览器里打开 movieclaw 的「设置 → 设备」，
核对配对码后批准。配对成功后服务器地址会记入当前上下文，之后的命令
无需再指定 --server。`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			s := SettingsOf(cmd)
			if serverFlag != "" {
				s.Server = serverFlag
			}
			return runLogin(s, nameFlag)
		},
	}
	cmd.Flags().StringVar(&serverFlag, "server", "", "movieclaw 服务器地址，如 http://192.168.1.10:3000")
	cmd.Flags().StringVar(&nameFlag, "name", "", "本机在网页设备列表里的名字，默认 mclaw@<主机名>")
	return cmd
}

func runLogin(s *Settings, clientName string) error {
	target, err := s.ResolveServer()
	if err != nil {
		return err
	}
	// 零交互原则（docs/design/cli.md §5.1）：配对必须有人在浏览器里点批准，
	// 非 TTY 下执行它只会静默挂到超时。与其挂住，不如立刻说清该怎么办。
	if !stdinIsTTY() {
		return clierr.Usagef("非交互环境无法完成配对：配对需要有人在浏览器里批准").
			WithHint("请在有终端的机器上执行 mclaw login；无人值守场景（CI / 容器）" +
				"改为在网页「设置 → 设备」创建令牌后，用环境变量 MOVIECLAW_TOKEN 注入")
	}

	client, err := s.NewAPIFor(target)
	if err != nil {
		return err
	}
	// 验证这一步的职责是「地址对不对、连不连得上」，用 /health 返回的 service 名
	// 回答即可。刻意不显示版本号：/health 是匿名端点，为一句文案而向未登录者
	// 公开精确版本，对自部署用户不是好交易。
	health, err := client.Request(http.MethodGet, "/health", nil, nil)
	if err != nil {
		return err
	}
	output.Info("✓ 已连接 %s", stringField(health, "service", "服务"))

	if clientName == "" {
		clientName = defaultClientName()
	}
	grant, err := client.Request(http.MethodPost, "/auth/device/authorize", nil, map[string]string{
		"client_type": "cli",
		"client_name": clientName,
	})
	if err != nil {
		return err
	}
	userCode := stringField(grant, "user_code", "")
	deviceCode := stringField(grant, "device_code", "")
	if userCode == "" || deviceCode == "" {
		return clierr.New("服务器返回的配对回执缺少必要字段").
			WithHint("服务器版本可能过旧，请升级 movieclaw")
	}
	output.Info("")
	output.Info("请在浏览器打开：%s", stringField(grant, "verification_uri", target+"/settings/devices"))
	output.Info("核对配对码：      %s", userCode)
	output.Info("")

	token, err := awaitGrant(s, target, grant, deviceCode)
	if err != nil {
		return err
	}
	if err := config.SaveToken(target, stringField(token, "token", "")); err != nil {
		return err
	}
	if err := config.SaveContext(target, "default"); err != nil {
		return err
	}
	output.Info("✓ 已授权：%s", stringField(token, "client_name", clientName))
	output.Info("  凭证已写入 %s（仅本用户可读）", config.CredentialsPath())
	return nil
}

// awaitGrant 按服务端下发的节奏轮询兑换，直到拿到令牌或得到确定的失败结论。
//
// 三种终态都不重试：被拒绝、已过期、配对码不存在——继续轮询只会刷屏。
func awaitGrant(s *Settings, server string, grant any, deviceCode string) (any, error) {
	interval := intField(grant, "interval", 2)
	if interval < 1 {
		interval = 1
	}
	expires := intField(grant, "expires_in", 300)
	if expires > maxWaitSeconds {
		expires = maxWaitSeconds
	}
	deadline := time.Now().Add(time.Duration(expires) * time.Second)
	output.Info("等待批准…（在浏览器里点「批准接入」后本命令会自动继续）")

	for time.Now().Before(deadline) {
		sleep(time.Duration(interval) * time.Second)
		client, err := s.NewAPIFor(server)
		if err != nil {
			return nil, err
		}
		data, status, err := client.RequestRaw(
			http.MethodPost, "/auth/device/token", nil,
			map[string]string{"device_code": deviceCode},
		)
		if err != nil {
			// 服务端把「已拒绝 / 已过期 / 不存在」统一成 400，都是终态
			var cliErr *clierr.Error
			if asCliError(err, &cliErr) && cliErr.ExitCode == clierr.Business {
				return nil, clierr.Authf("%s", cliErr.Message).
					WithHint("重新执行 mclaw login 发起新的配对")
			}
			return nil, err
		}
		if status == http.StatusOK && data != nil {
			return data, nil
		}
		// 202 等待批准、429 轮询过快：后者按服务端要求退避一拍
		if status == http.StatusTooManyRequests {
			interval++
		}
	}
	return nil, clierr.Authf("配对超时：没有等到批准").
		WithHint("确认已在浏览器的「设置 → 设备」里批准，然后重新执行 mclaw login")
}

// NewLogoutCommand 构造 `mclaw logout`。
func NewLogoutCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "logout",
		Short: "删除本机保存的授权",
		Long: `删除本机保存的令牌。

这只清本地凭证，不会吊销服务端的令牌——吊销是人在浏览器里的动作
（docs/design/device-auth.md §4.4）。想彻底停用这台机器，请到网页的
「设置 → 设备」里吊销它。`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			s := SettingsOf(cmd)
			target, err := s.ResolveServer()
			if err != nil {
				return err
			}
			if err := config.DeleteToken(target); err != nil {
				return err
			}
			output.Info("已删除本机保存的授权：%s", target)
			output.Info("提示：服务端的令牌仍然有效。要彻底停用这台机器，请到网页「设置 → 设备」里吊销。")
			return nil
		},
	}
}

// NewStatusCommand 构造 `mclaw status`。
func NewStatusCommand() *cobra.Command {
	var outputFlag string
	cmd := &cobra.Command{
		Use:   "status",
		Short: "查看服务器与授权状态",
		Long: `一眼看部署状态：服务健康、当前身份、凭证来源、spec 版本偏斜。

credential 这一项是排障的关键：「我明明配对过了」十次里有九次是因为 $HOME
不同（sudo / launchd / systemd / 容器）读到了别的配置目录。把凭证到底从哪儿
来的打出来，一眼就能看出问题。`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			s := SettingsOf(cmd)
			target, err := s.ResolveServer()
			if err != nil {
				return err
			}
			client, err := s.NewAPIFor(target)
			if err != nil {
				return err
			}
			health, err := client.Request(http.MethodGet, "/health", nil, nil)
			if err != nil {
				return err
			}
			identity := "未授权（mclaw login）"
			if me, err := client.Request(http.MethodGet, "/auth/me", nil, nil); err == nil {
				if nickname := stringField(me, "nickname", ""); nickname != "" {
					identity = nickname
				} else if username := stringField(me, "username", ""); username != "" {
					identity = username
				} else {
					identity = "已授权"
				}
			} else {
				var cliErr *clierr.Error
				if !asCliError(err, &cliErr) || cliErr.ExitCode != clierr.Auth {
					return err
				}
			}

			credential := "无（mclaw login）"
			if os.Getenv(config.EnvToken) != "" {
				credential = "环境变量 " + config.EnvToken
			} else if token, _ := config.LoadToken(target); token != "" {
				credential = config.CredentialsPath()
			}

			serverHash := stringField(health, "spec_hash", "")
			// 字段顺序即阅读顺序：先「连的是哪台、活着吗」，再「我是谁、凭证在哪」，
			// 最后才是 spec 指纹这类排障细节。
			var inSync any
			if serverHash != "" {
				inSync = serverHash == spec.ActiveHash
			}
			payload := jsonval.NewMap(
				"server", target,
				"service", stringField(health, "service", ""),
				"status", stringField(health, "status", ""),
				"environment", stringField(health, "environment", ""),
				"identity", identity,
				"credential", credential,
				"cli_spec_hash", spec.ActiveHash,
				"server_spec_hash", serverHash,
				"spec_in_sync", inSync,
			)
			format := outputFlag
			if format == "" {
				format = s.Output
			}
			return output.Emit(payload, format, s.Quiet)
		},
	}
	cmd.Flags().StringVarP(&outputFlag, "output", "o", "", "输出格式（覆盖全局设置）")
	return cmd
}

// defaultClientName 返回 mclaw@<主机名>。
//
// 它会显示在网页的审批卡和设备列表上，是用户判断「这是不是我那台机器」以及
// 日后决定吊销哪台的依据，所以要能认得出来。
func defaultClientName() string {
	host, err := os.Hostname()
	if err != nil || host == "" {
		host = "unknown-host"
	}
	return fmt.Sprintf("mclaw@%s", host)
}

func stringField(data any, key, fallback string) string {
	if value := jsonval.Str(jsonval.Object(data).Get(key)); value != "" {
		return value
	}
	return fallback
}

func intField(data any, key string, fallback int) int {
	value := jsonval.Object(data).Get(key)
	if value == nil {
		return fallback
	}
	return jsonval.Int(value)
}
