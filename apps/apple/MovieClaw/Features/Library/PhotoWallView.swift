import SwiftUI
import UIKit

/// 图片库的照片墙（对应 Web `components/photo-wall.tsx` + 库页里的相册部分）。
///
/// 与海报墙的区别只有一件事：混合长宽比。照片比例每张不同，得自己算位置：
/// - **比例在渲染前已知**（`primary_aspect` 由扫描入账时读的原图尺寸得来），每张放进
///   当前最短的一列，不等图片加载、零抖动；极端比例夹到 [0.5, 2]，角标提示「全景 / 长图」；
/// - **按月分组**：照片库的第一心智是「什么时候拍的」，每个月一段标题 +「N 张」+ 一面墙。
///   月份来自 `release_date`（EXIF 拍摄日），段标题的张数取服务端月份索引的全库数；
///   选了「最近添加」排序时同月照片不再连续，调用方传 `grouped: false` 走一条不分段的瀑布流；
/// - **按月份跳转**：Web 桌面端是右缘的悬浮时间刻度，手机上隐藏；这里做成墙顶的
///   「按月份跳转」菜单。跳转与 Web 同一口径——整个窗口换成从该月第一张开始的一页，
///   此后照常向下加载，墙顶再给一个向上补页的入口；
/// - 点照片开灯箱（缩放、拍摄信息、下载原图），见 `PhotoLightbox.swift` 的 `PhotoWallView.Lightbox`。
///
/// **本视图是滚动内容，不自带 ScrollView**：调用方放进自己页面的 ScrollView。
/// 数据通过 `fetch(offset, limit)` 自己分页加载；`reloadKey` 变化即从头重载；
/// `refreshToken` 变化（宿主每轮轮询 / 下拉刷新）按已加载窗口整窗重拉，不动滚动位置——
/// 扫描入库的新照片、删掉的照片跟着出现或消失（同 Web 墙轮询整窗重拉）。
/// 「回到上次位置」：宿主改 `jumpRequest` 把窗口换到那张，`onFirstVisible` 回报视口第一张的位置。
struct PhotoWallView: View {
    let libraryId: Int
    let reloadKey: AnyHashable
    let fetch: (_ offset: Int, _ limit: Int) async throws -> [API.LibraryItemView]
    /// 是否按月分段（默认分）；「最近添加」序传 false
    var grouped: Bool = true
    var refreshToken: Int = 0
    /// 跳到某个位置（「回到上次位置」）；每次请求带新的 id 以便重复跳同一处
    var jumpRequest: PhotoWallJump?
    /// 这一张正被后台处理的文案（整库刷新阶段 / 后台任务 / 正在读取规格），同 Web workingLabel
    var working: (API.LibraryItemView) -> String? = { _ in nil }
    /// 视口里第一张在整份排序里的位置
    var onFirstVisible: (Int) -> Void = { _ in }

    /// 一页条目数（同 Web WALL_PAGE_SIZE）
    static let pageSize = 60

    @Environment(\.api) private var api
    @State private var feed = Feed()
    @State private var width: CGFloat = 0
    @State private var lightbox: PhotoLightboxSession?
    @State private var scroller = WallScroller()
    /// 视口里的行（行下标 → 该行第一张的全局下标）
    @State private var visibleRows: [Int: Int] = [:]
    private var density: GalleryDensity { GalleryPrefs.shared.density }

    var body: some View {
        content
            .frame(maxWidth: .infinity)
            .onGeometryChange(for: CGFloat.self, of: { $0.size.width }) { width = $0 }
            .background(alignment: .top) { WallScrollAnchor(scroller: scroller).frame(height: 1) }
            .task(id: ReloadToken(key: reloadKey, grouped: grouped)) { await reload() }
            .onChange(of: refreshToken) { Task { await feed.refresh(fetch: fetch) } }
            .onChange(of: jumpRequest) { _, request in
                guard let request else { return }
                Task { if await feed.jump(to: request.offset, fetch: fetch) { scroller.scrollToTop() } }
            }
            .fullScreenCover(item: $lightbox) { session in
                Lightbox(libraryId: libraryId, feed: feed, fetch: fetch, index: session.index)
            }
    }

    private func reload() async {
        let libraryId = libraryId
        let api = api
        await feed.reload(fetch: fetch, index: grouped ? {
            // 月份索引：段标题的全库张数与跳转落点（与服务端 release_date 分档同口径）
            try await api.uiLibraryItemsIndex(libraryId: libraryId, sort: "release_date")
        } : nil)
    }

