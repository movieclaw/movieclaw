package overlay

import (
	"encoding/json"
	"strconv"
	"strings"

	"github.com/spf13/cobra"
	"github.com/yipengfei329/movieclaw/cli/internal/api"
	"github.com/yipengfei329/movieclaw/cli/internal/clierr"
	"github.com/yipengfei329/movieclaw/cli/internal/jsonval"
	"github.com/yipengfei329/movieclaw/cli/internal/output"
)

// NewDownloadCommand 构造 `mclaw download`：把一条种子提交到默认下载器。
func NewDownloadCommand() *cobra.Command {
	var siteID, torrentURL, savePath string
	var libraryID, tmdbID int
	var downloaderDefault bool

	cmd := &cobra.Command{
		Use:   "download [行号]",
		Short: "下载种子（用搜索结果行号，或显式给站点+链接）",
		Long: `把一条种子提交到默认下载器。

两种形态：

    mclaw download 3                          # 上次 mclaw search 结果的第 3 行

    mclaw download --site-id mteam --url ...  # 显式指定（脚本/跨会话）

行号形态默认与 Web 下载弹窗一样，先识别 TMDB 身份并预演路由；只有
身份唯一且路由可用才提交。歧义时用 --tmdb-id 确认候选后重试。

--library / --save-path 可显式覆盖自动识别；确实只想交给下载器自身决定
保存位置时，使用 --downloader-default。`,
		Args: cobra.MaximumNArgs(1),
	}
	flags := cmd.Flags()
	flags.StringVar(&siteID, "site-id", "", "显式形态：种子所属站点 id（与 --url 搭配，替代行号）")
	flags.StringVar(&torrentURL, "url", "", "显式形态：种子下载入口 download_url")
	flags.IntVar(&libraryID, "library", 0, "显式指定入库目标库 id，跳过智能选库")
	flags.StringVar(&savePath, "save-path", "", "显式指定保存目录（movieclaw 视角），跳过智能入库")
	flags.BoolVar(&downloaderDefault, "downloader-default", false, "跳过智能入库，使用下载器默认目录")
	flags.IntVar(&tmdbID, "tmdb-id", 0, "自动识别有歧义时，指定候选 TMDB ID 后重试")

	taken := []string{"site-id", "url", "library", "save-path", "downloader-default", "tmdb-id"}
	return withOverrides(cmd, taken, func(s *Settings, c *cobra.Command, args []string) error {
		hasRow := len(args) == 1
		row := 0
		if hasRow {
			parsed, err := strconv.Atoi(args[0])
			if err != nil {
				return clierr.Usagef("行号必须是整数，收到 %q", args[0]).
					WithHint("行号以 mclaw search 输出的 row 列为准；显式形态用 --site-id + --url")
			}
			row = parsed
		}
		if hasRow && (siteID != "" || torrentURL != "") {
			return clierr.Usagef("行号不能与 --site-id/--url 同时使用")
		}
		if !hasRow && (siteID != "") != (torrentURL != "") {
			return clierr.Usagef("--site-id 与 --url 必须同时提供")
		}
		explicitTargets := 0
		for _, given := range []bool{c.Flags().Changed("library"), savePath != "", downloaderDefault} {
			if given {
				explicitTargets++
			}
		}
		if explicitTargets > 1 {
			return clierr.Usagef("--library、--save-path 与 --downloader-default 只能选择一个")
		}
		if c.Flags().Changed("tmdb-id") && explicitTargets > 0 {
			return clierr.Usagef("--tmdb-id 不能与显式保存目标同时使用")
		}
		if c.Flags().Changed("tmdb-id") && !hasRow {
			return clierr.Usagef("--tmdb-id 只能用于搜索结果行号下载")
		}

		client, err := s.NewAPI()
		if err != nil {
			return err
		}

		var body map[string]any
		var hit *jsonval.Map
		switch {
		case hasRow:
			hit, err = snapshotRow(client.Server, row)
			if err != nil {
				return err
			}
			attrs := jsonval.Object(hit.Get("attrs"))
			titles := append(jsonval.Array(attrs.Get("titles_zh")), jsonval.Array(attrs.Get("titles_en"))...)
			body = map[string]any{
				"site_id":      hit.Get("site_id"),
				"download_url": hit.Get("download_url"),
				"year":         attrs.Get("year"),
				"subtitle":     emptyToNil(jsonval.Str(hit.Get("subtitle"))),
			}
			if len(titles) > 0 {
				body["title"] = titles[0]
			} else {
				body["title"] = nil
			}
			output.Info("下载：%s（%s）", jsonval.Str(hit.Get("title")), jsonval.Str(hit.Get("site_name")))
		case siteID != "" && torrentURL != "":
			body = map[string]any{"site_id": siteID, "download_url": torrentURL}
		default:
			return clierr.Usagef("请给出搜索结果行号，或同时提供 --site-id 与 --url")
		}

		// Web 用弹窗显式选择；CLI/Agent 没有中途交互，因此行号下载默认走
		// 智能选项，只有显式保存目标才跳过预检。
		switch {
		case hasRow && explicitTargets == 0:
			routed, err := autoRouteBody(client, hit, row, tmdbID, c.Flags().Changed("tmdb-id"), s.Output)
			if err != nil {
				return err
			}
			for key, value := range routed {
				body[key] = value
			}
		case c.Flags().Changed("library"):
			body["library_id"] = libraryID
		case savePath != "":
			body["save_path"] = savePath
		}

		data, err := client.Request("POST", "/downloaders/submit", nil, body)
		if err != nil {
			return err
		}
		if client.LastMessage != "" && !s.Quiet {
			output.Info("%s", client.LastMessage)
		}
		return output.Emit(data, s.Output, s.Quiet)
	})
}

