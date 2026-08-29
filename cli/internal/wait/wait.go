// Package wait 是两种「等任务跑完」的等待循环（docs/design/cli.md §8.3）。
//
// 独立成包是因为生成层和精选层都要用：`mclaw library scan start --wait` 由
// spec 的 x-cli-long-task 生成，`mclaw jobs wait` 与 `mclaw library
// organize-files --wait` 是手写的精选命令，两边共用同一套节奏与文案。
package wait

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"time"

	"github.com/yipengfei329/movieclaw/cli/internal/api"
	"github.com/yipengfei329/movieclaw/cli/internal/clierr"
	"github.com/yipengfei329/movieclaw/cli/internal/jsonval"
	"github.com/yipengfei329/movieclaw/cli/internal/output"
)

// jobTerminalStatuses 是统一 Job 体系的终态集合。
var jobTerminalStatuses = map[string]bool{
	"succeeded": true, "failed": true, "cancelled": true, "blocked": true,
}

// interrupted 返回一个在收到 Ctrl-C 时关闭的通道，以及注销函数。
//
// Python 版靠 KeyboardInterrupt 打断轮询；Go 里默认 SIGINT 直接杀进程，
// 等待循环就没机会告诉用户「任务还在后台跑，没有被取消」——这句话是必须
// 说的，否则用户会以为按下 Ctrl-C 就取消了下载。
var interrupted = func() (<-chan os.Signal, func()) {
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, os.Interrupt)
	return ch, func() { signal.Stop(ch) }
}

// sleepOrInterrupt 睡到时间到或被 Ctrl-C 打断；被打断返回 true。
func sleepOrInterrupt(d time.Duration, sig <-chan os.Signal) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-sig:
		return true
	case <-timer.C:
		return false
	}
}

// pollInterval 是轮询节奏自适应：起步勤快，跑得久了放缓，别给服务器添乱。
func pollInterval(elapsed time.Duration) time.Duration {
	switch {
	case elapsed < 30*time.Second:
		return 2 * time.Second
	case elapsed < 120*time.Second:
		return 5 * time.Second
	default:
		return 10 * time.Second
	}
}

// LongTask 描述一次长任务等待：进度从哪读、读哪个字段、超时了让用户敲什么。
type LongTask struct {
	// ProgressPath 是已经替换好路径参数的进度端点（相对 /api/v1）。
	ProgressPath string
	// ProgressField 非空时读该字段，字段变 null 即视为结束。
	ProgressField string
	// DoneField 是 ProgressField 为空时的备用判据：该字段转假即结束。
	DoneField string
	// ProgressCommand 是超时提示里让用户自己去查进度的命令。
	ProgressCommand string
}

// Long 轮询进度端点直到终态（docs/design/cli.md §8.3）。
func Long(client *api.Client, task LongTask, waitTimeout time.Duration) error {
	path := task.ProgressPath

	sig, stop := interrupted()
	defer stop()

	started := time.Now()
	lastLine := ""
	// 启动竞态防护：服务端是「响应先回、后台任务随后注册状态」，立刻轮询会把
	// 「还没开始」误判成「已结束」。先小睡一拍；此后若从未观测到运行态，需要
	// 连续多次读到终态才下结论（快任务确实可能在首次轮询前就跑完）。
	if sleepOrInterrupt(time.Second, sig) {
		return abortWait("已停止等待（任务仍在后台执行，未被取消；如需取消请用对应的 stop 命令）")
	}
	sawRunning := false
	doneStreak := 0
	for {
		elapsed := time.Since(started)
		if elapsed > waitTimeout {
			return clierr.Newf(clierr.TaskFailed,
				"等待超时（%.0f 秒），任务仍在后台执行", waitTimeout.Seconds()).
				WithHint("稍后可用 %s 查看进度，或调大 --wait-timeout", task.ProgressCommand)
		}
		data, err := client.Request(http.MethodGet, path, nil, nil)
		if err != nil {
			return err
		}
		var progress any = data
		done := false
		if task.ProgressField != "" {
			progress = jsonval.At(data, task.ProgressField)
			done = progress == nil
		} else {
			done = !jsonval.Truthy(jsonval.At(data, task.DoneField))
		}
		if done {
			if sawRunning {
				// 注意：进度接口不区分成功/失败，这里只能断言「已结束」；
				// 若怀疑失败可查系统日志（mclaw logs tail）
				output.Info("任务已结束")
				return nil
			}
			doneStreak++
			if doneStreak >= 3 {
				output.Info("任务已结束（未观测到运行状态，可能启动后瞬间完成）")
				return nil
			}
			if sleepOrInterrupt(2*time.Second, sig) {
				return abortWait("已停止等待（任务仍在后台执行，未被取消；如需取消请用对应的 stop 命令）")
			}
			continue
		}
		sawRunning = true
		doneStreak = 0
		line := compactJSON(progress)
		if line != lastLine {
			output.Info("进行中：%s", line)
			lastLine = line
		}
		if sleepOrInterrupt(pollInterval(elapsed), sig) {
			return abortWait("已停止等待（任务仍在后台执行，未被取消；如需取消请用对应的 stop 命令）")
		}
	}
}