    @ViewBuilder
    private var content: some View {
        switch feed.phase {
        case .loading:
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, minHeight: 240)
        case let .failed(message):
            ErrorState(message: message, retry: { await reload() })
                .frame(minHeight: 280)
        case .loaded:
            if feed.items.isEmpty {
                EmptyState(systemImage: "photo.on.rectangle.angled", title: "这个库还没有内容。")
                    .frame(minHeight: 280)
            } else {
                wall
            }
        }
    }

    private var wall: some View {
        let rows = feed.rows(width: width, density: density, grouped: grouped)
        return LazyVStack(alignment: .leading, spacing: 0) {
            toolbarRow
            ForEach(Array(rows.enumerated()), id: \.element.id) { position, row in
                rowView(row)
                    .onAppear {
                        if position >= rows.count - 4 { Task { await feed.loadMore(fetch: fetch) } }
                    }
                    .onScrollVisibilityChange(threshold: 0.2) { visible in
                        trackVisible(position: position, row: row, visible: visible)
                    }
            }
            if feed.hasMore {
                ProgressView()
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 20)
                    .onAppear { Task { await feed.loadMore(fetch: fetch) } }
            }
        }
    }

    /// 墙顶：向上补页（跳转过之后仍能往上翻）+ 按月份跳转
    @ViewBuilder
    private var toolbarRow: some View {
        let showsJump = grouped && feed.monthIndex.count > 1
        if feed.start > 0 || showsJump {
            HStack(spacing: 8) {
                if feed.start > 0 {
                    Button {
                        Task { await feed.loadPrevious(fetch: fetch) }
                    } label: {
                        if feed.loadingPrevious {
                            ProgressView().controlSize(.small)
                        } else {
                            Label("加载更早的照片", systemImage: "arrow.up")
                        }
                    }
                    .buttonStyle(.glass)
                }
                Spacer(minLength: 0)
                if showsJump { monthJumpMenu }
            }
            .font(.subheadline)
            .padding(.bottom, 14)
        }
    }

    /// 月份跳转：按年分节，每项「8 月 · 16 张」；点击把窗口换到该月第一张
    private var monthJumpMenu: some View {
        let years = Self.yearSections(feed.monthIndex)
        return Menu {
            ForEach(years, id: \.title) { section in
                Section(section.title) {
                    ForEach(section.entries, id: \.initial) { entry in
                        Button("\(Self.shortMonth(entry.initial)) · \(entry.count) 张") {
                            Task {
                                if await feed.jump(to: entry.offset, fetch: fetch) { scroller.scrollToTop() }
                            }
                        }
                        .accessibilityLabel("\(Self.formatMonth(entry.initial)) · \(entry.count) 张")
                    }
                }
            }
        } label: {
            Label("按月份跳转", systemImage: "calendar")
        }
        .buttonStyle(.glass)
    }

    private func trackVisible(position: Int, row: PhotoRow, visible: Bool) {
        var first: Int?
        if case let .band(band, indices) = row.kind, let tile = band.tiles.min(by: { $0.id < $1.id }) { first = indices[tile.id] }
        if visible, let first { visibleRows[position] = first } else { visibleRows[position] = nil }
        guard let top = visibleRows.min(by: { $0.key < $1.key })?.value else { return }
        onFirstVisible(feed.start + top)
    }

    @ViewBuilder
    private func rowView(_ row: PhotoRow) -> some View {
        switch row.kind {
        case let .header(month, count):
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(Self.formatMonth(month))
                    .font(.body.weight(.semibold))
                    .foregroundStyle(.white.opacity(0.85))
                Text("\(count) 张")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(Theme.textFaint)
            }
            .padding(.top, row.isFirst ? 0 : 28)
            .padding(.bottom, 12)
        case let .band(band, indices):
            GalleryDensity.BandView(band: band) { placed in
                tile(at: indices[placed.id])
            }
        }
    }

    @ViewBuilder
    private func tile(at index: Int) -> some View {
        if feed.items.indices.contains(index) {
            let item = feed.items[index]
            Button {
                lightbox = PhotoLightboxSession(index: index)
            } label: {
                PhotoTile(item: item, url: api.image(item.posterUrl, density.variant), working: working(item))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("查看 \(item.title)")
        }
    }

    // MARK: 月份文案（同 Web photoMonthOf / formatPhotoMonth）

    /// 条目的月份档名：`2026-08`，缺日期归「未知」
    static func monthOf(_ item: API.LibraryItemView) -> String {
        guard let date = item.releaseDate, date.count >= 7 else { return "未知" }
        return String(date.prefix(7))
    }

    /// 「2026 年 8 月」/「日期未知」
    static func formatMonth(_ month: String) -> String {
        guard month != "未知" else { return "日期未知" }
        let parts = month.split(separator: "-")
        guard parts.count >= 2, let mon = Int(parts[1]) else { return month }
        return "\(parts[0]) 年 \(mon) 月"
    }

    private static func shortMonth(_ month: String) -> String {
        guard month != "未知" else { return "日期未知" }
        let parts = month.split(separator: "-")
        guard parts.count >= 2, let mon = Int(parts[1]) else { return month }
        return "\(mon) 月"
    }

    private static func yearSections(_ index: [API.LibraryIndexEntryView]) -> [(title: String, entries: [API.LibraryIndexEntryView])] {
        var sections: [(title: String, entries: [API.LibraryIndexEntryView])] = []
        for entry in index {
            let title = entry.initial == "未知" ? "日期未知" : "\(entry.initial.prefix(4)) 年"
            if sections.last?.title == title {
                sections[sections.count - 1].entries.append(entry)
            } else {
                sections.append((title, [entry]))
            }
        }
        return sections
    }
}