// snapshotRow 取出上次搜索快照里的第 row 行。
//
// 快照带服务器地址：换了服务器还按老行号下载会把种子投到另一台机器上，
// 那是最难排查的一类错误，所以宁可直接拒绝。
func snapshotRow(server string, row int) (*jsonval.Map, error) {
	shot := loadSnapshot()
	if shot == nil {
		return nil, clierr.Usagef("没有可用的搜索快照").
			WithHint("先执行 mclaw search <关键词>，或改用 --site-id + --url 显式指定")
	}
	if shot.Server != "" && shot.Server != server {
		return nil, clierr.Usagef("搜索快照来自另一台服务器（%s），不能提交到 %s", shot.Server, server).
			WithHint("在当前服务器重新执行 mclaw search 后再用行号下载")
	}
	if row < 1 || row > len(shot.Items) {
		return nil, clierr.Usagef("行号超出范围：%d（上次搜索「%s」共 %d 条）",
			row, shot.Keyword, len(shot.Items)).
			WithHint("行号以 mclaw search 输出的 row 列为准")
	}
	return jsonval.Object(shot.Items[row-1]), nil
}

// autoRouteBody 复用 Web 的「识别预检 → 确认身份」协议，返回真实提交所需字段。
//
// CLI 面向脚本和 Agent，不能像弹窗一样停下来等点击。因此唯一身份自动继续；
// 歧义把结构化候选写到 stdout 并以退出码 7 停止，调用方可带 --tmdb-id 重试。
// 任何不确定或不可入库状态都不会静默落下载器默认目录。
func autoRouteBody(
	client *api.Client, hit *jsonval.Map, row, tmdbID int, tmdbGiven bool, format string,
) (map[string]any, error) {
	kind, title, year, ok := torrentIdentity(hit)
	if !ok {
		return nil, clierr.New("搜索结果缺少可靠的媒体类型、片名或年份，未提交下载").
			WithHint("可指定 --library <id> 或 --save-path <目录>；" +
				"确认无需自动入库时显式加 --downloader-default")
	}
	resolveBody := map[string]any{
		"kind":     kind,
		"title":    title,
		"year":     year,
		"subtitle": emptyToNil(jsonval.Str(hit.Get("subtitle"))),
	}
	if tmdbGiven {
		resolveBody["selected_tmdb_id"] = tmdbID
	}
	raw, err := client.Request("POST", "/downloaders/resolve-target", nil, resolveBody)
	if err != nil {
		return nil, err
	}
	target := jsonval.Object(raw)
	switch status := jsonval.Str(target.Get("status")); status {
	case "ambiguous":
		// 歧义是机器可恢复状态：stdout 保持结构化，stderr 只写下一步。
		if err := output.Emit(jsonval.NewMap(
			"status", status,
			"candidates", jsonval.Array(target.Get("candidates")),
		), format, false); err != nil {
			return nil, err
		}
		return nil, clierr.Newf(clierr.Ambiguous, "识别到多个可能的影视条目，未提交下载").
			WithHint("确认候选后重试：mclaw download %d --tmdb-id <候选 tmdb_id>", row)
	case "ready":
		if target.Get("tmdb_id") == nil {
			return nil, notIdentified()
		}
	default:
		return nil, notIdentified()
	}
	if !jsonval.Truthy(target.Get("ok")) || target.Get("library_id") == nil {
		message := jsonval.Str(target.Get("warning"))
		if message == "" {
			message = "已识别资源，但当前配置不能完成自动入库"
		}
		return nil, clierr.New("%s", message).
			WithHint("请按提示修复媒体库、监听目录或路径映射；也可显式指定 " +
				"--library/--save-path，或用 --downloader-default 跳过自动入库")
	}

	route := jsonval.Str(target.Get("route_reason"))
	if route == "" {
		route = "入库到「" + jsonval.Str(target.Get("library_name")) + "」"
	}
	if path := jsonval.Str(target.Get("path")); path != "" {
		output.Info("智能入库：%s；投递目录 %s", route, path)
	} else {
		output.Info("智能入库：%s", route)
	}
	return map[string]any{
		"auto_route": true,
		"media_kind": kind,
		"tmdb_id":    target.Get("tmdb_id"),
		"title":      title,
		"year":       year,
		"subtitle":   emptyToNil(jsonval.Str(hit.Get("subtitle"))),
	}, nil
}

