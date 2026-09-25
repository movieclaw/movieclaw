import SwiftUI

/// 海报墙的「窗口式」分页器（单库页 / 合集 / 收藏共用）。
///
/// 对应 Web 海报墙的 load / loadPrev / jumpTo / reload 四件事：
/// - 墙是整份排序结果上的一个**窗口** `[start, start + items.count)`；
/// - 向下滚到底追加下一页，「A–Z 跳转」「回到上次位置」直接把窗口换成从某个 offset 开始的一页
///   （而不是从头一页页追到那儿），之后向上滚到顶再把上一页补回来；
/// - 轮询 / 从详情页返回时按已加载的页数整窗重拉（`refresh`），在详情页取消收藏的那部会跟着消失，
///   不缩窗口也不动滚动位置。
/// 请求用序号作废：换排序/筛选后，上一轮晚到的响应不会覆盖新窗口。
@MainActor
@Observable
final class LibraryWallPager<Item: Identifiable & Sendable> where Item.ID: Sendable {
    typealias Fetch = @Sendable (_ offset: Int, _ limit: Int) async throws -> [Item]

    let pageSize: Int
    private(set) var items: [Item]?
    /// 窗口在整份排序里的起点
    private(set) var start = 0
    private(set) var hasMore = false
    private(set) var failed: String?
    /// 整份结果的总数；接口不给总数时为 nil（按「本页是否满页」判断还有没有下一页）
    var total: Int?
    private var loading = false
    private var loadingPrev = false
    private var generation = 0
    @ObservationIgnored private var fetch: Fetch?

    init(pageSize: Int = 60) {
        self.pageSize = pageSize
    }

    /// 换数据源（排序/筛选变化）：清空并从 offset 起载入第一页
    func reset(_ fetch: @escaping Fetch, from offset: Int = 0, keepItems: Bool = false) async {
        self.fetch = fetch
        generation += 1
        if !keepItems { items = nil }
        await jump(to: offset)
    }

    /// 窗口换成从 offset 开始的一页
    func jump(to offset: Int) async {
        guard let fetch else { return }
        generation += 1
        let gen = generation
        loading = true
        defer { if gen == generation { loading = false } }
        do {
            let page = try await fetch(offset, pageSize)
            guard gen == generation else { return }
            start = offset
            items = page
            hasMore = page.count >= pageSize && (total.map { offset + page.count < $0 } ?? true)
            failed = nil
        } catch is CancellationError {
        } catch {
            guard gen == generation else { return }
            if items == nil { failed = error.localizedDescription }
        }
    }

    /// 墙尾追加一页
    func loadMore() async {
        guard let fetch, hasMore, !loading, let current = items else { return }
        let gen = generation
        loading = true
        defer { if gen == generation { loading = false } }
        do {
            let page = try await fetch(start + current.count, pageSize)
            guard gen == generation else { return }
            let seen = Set(current.map(\.id))
            items = current + page.filter { !seen.contains($0.id) }
            hasMore = page.count >= pageSize && (total.map { start + (items?.count ?? 0) < $0 } ?? true)
        } catch {
            guard gen == generation else { return }
            hasMore = false
        }
    }

    /// 墙顶还有上文时补上一页（跳转之后仍然能往上滑）
    func loadPrevious() async {
        guard let fetch, start > 0, !loadingPrev, let current = items else { return }
        let gen = generation
        loadingPrev = true
        defer { loadingPrev = false }
        let from = max(0, start - pageSize)
        guard let page = try? await fetch(from, start - from), gen == generation, !page.isEmpty else { return }
        let seen = Set(current.map(\.id))
        start -= page.count
        items = page.filter { !seen.contains($0.id) } + current
    }

    /// 按已加载的条数整窗重拉并整体替换（轮询 / 返回对账），失败保留旧窗口
    func refresh() async {
        guard let fetch, !loading, let current = items else {
            if items == nil, self.fetch != nil { await jump(to: start) }
            return
        }
        let gen = generation
        let count = max(pageSize, current.count)
        let origin = start
        do {
            var rows: [Item] = []
            var offset = origin
            while offset < origin + count {
                let page = try await fetch(offset, pageSize)
                rows += page
                if page.count < pageSize { break }
                offset += pageSize
            }
            guard gen == generation, !loading else { return }
            if rows.isEmpty, origin > 0 {
                await jump(to: 0)
                return
            }
            items = rows
            hasMore = rows.count >= count && (total.map { origin + rows.count < $0 } ?? true)
        } catch {}
    }

    /// 本地改一格（收藏心、已看标记等乐观更新）
    func update(_ id: Item.ID, _ transform: (inout Item) -> Void) {
        guard var list = items, let index = list.firstIndex(where: { $0.id == id }) else { return }
        transform(&list[index])
        items = list
    }

    func offset(of id: Item.ID) -> Int? {
        items?.firstIndex { $0.id == id }.map { start + $0 }
    }
}

/// 「回到上次浏览的位置」的记录（Web `lib/library-wall-recall.ts`）：
/// 每面墙（scope）只记一条：当前形态（view，排序变了位置就没意义）、第一格的 offset、时间。
/// 太浅（< 24 格）或超过 14 天的不提示。
enum LibraryWallRecall {
    private static let key = "movieclaw.library.wall-recall"
    static let minOffset = 24
    static let maxAge: TimeInterval = 14 * 86400

    static func read(scope: String, view: String) -> Int? {
        guard let all = UserDefaults.standard.dictionary(forKey: key),
              let saved = all[scope] as? [String: Any],
              saved["view"] as? String == view,
              let offset = saved["offset"] as? Int, offset >= minOffset,
              let at = saved["at"] as? Double, Date.now.timeIntervalSince1970 - at <= maxAge else { return nil }
        return offset
    }

