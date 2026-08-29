package tree

import (
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"github.com/yipengfei329/movieclaw/cli/internal/api"
	"github.com/yipengfei329/movieclaw/cli/internal/clierr"
	"github.com/yipengfei329/movieclaw/cli/internal/flagx"
	"github.com/yipengfei329/movieclaw/cli/internal/jsonval"
	"github.com/yipengfei329/movieclaw/cli/internal/output"
	"github.com/yipengfei329/movieclaw/cli/internal/overlay"
	"github.com/yipengfei329/movieclaw/cli/internal/wait"
	"golang.org/x/term"
)

// stdinIsTTY 供测试覆盖：危险确认在 TTY 下才可能交互。
var stdinIsTTY = func() bool { return term.IsTerminal(int(os.Stdin.Fd())) }

// makeCommand 由一个 spec 操作构造一条命令。
func makeCommand(op Operation, opsByID map[string]Operation) *cobra.Command {
	pathParams := op.PathParams()
	queryParams := op.QueryParams()

	// 每条命令持有自己的一组标志值。cobra 的 RunE 在解析后执行，届时用
	// Flags().Changed 区分「没传」与「传了零值」。
	queryValues := map[string]*paramValue{}
	bodyValues := map[string]*paramValue{}
	var inputFile, uploadFile, outputFile string
	var waitFlag bool
	var waitTimeout time.Duration
	overrides := &overlay.Overrides{}

	segments := strings.Split(op.OperationID, ".")
	cmd := &cobra.Command{
		Use:          useLine(segments[len(segments)-1], pathParams),
		Short:        shortHelp(op),
		Long:         longHelp(op),
		Args:         cobra.ExactArgs(len(pathParams)),
		SilenceUsage: true,
	}

	flags := cmd.Flags()
	for _, p := range queryParams {
		value := &paramValue{name: strings.ReplaceAll(p.Name, "_", "-")}
		value.register(flags, p.Schema, p.Description)
		queryValues[p.Name] = value
	}
	for _, f := range op.BodyFields {
		value := &paramValue{name: strings.TrimPrefix(f.FlagName(), "--")}
		usage := bodyFieldUsage(f)
		if f.Kind == "json" {
			// JSON 字面量统一按字符串收，执行时再解析——校验错误要带字段名
			flags.StringVar(&value.strVal, value.name, "", usage)
			value.kind = "json"
		} else {
			value.register(flags, f.Schema, usage)
		}
		bodyValues[f.DestName()] = value
	}
	if op.HasJSONBody && !op.HasInputField() {
		flags.StringVar(&inputFile, "input", "",
			"从文件读取整体 JSON 请求体（- 表示 stdin），替代逐字段传参")
	}
	if op.IsUpload {
		flags.StringVar(&uploadFile, "file", "", "要上传的本地文件路径")
		_ = cmd.MarkFlagRequired("file")
	}
	if op.IsDownload {
		flags.StringVar(&outputFile, "output-file", "", "下载内容保存到的本地路径")
		_ = cmd.MarkFlagRequired("output-file")
	}
	if op.LongTask != nil || op.Job != nil {
		waitHelp := "等待任务完成（轮询进度到终态）；--wait=false 启动后立即返回"
		defaultWait := true
		if op.Job != nil {
			waitHelp = "等待持久化任务完成；默认立即返回任务 ID"
			defaultWait = false
		}
		flags.BoolVar(&waitFlag, "wait", defaultWait, waitHelp)
		flagx.Var(flags, &waitTimeout, "wait-timeout", time.Hour,
			"--wait 的最长等待秒数，超时退出码 6（任务继续后台执行）")
	}
	// API 参数与全局覆盖标志重名时 API 参数优先（守护测试保证目前无重名）
	taken := map[string]bool{}
	for _, p := range op.Params {
		taken[strings.ReplaceAll(p.Name, "_", "-")] = true
	}
	for _, f := range op.BodyFields {
		taken[strings.TrimPrefix(f.FlagName(), "--")] = true
	}
	overrides.Register(flags, taken)

	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		settings := overrides.Merge(overlay.SettingsOf(cmd), cmd.Flags())
		return execute(cmd, op, opsByID, settings, executeInput{
			args:        args,
			queryValues: queryValues,
			bodyValues:  bodyValues,
			inputFile:   inputFile,
			uploadFile:  uploadFile,
			outputFile:  outputFile,
			wait:        waitFlag,
			waitTimeout: waitTimeout,
		})
	}
	return cmd
}

