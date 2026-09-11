package overlay

import (
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/movieclaw/movieclaw/cli/internal/clierr"
	"github.com/movieclaw/movieclaw/cli/internal/flagx"
	"github.com/movieclaw/movieclaw/cli/internal/jsonval"
	"github.com/movieclaw/movieclaw/cli/internal/output"
	"github.com/movieclaw/movieclaw/cli/internal/wait"
	"github.com/spf13/cobra"
)

// NewLibraryGroup 构造 `mclaw library` 组；生成层随后并入其余媒体库命令。
//
// 组里这两条命令各自编排「预览 → 明确确认 → 执行」，隐藏底层工作流端点：
// 页面上那是一个四段式对话框，CLI 把它固化成一条命令——任何正式执行都
// **强制先预览并回显影响面**，再要求 --yes。
func NewLibraryGroup() *cobra.Command {
	group := &cobra.Command{
		Use: "library",
		Short: "管理本地电影/剧集媒体库、库存文件、识别结果、元数据、图片与字幕；" +
			"organize-files 按命名模板整理存量文件名（改名字、留在原根），" +
			"consolidate-roots 把多个根下的内容并到一个根（换位置、不碰名字）",
		RunE:         func(cmd *cobra.Command, _ []string) error { return cmd.Help() },
		SilenceUsage: true,
	}
	group.Long = group.Short
	group.AddCommand(newLibraryOrganizeFilesCommand())
	group.AddCommand(newLibraryReconcilePathsCommand())
	group.AddCommand(newLibraryItemsGroup())
	group.AddCommand(newLibraryConsolidateRootsCommand())
	return group
}

func newLibraryOrganizeFilesCommand() *cobra.Command {
	var dryRun, waitDone bool
	var waitTimeout time.Duration
	cmd := &cobra.Command{
		Use:   "organize-files <library_id>",
		Short: "按配置的命名模板整理存量文件名（预览影响面 → --yes 确认 → 执行并等待）",
		Long: `按当前生效的命名模板批量改名归位库内文件（默认模板即 Emby/Plex 规范）。

模板在 mclaw scrape set 里配（也可按库覆盖）。**改了模板就跑一次这个命令**，
存量文件才会跟着变——可以反复改、反复整理，条目目录改名时海报/NFO/字幕/
分集剧照都跟着搬，不会留下空壳目录。

示例：

    mclaw library organize-files 1 --dry-run     # 只看计划

    mclaw library organize-files 1 --yes         # 执行（先回显影响面）

源文件绝不删除；执行与扫描互斥（扫描进行中会被服务端拒绝）。`,
		Args: cobra.ExactArgs(1),
	}
	cmd.Flags().BoolVar(&dryRun, "dry-run", false, "只输出整理计划，不动磁盘")
	cmd.Flags().BoolVar(&waitDone, "wait", true, "等待整理完成")
	flagx.Var(cmd.Flags(), &waitTimeout, "wait-timeout", time.Hour, "--wait 的最长等待秒数")

	return withOverrides(cmd, []string{"dry-run", "wait", "wait-timeout"},
		func(s *Settings, _ *cobra.Command, args []string) error {
			libraryID, err := parseLibraryID(args[0])
			if err != nil {
				return err
			}
			client, err := s.NewAPI()
			if err != nil {
				return err
			}
			base := "/libraries/" + libraryID
			raw, err := client.Request("POST", base+"/file-organization-preview", nil, nil)
			if err != nil {
				return err
			}
			preview := jsonval.Object(raw)
			renames := jsonval.Array(preview.Get("renames"))
			output.Info("整理计划：共 %s 个文件——改名 %d 项，已规范 %s 项，跳过 %d 项",
				jsonval.Plain(preview.Get("total")), len(renames),
				jsonval.Plain(preview.Get("already_ok")), len(jsonval.Array(preview.Get("skips"))))
			if dryRun {
				return output.Emit(preview, s.Output, s.Quiet)
			}
			if len(renames) == 0 {
				output.Info("没有需要整理的文件")
				return nil
			}
			if !s.Yes {
				if err := output.Emit(preview, s.Output, s.Quiet); err != nil {
					return err
				}
				return clierr.Newf(clierr.NeedConfirm, "即将改名 %d 个文件，需要确认", len(renames)).
					WithHint("核对上面的整理计划后重跑并加 --yes；只看计划用 --dry-run")
			}
			started, err := client.Request("POST", base+"/file-organizations", nil, nil)
			if err != nil {
				return err
			}
			if client.LastMessage != "" && !s.Quiet {
				output.Info("%s", client.LastMessage)
			}
			if err := output.Emit(started, s.Output, s.Quiet); err != nil {
				return err
			}
			if !waitDone {
				return nil
			}
			// 与生成层的 library.scan 共用同一套等待：整理进度挂在库详情上
			return wait.Long(client, wait.LongTask{
				ProgressPath:    "/libraries/" + libraryID,
				ProgressField:   "organize_progress",
				ProgressCommand: "mclaw library get " + libraryID,
			}, waitTimeout)
		})
}

