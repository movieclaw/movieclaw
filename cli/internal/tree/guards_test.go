package tree

import (
	"sort"
	"strings"
	"testing"
)

// 本文件是命令面的守护测试，从退役的 tests/cli/test_tree_snapshot.py 原样搬来。
// 它们守的都是同一件事：**没有静默的第三条路**（docs/design/cli.md §3.2）——
// 新端点要么进命令树，要么显式标注豁免并在这里登记，不允许悄悄消失。

// knownNonGenerated 是唯二允许不生成命令的两类端点：x-cli-hidden（Web 基础
// 设施，或语义由精选命令承担）与 x-cli-stream（SSE 流，精选层手写接入）。
var knownNonGenerated = []string{
	"auth.device.authorize",
	"auth.device.token",
	"auth.tokens.create",
	"auth.tokens.list",
	"auth.tokens.revoke",
	"auth.devices.requests",
	"auth.devices.approve",
	"auth.devices.deny",
	"images.asset",
	"images.proxy",
	"libraries.cover",
	"ui.library.files.preview-subtitles",
	"ui.library.files.thumb",
	"ui.library.items.ids",
	"ui.library.items.index",
	"playback.activity",
	"playback.decide",
	"playback.item.info",
	"playback.item.episodes",
	"playback.session.start",
	"playback.session.ping",
	"playback.session.stop",
	"playback.session.playlist",
	"playback.session.segment",
	"playback.session.diagnostics",
	"playback.session.master",
	"playback.session.subtitle-playlist",
	"playback.file.stream",
	"playback.file.subtitle",
	"playback.file.fonts",
	"playback.file.font",
	"playback.hardware.probe",
	"playback.file.trickplay",
	"playback.file.trickplay.sheet",
	"playback.metric.report",
	"playback.client-log",
	"playback.stats",
	"playback.device.revoke",
	"transcode.source",
	"transcode.artifact.put",
	"playback.progress",
	"playback.resume",
	"playback.policy.show",
	"playback.policy.set",
	"workflow.library.organize-files.preview",
	"workflow.library.organize-files.start",
	"workflow.library.reconcile-paths.preview",
	"workflow.library.reconcile-paths.start",
	"system.spec",
	"auth.login",
	"auth.logout",
	"workflow.search.torrents.stream",
	"session.fork",
	"session.follow",
	"fs.browse",
	"jobs.stream",
	"playback.recent",
	"dl.tasks",
	"dl.torrent.replace",
	"dl.torrent.delete",
	"ui.discovery.get",
	"discover.get-person-details",
	"ui.subscriptions.preview-title",
}

// TestNonGeneratedEndpointsAreAllKnown 强制新端点显式表态：进命令树，或登记豁免。
func TestNonGeneratedEndpointsAreAllKnown(t *testing.T) {
	var actual []string
	for _, op := range IterOperations(loadSpec(t)) {
		if op.OperationID != "" && !op.Generable() {
			actual = append(actual, op.OperationID)
		}
	}
	compareSets(t, "不生成命令的端点", knownNonGenerated, actual,
		"新端点要么进生成范围，要么标注 x-cli-hidden/x-cli-stream 并在 knownNonGenerated 登记")
}

// TestDomainHelpCoversEveryDomain 校验域级 --help 齐全：它是模型的二次探索入口，
// 缺一个域，那个域对模型就等于不存在。
func TestDomainHelpCoversEveryDomain(t *testing.T) {
	seen := map[string]bool{}
	var domains []string
	for _, op := range IterOperations(loadSpec(t)) {
		if !op.Generable() {
			continue
		}
		domain := strings.SplitN(op.OperationID, ".", 2)[0]
		if !seen[domain] {
			seen[domain] = true
			domains = append(domains, domain)
		}
	}
	var declared []string
	for domain := range domainHelp {
		declared = append(declared, domain)
	}
	compareSets(t, "域简介", declared, domains, "在 help_text.go 的 domainHelp 里增删对应条目")
}