type executeInput struct {
	args        []string
	queryValues map[string]*paramValue
	bodyValues  map[string]*paramValue
	inputFile   string
	uploadFile  string
	outputFile  string
	wait        bool
	waitTimeout time.Duration
}

func execute(
	cmd *cobra.Command,
	op Operation,
	opsByID map[string]Operation,
	settings *overlay.Settings,
	in executeInput,
) error {
	path := apiPath(op.Path)
	pathArgs := map[string]any{}
	var argBits []string
	for i, p := range op.PathParams() {
		converted, err := convertPathArg(in.args[i], p.Schema)
		if err != nil {
			return clierr.Usagef("参数 %s %v", p.Name, err)
		}
		pathArgs[p.Name] = converted
		path = strings.ReplaceAll(path, "{"+p.Name+"}", in.args[i])
		argBits = append(argBits, fmt.Sprintf("%s=%s", p.Name, in.args[i]))
	}
	argLine := strings.Join(argBits, " ")
	if argLine == "" {
		argLine = "（无参数）"
	}
	if err := confirmDanger(op, settings, argLine); err != nil {
		return err
	}

	query := url.Values{}
	flags := cmd.Flags()
	for name, value := range in.queryValues {
		if !flags.Changed(value.name) {
			continue
		}
		query.Set(name, fmt.Sprint(value.Value()))
	}

	var body any
	if op.HasJSONBody {
		built, err := buildBody(op, flags, in)
		if err != nil {
			return err
		}
		body = built
	}

	client, err := settings.NewAPI()
	if err != nil {
		return err
	}

	if op.IsDownload {
		if err := client.Download(path, query, in.outputFile); err != nil {
			return err
		}
		output.Info("已保存：%s", in.outputFile)
		return nil
	}

	method := strings.ToUpper(op.Method)
	var data any
	if op.IsUpload {
		data, err = client.Upload(method, path, query, in.uploadFile)
	} else {
		data, err = client.Request(method, path, query, body)
	}
	if err != nil {
		return err
	}
	if client.LastMessage != "" && !settings.Quiet {
		output.Info("%s", client.LastMessage)
	}
	if err := output.Emit(data, settings.Output, settings.Quiet); err != nil {
		return err
	}
	if in.wait {
		switch {
		case op.Job != nil:
			jobID := jsonval.Str(jsonval.At(data, op.Job.IDPath))
			if jobID == "" {
				return clierr.New("服务端已接收任务，但响应中没有可追踪的任务 ID").
					WithHint("请用 mclaw jobs list --active-only 查找刚创建的任务")
			}
			return wait.Job(client, jobID, in.waitTimeout)
		case op.LongTask != nil:
			task, ok := longTaskFor(op, opsByID, pathArgs)
			if !ok {
				return nil
			}
			return wait.Long(client, task, in.waitTimeout)
		}
	}
	return nil
}

// longTaskFor 把 x-cli-long-task 的声明解析成一次具体的等待：进度端点的路径
// 参数就地替换成本次调用的实参。progress_op 指向不存在的操作时不等待——
// spec 写错了不该让命令本身失败，请求已经发出去了。
func longTaskFor(
	op Operation, opsByID map[string]Operation, pathArgs map[string]any,
) (wait.LongTask, bool) {
	progressOp, ok := opsByID[op.LongTask.ProgressOp]
	if !ok {
		return wait.LongTask{}, false
	}
	path := apiPath(progressOp.Path)
	for _, p := range progressOp.PathParams() {
		path = strings.ReplaceAll(path, "{"+p.Name+"}", fmt.Sprint(pathArgs[p.Name]))
	}
	return wait.LongTask{
		ProgressPath:    path,
		ProgressField:   op.LongTask.ProgressField,
		DoneField:       op.LongTask.DoneField,
		ProgressCommand: "mclaw " + strings.ReplaceAll(progressOp.OperationID, ".", " "),
	}, true
}

