package overlay

import (
	"encoding/json"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"github.com/spf13/cobra"
	"github.com/yipengfei329/movieclaw/cli/internal/api"
	"github.com/yipengfei329/movieclaw/cli/internal/clierr"
	"github.com/yipengfei329/movieclaw/cli/internal/config"
	"github.com/yipengfei329/movieclaw/cli/internal/jsonval"
	"github.com/yipengfei329/movieclaw/cli/internal/output"
	"github.com/yipengfei329/movieclaw/cli/internal/sse"
)

// sortKeys 是种子结果的可排序字段（一律降序）。
var sortKeys = map[string]func(map[string]any) float64{
	"seeders":  func(r map[string]any) float64 { return numberOf(r["seeders"]) },
	"size":     func(r map[string]any) float64 { return numberOf(r["size_bytes"]) },
	"snatched": func(r map[string]any) float64 { return numberOf(r["snatched"]) },
}

func numberOf(value any) float64 {
	switch v := value.(type) {
	case float64:
		return v
	case int:
		return float64(v)
	}
	return 0
}

// snapshot 是 `mclaw search torrents` 落盘的结果快照，供 `mclaw download <行号>`
// 引用——省掉复制一条长下载链接。
type snapshot struct {
	Server  string           `json:"server"`
	Keyword string           `json:"keyword"`
	Items   []map[string]any `json:"items"`
}

func snapshotPath() string {
	return filepath.Join(config.Dir(), "last-search.json")
}

func saveSnapshot(shot snapshot) error {
	path := snapshotPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return clierr.Usagef("无法创建配置目录：%v", err)
	}
	encoded, err := json.Marshal(shot)
	if err != nil {
		return clierr.New("搜索快照序列化失败：%v", err)
	}
	if err := os.WriteFile(path, encoded, 0o600); err != nil {
		return clierr.Usagef("无法写入搜索快照：%s（%v）", path, err)
	}
	return nil
}

// loadSnapshot 读上次搜索快照。读不到或坏了都返回 nil——调用方给的提示
// （「先执行 mclaw search」）比一条解析错误有用。
func loadSnapshot() *snapshot {
	raw, err := os.ReadFile(snapshotPath())
	if err != nil {
		return nil
	}
	var shot snapshot
	if err := json.Unmarshal(raw, &shot); err != nil {
		return nil
	}
	return &shot
}

// NewSearchGroup 构造 `mclaw search` 组：三条精选子命令 + 生成层随后并入的
// history / presets 等命令，首参不是子命令名时转交给 torrents。
func NewSearchGroup() *cobra.Command {
	group := newDefaultCommandGroup("search",
		"统一搜索影视条目、PT 种子和本地媒体库，并管理搜索预设与历史", "torrents")
	group.AddCommand(newSearchTitlesCommand())
	group.AddCommand(newSearchTorrentsCommand())
	group.AddCommand(newSearchLibraryItemsCommand())
	return group
}

func newSearchTitlesCommand() *cobra.Command {
	var provider string
	var incognito bool
	cmd := &cobra.Command{
		Use:   "titles <关键词>",
		Short: "按片名搜索 TMDB、豆瓣或全部影视来源",
		Long: `搜索影视条目。

示例：

    mclaw search titles "沙丘" --provider all

TMDB 与豆瓣单边失败时仍会返回另一边结果；默认记录搜索历史。`,
		Args: cobra.ExactArgs(1),
	}
	cmd.Flags().StringVar(&provider, "provider", "all", "影视数据来源：all / tmdb / douban")
	cmd.Flags().BoolVar(&incognito, "incognito", false, "无痕搜索：不写入服务端搜索历史")
	return withOverrides(cmd, []string{"provider", "incognito"},
		func(s *Settings, _ *cobra.Command, args []string) error {
			if provider != "all" && provider != "tmdb" && provider != "douban" {
				return clierr.Usagef("--provider 只能是 all、tmdb 或 douban，收到 %q", provider)
			}
			client, err := s.NewAPI()
			if err != nil {
				return err
			}
			result, err := client.Request("POST", "/search/titles", nil, map[string]any{
				"query":        args[0],
				"provider":     provider,
				"save_history": !incognito,
			})
			if err != nil {
				return err
			}
			// 单边来源失败不影响另一边的结果，如实说明后照常输出
			for _, item := range jsonval.Array(jsonval.At(result, "providers")) {
				status := jsonval.Object(item)
				if jsonval.Truthy(status["success"]) {
					continue
				}
				message := jsonval.Str(status["message"])
				if message == "" {
					message = "未知错误"
				}
				output.Info("%s 搜索失败：%s", jsonval.Str(status["provider"]), message)
			}
			return output.Emit(jsonval.At(result, "titles"), s.Output, s.Quiet)
		})
}

