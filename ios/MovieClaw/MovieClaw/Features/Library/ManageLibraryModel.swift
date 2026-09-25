import Foundation

// 媒体库管理页（/library/manage）的纯逻辑：状态归类、页头摘要、筛选、换位、可见范围文案、
// 收藏范围重叠提示。逐条镜像 Web `lib/library-manage.ts` 与 `lib/library-routing-warnings.ts`，
// 改口径请两端一起改。放在无 UI 的文件里便于对照（设计见 docs/design/library-manage.md §2.2）。

/// 状态列的语气：决定圆点 / 胶囊颜色（灰 / 蓝 / 黄 / 红）
enum ManageStatusTone { case idle, busy, pending, missing }

/// 一行库的状态（Web `LibraryStatus`）
struct ManageLibraryStatus: Equatable {
    enum Kind { case scan, organize, refresh, chapters, importing, missing, unidentified, idle }

    var tone: ManageStatusTone
    var kind: Kind
    /// 主文案（第一行）
    var title: String
    /// 补充文案（第二行）；没有则为空串
    var detail: String
    /// 0-100 的进度；分母未知或非进度型状态为 nil
    var percent: Int?

    /// 一行库的状态归类。优先级自上而下取第一个命中：
    /// 扫描 → 整理 → 刷新元数据 → 生成章节 → 入库中 → 有缺失 → 有待识别 → 空闲。
    /// 前三种长任务互斥（共用一把库级锁）；生成章节是低优先级后台作业，常排在它们后面。
    static func of(_ library: API.LibraryView) -> ManageLibraryStatus {
        if library.scanning {
            let p = library.scanProgress
            let percent = p.flatMap { percentOf($0.processed, $0.total) }
            let label = p.map { ScanPhase.label($0.phase) } ?? "正在扫描"
            return .init(
                tone: .busy, kind: .scan,
                title: percent.map { "\(label) \($0)%" } ?? label,
                detail: p.map { $0.total > 0 ? "\($0.processed) / \($0.total)" : "正在统计待处理的文件数" } ?? "正在统计待处理的文件数",
                percent: percent
            )
        }
        if library.organizing {
            let p = library.organizeProgress
            let percent = p.flatMap { percentOf($0.processed, $0.total) }
            return .init(
                tone: .busy, kind: .organize,
                title: percent.map { "正在整理文件名 \($0)%" } ?? "正在整理文件名",
                detail: p.map { $0.total > 0 ? "\($0.processed) / \($0.total)" : "" } ?? "",
                percent: percent
            )
        }
        if let refresh = library.metadataRefresh, refresh.refreshing {
            let percent = percentOf(refresh.processed, refresh.total)
            let title = refresh.stopping ? "正在停止刷新" : percent.map { "刷新元数据 \($0)%" } ?? "刷新元数据"
            let detail = refresh.active.first.map { "正在处理「\($0.title)」· \($0.phase)" } ?? "\(refresh.processed) / \(refresh.total)"
            return .init(tone: .busy, kind: .refresh, title: title, detail: detail, percent: percent)
        }
        if let job = library.chapterJob {
            let running = chapterJobRunning(job)
            let detail: String
            if !running {
                detail = "等前面的任务跑完再开始"
            } else if job.total > 0 {
                detail = "\(job.processed) / \(job.total)" + (job.failed > 0 ? " · \(job.failed) 个失败" : "")
            } else {
                detail = "正在统计待处理的文件数"
            }
            return .init(tone: .busy, kind: .chapters, title: chapterJobLabel(job), detail: detail,
                         percent: running ? percentOf(job.processed, job.total) : nil)
        }
        let deferred = library.lastScan?.deferred ?? 0
        if deferred > 0 {
            return .init(tone: .busy, kind: .importing, title: "\(deferred) 个新文件入库中", detail: "等文件写完自动补扫", percent: nil)
        }
        let unidentified = library.stats.unidentifiedCount
        let missing = library.stats.missingCount
        if missing > 0 {
            return .init(
                tone: .missing, kind: .missing,
                title: unidentified > 0 ? "\(unidentified) 个待识别 · \(missing) 个缺失" : "\(missing) 个缺失",
                detail: lastScanDetail(library), percent: nil
            )
        }
        if unidentified > 0 {
            return .init(tone: .pending, kind: .unidentified, title: "\(unidentified) 个待识别", detail: lastScanDetail(library), percent: nil)
        }
        // 空闲不是需要看的状态：只留「最近扫描 X 前」这行事实
        return .init(tone: .idle, kind: .idle, title: "空闲", detail: lastScanDetail(library), percent: nil)
    }

    static func percentOf(_ processed: Int, _ total: Int) -> Int? {
        guard total > 0 else { return nil }
        return min(100, Int((Double(processed) / Double(total) * 100).rounded()))
    }

