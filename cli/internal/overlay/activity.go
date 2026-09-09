package overlay

import (
	"net/http"
	"net/url"
	"strings"

	"github.com/movieclaw/movieclaw/cli/internal/jsonval"
	"github.com/movieclaw/movieclaw/cli/internal/output"
	"github.com/spf13/cobra"
)

// 与 Web 活动页（apps/web/lib/job-attention.ts）同一份状态口径。
// 命令行不该自己发明一套「算不算进行中」，否则同一台服务器上 Web 说 2、
// CLI 说 3，用户不知道该信谁。
var (
	attentionJobStatuses = map[string]bool{"blocked": true, "failed": true}
	activeJobStatuses    = map[string]bool{
		"queued": true, "running": true, "retry_wait": true,
		"cancelling": true, "waiting": true,
	}
)

// activityJobStatusQuery 是一次拉齐「要处理 + 进行中」两档的状态过滤。
// 已结束的作业不参与本命令的计数，因此不拉——首屏要回答的是「现在有什么事」。
const activityJobStatusQuery = "queued,running,retry_wait,cancelling,waiting,blocked,failed"

// activityJobLimit 取接口允许的上限。真到了 200 条未结束作业，用户要的也不是
// 一个更精确的数字，而是赶紧去 jobs list 看它们到底怎么了。
const activityJobLimit = "200"

// NewActivityCommand 构造 `mclaw activity`：一屏回答「现在这台机器上有什么事」。
//
// 收进精选层的理由（docs/design/cli.md §7 的准入标准）：它要把三个接口的结果
// 按业务口径合并——一个下载任务和它触发的入库作业是同一件事，只能算一件。
// 这个归并规则在 Web 上住在 lib/task-activity.ts，不属于任何单个接口，
// 生成层做不出来。
func NewActivityCommand() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "activity",
		Short: "一眼看清现在有几件事要你处理、几件在跑、几个人在看",
		Long: `把三件事汇总成一屏：需要你处理的任务（下载出错、卡住、入库失败）、
正在进行的任务（下载中、正在入库、正在生成字幕），以及此刻正在播放的人数。

适合登录服务器后第一条敲的命令，也适合放进每日巡检脚本——-o json 时给的是
结构化计数，可以直接判断要不要报警。

计数口径与 Web 活动页完全一致：一个下载任务和它触发的入库作业算一件事，
刷流做种不计入（它常年几十上百个在跑，计进去这个数字就没意义了）。

看明细请继续用：

    mclaw dl tasks                           # 下载到哪了、为哪部片下的
    mclaw jobs list --status blocked,failed  # 哪些后台作业需要处理
    mclaw playback activity                  # 谁在看、哪台设备、多快`,
		Args: cobra.NoArgs,
	}
	return withOverrides(cmd, nil, func(s *Settings, _ *cobra.Command, _ []string) error {
		client, err := s.NewAPI()
		if err != nil {
			return err
		}
		tasks, err := client.Request(http.MethodGet, "/downloaders/tasks", nil, nil)
		if err != nil {
			return err
		}
		jobs, err := client.Request(http.MethodGet, "/jobs",
			url.Values{"status": {activityJobStatusQuery}, "limit": {activityJobLimit}}, nil)
		if err != nil {
			return err
		}
		media, err := client.Request(http.MethodGet, "/playback/activity", nil, nil)
		if err != nil {
			return err
		}
		return output.Emit(summarizeActivity(tasks, jobs, media), s.Output, s.Quiet)
	})
}