// buildBody 从命令标志组装 JSON 请求体（或采用 --input 整体替代）。
func buildBody(op Operation, flags flagLookup, in executeInput) (any, error) {
	if !op.HasInputField() && in.inputFile != "" {
		return loadInputBody(in.inputFile)
	}
	body := map[string]any{}
	var missing []string
	for _, f := range op.BodyFields {
		value := in.bodyValues[f.DestName()]
		if value == nil || !flags.Changed(value.name) {
			if f.Required {
				missing = append(missing, f.FlagName())
			}
			continue
		}
		if f.Kind == "json" {
			var parsed any
			if err := json.Unmarshal([]byte(value.strVal), &parsed); err != nil {
				return nil, clierr.Usagef("%s 不是合法 JSON：%v", f.FlagName(), err)
			}
			body[f.Name] = parsed
			continue
		}
		body[f.Name] = value.Value()
	}
	if len(missing) > 0 {
		return nil, clierr.Usagef("缺少必填参数：%s", strings.Join(missing, " ")).
			WithHint("逐个字段传参，或用 --input body.json 整体提供请求体（- 表示 stdin）")
	}
	return body, nil
}

type flagLookup interface{ Changed(name string) bool }

// loadInputBody 从文件或 stdin（-）读取整体 JSON 请求体。
func loadInputBody(source string) (any, error) {
	var raw []byte
	var err error
	if source == "-" {
		raw, err = io.ReadAll(os.Stdin)
	} else {
		raw, err = os.ReadFile(source)
	}
	if err != nil {
		return nil, clierr.Usagef("无法读取 --input 文件：%s（%v）", source, err)
	}
	var parsed any
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return nil, clierr.Usagef("--input 内容不是合法 JSON：%v", err).
			WithHint("请提供 JSON 对象；字段结构见对应 API schema")
	}
	return parsed, nil
}

// confirmDanger 是危险操作门槛（docs/design/cli.md §5.6）：confirm 需 --yes
// （TTY 可交互确认）；destructive 一律要求显式 --yes，并回显影响面。
func confirmDanger(op Operation, settings *overlay.Settings, argLine string) error {
	if op.Dangerous == "" {
		return nil
	}
	if settings.Yes {
		if op.Dangerous == "destructive" {
			output.Info("⚠ 破坏性操作（会删除磁盘文件）：%s｜%s", op.Summary, argLine)
		}
		return nil
	}
	if op.Dangerous == "confirm" && stdinIsTTY() {
		if askConfirm(fmt.Sprintf("确认执行「%s」？", op.Summary)) {
			return nil
		}
		return clierr.Newf(clierr.NeedConfirm, "已取消")
	}
	hint := "确认无误后重新执行并加 --yes"
	if op.Dangerous == "destructive" {
		hint += "（此操作会删除磁盘文件，请核对参数）"
	}
	return clierr.Newf(clierr.NeedConfirm, "该操作需要确认：%s", op.Summary).WithHint("%s", hint)
}

// askConfirm 供测试覆盖。
var askConfirm = func(prompt string) bool {
	fmt.Fprintf(os.Stderr, "%s [y/N] ", prompt)
	var answer string
	if _, err := fmt.Fscanln(os.Stdin, &answer); err != nil {
		return false
	}
	answer = strings.ToLower(strings.TrimSpace(answer))
	return answer == "y" || answer == "yes"
}

var _ = api.APIPrefix // 保持依赖显式，便于阅读调用链