    /// 「最近扫描 X 前 · 结论」：扫描常毫秒级完成，只写时间的话一个本就最新的库扫完前后长得一模一样，
    /// 用户会以为没点上——结论挑用户关心的：新增几个文件、标记几个缺失；都没有就明说「无新文件」
    static func lastScanDetail(_ library: API.LibraryView) -> String {
        guard let scan = library.lastScan else { return "尚未扫描" }
        var parts = ["最近扫描 \(libraryFromNow(scan.finishedAt))"]
        if scan.cancelled { parts.append("手动停止") }
        if scan.scanned > 0 { parts.append("新增 \(scan.scanned) 个文件") }
        if scan.markedMissing > 0 { parts.append("标记缺失 \(scan.markedMissing)") }
        if !scan.cancelled, scan.scanned == 0, scan.markedMissing == 0 { parts.append("无新文件") }
        return parts.joined(separator: " · ")
    }

    static func chapterJobRunning(_ job: API.ChapterJobView) -> Bool {
        job.status == "running" || job.status == "cancelling"
    }

    /// 「生成章节」菜单项与状态列共用的一句话：没作业时是动作名，有作业时如实说到哪了
    static func chapterJobLabel(_ job: API.ChapterJobView?) -> String {
        guard let job else { return "生成章节" }
        if job.stopping { return "正在停止生成章节" }
        if !chapterJobRunning(job) { return "生成章节排队中" }
        return percentOf(job.processed, job.total).map { "正在生成章节 \($0)%" } ?? "正在生成章节"
    }
}

/// 页头摘要胶囊对应的筛选：只看在跑任务的库 / 只看有待处理的库
enum ManageLibraryFocus { case busy, attention }

/// 管理页的库列表筛选（Web `LibraryFilter`）
struct ManageLibraryFilter: Equatable {
    var query = ""
    var kind: String?
    var focus: ManageLibraryFocus?

    var isActive: Bool { !query.trimmingCharacters(in: .whitespaces).isEmpty || kind != nil || focus != nil }

    /// 客户端筛选：库的规模在几十以内，一次全拉后本地过滤即可；搜索匹配库名或任一根目录（不分大小写）
    func apply(_ libraries: [API.LibraryView]) -> [API.LibraryView] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        return libraries.filter { library in
            if let kind, library.kind != kind { return false }
            if focus == .busy, !ManageLibraryRules.isBusy(library) { return false }
            if focus == .attention, !ManageLibraryRules.needsAttention(library) { return false }
            if q.isEmpty { return true }
            if library.name.lowercased().contains(q) { return true }
            return library.rootPaths.contains { $0.lowercased().contains(q) }
        }
    }
}

enum ManageLibraryRules {
    static let kindOrder = ["movie", "tv", "video", "photo"]

    /// 是否有长任务在跑（摘要「N 个在跑任务」与筛选用）
    static func isBusy(_ library: API.LibraryView) -> Bool {
        library.scanning || library.organizing || library.metadataRefresh?.refreshing == true || library.chapterJob != nil
    }

    /// 是否有要你动手的文件（待识别或缺失）。任务在跑或还在入库时状态归进度，不算「待处理」
    static func needsAttention(_ library: API.LibraryView) -> Bool {
        if isBusy(library) || (library.lastScan?.deferred ?? 0) > 0 { return false }
        return library.stats.unidentifiedCount > 0 || library.stats.missingCount > 0
    }

    /// 页头摘要：规模事实一句话 + 在跑任务数 + 待处理库数 + 待处理里是否含缺失（含则胶囊用红）
    static func summary(_ libraries: [API.LibraryView]) -> (facts: String, busy: Int, attention: Int, missing: Bool) {
        let items = libraries.reduce(0) { $0 + $1.stats.itemCount }
        let bytes = libraries.reduce(0) { $0 + $1.stats.totalSizeBytes }
        let needing = libraries.filter(needsAttention)
        return (
            "\(libraries.count) 个媒体库 · \(items) 个条目 · \(libraryBytes(bytes))",
            libraries.filter(isBusy).count,
            needing.count,
            needing.contains { $0.stats.missingCount > 0 }
        )
    }

    /// 把 from 位置的元素挪到 to（其余相对顺序不变）；越界或原地返回 nil
    static func move<T>(_ list: [T], from: Int, to: Int) -> [T]? {
        guard from != to, list.indices.contains(from), list.indices.contains(to) else { return nil }
        var next = list
        let item = next.remove(at: from)
        next.insert(item, at: to)
        return next
    }

    /// 可见范围文案：只说库开放给谁；「你本人在不在浏览范围内」由锁图标表达，不混进文字
    static func accessLabel(_ library: API.LibraryView) -> String {
        if library.accessMode == "everyone" { return "全部成员" }
        if !library.memberIds.isEmpty { return "指定成员 \(library.memberIds.count)" }
        return library.adminVisible ? "仅自己" : "无人可见"
    }