// summarizeActivity 把三份快照合并成计数。抽成纯函数是为了能直接单测——
// 计数口径出错不会让命令报错，只会静默给出与 Web 对不上的数字。
func summarizeActivity(tasks, jobs, media any) *jsonval.Map {
	taskItems := jsonval.Array(jsonval.At(tasks, "items"))
	jobItems := jsonval.Array(jsonval.At(jobs, "items"))

	// infohash → 入库作业：下载与入库是同一件事的两段，合并后只算一件
	ingestByHash := map[string]any{}
	for _, job := range jobItems {
		if jsonval.Str(jsonval.At(job, "job_type")) != "library.ingest" {
			continue
		}
		for _, resource := range jsonval.Array(jsonval.At(job, "resources")) {
			if jsonval.Str(jsonval.At(resource, "resource_type")) != "download" {
				continue
			}
			hash := strings.ToLower(jsonval.Str(jsonval.At(resource, "resource_id")))
			if _, seen := ingestByHash[hash]; !seen {
				ingestByHash[hash] = job
			}
		}
	}

	// 按作品合并下载任务：同一部片的多个种子是一件事。没有媒体身份的
	// （未识别 / 外部种子）各算各的，不能因为名字像就折叠。
	groupAttention := map[string]bool{}
	var groupOrder []string
	boost, linkedJobIDs := 0, map[string]bool{}
	for _, task := range taskItems {
		hash := strings.ToLower(jsonval.Str(jsonval.At(task, "info_hash")))
		if job, ok := ingestByHash[hash]; ok {
			linkedJobIDs[jsonval.Str(jsonval.At(job, "id"))] = true
		}
		if jsonval.Str(jsonval.At(task, "source")) == "boost" {
			boost++
			continue
		}
		key := "task:" + jsonval.Str(jsonval.At(task, "id"))
		if id := jsonval.At(task, "media_item_id"); id != nil {
			key = "media:" + jsonval.Plain(id)
		}
		if _, seen := groupAttention[key]; !seen {
			groupOrder = append(groupOrder, key)
		}
		groupAttention[key] = groupAttention[key] || taskNeedsAttention(task, ingestByHash[hash])
	}
	attentionDownloads := 0
	for _, key := range groupOrder {
		if groupAttention[key] {
			attentionDownloads++
		}
	}
	activeDownloads := len(groupOrder) - attentionDownloads

	// 已经并进下载那件事的入库作业不再单独计数——这正是侧栏数字曾经比
	// 页面大一号的原因（见 apps/web/lib/task-activity.ts）
	attentionJobs, activeJobs := 0, 0
	for _, job := range jobItems {
		if linkedJobIDs[jsonval.Str(jsonval.At(job, "id"))] {
			continue
		}
		status := jsonval.Str(jsonval.At(job, "status"))
		switch {
		case attentionJobStatuses[status] && jsonval.At(job, "dismissed_at") == nil:
			attentionJobs++
		case activeJobStatuses[status]:
			activeJobs++
		}
	}

	playing := len(jsonval.Array(jsonval.At(media, "sessions"))) +
		jsonval.Int(jsonval.At(media, "hidden_session_count"))
	fileDownloads := len(jsonval.Array(jsonval.At(media, "downloads"))) +
		jsonval.Int(jsonval.At(media, "hidden_download_count"))

	unavailable := 0
	for _, source := range jsonval.Array(jsonval.At(tasks, "sources")) {
		if jsonval.Str(jsonval.At(source, "status")) != "active" {
			unavailable++
		}
	}

	attention := attentionDownloads + attentionJobs
	active := activeDownloads + activeJobs
	return jsonval.NewMap(
		"attention", attention,
		"attention_downloads", attentionDownloads,
		"attention_jobs", attentionJobs,
		"active", active,
		"active_downloads", activeDownloads,
		"active_jobs", activeJobs,
		"seeding_boost", boost,
		"playing", playing,
		"file_downloads", fileDownloads,
		"downloaders_unavailable", unavailable,
		"next", nextStep(attention, active, playing, unavailable),
	)
}

// taskNeedsAttention 判断一条下载任务是不是**现在**要用户动手，口径与
// apps/web/lib/download-attention.ts 一致。
//
// 外部任务（不是 movieclaw 投递的种子）不参与：它们没有工单可救援、没有入库
// 可推进，下载器里积压的陈年错误种子若全算成待办，会把真正要处理的订阅任务
// 淹没在里面。
func taskNeedsAttention(task, ingestJob any) bool {
	if jsonval.Str(jsonval.At(task, "source")) == "external" {
		return false
	}
	state := jsonval.Str(jsonval.At(task, "state"))
	if jsonval.Truthy(jsonval.At(task, "can_replace")) ||
		state == "error" || state == "missing" ||
		jsonval.At(task, "landing_error") != nil {
		return true
	}
	// 种子里根本没有声明的那几集：等这个任务永远等不到，已经退回重找资源
	for _, sub := range jsonval.Array(jsonval.At(task, "subscriptions")) {
		for _, unit := range jsonval.Array(jsonval.At(sub, "units")) {
			if jsonval.Truthy(jsonval.At(unit, "content_missing")) {
				return true
			}
		}
	}
	if ingestJob == nil {
		return false
	}
	return attentionJobStatuses[jsonval.Str(jsonval.At(ingestJob, "status"))] &&
		jsonval.At(ingestJob, "dismissed_at") == nil
}

// nextStep 给出此刻最该敲的下一条命令。与 Web 侧栏角标同一条判断链：
// 有要处理的就说要处理的，否则说进行中，都没有才说看播放。
func nextStep(attention, active, playing, unavailable int) string {
	switch {
	case attention > 0:
		return "mclaw jobs list --status blocked,failed 与 mclaw dl tasks 查看需要处理的任务"
	case active > 0:
		return "mclaw dl tasks 看下载与入库进度"
	case unavailable > 0:
		return "有下载器连不上，mclaw dl list 查看接入状态"
	case playing > 0:
		return "mclaw playback activity 看谁在播、播得顺不顺"
	default:
		return "现在没有任务，也没有人在看"
	}
}