func newSearchLibraryItemsCommand() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "library-items <关键词>",
		Short: "搜索全部可见媒体库中的已入库条目",
		Long: `按标题或原名搜索本地媒体库。

示例：

    mclaw search library-items "沙丘"`,
		Args: cobra.ExactArgs(1),
	}
	return withOverrides(cmd, nil, func(s *Settings, _ *cobra.Command, args []string) error {
		client, err := s.NewAPI()
		if err != nil {
			return err
		}
		result, err := client.Request("GET", "/search/library-items",
			url.Values{"keyword": {args[0]}}, nil)
		if err != nil {
			return err
		}
		return output.Emit(result, s.Output, s.Quiet)
	})
}

func newSearchTorrentsCommand() *cobra.Command {
	var categories, sites []string
	var page, limit int
	var sortKey, resolution string
	var freeOnly, incognito, streamEvents bool

	cmd := &cobra.Command{
		Use:   "torrents <关键词>",
		Short: "跨 PT 站点搜索种子（结果行号可直接下载）",
		Long: `跨站点搜索种子。

快的站点先出结果（进度在 stderr），全部站点返回后输出按 --sort 排序的
稳定结果；每行的 row 行号可直接用于下载：

    mclaw search torrents "沙丘2" --resolution 2160p

    mclaw download 3          # 下载上面结果里的第 3 行`,
		Args: cobra.ExactArgs(1),
	}
	flags := cmd.Flags()
	flags.StringArrayVar(&categories, "category", nil, "分类过滤（可多次指定）")
	flags.StringArrayVar(&sites, "site", nil, "限定站点（可多次指定，站点 id）")
	flags.IntVar(&page, "page", 1, "页码（各站点独立分页）")
	flags.StringVar(&sortKey, "sort", "seeders", "排序字段（降序）：seeders / size / snatched")
	flags.StringVar(&resolution, "resolution", "", "按分辨率过滤（如 2160p / 1080p）")
	flags.BoolVar(&freeOnly, "free-only", false, "只要免费种")
	flags.IntVar(&limit, "limit", 50, "输出条数上限")
	flags.BoolVar(&incognito, "incognito", false, "无痕搜索：不写入服务端搜索历史")
	flags.BoolVar(&streamEvents, "stream-events", false, "逐事件输出 NDJSON（增量消费方用；不落快照）")

	taken := []string{"category", "site", "page", "sort", "resolution", "free-only",
		"limit", "incognito", "stream-events"}
	return withOverrides(cmd, taken, func(s *Settings, _ *cobra.Command, args []string) error {
		if _, ok := sortKeys[sortKey]; !ok {
			return clierr.Usagef("--sort 只能是 seeders、size 或 snatched，收到 %q", sortKey)
		}
		keyword := args[0]
		params := url.Values{"keyword": {keyword}, "page": {strconv.Itoa(page)}}
		for _, item := range categories {
			params.Add("categories", item)
		}
		for _, item := range sites {
			params.Add("sites", item)
		}
		if incognito {
			params.Set("no_history", "true")
		}

		client, err := s.NewAPI()
		if err != nil {
			return err
		}
		var collected []map[string]any
		sawDone := false
		streamErr := runTorrentSearch(client, params, keyword, streamEvents, &collected, &sawDone)
		if streamErr != nil && !sawDone {
			return streamErr
		}
		if !sawDone {
			// 流在 done 之前闭合：结果不完整，绝不能当完整结果输出/落快照
			return clierr.Newf(clierr.Network,
				"搜索流提前中断，结果不完整（仅收到 %d 条，未落快照）", len(collected)).
				WithHint("网络可能不稳或服务已重启，请重试 mclaw search torrents")
		}
		if streamEvents {
			return nil
		}

		// 客户端侧筛选/排序：与页面筛选弹层同语义，但发生在完整结果集上
		rows := collected
		if resolution != "" {
			var kept []map[string]any
			for _, row := range rows {
				attrs := jsonval.Object(row["attrs"])
				if strings.EqualFold(jsonval.Str(attrs["resolution"]), resolution) {
					kept = append(kept, row)
				}
			}
			rows = kept
		}
		if freeOnly {
			var kept []map[string]any
			for _, row := range rows {
				if jsonval.Truthy(row["free"]) {
					kept = append(kept, row)
				}
			}
			rows = kept
		}
		key := sortKeys[sortKey]
		sort.SliceStable(rows, func(i, j int) bool { return key(rows[i]) > key(rows[j]) })
		if total := len(rows); total > limit {
			output.Info("共 %d 条，已截断到前 %d 条（--limit 可调）", total, limit)
			rows = rows[:limit]
		}

		// 快照供 mclaw download <行号> 引用（行号 = 截断排序后的展示顺序）
		if err := saveSnapshot(snapshot{Server: client.Server, Keyword: keyword, Items: rows}); err != nil {
			return err
		}
		views := make([]map[string]any, 0, len(rows))
		for i, row := range rows {
			views = append(views, rowView(i+1, row))
		}
		if err := output.Emit(views, s.Output, s.Quiet); err != nil {
			return err
		}
		if len(rows) > 0 {
			output.Info("下载某一行：mclaw download <row>")
		}
		return nil
	})
}