/// 「回到上次位置」的一次跳转请求
struct PhotoWallJump: Equatable {
    let id = UUID()
    var offset: Int
}

/// reloadKey 与分段开关任一变化都从头重载（分段决定要不要拉月份索引）
private struct ReloadToken: Equatable {
    var key: AnyHashable
    var grouped: Bool
}

private struct PhotoLightboxSession: Identifiable {
    let id = UUID()
    var index: Int
}

/// 墙上的一行：月份段标题或一条带子
private struct PhotoRow: Identifiable {
    enum Kind {
        case header(month: String, count: Int)
        /// 带子 + 本段瓦片局部下标 → 整份已加载列表下标的映射（灯箱按全局下标翻页）
        case band(GalleryDensity.Band, indices: [Int])
    }

    var id: String
    var kind: Kind
    var isFirst = false
}

/// 一张照片瓦片：微缩图模糊铺底 → 缩略图盖上；极端比例角标；文件缺失置灰
private struct PhotoTile: View {
    let item: API.LibraryItemView
    let url: URL?
    var working: String?

    private var dead: Bool { item.fileCount > 0 && item.missingCount >= item.fileCount }

    private var extremeLabel: String? {
        if item.primaryAspect > GalleryDensity.maxAspect { return "全景" }
        if item.primaryAspect < GalleryDensity.minAspect { return "长图" }
        return nil
    }

    var body: some View {
        ZStack {
            // 渐进式加载第一级：列表自带的约 300 字节微缩图，缩略图到达前先看到照片的大致颜色
            if let blur = Self.decodeDataURI(item.posterBlur) {
                Image(uiImage: blur)
                    .resizable()
                    .scaledToFill()
                    .blur(radius: 8)
                    .scaleEffect(1.1)
            }
            RemoteImage(url: url, placeholderSymbol: "photo")
                .opacity(dead ? 0.5 : 1)
                .grayscale(dead ? 1 : 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .clipped()
        .overlay(alignment: .topLeading) {
            if let extremeLabel {
                Text(extremeLabel)
                    .font(.caption2.weight(.bold))
                    .tracking(0.5)
                    .foregroundStyle(Theme.accent)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(.black.opacity(0.7), in: .rect(cornerRadius: 6))
                    .padding(8)
            }
        }
        .overlay(alignment: .bottom) {
            if let working {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.mini).tint(Theme.info)
                    Text(working).lineLimit(1)
                }
                .font(.caption2.weight(.medium))
                .foregroundStyle(Theme.info)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 8)
                .padding(.vertical, 5)
                .background(Color(red: 7 / 255, green: 12 / 255, blue: 20 / 255).opacity(0.92))
            } else if dead {
                Text("文件已缺失")
                    .font(.caption2)
                    .foregroundStyle(Theme.textMuted)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(Color(red: 7 / 255, green: 12 / 255, blue: 20 / 255).opacity(0.85))
            }
        }
        .clipShape(.rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(.white.opacity(0.07)))
    }

    /// `data:image/...;base64,xxxx` → UIImage
    private static func decodeDataURI(_ raw: String?) -> UIImage? {
        guard let raw, raw.hasPrefix("data:"), let comma = raw.firstIndex(of: ",") else { return nil }
        guard let data = Data(base64Encoded: String(raw[raw.index(after: comma)...])) else { return nil }
        return UIImage(data: data)
    }
}