func newLibraryReconcilePathsCommand() *cobra.Command {
	var oldRoot, newRoot string
	var dryRun bool
	cmd := &cobra.Command{
		Use:   "reconcile-paths <library_id>",
		Short: "修复旧根路径遗留台账（预览影响面 → --yes 执行）",
		Long: `收口容器挂载前缀变更后遗留的旧路径台账。

正式执行会以持久化扫描作业重新盘点目标根；只会合并、标记或删除
library_file 台账记录，绝不会删除任何媒体文件。`,
		Args: cobra.ExactArgs(1),
	}
	cmd.Flags().StringVar(&oldRoot, "old-root", "", "已移除的旧根路径")
	cmd.Flags().StringVar(&newRoot, "new-root", "", "当前媒体库配置中的目标根路径")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false, "只输出修复预览，不扫描、不修改台账")
	_ = cmd.MarkFlagRequired("old-root")
	_ = cmd.MarkFlagRequired("new-root")

	return withOverrides(cmd, []string{"old-root", "new-root", "dry-run"},
		func(s *Settings, _ *cobra.Command, args []string) error {
			libraryID, err := parseLibraryID(args[0])
			if err != nil {
				return err
			}
			client, err := s.NewAPI()
			if err != nil {
				return err
			}
			base := "/libraries/" + libraryID
			body := map[string]any{"old_root": oldRoot, "new_root": newRoot}
			raw, err := client.Request("POST", base+"/path-reconciliation-preview", nil, body)
			if err != nil {
				return err
			}
			preview := jsonval.Object(raw)
			output.Info("路径迁移预览：同相对路径 %s，可安全合并 %s，将标记缺失 %s，身份冲突 %d，磁盘删除 0",
				plainOrZero(preview.Get("same_path_candidates")), plainOrZero(preview.Get("safe_merges")),
				plainOrZero(preview.Get("marked_missing")), len(jsonval.Array(preview.Get("conflicts"))))
			if dryRun {
				return output.Emit(preview, s.Output, s.Quiet)
			}
			if !s.Yes {
				if err := output.Emit(preview, s.Output, s.Quiet); err != nil {
					return err
				}
				return clierr.Newf(clierr.NeedConfirm, "路径迁移修复会修改旧路径台账，需要确认").
					WithHint("核对预览后重跑并加 --yes；只看预览用 --dry-run")
			}
			started, err := client.Request("POST", base+"/path-reconciliations", nil, body)
			if err != nil {
				return err
			}
			if client.LastMessage != "" && !s.Quiet {
				output.Info("%s", client.LastMessage)
			}
			return output.Emit(started, s.Output, s.Quiet)
		})
}

// parseLibraryID 校验并回传库 id 的字符串形态（要拼进 URL）。
func parseLibraryID(raw string) (string, error) {
	if _, err := strconv.Atoi(raw); err != nil {
		return "", clierr.Usagef("媒体库 id 必须是整数，收到 %q", raw).
			WithHint("用 mclaw library list 查看可用的库 id")
	}
	return raw, nil
}