func notIdentified() error {
	return clierr.New("未能可靠识别该资源，未提交下载").
		WithHint("可指定 --library <id> 或 --save-path <目录>；" +
			"确认无需自动入库时显式加 --downloader-default")
}

// torrentIdentity 按 Web 下载弹窗的同一门槛，从搜索结果提取智能入库身份三件套。
func torrentIdentity(hit *jsonval.Map) (kind, title string, year int, ok bool) {
	attrs := jsonval.Object(hit.Get("attrs"))
	titles := append(jsonval.Array(attrs.Get("titles_zh")), jsonval.Array(attrs.Get("titles_en"))...)
	kind = jsonval.Str(attrs.Get("media_type"))
	if kind != "movie" && kind != "tv" {
		return "", "", 0, false
	}
	if len(titles) == 0 {
		return "", "", 0, false
	}
	title = strings.TrimSpace(jsonval.Str(titles[0]))
	if title == "" {
		return "", "", 0, false
	}
	// 年份必须是整数：JSON 里 1994.0 与字符串 "1994" 都不接受，
	// 与 Web 端的门槛保持一致，宁可让用户显式指定目标。
	number, isNumber := attrs.Get("year").(json.Number)
	if !isNumber {
		return "", "", 0, false
	}
	value, err := number.Int64()
	if err != nil {
		return "", "", 0, false
	}
	year = int(value)
	if year < 1888 || year > 2100 {
		return "", "", 0, false
	}
	return kind, title, year, true
}

// emptyToNil 把空串转成 nil：这些字段在 API 里是可空的，空串和「没有」不是一回事。
func emptyToNil(value string) any {
	if value == "" {
		return nil
	}
	return value
}
