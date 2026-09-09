package overlay

import (
	"strings"
	"testing"

	"github.com/movieclaw/movieclaw/cli/internal/jsonval"
)

// decode 把测试里手写的 JSON 变成命令真正拿到的形态（保序对象 + json.Number）。
func decode(t *testing.T, raw string) any {
	t.Helper()
	value, err := jsonval.Decode([]byte(raw))
	if err != nil {
		t.Fatalf("测试数据不是合法 JSON：%v", err)
	}
	return value
}

func count(t *testing.T, summary *jsonval.Map, key string) int {
	t.Helper()
	return jsonval.Int(summary.Get(key))
}

// TestSummarizeActivityMergesDownloadAndIngest 守的是本命令存在的理由：
// 一个下载任务和它触发的入库作业是同一件事，只能算一件。各算各的就会出现
// 「CLI 说 3、Web 说 2」——数字对不上，提醒就失去可信度。
func TestSummarizeActivityMergesDownloadAndIngest(t *testing.T) {
	tasks := decode(t, `{
      "items": [
        {"id": "1-aa", "info_hash": "AA", "source": "subscription", "state": "completed",
         "media_item_id": 7, "can_replace": false, "landing_error": null, "subscriptions": []},
        {"id": "1-bb", "info_hash": "bb", "source": "subscription", "state": "downloading",
         "media_item_id": 7, "can_replace": false, "landing_error": null, "subscriptions": []}
      ],
      "sources": [{"id": 1, "status": "active"}]
    }`)
	jobs := decode(t, `{
      "items": [
        {"id": "job-ingest", "job_type": "library.ingest", "status": "running",
         "dismissed_at": null, "resources": [{"resource_type": "download", "resource_id": "aa"}]}
      ]
    }`)
	media := decode(t, `{"sessions": [], "downloads": [], "hidden_session_count": 0, "hidden_download_count": 0}`)

	summary := summarizeActivity(tasks, jobs, media)
	// 同一部片的两个种子合成一件，入库作业已并入下载那一件，不再单独计数
	if got := count(t, summary, "active"); got != 1 {
		t.Errorf("进行中应为 1（一部片 = 一件事），实际 %d", got)
	}
	if got := count(t, summary, "active_jobs"); got != 0 {
		t.Errorf("已并入下载的入库作业不该重复计数，实际 %d", got)
	}
	if got := count(t, summary, "attention"); got != 0 {
		t.Errorf("没有异常时需要处理应为 0，实际 %d", got)
	}
}

// TestSummarizeActivityAttentionSources 覆盖「需要处理」的几条判定，
// 口径与 apps/web/lib/download-attention.ts 一致。
func TestSummarizeActivityAttentionSources(t *testing.T) {
	tasks := decode(t, `{
      "items": [
        {"id": "1-aa", "info_hash": "aa", "source": "subscription", "state": "stalled",
         "media_item_id": 1, "can_replace": true, "landing_error": null, "subscriptions": []},
        {"id": "1-bb", "info_hash": "bb", "source": "subscription", "state": "completed",
         "media_item_id": 2, "can_replace": false, "landing_error": null,
         "subscriptions": [{"units": [{"content_missing": true}]}]},
        {"id": "1-cc", "info_hash": "cc", "source": "external", "state": "error",
         "media_item_id": null, "can_replace": false, "landing_error": null, "subscriptions": []},
        {"id": "1-dd", "info_hash": "dd", "source": "boost", "state": "downloading",
         "media_item_id": null, "can_replace": false, "landing_error": null, "subscriptions": []}
      ],
      "sources": [{"id": 1, "status": "active"}, {"id": 2, "status": "unavailable"}]
    }`)
	jobs := decode(t, `{
      "items": [
        {"id": "j1", "job_type": "subtitle.generate", "status": "failed",
         "dismissed_at": null, "resources": []},
        {"id": "j2", "job_type": "subtitle.generate", "status": "failed",
         "dismissed_at": "2026-01-01T00:00:00Z", "resources": []},
        {"id": "j3", "job_type": "library.scan", "status": "running",
         "dismissed_at": null, "resources": []}
      ]
    }`)
	media := decode(t, `{"sessions": [{"device_id": "d1"}], "downloads": [],
      "hidden_session_count": 1, "hidden_download_count": 2}`)

	summary := summarizeActivity(tasks, jobs, media)
	// 换种候选 + 内容不符 = 2 件下载要处理；失败作业 1 件（已忽略的那条不算）
	if got := count(t, summary, "attention_downloads"); got != 2 {
		t.Errorf("需要处理的下载应为 2，实际 %d", got)
	}
	if got := count(t, summary, "attention_jobs"); got != 1 {
		t.Errorf("已忽略的失败作业不该计入需要处理，实际 %d", got)
	}
	// 外部种子报错不计入待办（陈年错误种子会淹没真正要处理的任务），
	// 但仍作为进行中如实显示；刷流做种既不报警也不计入进行中
	if got := count(t, summary, "active_downloads"); got != 1 {
		t.Errorf("外部任务应计入进行中且刷流不计入，进行中下载实际 %d", got)
	}
	if got := count(t, summary, "seeding_boost"); got != 1 {
		t.Errorf("刷流做种应单独报数，实际 %d", got)
	}
	if got := count(t, summary, "playing"); got != 2 {
		t.Errorf("范围外折叠的会话也算有人在播，实际 %d", got)
	}
	if got := count(t, summary, "file_downloads"); got != 2 {
		t.Errorf("范围外折叠的下载也要报数，实际 %d", got)
	}
	if got := count(t, summary, "downloaders_unavailable"); got != 1 {
		t.Errorf("连不上的下载器应报数，实际 %d", got)
	}
	if next := jsonval.Str(summary.Get("next")); !strings.Contains(next, "blocked,failed") {
		t.Errorf("有待办时下一步应指向需要处理的任务，实际 %q", next)
	}
}

// TestSummarizeActivityEmpty 空场景不该报出任何数字，也要给出明确的结论。
func TestSummarizeActivityEmpty(t *testing.T) {
	empty := decode(t, `{"items": [], "sources": []}`)
	media := decode(t, `{"sessions": [], "downloads": []}`)
	summary := summarizeActivity(empty, empty, media)
	for _, key := range []string{"attention", "active", "playing", "seeding_boost"} {
		if got := count(t, summary, key); got != 0 {
			t.Errorf("空场景 %s 应为 0，实际 %d", key, got)
		}
	}
	if next := jsonval.Str(summary.Get("next")); next != "现在没有任务，也没有人在看" {
		t.Errorf("空场景的结论不对：%q", next)
	}
}