// plainOrZero 渲染计数字段；服务端没给这个字段时显示 0 而不是空白。
func plainOrZero(value any) string {
	if value == nil {
		return "0"
	}
	return jsonval.Plain(value)
}

// newLibraryItemsGroup 预建 `mclaw library items` 组，只为收编手写的 transfer；
// 其余 library.items.* 生成命令会被建树过程并入同一个组。
func newLibraryItemsGroup() *cobra.Command {
	const help = "查看和管理已经入库的电影、剧集条目及其物理文件"
	group := &cobra.Command{
		Use:          "items",
		Short:        help,
		Long:         help,
		RunE:         func(cmd *cobra.Command, _ []string) error { return cmd.Help() },
		SilenceUsage: true,
	}
	group.AddCommand(newLibraryItemsTransferCommand())
	return group
}

// newLibraryItemsTransferCommand 把「转移条目到另一个库」做成单条与批量同一条命令。
//
// 为什么同名覆盖生成命令而不是新起一个：批量是单条的泛化，不是另一件事。
// `... transfer 3 42 --to 7` 是搬一部，`... transfer 3 --all --to 7` 是搬整库，
// 命令面不因为多了批量而变胖。任何正式执行都强制先预检并回显影响面再要 --yes。
func newLibraryItemsTransferCommand() *cobra.Command {
	var (
		targetLibrary int
		items         []string
		itemsFrom     string
		all           bool
		onConflict    string
		dryRun        bool
		waitDone      bool
		waitTimeout   time.Duration
	)
	cmd := &cobra.Command{
		Use:   "transfer <library_id> [media_item_id]",
		Short: "把条目转移到另一个媒体库（预检影响面 → --yes 确认 → 执行）",
		Long: `把一部或一批作品连同磁盘目录搬到另一个媒体库，台账一起随迁。

用于合并媒体库、纠正分错库的作品（最典型的是韩剧被路由进了「大陆华语剧」）。

选择哪些条目（四选一）：

    mclaw library items transfer 3 42 --to 7          # 就这一部
    mclaw library items transfer 3 --item 42,43 --to 7
    mclaw library items transfer 3 --all --to 7       # 整个库
    mclaw library items list 3 --c KR --all -o json \
      | mclaw library items transfer 3 --to 7 --items-from -

筛选走管道而不是在这条命令上重造：筛选面已经在 library items list 上，
复制一份必然分叉。--items-from 既吃 items list 的 JSON，也吃一行一个 id
的纯文本，所以失败重跑可以直接把上一次的结论喂回来：

    mclaw jobs show <job_id> -o json \
      | mclaw library items transfer 3 --to 7 --items-from - --yes

执行前会先打出预检：要搬多少、目标盘够不够（同盘搬运是瞬间完成的改名，
不占新空间）、哪些同名、跨盘会断开哪些做种硬链接（那部分源盘不会释放）。
核对之后加 --yes 才真正执行。

绝不覆盖目标已有的内容；单条出问题只跳过那一条，其余照搬。`,
		Args: cobra.RangeArgs(1, 2),
	}
	flags := cmd.Flags()
	flags.IntVar(&targetLibrary, "target-library-id", 0, "转移目标库 id（必须与当前库同类型）")
	flags.IntVar(&targetLibrary, "to", 0, "--target-library-id 的简写")
	flags.StringSliceVar(&items, "item", nil, "要转移的条目 id，可重复或逗号分隔")
	flags.StringVar(&itemsFrom, "items-from", "",
		"从文件读条目 id（- 表示标准输入）；接受 items list 的 JSON 或一行一个 id")
	flags.BoolVar(&all, "all", false, "转移该库的全部条目")
	flags.StringVar(&onConflict, "on-conflict", "skip",
		"目标已有同名目录时：skip=跳过这一条其余照搬（缺省）；"+
			"merge=同一部作品的其他版本就并进去（撞名的退让成「标题 - 分辨率.ext」，绝不覆盖）；"+
			"fail=整批中止")
	flags.BoolVar(&dryRun, "dry-run", false, "只输出预检，不动磁盘")
	flags.BoolVar(&waitDone, "wait", false, "等待转移完成（跨盘搬大库可能要几小时，缺省不等）")
	flagx.Var(flags, &waitTimeout, "wait-timeout", 6*time.Hour, "--wait 的最长等待秒数")

	taken := []string{
		"target-library-id", "to", "item", "items-from", "all",
		"on-conflict", "dry-run", "wait", "wait-timeout",
	}
	return withOverrides(cmd, taken, func(s *Settings, c *cobra.Command, args []string) error {
		libraryID, err := parseLibraryID(args[0])
		if err != nil {
			return err
		}
		ids, err := collectItemIDs(args, items, itemsFrom, c.InOrStdin())
		if err != nil {
			return err
		}
		if !all && len(ids) == 0 {
			return clierr.Usagef("没有指定要转移哪些条目").
				WithHint("给出位置参数 <media_item_id>，或用 --item / --items-from / --all")
		}
		if targetLibrary == 0 {
			return clierr.Usagef("必须指定目标媒体库").
				WithHint("加 --to <library_id>；用 mclaw library list 查看可用的库 id")
		}
		client, err := s.NewAPI()
		if err != nil {
			return err
		}
		body := map[string]any{
			"target_library_id": targetLibrary,
			"media_item_ids":    ids,
			"all_items":         all,
			"on_conflict":       onConflict,
		}
		base := "/libraries/" + libraryID
		raw, err := client.Request("POST", base+"/item-transfer-preview", nil, body)
		if err != nil {
			return err
		}
		preview := jsonval.Object(raw)
		reportTransferPreflight(preview)
		if blocked := jsonval.Array(preview.Get("blocked")); len(blocked) > 0 {
			reasons := make([]string, 0, len(blocked))
			for _, item := range blocked {
				reasons = append(reasons, jsonval.Plain(item))
			}
			return clierr.New("%s", strings.Join(reasons, "；")).
				WithHint("处理上面的问题后重试；只看预检用 --dry-run")
		}
		if dryRun {
			return output.Emit(preview, s.Output, s.Quiet)
		}
		movable := jsonval.Int(preview.Get("movable"))
		if movable == 0 {
			output.Info("没有可以转移的条目")
			return nil
		}
		if !s.Yes {
			if err := output.Emit(preview, s.Output, s.Quiet); err != nil {
				return err
			}
			err := clierr.Newf(clierr.NeedConfirm, "即将转移 %d 个条目，需要确认", movable)
			hint := "核对上面的预检后重跑并加 --yes；只看预检用 --dry-run"
			// 有可合并的同名时主动指路：缺省 skip 会让用户白跑一次
			if same := jsonval.Int(jsonval.Object(preview.Get("conflicts")).Get("same_anchor")); same > 0 {
				hint += fmt.Sprintf("。其中 %d 个是目标库已有的同一部作品的其他版本，"+
					"按缺省策略会跳过；想把它们并进同一个条目目录就加 --on-conflict merge", same)
			}
			return err.WithHint("%s", hint)
		}
		started, err := client.Request("POST", base+"/item-transfers", nil, body)
		if err != nil {
			return err
		}
		if client.LastMessage != "" && !s.Quiet {
			output.Info("%s", client.LastMessage)
		}
		if err := output.Emit(started, s.Output, s.Quiet); err != nil {
			return err
		}
		jobID := jsonval.Str(jsonval.Object(started).Get("job_id"))
		if !waitDone {
			if jobID != "" && !s.Quiet {
				output.Info("转移在后台进行；用 mclaw jobs wait %s 等它跑完", jobID)
			}
			return nil
		}
		return wait.Job(client, jobID, waitTimeout)
	})
}

