package overlay

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"time"

	"github.com/spf13/cobra"
	"github.com/yipengfei329/movieclaw/cli/internal/api"
	"github.com/yipengfei329/movieclaw/cli/internal/clierr"
	"github.com/yipengfei329/movieclaw/cli/internal/jsonval"
	"github.com/yipengfei329/movieclaw/cli/internal/output"
	"github.com/yipengfei329/movieclaw/cli/internal/sse"
)

// terminalEvents 是一次模型处理的终态事件；读到即停止跟随。
var terminalEvents = map[string]bool{
	"agent_done": true, "agent_error": true, "agent_cancelled": true,
}

// maxReconnects 是事件流断开后的最大续传次数。超过就停手——会话仍在服务端
// 跑，让用户自己决定是接回来还是去看完整轨迹，比无限重连刷屏强。
const maxReconnects = 5

// NewSessionGroup 构造 `mclaw session` 组；生成层随后把其余会话命令并进来。
func NewSessionGroup() *cobra.Command {
	group := &cobra.Command{
		Use:          "session",
		Short:        "AI 会话开始/继续、指定消息重试、SSE 跟随、完整轨迹与上下文管理",
		Long:         "AI 会话开始/继续、指定消息重试、SSE 跟随、完整轨迹与上下文管理",
		RunE:         func(cmd *cobra.Command, _ []string) error { return cmd.Help() },
		SilenceUsage: true,
	}
	group.AddCommand(newSessionStartCommand())
	group.AddCommand(newSessionRetryCommand())
	group.AddCommand(newSessionFollowCommand())
	return group
}

func newSessionStartCommand() *cobra.Command {
	var sessionID, model string
	var detach bool
	cmd := &cobra.Command{
		Use:   "start <提问>",
		Short: "开始新会话或继续已有会话，并实时显示执行过程",
		Long: `提交提问并启动模型处理。

不传 --session-id 时创建新会话；传入时从服务端轨迹重建有效上下文后继续。
默认实时显示模型输出直到终态；--detach 只返回 session_id/message_id，稍后可用
session follow 接回事件流。

示例：

    mclaw session start "整理我的订阅"

    mclaw session start "继续" --session-id <session_id> --detach`,
		Args: cobra.ExactArgs(1),
	}
	cmd.Flags().StringVar(&sessionID, "session-id", "", "已有会话编号（从 session list 获取）；给出时继续该会话")
	addSubmissionFlags(cmd, &model, &detach)
	return withOverrides(cmd, []string{"session-id", "model", "detach"},
		func(s *Settings, _ *cobra.Command, args []string) error {
			body := map[string]any{"content": args[0]}
			if sessionID != "" {
				body["session_id"] = sessionID
			}
			if model != "" {
				body["model"] = model
			}
			return submitAndFollow(s, "/sessions", body, detach)
		})
}

func newSessionRetryCommand() *cobra.Command {
	var messageID, prompt, model string
	var detach bool
	cmd := &cobra.Command{
		Use:   "retry <session_id>",
		Short: "重新提问指定用户消息，并替换该消息及后续轨迹",
		Long: `重新提问 --message-id 指定的消息；现有目标消息及其后的轨迹会被永久删除。

目标必须是 user message。不传 --prompt 时按原文重试；传入时改用新问题。
成功后会生成新的 message_id。默认实时显示新一轮输出；--detach 立即返回。

示例：

    mclaw session retry <session_id> --message-id <message_id>

    mclaw session retry <session_id> --message-id <message_id> --prompt "改写后的问题"`,
		Args: cobra.ExactArgs(1),
	}
	cmd.Flags().StringVar(&messageID, "message-id", "",
		"要重新提问的 user message 编号（先用 get-transcript 查看）")
	cmd.Flags().StringVar(&prompt, "prompt", "", "替换后的问题；留空时按原文重试")
	_ = cmd.MarkFlagRequired("message-id")
	addSubmissionFlags(cmd, &model, &detach)
	return withOverrides(cmd, []string{"message-id", "prompt", "model", "detach"},
		func(s *Settings, _ *cobra.Command, args []string) error {
			body := map[string]any{"message_id": messageID}
			if prompt != "" {
				body["content"] = prompt
			}
			if model != "" {
				body["model"] = model
			}
			return submitAndFollow(s, "/sessions/"+args[0]+"/retry", body, detach)
		})
}

func newSessionFollowCommand() *cobra.Command {
	var fromID int
	cmd := &cobra.Command{
		Use:   "follow <session_id>",
		Short: "跟随会话当前消息的实时事件流（支持断点续传）",
		Long: `接回会话当前或最近一条用户消息的处理事件。

适用于 start/retry --detach 后或网络中断后继续观看；已结束时回放到终态后
立即返回。--from-id 只接收该 SSE 事件序号之后的事件。`,
		Args: cobra.ExactArgs(1),
	}
	cmd.Flags().IntVar(&fromID, "from-id", 0, "从指定事件序号之后续传（缺省回放全部）")
	return withOverrides(cmd, []string{"from-id"},
		func(s *Settings, c *cobra.Command, args []string) error {
			client, err := s.NewAPI()
			if err != nil {
				return err
			}
			cursor := ""
			if c.Flags().Changed("from-id") {
				cursor = strconv.Itoa(fromID)
			}
			event, payload, err := followSession(client, args[0], cursor)
			if err != nil {
				return err
			}
			return finishSession(event, payload)
		})
}