// MARK: - 数据窗口

extension PhotoWallView {
    /// 照片墙已加载的窗口：`[start, start + items.count)` 这一段条目。
    ///
    /// 与 Web 海报墙同一套窗口模型：平时从头向下追加；按月份跳转时整个窗口换成从
    /// 目标位置开始的一页（而不是从头一路加载到那里——几万张的相册那是几百次请求），
    /// 之后照常向下追加，墙顶可以向上补页。墙与灯箱共用同一个实例，灯箱翻到末尾
    /// 要的下一页墙上也立刻出现。
    @Observable
    final class Feed {
        enum Phase { case loading, failed(String), loaded }

        private(set) var phase: Phase = .loading
        private(set) var items: [API.LibraryItemView] = []
        /// 窗口第一张在整份排序里的位置
        private(set) var start = 0
        private(set) var hasMore = false
        private(set) var loadingPrevious = false
        /// 月份索引（全库每月张数与首张位置）；不分段时为空
        private(set) var monthIndex: [API.LibraryIndexEntryView] = []

        @ObservationIgnored private var loading = false
        @ObservationIgnored private var generation = 0
        @ObservationIgnored private var version = 0
        @ObservationIgnored private var rowsCache: (key: String, rows: [PhotoRow])?

        typealias Fetch = (Int, Int) async throws -> [API.LibraryItemView]

        func reload(fetch: Fetch, index: (() async throws -> [API.LibraryIndexEntryView])?) async {
            generation += 1
            let current = generation
            if case .failed = phase { phase = .loading }
            loading = true
            defer { if current == generation { loading = false } }
            do {
                let page = try await fetch(0, PhotoWallView.pageSize)
                // 索引拉不到不影响看图：只是没有全库张数与跳转
                let months = (try? await index?()) ?? []
                guard current == generation else { return }
                start = 0
                hasMore = page.count >= PhotoWallView.pageSize
                monthIndex = months
                replace(with: Self.dedupe(page))
                phase = .loaded
            } catch is CancellationError {
            } catch {
                guard current == generation else { return }
                if items.isEmpty { phase = .failed(error.localizedDescription) }
                hasMore = false
            }
        }

        func loadMore(fetch: Fetch) async {
            guard hasMore, !loading, case .loaded = phase else { return }
            loading = true
            let current = generation
            defer { if current == generation { loading = false } }
            do {
                let page = try await fetch(start + items.count, PhotoWallView.pageSize)
                guard current == generation else { return }
                hasMore = page.count >= PhotoWallView.pageSize
                replace(with: Self.dedupe(items + page))
            } catch {
                if current == generation, !(error is CancellationError) { hasMore = false }
            }
        }

        /// 向上补一页（跳转之后往回翻）
        func loadPrevious(fetch: Fetch) async {
            guard start > 0, !loading else { return }
            loading = true
            loadingPrevious = true
            let current = generation
            defer {
                if current == generation {
                    loading = false
                    loadingPrevious = false
                }
            }
            let offset = max(0, start - PhotoWallView.pageSize)
            do {
                let page = try await fetch(offset, start - offset)
                guard current == generation else { return }
                start = offset
                replace(with: Self.dedupe(page + items))
            } catch {}
        }

        /// 按已加载的窗口整窗重拉并整体替换（轮询 / 下拉刷新），窗口起点不变、失败保留旧窗口
        func refresh(fetch: Fetch) async {
            guard !loading, case .loaded = phase else { return }
            let current = generation
            let count = max(PhotoWallView.pageSize, items.count)
            var rows: [API.LibraryItemView] = []
            var offset = start
            do {
                while offset < start + count {
                    let page = try await fetch(offset, PhotoWallView.pageSize)
                    rows += page
                    if page.count < PhotoWallView.pageSize { break }
                    offset += PhotoWallView.pageSize
                }
            } catch {
                return
            }
            guard current == generation, !loading else { return }
            hasMore = rows.count >= count
            let next = Self.dedupe(rows)
            if next.map(\.mediaItemId) != items.map(\.mediaItemId) || next != items { replace(with: next) }
        }

