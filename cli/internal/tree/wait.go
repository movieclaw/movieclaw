package tree

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

// waitLongTask 是长任务 --wait：轮询进度端点直到终态（docs/design/cli.md §8.3）。
func waitLongTask(
	op Operation,
	opsByID map[string]Operation,
	client *api.Client,
	pathArgs map[string]any,
	waitTimeout time.Duration,
) error {
	task := op.LongTask
	progressOp, ok := opsByID[task.ProgressOp]
	if !ok {
		return nil
	}
	path := apiPath(progressOp.Path)
	for _, p := range progressOp.PathParams() {
		path = strings.ReplaceAll(path, "{"+p.Name+"}", fmt.Sprint(pathArgs[p.Name]))
	}

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
			progressCmd := "mclaw " + strings.ReplaceAll(progressOp.OperationID, ".", " ")
			return clierr.Newf(clierr.TaskFailed,
				"等待超时（%.0f 秒），任务仍在后台执行", waitTimeout.Seconds()).
				WithHint("稍后可用 %s 查看进度，或调大 --wait-timeout", progressCmd)
		}
		data, err := client.Request(http.MethodGet, path, nil, nil)
		if err != nil {
			return err
		}
		var progress any = data
		done := false
		if task.ProgressField != "" {
			progress = fieldOf(data, task.ProgressField)
			done = progress == nil
		} else {
			done = !truthy(fieldOf(data, task.DoneField))
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

// waitPersistentJob 等待统一 Job 终态；本地停止等待不会取消服务端任务。
func waitPersistentJob(client *api.Client, startData any, meta Job, waitTimeout time.Duration) error {
	jobID, _ := fieldOf(startData, meta.IDPath).(string)
	if jobID == "" {
		return clierr.New("服务端已接收任务，但响应中没有可追踪的任务 ID").
			WithHint("请用 mclaw jobs list --active-only 查找刚创建的任务")
	}

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
		job, _ := fieldOf(payload, "job").(map[string]any)
		if rev := intOf(job["revision"]); rev > revision {
			revision = rev
		}
		status := str(job["status"])
		progress, _ := job["progress"].(map[string]any)
		line := str(progress["message"])
		if line == "" {
			line = status
		}
		if line == "" {
			line = "等待执行"
		}
		if percent, ok := progress["percent"]; ok && percent != nil {
			line = fmt.Sprintf("%s（%s%%）", line, plain(percent))
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
		errObj, _ := job["error"].(map[string]any)
		message := str(errObj["message"])
		if message == "" {
			message = str(progress["message"])
		}
		if message == "" {
			message = "任务状态：" + status
		}
		hint := fmt.Sprintf("执行 mclaw jobs show %s 查看详情", jobID)
		if hasHandoffAction(errObj["actions"]) {
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
	list, ok := actions.([]any)
	if !ok {
		return false
	}
	for _, item := range list {
		if action, ok := item.(map[string]any); ok && str(action["type"]) == "handoff_agent" {
			return true
		}
	}
	return false
}

// fieldOf 按点分路径读取响应字段；不存在返回 nil。
func fieldOf(value any, path string) any {
	if path == "" {
		return nil
	}
	for _, token := range strings.Split(path, ".") {
		obj, ok := value.(map[string]any)
		if !ok {
			return nil
		}
		value = obj[token]
	}
	return value
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