    static func write(scope: String, view: String, offset: Int) {
        var all = UserDefaults.standard.dictionary(forKey: key) ?? [:]
        all[scope] = ["view": view, "offset": offset, "at": Date.now.timeIntervalSince1970]
        UserDefaults.standard.set(all, forKey: key)
    }
}

/// 进页面时问一句要不要跳回上次位置；不理它、往下滑一屏就自己消失
struct WallRecallPill: View {
    var onJump: () -> Void
    var onDismiss: () -> Void

    var body: some View {
        HStack(spacing: 4) {
            Button(action: onJump) {
                Label("回到上次浏览的位置", systemImage: "clock.arrow.circlepath")
                    .font(.subheadline.weight(.semibold))
                    .padding(.leading, 16).padding(.trailing, 6).padding(.vertical, 10)
            }
            .accessibilityIdentifier("wall-recall")
            Button(action: onDismiss) {
                Image(systemName: "xmark").font(.caption.weight(.bold)).padding(10)
            }
            .accessibilityLabel("不用了")
        }
        .foregroundStyle(Theme.text)
        .glassEffect(.regular.interactive(), in: .capsule)
        .padding(.bottom, 12)
        .transition(.move(edge: .bottom).combined(with: .opacity))
    }
}

/// 墙尾的加载指示（Web `WallLoadMore`）：**真正滚进视口**才触发下一页，文案报窗口进度。
///
/// 不能用 onAppear / task：墙外层是普通 VStack，子视图一创建就算「出现」，
/// 会一页接一页把整库拉完。这里按滚动可见性判断（等价 Web 的 IntersectionObserver）；
/// 加载完仍在视口里（一页不满一屏）就接着要下一页。
struct WallLoadMoreFooter: View {
    let hasMore: Bool
    let start: Int
    let loaded: Int
    var total: Int?
    var onReach: () async -> Void
    @State private var visible = false

    var body: some View {
        if hasMore {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("加载中…（第 \(start + 1)–\(start + loaded) 部 / 共 \(max(total ?? 0, start + loaded))）")
            }
            .font(.footnote)
            .foregroundStyle(Theme.textFaint)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 20)
            .onScrollVisibilityChange(threshold: 0.01) { visible = $0 }
            .task(id: "\(visible)|\(start + loaded)") {
                if visible { await onReach() }
            }
            .accessibilityIdentifier("wall-load-more")
        }
    }
}

/// 墙顶的向上补页哨兵（Web `WallLoadPrev`）：窗口不从 0 开始时，滚到顶才把上一页接回来
struct WallLoadPreviousSentinel: View {
    let start: Int
    var onReach: () async -> Void
    @State private var visible = false

    var body: some View {
        if start > 0 {
            Color.clear
                .frame(height: 1)
                .onScrollVisibilityChange(threshold: 0.01) { visible = $0 }
                .task(id: "\(visible)|\(start)") {
                    if visible { await onReach() }
                }
        }
    }
}

/// 海报墙网格：手机上最小列宽 140pt（竖版）/ 160pt（横版），列距 12、行距 20，与 Web 同口径
struct LibraryPosterGrid<Item: Identifiable, Cell: View>: View {
    let items: [Item]
    var wide = false
    @ViewBuilder let cell: (Item) -> Cell

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: wide ? 160 : 140), spacing: 12, alignment: .top)], alignment: .leading, spacing: 20) {
            ForEach(items) { item in
                cell(item)
            }
        }
        .padding(.horizontal, Theme.pagePadding)
    }
}

/// 库存条目 → 海报格（Web `InventoryCell`）：缺失提示、死条目置灰、后台处理中点亮、剧集长按看库存概况与订阅动作
struct LibraryInventoryCell: View {
    let item: API.LibraryItemView
    let libraryId: Int
    var frameAspect: CGFloat?
    /// 按评分排序 / 筛选时副行常显评分
    var showRating = false
    var working: String?
    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Router.self) private var router

    var body: some View {
        let dead = item.fileCount > 0 && item.missingCount >= item.fileCount
        let rating = (item.rating ?? 0) > 0 ? "★ " + String(format: "%.1f", item.rating ?? 0) : nil
        let aspect = frameAspect ?? (item.primaryAspect >= 1 ? 16 / 9 : Theme.posterAspect)
        NavigationLink(value: AppRoute.libraryItem(libraryId: item.libraryId ?? libraryId, itemId: item.mediaItemId)) {
            LibraryPosterCell(
                title: item.title, year: item.year, extent: showRating ? rating : nil,
                url: api.image(item.posterUrl, ImageVariant.card(aspect: item.primaryAspect)),
                imageAspect: item.primaryAspect, frameAspect: aspect,
                favorite: item.isFavorite, dead: dead,
                abnormal: dead ? "文件已全部缺失" : item.missingCount > 0 ? "\(item.missingCount) 个文件缺失" : nil,
                working: working,
                placeholderSymbol: LibraryKindMeta.symbol(item.kind)
            )
        }
        .buttonStyle(.plain)
        .contextMenu {
            if item.kind == "tv", let summary = item.inventorySummary {
                Text(formatInventorySummary(summary))
            }
            if let rating, !showRating { Text(rating) }
            if permissions.canSubscribe, let tmdb = item.tmdbId,
               let action = LibraryInventoryAction.of(kind: item.kind, summary: item.inventorySummary) {
                Button {
                    router.present(.subscribe(SubscribeRequest(titleRef: "tmdb:tv:\(tmdb)", title: item.title)))
                } label: {
                    Label(action.label, systemImage: action.systemImage)
                }
            }
        }
        .id(item.mediaItemId)
    }
}