// TestDomainHelpUsesProductLanguage 校验一级帮助沿用页面心智，并在短描述里
// 区分容易混淆的相邻域——模型选域靠的就是这一行。
func TestDomainHelpUsesProductLanguage(t *testing.T) {
	for domain, want := range map[string]string{
		"discover":      "TMDB/豆瓣",
		"search":        "影视条目、PT 种子和本地媒体库",
		"subscriptions": "自动搜索、下载和整理入库",
		"channels":      "AI 对话入口",
		"members":       "家庭成员",
		"appearance":    "首页背景",
		"ui":            "界面质感",
	} {
		if !strings.Contains(domainHelp[domain], want) {
			t.Errorf("域 %s 的简介里没有 %q：%s", domain, want, domainHelp[domain])
		}
	}
}

// TestDomainCommandSets 锁住四个高频域的公开面：不让页面实现细节
// （layout/hero/row）、旧术语或含混命名悄悄漏进模型能选的命令里。
func TestDomainCommandSets(t *testing.T) {
	want := map[string][]string{
		"discover": {
			"discover.list-collections",
			"discover.browse-collection",
			"discover.filter-options",
			"discover.filter-titles",
			"discover.get-title-details",
			"discover.region.show",
			"discover.region.set",
		},
		"subscriptions": {
			"subscriptions.check-automation-readiness",
			"subscriptions.create",
			"subscriptions.delete",
			"subscriptions.download-selected-torrent",
			"subscriptions.get",
			"subscriptions.list",
			"subscriptions.list-active-downloads",
			"subscriptions.list-activities",
			"subscriptions.list-today-arrivals",
			"subscriptions.preview-download-routing",
			"subscriptions.search-missing-resources",
			"subscriptions.set-follow-future",
			"subscriptions.set-tracking-state",
			"subscriptions.upgrade-run",
			"subscriptions.unsubscribe",
			"subscriptions.update",
		},
		"library": {
			"library.artwork.download",
			"library.artwork.list-candidates",
			"library.artwork.select",
			"library.create",
			"library.delete",
			"library.get",
			"library.identification.assign-file-to-title",
			"library.identification.assign-files-to-title",
			"library.identification.ignore-all-unidentified-files",
			"library.identification.ignore-file",
			"library.identification.list-ignored-files",
			"library.identification.list-review-cases",
			"library.identification.list-unidentified-files",
			"library.identification.mark-files-as-extras",
			"library.identification.resolve-review",
			"library.identification.restore-files",
			"library.items.annotate-media-source",
			"library.items.delete",
			"library.items.delete-file",
			"library.items.purge-file",
			"library.items.restore-file",
			"library.items.get",
			"library.items.get-transfer-status",
			"library.items.list",
			"library.items.list-episodes",
			"library.items.list-media-source-annotation-candidates",
			"library.items.preview-reidentification",
			"library.items.preview-transfer",
			"library.items.refresh-metadata",
			"library.items.reidentify",
			"library.items.set-scrape-library",
			"library.items.transfer",
			"library.list",
			"library.list-routing-options",
			"library.metadata.get-refresh-status",
			"library.metadata.refresh-library",
			"library.metadata.stop-refresh",
			"library.missing.clear-records",
			"library.missing.list",
			"library.missing.redownload",
			"library.reorder",
			"library.scan.start",
			"library.scan.stop",
			"library.set-default",
			"library.subtitles.calibrate-timing",
			"library.subtitles.delete",
			"library.subtitles.generate",
			"library.subtitles.preview-generation",
			"library.update",
		},
		"search": {
			"search.titles",
			"search.torrents",
			"search.library-items",
			"search.history.list",
			"search.history.get-results",
			"search.history.delete",
			"search.history.clear",
			"search.presets.list",
			"search.presets.update",
		},
	}
	ops := IterOperations(loadSpec(t))
	for domain, expected := range want {
		var actual []string
		for _, op := range ops {
			if op.Generable() && strings.HasPrefix(op.OperationID, domain+".") {
				actual = append(actual, op.OperationID)
			}
		}
		compareSets(t, domain+" 域命令", expected, actual, "确认属预期变更后更新本清单")
	}
}