    /// 可见范围偏离默认（对全部成员开放、你自己也能看）才在行内挂胶囊
    static func accessRestricted(_ library: API.LibraryView) -> Bool {
        library.accessMode != "everyone" || !library.viewerAccess
    }

    /// 库名下的配置备注：只说偏离默认的部分（首页展示、实时监控开是默认，关了才说）
    static func configNotes(_ library: API.LibraryView) -> [String] {
        var notes: [String] = []
        if library.excludeFromHome { notes.append("从首页排除") }
        if !library.realtimeWatch { notes.append("实时监控关") }
        return notes
    }

    /// 库存：影视库按「部」、图片库按「张」、其他库按「条目」
    static func inventory(_ library: API.LibraryView) -> (primary: String, secondary: String) {
        let unit = library.kind == "photo" ? "张" : library.kind == "video" ? "个条目" : "部"
        return ("\(library.stats.itemCount) \(unit)", "\(library.stats.fileCount) 个文件")
    }

    /// 同类型两库的收藏范围可能同时命中同一部作品且条件数相同——命中顺序只能靠创建先后。
    /// 只读提示（不阻断），并点名给创建更晚的那个库补上它缺的维度（Web `routingOverlapWarnings`）
    static func routingOverlapWarnings(_ libraries: [API.LibraryView]) -> [String] {
        let fieldLabels: [(String, String)] = [("genres", "类型"), ("origin_countries", "区域")]
        let declared = libraries.filter { !$0.matchRules.isEmpty }
        var warnings: [String] = []
        func field(_ rule: [String: API.JSONValue]) -> String? { rule["field"]?.stringValue }
        func values(_ rule: [String: API.JSONValue]) -> [API.JSONValue] {
            if case let .array(items) = rule["values"] { return items }
            return []
        }
        for i in declared.indices {
            for j in declared.indices where j > i {
                let a = declared[i], b = declared[j]
                guard a.kind == b.kind, a.matchRules.count == b.matchRules.count else { continue }
                let compatible = a.matchRules.allSatisfy { ra in
                    guard let rb = b.matchRules.first(where: { field($0) == field(ra) }) else { return true }
                    let vb = values(rb)
                    return values(ra).contains { vb.contains($0) }
                }
                guard compatible else { continue }
                let (first, later) = a.id < b.id ? (a, b) : (b, a)
                let laterFields = Set(later.matchRules.compactMap(field))
                let missing = fieldLabels.filter { !laterFields.contains($0.0) }.map(\.1)
                let fix = missing.isEmpty
                    ? "两库已声明相同的维度：错开重叠的取值即可消除歧义。"
                    : "想让「\(later.name)」优先收这类作品：编辑它，补上「\(missing.joined(separator: "」或「"))」条件（用「全选」也可以）——条件多的库优先命中；想维持现状则无需改动。"
                warnings.append("「\(a.name)」与「\(b.name)」的收藏范围可能同时命中同一部作品且条件数相同，届时优先进创建更早的「\(first.name)」。\(fix)")
            }
        }
        return warnings
    }
}

/// 管理页各处共用的确认文案（Web `lib/library-confirm.ts`）：短导语 + 动作清单，最后一条讲安全边界
enum ManageConfirmText {
    static func bullets(_ lead: String, _ items: [String]) -> String {
        ([lead] + items.map { "• \($0)" }).joined(separator: "\n")
    }

    static let scan = bullets("本次扫描会检查库文件夹的最新变化：", [
        "找出新增的影片文件，自动识别并加入媒体库",
        "标记已经不在硬盘上的文件（记录保留，文件回来自动恢复）",
        "为新入库的影片补齐简介、海报等信息",
        "首次扫描会读取每个文件的画质与音轨信息，文件多时较慢、可随时停止",
        "不会移动、修改或删除你的任何文件",
    ])

    static let refresh = bullets("本次刷新会为库里的全部影片：", [
        "重新获取最新的简介、评分、演职员和剧集信息",
        "海报或背景图有更新时重新下载（你手动锁定的不动）",
        "同步更新影片文件夹里的海报和 NFO 信息文件",
        "影片较多时需要一段时间，可随时停止",
        "不会移动、修改或删除你的视频文件",
    ])

    static let rereadNfo = bullets("本次会为库里的全部视频：", [
        "重新读取视频旁同名的 NFO 文件，标题、简介、系列等以 NFO 为准",
        "NFO 里写了系列（<set>）的，按库设置自动归进系列合集",
        "重新生成封面",
        "不联网，不会修改或删除你的文件",
    ])

    static let chapters = bullets("后台低优先级执行，可在任务中心观察或取消：", [
        "只处理章节图还缺的文件（含上次没抓完、图丢了的），已经齐的不动",
        "每个文件按章节数定位读取若干次，网络挂载的库会有读取流量",
        "不会移动、修改或删除你的视频文件",
    ]) + "\n\n选「已有的章节也重新生成」会按当前章节与合成策略全部重新生成，你手动选定的图不会被覆盖。"
}