// reportTransferPreflight 把预检里用户真正要判断的四件事讲成人话。
//
// 只报数字是不够的：同盘搬运不占新空间、跨盘会让源盘一字节都不释放（下载
// 目录还引用着硬链接）、同名的三种性质处理方式完全不同——这些不说清楚，
// 用户看到一个「共 593 部、29 TiB」根本没法做决定。
func reportTransferPreflight(preview *jsonval.Map) {
	selected := jsonval.Int(preview.Get("selected"))
	movable := jsonval.Int(preview.Get("movable"))
	output.Info("转移计划：选中 %d 个条目，可搬 %d 个", selected, movable)

	crossItems := jsonval.Int(preview.Get("cross_device_items"))
	if crossItems == 0 {
		output.Info("  全部在同一块盘上——搬运是瞬间完成的改名，不占用额外空间")
	} else {
		output.Info("  跨盘复制 %d 个（%s）；目标盘需要 %s，当前剩余 %s",
			crossItems,
			humanBytes(jsonval.Int(preview.Get("cross_device_bytes"))),
			humanBytes(jsonval.Int(preview.Get("target_required_bytes"))),
			humanBytes(jsonval.Int(preview.Get("target_free_bytes"))))
		if hard := jsonval.Int(preview.Get("hardlinked_items")); hard > 0 {
			output.Info("  其中 %d 个与做种目录有硬链接：复制后做种不受影响，"+
				"但源盘不会释放这 %s（源盘预计只释放 %s）",
				hard,
				humanBytes(jsonval.Int(preview.Get("hardlinked_bytes"))),
				humanBytes(jsonval.Int(preview.Get("source_reclaimable_bytes"))))
		}
	}

	conflicts := jsonval.Object(preview.Get("conflicts"))
	same := jsonval.Int(conflicts.Get("same_anchor"))
	other := jsonval.Int(conflicts.Get("different_anchor"))
	unknown := jsonval.Int(conflicts.Get("unknown"))
	if same+other+unknown > 0 {
		output.Info("  同名 %d 个：%d 个是目标库已有的同一部作品的其他版本，"+
			"%d 个只是目录重名的另一部片，%d 个目标位置有内容但媒体库没有记录——一律跳过",
			same+other+unknown, same, other, unknown)
	}
	if seeding := preview.Get("seeding_in_place_items"); seeding == nil {
		output.Info("  下载器连不上，无法确认是否有条目正被原地做种")
	} else if count := jsonval.Int(seeding); count > 0 {
		output.Info("  警告：%d 个条目的目录名与下载器中的任务同名，"+
			"若下载器直接对库内路径做种，搬走后这些做种任务会失效", count)
	}
}