// runTorrentSearch 消费搜索流：站点进度打 stderr，结果收进 collected。
// --stream-events 形态下直接把每帧转成 NDJSON 打到 stdout，不做聚合。
func runTorrentSearch(
	client *api.Client,
	params url.Values,
	keyword string,
	raw bool,
	collected *[]map[string]any,
	sawDone *bool,
) error {
	return streamEvents(client, "/search/torrents/stream", params, "",
		func(event sse.Event, payload map[string]any) bool {
			if raw {
				line := map[string]any{"event": event.Event}
				for key, value := range payload {
					line[key] = value
				}
				if encoded, err := json.Marshal(line); err == nil {
					os.Stdout.Write(append(encoded, '\n'))
				}
				if event.Event == "done" {
					*sawDone = true
					return false
				}
				return true
			}
			switch event.Event {
			case "start":
				output.Info("开始搜索「%s」，共 %d 个站点",
					keyword, len(jsonval.Array(payload["sites"])))
			case "site_result":
				for _, item := range jsonval.Array(payload["items"]) {
					if row := jsonval.Object(item); row != nil {
						*collected = append(*collected, row)
					}
				}
				output.Info("  %s：%s 条（%sms）", jsonval.Str(payload["site_name"]),
					jsonval.Plain(payload["count"]), jsonval.Plain(payload["elapsed_ms"]))
			case "site_error":
				output.Info("  %s：失败——%s",
					jsonval.Str(payload["site_name"]), jsonval.Str(payload["error"]))
			case "done":
				*sawDone = true
				output.Info("完成：共 %s 条（%sms）",
					jsonval.Plain(payload["total"]), jsonval.Plain(payload["elapsed_ms"]))
				return false
			}
			return true
		})
}

// rowView 是种子结果在表格里的展示形态：行号在最前，方便直接 mclaw download。
func rowView(index int, hit map[string]any) map[string]any {
	attrs := jsonval.Object(hit["attrs"])
	return map[string]any{
		"row":        index,
		"title":      hit["title"],
		"size":       hit["size"],
		"seeders":    hit["seeders"],
		"resolution": attrs["resolution"],
		"group":      attrs["release_group"],
		"site":       hit["site_name"],
		"free":       hit["free"],
	}
}