        /// 把窗口换成从 offset 开始的一页；成功返回 true（调用方据此滚回墙顶）
        func jump(to offset: Int, fetch: Fetch) async -> Bool {
            generation += 1
            let current = generation
            loading = true
            defer { if current == generation { loading = false } }
            do {
                let page = try await fetch(offset, PhotoWallView.pageSize)
                guard current == generation else { return false }
                start = offset
                hasMore = page.count >= PhotoWallView.pageSize
                replace(with: Self.dedupe(page))
                return true
            } catch {
                return false
            }
        }

        private func replace(with next: [API.LibraryItemView]) {
            items = next
            version += 1
        }

        private static func dedupe(_ items: [API.LibraryItemView]) -> [API.LibraryItemView] {
            var seen = Set<Int>()
            return items.filter { seen.insert($0.mediaItemId).inserted }
        }

        /// 行模型。按月**归并**而不是按连续段切：排序刚切换的一瞬间同月可能不连续，
        /// 归并保证每个月只有一段、行 id 唯一
        fileprivate func rows(width: CGFloat, density: GalleryDensity, grouped: Bool) -> [PhotoRow] {
            guard width > 0 else { return [] }
            let key = "\(version)|\(Int(width))|\(density.rawValue)|\(grouped)|\(monthIndex.count)"
            if let rowsCache, rowsCache.key == key { return rowsCache.rows }
            var buckets: [(month: String, indices: [Int])] = []
            if grouped {
                var position: [String: Int] = [:]
                for (i, item) in items.enumerated() {
                    let month = PhotoWallView.monthOf(item)
                    if let at = position[month] {
                        buckets[at].indices.append(i)
                    } else {
                        position[month] = buckets.count
                        buckets.append((month, [i]))
                    }
                }
            } else {
                buckets = [("", Array(items.indices))]
            }
            let totals = Dictionary(monthIndex.map { ($0.initial, $0.count) }, uniquingKeysWith: { first, _ in first })
            var rows: [PhotoRow] = []
            for (b, bucket) in buckets.enumerated() {
                if !bucket.month.isEmpty {
                    rows.append(PhotoRow(
                        id: "m-\(bucket.month)",
                        kind: .header(month: bucket.month, count: totals[bucket.month] ?? bucket.indices.count),
                        isFirst: b == 0
                    ))
                }
                let layout = density.layout(aspects: bucket.indices.map { items[$0].primaryAspect }, width: width)
                for band in GalleryDensity.bands(frames: layout.frames, height: layout.height) {
                    rows.append(PhotoRow(id: "b-\(bucket.month)-\(band.id)", kind: .band(band, indices: bucket.indices)))
                }
            }
            rowsCache = (key, rows)
            return rows
        }
    }
}

// MARK: - 滚回墙顶

/// 跳转后把外层 ScrollView 滚到墙顶。
///
/// 本视图不自带 ScrollView（嵌在调用方页面里），SwiftUI 的 ScrollViewReader 必须包住
/// ScrollView 才能用；这里在墙顶放一个零高的 UIKit 锚点，沿父视图链找到承载它的
/// UIScrollView，直接把锚点滚到顶栏下方。锚点在懒加载行之外，位置始终是真实的。
private final class WallScroller {
    weak var anchor: UIView?

    func scrollToTop() {
        guard let anchor else { return }
        var view = anchor.superview
        while let current = view, !(current is UIScrollView) { view = current.superview }
        guard let scrollView = view as? UIScrollView else { return }
        let y = anchor.convert(CGPoint.zero, to: scrollView).y
        let top = scrollView.adjustedContentInset.top
        let target = max(-top, y - top - 8)
        scrollView.setContentOffset(CGPoint(x: scrollView.contentOffset.x, y: target), animated: false)
    }
}

private struct WallScrollAnchor: UIViewRepresentable {
    let scroller: WallScroller

    func makeUIView(context: Context) -> UIView {
        let view = UIView()
        view.isUserInteractionEnabled = false
        scroller.anchor = view
        return view
    }

    func updateUIView(_ view: UIView, context: Context) {
        scroller.anchor = view
    }
}