// collectItemIDs 汇总三种选择集来源：位置参数、--item、--items-from。
func collectItemIDs(args, items []string, itemsFrom string, stdin io.Reader) ([]int, error) {
	var raw []string
	if len(args) == 2 {
		raw = append(raw, args[1])
	}
	raw = append(raw, items...)
	if itemsFrom != "" {
		fromFile, err := readItemIDs(itemsFrom, stdin)
		if err != nil {
			return nil, err
		}
		raw = append(raw, fromFile...)
	}
	seen := map[int]bool{}
	ids := make([]int, 0, len(raw))
	for _, entry := range raw {
		for _, token := range strings.Split(entry, ",") {
			token = strings.TrimSpace(token)
			if token == "" {
				continue
			}
			value, err := strconv.Atoi(token)
			if err != nil {
				return nil, clierr.Usagef("条目 id 必须是整数，收到 %q", token)
			}
			if !seen[value] {
				seen[value] = true
				ids = append(ids, value)
			}
		}
	}
	return ids, nil
}

// readItemIDs 从文件或标准输入读条目 id。
//
// 两种形态都认（写在 help 里，不靠猜）：mclaw 自己的 JSON 输出（抽其中的
// id / media_item_id 字段），或一行一个 id 的纯文本。前者让 Agent 不必
// 依赖 jq 就能把上一条命令的输出接进来。
func readItemIDs(path string, stdin io.Reader) ([]string, error) {
	var data []byte
	var err error
	if path == "-" {
		data, err = io.ReadAll(stdin)
	} else {
		data, err = os.ReadFile(path)
	}
	if err != nil {
		return nil, clierr.New("读取条目清单失败：%v", err)
	}
	if trimmed := strings.TrimSpace(string(data)); strings.HasPrefix(trimmed, "{") ||
		strings.HasPrefix(trimmed, "[") {
		value, decodeErr := jsonval.Decode([]byte(trimmed))
		if decodeErr != nil {
			return nil, clierr.New("条目清单不是合法的 JSON：%v", decodeErr)
		}
		// 先在整份文档里找 media_item_id，找不到才退回 id。顺序反过来会踩
		// 一个很实际的坑：mclaw jobs show 的输出顶层带着**作业**的 id，
		// 先序遍历会把它当成条目 id 取走，而失败明细里的 media_item_id
		// 反倒被忽略。
		ids := idsFromJSON(value, "media_item_id")
		if len(ids) == 0 {
			ids = idsFromJSON(value, "id")
		}
		if len(ids) == 0 {
			return nil, clierr.New("这份 JSON 里没有找到条目 id").
				WithHint("需要含 id 或 media_item_id 字段的对象数组，" +
					"例如 mclaw library items list <库id> -o json 的输出")
		}
		return ids, nil
	}
	var out []string
	for _, line := range strings.Split(string(data), "\n") {
		if line = strings.TrimSpace(line); line != "" {
			out = append(out, line)
		}
	}
	return out, nil
}