// addSubmissionFlags 是提交消息类动作共用的模型与后台执行选项。
func addSubmissionFlags(cmd *cobra.Command, model *string, detach *bool) {
	cmd.Flags().StringVar(model, "model", "", "模型 ID（缺省用默认供应商的默认模型）")
	cmd.Flags().BoolVar(detach, "detach", false, "提交后立即返回会话与消息编号，不等待模型完成")
}

// submitAndFollow 提交会启动模型处理的会话动作，并按需跟随其事件流。
func submitAndFollow(s *Settings, path string, body map[string]any, detach bool) error {
	client, err := s.NewAPI()
	if err != nil {
		return err
	}
	started, err := client.Request("POST", path, nil, body)
	if err != nil {
		return err
	}
	sessionID := jsonval.Str(jsonval.At(started, "session_id"))
	output.Info("消息已提交：session_id=%s message_id=%s",
		sessionID, jsonval.Str(jsonval.At(started, "message_id")))
	if detach {
		return output.Emit(started, s.Output, s.Quiet)
	}
	event, payload, err := followSession(client, sessionID, "")
	if err != nil {
		return err
	}
	return finishSession(event, payload)
}

// followSession 消费当前用户消息触发的事件流直到终态；断流自动续传。
func followSession(
	client *api.Client, sessionID, lastEventID string,
) (string, *jsonval.Map, error) {
	cursor := lastEventID
	failures := 0
	for {
		var finalEvent string
		var finalPayload *jsonval.Map
		err := streamEvents(client, "/sessions/"+sessionID+"/events", nil, cursor,
			func(event sse.Event, payload *jsonval.Map) bool {
				failures = 0
				if event.HasID {
					cursor = strconv.Itoa(event.ID)
				}
				if terminalEvents[event.Event] {
					finalEvent, finalPayload = event.Event, payload
					return false
				}
				renderSessionEvent(event.Event, payload)
				return true
			})
		if finalEvent != "" {
			return finalEvent, finalPayload, nil
		}
		var cliErr *clierr.Error
		if !asCliError(err, &cliErr) || cliErr.ExitCode != clierr.Network {
			return "", nil, err
		}
		failures++
		if failures > maxReconnects {
			return "", nil, clierr.New("事件流连续断开 %d 次，停止重连（会话可能仍在执行）", failures).
				WithHint("稍后用 mclaw session follow %s 接回，"+
					"或 mclaw session get-transcript %s 查看完整轨迹", sessionID, sessionID)
		}
		delay := min(500*time.Millisecond<<(failures-1), 5*time.Second)
		output.Info("（连接断开，%.1fs 后续传…）", delay.Seconds())
		sleep(delay)
	}
}

// renderSessionEvent 把一帧事件画到终端：正文进 stdout，过程信息进 stderr。
//
// 这条分工是给管道用的：`mclaw session start ... > answer.txt` 应该只拿到
// 模型正文，工具调用与耗时统计不该混进去。
func renderSessionEvent(event string, payload *jsonval.Map) {
	switch event {
	case "agent_start":
		output.Info("[会话开始] %s/%s",
			jsonval.Str(payload.Get("provider")), jsonval.Str(payload.Get("model")))
	case "text_delta":
		fmt.Print(jsonval.Str(payload.Get("delta")))
	case "tool_call":
		call := jsonval.Object(payload.Get("tool_call"))
		args := "{}"
		if encoded, err := json.Marshal(call.Get("arguments")); err == nil {
			args = string(encoded)
		}
		if len([]rune(args)) > 120 {
			args = string([]rune(args)[:117]) + "..."
		}
		output.Info("\n→ 工具 %s：%s", jsonval.Str(call.Get("name")), args)
	case "tool_result":
		result := jsonval.Object(payload.Get("tool_result"))
		mark := "✓"
		if jsonval.Truthy(result.Get("is_error")) {
			mark = "✗"
		}
		output.Info("  %s 完成（%sms）", mark, jsonval.Plain(result.Get("elapsed_ms")))
	case "context_compacted":
		compaction := jsonval.Object(payload.Get("compaction"))
		output.Info("\n[上下文已压缩] %s → %s tokens",
			jsonval.Plain(compaction.Get("tokens_before")), jsonval.Plain(compaction.Get("tokens_after")))
	}
}

// finishSession 是终态渲染与退出码结算：只有 agent_error 算命令失败。
func finishSession(event string, payload *jsonval.Map) error {
	switch event {
	case "agent_done":
		result := jsonval.Object(payload.Get("result"))
		usage := jsonval.Object(result.Get("usage"))
		fmt.Fprintln(os.Stdout)
		output.Info("[完成] %s 步，%sms，tokens in/out=%s/%s",
			jsonval.Plain(result.Get("steps")), jsonval.Plain(result.Get("elapsed_ms")),
			jsonval.Plain(usage.Get("input_tokens")), jsonval.Plain(usage.Get("output_tokens")))
		return nil
	case "agent_cancelled":
		output.Info("\n[已停止] 当前消息的模型处理已停止")
		return nil
	}
	message := jsonval.Str(payload.Get("error"))
	if message == "" {
		message = "AI 会话执行失败"
	}
	return clierr.New("%s", message).
		WithHint("完整过程可用 mclaw session get-transcript <session_id> 回看")
}