// TestSessionExposesOnlyMessageSemantics 校验 session 的公开面只有会话/message
// 语义，不重新引入 run、turn 或 rewind 这些内部调度概念。
func TestSessionExposesOnlyMessageSemantics(t *testing.T) {
	var actual []string
	for _, op := range IterOperations(loadSpec(t)) {
		if strings.HasPrefix(op.OperationID, "session.") {
			actual = append(actual, op.OperationID)
		}
	}
	compareSets(t, "session 域端点", []string{
		"session.compact-context",
		"session.delete",
		"session.follow",
		"session.fork",
		"session.get-transcript",
		"session.list",
		"session.rename",
		"session.retry",
		"session.start",
		"session.stop",
	}, actual, "session 的公开面只允许会话/message 语义")
}

// TestAPIParamsDoNotShadowCLIFlags 校验 API 参数名不与 CLI 内置标志重名。
// 撞上了那条命令就失去该标志（如 -o json），只能在路由侧改名。
func TestAPIParamsDoNotShadowCLIFlags(t *testing.T) {
	reserved := map[string]bool{
		"output": true, "server": true, "context": true, "timeout": true,
		"quiet": true, "debug": true, "yes": true,
		"file": true, "output_file": true, "wait": true, "wait_timeout": true,
		// "input" 不在保留清单：API 字段叫 input 时（如 session.start 的用户
		// 输入），API 字段优先，该命令放弃 --input 整体替代形态
	}
	for _, op := range IterOperations(loadSpec(t)) {
		if !op.Generable() {
			continue
		}
		for _, p := range op.Params {
			if reserved[p.Name] {
				t.Errorf("%s 的参数 %s 与 CLI 内置标志重名，请在路由侧改名", op.OperationID, p.Name)
			}
		}
		for _, f := range op.BodyFields {
			if reserved[f.Name] {
				t.Errorf("%s 的请求体字段 %s 与 CLI 内置标志重名，请在路由侧改名", op.OperationID, f.Name)
			}
		}
	}
}

// TestDangerousAndLongTaskAnnotations 校验 x-cli 标注真的驱动了危险确认与两类
// 后台等待协议——标注掉了不会报错，只会静默失去确认门槛，所以要显式盯住。
func TestDangerousAndLongTaskAnnotations(t *testing.T) {
	ops := map[string]Operation{}
	for _, op := range IterOperations(loadSpec(t)) {
		ops[op.OperationID] = op
	}
	for id, want := range map[string]string{
		"library.items.delete":      "destructive",
		"subscriptions.delete":      "confirm",
		"subscriptions.unsubscribe": "confirm",
		"library.items.transfer":    "confirm",
	} {
		if got := ops[id].Dangerous; got != want {
			t.Errorf("%s 的 x-cli-dangerous 是 %q，期望 %q", id, got, want)
		}
	}
	for id, want := range map[string]string{
		"library.scan.start":                    "job_id",
		"library.metadata.refresh-library":      "job_id",
		"workflow.library.organize-files.start": "job_id",
		"library.items.refresh-metadata":        "job_id",
		"library.items.transfer":                "job_id",
		"library.subtitles.generate":            "id",
	} {
		job := ops[id].Job
		if job == nil {
			t.Errorf("%s 缺少 x-cli-job 标注", id)
			continue
		}
		if job.IDPath != want {
			t.Errorf("%s 的 x-cli-job.id_path 是 %q，期望 %q", id, job.IDPath, want)
		}
	}
}

// compareSets 比较两个集合并逐条报差异（顺序无关）。
func compareSets(t *testing.T, label string, want, got []string, hint string) {
	t.Helper()
	inWant := map[string]bool{}
	for _, item := range want {
		inWant[item] = true
	}
	inGot := map[string]bool{}
	for _, item := range got {
		inGot[item] = true
	}
	var extra, missing []string
	for _, item := range got {
		if !inWant[item] {
			extra = append(extra, item)
		}
	}
	for _, item := range want {
		if !inGot[item] {
			missing = append(missing, item)
		}
	}
	sort.Strings(extra)
	sort.Strings(missing)
	if len(extra) > 0 || len(missing) > 0 {
		t.Errorf("%s 发生变化：多出 %v，少了 %v。%s", label, extra, missing, hint)
	}
}