// idsFromJSON 递归收集 JSON 里指定字段的值。
//
// 递归是为了同时吃下裸数组（items list）和带信封的结论（jobs show 的
// result.failures）——两者都是用户手上现成的东西，不该逼他先用 jq 剥一层。
func idsFromJSON(value any, field string) []string {
	var out []string
	switch typed := value.(type) {
	case []any:
		for _, entry := range typed {
			out = append(out, idsFromJSON(entry, field)...)
		}
	case *jsonval.Map:
		if raw := typed.Get(field); raw != nil {
			if text := jsonval.Plain(raw); text != "" {
				return []string{text}
			}
		}
		for _, key := range typed.Keys() {
			out = append(out, idsFromJSON(typed.Get(key), field)...)
		}
	}
	return out
}

// humanBytes 把字节数写成人读的量级（预检文案里全是 TB 级数字）。
func humanBytes(value int) string {
	const unit = 1024
	if value < unit {
		return fmt.Sprintf("%d B", value)
	}
	size := float64(value)
	units := []string{"KiB", "MiB", "GiB", "TiB", "PiB"}
	index := -1
	for size >= unit && index < len(units)-1 {
		size /= unit
		index++
	}
	return fmt.Sprintf("%.1f %s", size, units[index])
}

// newLibraryConsolidateRootsCommand 把「换位置」做成库的原生操作。
//
// 与 organize-files 的分工要说死：整理**改名字**、永远留在当前根下；归并
// **换位置**、不碰名字。一次操作只做一件事，用户才说得清刚才那一下改了什么。
func newLibraryConsolidateRootsCommand() *cobra.Command {
	var (
		into        string
		fromRoots   []string
		dryRun      bool
		waitDone    bool
		waitTimeout time.Duration
	)
	cmd := &cobra.Command{
		Use:   "consolidate-roots <library_id>",
		Short: "把若干个根路径下的条目并到一个根（预检影响面 → --yes 确认 → 执行）",
		Long: `把媒体库里散在多个根路径下的条目，连同磁盘目录一起并到指定的那个根。

用于换盘、换挂载点，以及把历史遗留的多级分类目录拍平成一层：

    mclaw library consolidate-roots 3 --into /media/电影 --dry-run
    mclaw library consolidate-roots 3 --into /media/电影 --yes

--from 留空表示「除 --into 之外的全部根」（拍平目录最常见的意图）；
--into 允许是一个还不在媒体库配置里的新路径（换盘场景），归并会先把它加进
配置再开始搬——顺序反了文件会先落到库根之外，下次扫描会把它们全标缺失。

同一块盘上的归并是瞬间完成的改名，不占用额外空间，硬链接（做种）也完整保留。
全部搬完并且没有任何跳过或失败时，才会把源根从配置里摘掉。

与「整理文件名」的分工：整理改名字、留在原根；归并换位置、不碰名字。`,
		Args: cobra.ExactArgs(1),
	}
	flags := cmd.Flags()
	flags.StringVar(&into, "into", "", "要并到的目标根路径（可以是尚未配置的新路径）")
	flags.StringSliceVar(&fromRoots, "from", nil,
		"要并过来的源根路径，可重复；留空表示除 --into 外的全部根")
	flags.BoolVar(&dryRun, "dry-run", false, "只输出预检，不动磁盘")
	flags.BoolVar(&waitDone, "wait", false, "等待归并完成（跨盘搬大库可能要几小时，缺省不等）")
	flagx.Var(flags, &waitTimeout, "wait-timeout", 6*time.Hour, "--wait 的最长等待秒数")
	_ = cmd.MarkFlagRequired("into")

	taken := []string{"into", "from", "dry-run", "wait", "wait-timeout"}
	return withOverrides(cmd, taken, func(s *Settings, _ *cobra.Command, args []string) error {
		libraryID, err := parseLibraryID(args[0])
		if err != nil {
			return err
		}
		client, err := s.NewAPI()
		if err != nil {
			return err
		}
		body := map[string]any{"into": into, "from_roots": fromRoots}
		base := "/libraries/" + libraryID
		raw, err := client.Request("POST", base+"/root-consolidation-preview", nil, body)
		if err != nil {
			return err
		}
		preview := jsonval.Object(raw)
		sources := make([]string, 0, len(jsonval.Array(preview.Get("from_roots"))))
		for _, root := range jsonval.Array(preview.Get("from_roots")) {
			sources = append(sources, jsonval.Plain(root))
		}
		output.Info("归并计划：把 %s 下的内容并入 %s",
			strings.Join(sources, "、"), jsonval.Plain(preview.Get("into")))
		if jsonval.Truthy(preview.Get("into_is_new_root")) {
			output.Info("  目标根当前不在媒体库配置里，归并会先把它加进去")
		}
		reportTransferPreflight(preview)
		if blocked := jsonval.Array(preview.Get("blocked")); len(blocked) > 0 {
			reasons := make([]string, 0, len(blocked))
			for _, item := range blocked {
				reasons = append(reasons, jsonval.Plain(item))
			}
			return clierr.New("%s", strings.Join(reasons, "；")).
				WithHint("处理上面的问题后重试；只看预检用 --dry-run")
		}
		if dryRun {
			return output.Emit(preview, s.Output, s.Quiet)
		}
		movable := jsonval.Int(preview.Get("movable"))
		if movable == 0 {
			output.Info("这些根下没有需要搬运的条目")
			return nil
		}
		if !s.Yes {
			if err := output.Emit(preview, s.Output, s.Quiet); err != nil {
				return err
			}
			return clierr.Newf(clierr.NeedConfirm, "即将归并 %d 个条目，需要确认", movable).
				WithHint("核对上面的预检后重跑并加 --yes；只看预检用 --dry-run")
		}
		started, err := client.Request("POST", base+"/root-consolidations", nil, body)
		if err != nil {
			return err
		}
		if client.LastMessage != "" && !s.Quiet {
			output.Info("%s", client.LastMessage)
		}
		if err := output.Emit(started, s.Output, s.Quiet); err != nil {
			return err
		}
		jobID := jsonval.Str(jsonval.Object(started).Get("job_id"))
		if !waitDone {
			if jobID != "" && !s.Quiet {
				output.Info("归并在后台进行；用 mclaw jobs wait %s 等它跑完", jobID)
			}
			return nil
		}
		return wait.Job(client, jobID, waitTimeout)
	})
}