// Job 等待统一 Job 体系里的一个任务到终态；本地停止等待不会取消服务端任务。
func Job(client *api.Client, jobID string, waitTimeout time.Duration) error {
	sig, stop := interrupted()
	defer stop()

	started := time.Now()
	revision := 0
	lastLine := ""
	abortMessage := fmt.Sprintf(
		"已停止等待，任务 %s 仍在后台执行；需要取消时运行 mclaw jobs cancel %s", jobID, jobID)
	for {
		select {
		case <-sig:
			return abortWait(abortMessage)
		default:
		}
		remaining := waitTimeout - time.Since(started)
		if remaining <= 0 {
			return clierr.Newf(clierr.TaskFailed,
				"等待超时（%.0f 秒），任务仍在后台执行", waitTimeout.Seconds()).
				WithHint("稍后执行 mclaw jobs show %s，或调大 --wait-timeout", jobID)
		}
		waitSeconds := remaining.Seconds()
		if waitSeconds > 25 {
			waitSeconds = 25
		}
		params := url.Values{}
		params.Set("after_revision", fmt.Sprint(revision))
		params.Set("wait_seconds", trimFloat(waitSeconds))
		payload, err := client.Request(http.MethodGet, "/jobs/"+jobID+"/wait", params, nil)
		if err != nil {
			return err
		}
		job := jsonval.Object(jsonval.At(payload, "job"))
		if rev := jsonval.Int(job.Get("revision")); rev > revision {
			revision = rev
		}
		status := jsonval.Str(job.Get("status"))
		progress := jsonval.Object(job.Get("progress"))
		line := jsonval.Str(progress.Get("message"))
		if line == "" {
			line = status
		}
		if line == "" {
			line = "等待执行"
		}
		if percent := progress.Get("percent"); percent != nil {
			line = fmt.Sprintf("%s（%s%%）", line, jsonval.Plain(percent))
		}
		if line != lastLine {
			output.Info("%s · %s", jobID, line)
			lastLine = line
		}
		if !jobTerminalStatuses[status] {
			continue
		}
		if status == "succeeded" {
			output.Info("任务已完成：%s", jobID)
			return nil
		}
		errObj := jsonval.Object(job.Get("error"))
		message := jsonval.Str(errObj.Get("message"))
		if message == "" {
			message = jsonval.Str(progress.Get("message"))
		}
		if message == "" {
			message = "任务状态：" + status
		}
		hint := fmt.Sprintf("执行 mclaw jobs show %s 查看详情", jobID)
		if hasHandoffAction(errObj.Get("actions")) {
			hint += "；也可以交给 MovieClaw Agent 处理"
		}
		return clierr.Newf(clierr.TaskFailed, "%s", message).WithHint("%s", hint)
	}
}

// abortWait 打印「任务还在后台」的说明后以业务错误码退出。
//
// 退出码用 1（业务错误）而不是 6：任务本身没失败，是用户主动不等了。
func abortWait(message string) error {
	output.Info("%s", message)
	return clierr.Newf(clierr.Business, "已中断等待")
}

func hasHandoffAction(actions any) bool {
	for _, item := range jsonval.Array(actions) {
		if jsonval.Str(jsonval.At(item, "type")) == "handoff_agent" {
			return true
		}
	}
	return false
}

func intOf(value any) int {
	switch v := value.(type) {
	case json.Number:
		n, err := v.Int64()
		if err != nil {
			return 0
		}
		return int(n)
	case float64:
		return int(v)
	case int:
		return v
	}
	return 0
}

// compactJSON 把进度对象渲染成一行；不转义 < > &，中文原样输出。
func compactJSON(value any) string {
	var buf bytes.Buffer
	encoder := json.NewEncoder(&buf)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return fmt.Sprint(value)
	}
	return strings.TrimRight(buf.String(), "\n")
}

// trimFloat 渲染秒数：整数不带小数点，避免 wait_seconds=25.000000。
func trimFloat(value float64) string {
	s := fmt.Sprintf("%.3f", value)
	s = strings.TrimRight(s, "0")
	return strings.TrimRight(s, ".")
}
