import SwiftUI

// MARK: - 偏好

/// 瀑布流密度（同 Web `components/photo-wall.tsx` 的 PhotoWallDensity）。
///
/// 图片库的相册墙与影视库的图床浏览共用这一个偏好（Web 也是同一个 localStorage 键）。
/// 每档各自定目标列宽、最少列数与间距：只按列宽算列数在手机宽度上三档会落到同样的列数，
/// 点了看不出变化；定了最少列数后紧凑在手机上也是三列、宽松是单列大图，任何宽度下
/// 三档都是三种明显不同的画面。
enum GalleryDensity: String, CaseIterable, Identifiable {
    case compact
    case standard
    case loose

    var id: String { rawValue }

    /// 菜单文案（照搬 Web）
    var label: String {
        switch self {
        case .compact: "紧凑"
        case .standard: "标准"
        case .loose: "宽松"
        }
    }

    /// 目标列宽：列数 = floor((容器宽 + 间距) / (目标列宽 + 间距))
    var column: CGFloat {
        switch self {
        case .compact: 150
        case .standard: 230
        case .loose: 340
        }
    }

    var minColumns: Int {
        switch self {
        case .compact: 3
        case .standard: 2
        case .loose: 1
        }
    }

    /// 间距：留白一宽视线就被格线切碎，三档都压得很窄（用户反馈 2026-09-07）
    var gap: CGFloat {
        switch self {
        case .compact: 3
        case .standard: 6
        case .loose: 10
        }
    }

    /// 瓦片取哪个规格的图：列窄的两档用 480px 的 photo-tile 派生图；宽松档列宽更大，
    /// 相册墙直接用 720px 的缩略图本体（nil），图廊没有那层缩略图，自己要 gallery-tile
    var variant: ImageVariant? {
        self == .loose ? nil : .photoTile
    }
}

/// 看图的浏览偏好（只是本机的便利设置，不是账号数据），UserDefaults 持久化，
/// 键名沿用 Web 的 localStorage 键，方便对照。
@Observable
final class GalleryPrefs {
    static let shared = GalleryPrefs()

    private static let modeKey = "movieclaw.library.gallery-mode"
    private static let groupedKey = "movieclaw.library.gallery-grouped"
    private static let densityKey = "movieclaw.photo-wall.density"

    /// 是否处于图床浏览模式（⋯ 菜单「图床浏览 / 回到海报墙」）：进详情再退回来还在图廊
    var galleryMode: Bool {
        didSet { UserDefaults.standard.set(galleryMode, forKey: Self.modeKey) }
    }

    /// 图床浏览是否按作品分组：默认分组，关掉就是整库一条瀑布流（用户决策 2026-09-07）
    var grouped: Bool {
        didSet { UserDefaults.standard.set(grouped, forKey: Self.groupedKey) }
    }

    /// 瀑布流密度：图床浏览与照片墙共用，默认标准
    var density: GalleryDensity {
        didSet { UserDefaults.standard.set(density.rawValue, forKey: Self.densityKey) }
    }

    private init() {
        let defaults = UserDefaults.standard
        galleryMode = defaults.object(forKey: Self.modeKey) as? Bool ?? false
        grouped = defaults.object(forKey: Self.groupedKey) as? Bool ?? true
        density = defaults.string(forKey: Self.densityKey).flatMap(GalleryDensity.init(rawValue:)) ?? .standard
    }
}

/// ⋯ 菜单里的看图偏好项（同 Web WallPrefItems）：「按作品分组」开关 + 「瀑布流密度」三档。
///
/// 菜单外壳（触发键、其它项）留在各调用方；照片库没有分组一说，传 `showsGrouping: false`
/// 只渲染密度。
struct GalleryPrefMenuItems: View {
    /// 是否显示「按作品分组」（图床浏览有，照片库没有）
    var showsGrouping: Bool = true
    @Bindable private var prefs = GalleryPrefs.shared

    var body: some View {
        if showsGrouping {
            Toggle("按作品分组", isOn: $prefs.grouped)
        }
        Section("瀑布流密度") {
            Picker("瀑布流密度", selection: $prefs.density) {
                ForEach(GalleryDensity.allCases) { density in
                    Text(density.label).tag(density)
                }
            }
            .pickerStyle(.inline)
            .labelsHidden()
        }
    }
}

// MARK: - 瀑布流排版（图床与照片墙共用）

extension GalleryDensity {
    /// 一张瓦片的位置：`id` 是它在本面墙条目列表里的下标
    struct Placed: Identifiable, Hashable {
        var id: Int
        var frame: CGRect
    }

    /// 横向切成的一条「带子」：LazyVStack 的一行。瓦片坐标相对带子顶部。
    ///
    /// 为什么切带子：SwiftUI 没有现成的懒加载瀑布流，整面墙放进一个视图的话，几千张图
    /// 会一次全部创建并发起加载。把排好位置的墙横切成定高的带子交给 LazyVStack，
    /// 只有视口附近的带子才会创建——相当于 Web 的 useTileWindow 虚拟化。
    /// 跨越带子边界的瓦片在两条带子里各画一次、各自裁掉出界的部分，拼起来是完整的一张，
    /// 点任何一半都是同一张图（同一个动作）。
    struct Band: Identifiable {
        var id: Int
        var height: CGFloat
        var tiles: [Placed]
    }

    /// 夹紧比例：全景图与长截图不能撑爆一列（超出部分裁掉，全屏时看完整原图）
    static let minAspect = 0.5
    static let maxAspect = 2.0

    static func clampAspect(_ raw: Double) -> Double {
        min(maxAspect, max(minAspect, raw > 0 ? raw : 1))
    }

    /// 最短列放置（Pinterest 原版算法）：比例在渲染前已知，不等图片加载、零抖动。
    /// 张数少于列数时退化成一根孤柱，改一行等高（同 Web layoutSparseRow）。
    func layout(aspects: [Double], width: CGFloat) -> (frames: [CGRect], height: CGFloat) {
        guard !aspects.isEmpty, width > 0 else { return ([], 0) }
        let columns = max(minColumns, Int(((width + gap) / (column + gap)).rounded(.down)))
        if aspects.count < columns {
            return sparseRow(aspects: aspects, width: width)
        }
        let columnWidth = (width - gap * CGFloat(columns - 1)) / CGFloat(columns)
        var heights = [CGFloat](repeating: 0, count: columns)
        var frames: [CGRect] = []
        frames.reserveCapacity(aspects.count)
        for raw in aspects {
            let height = columnWidth / Self.clampAspect(raw)
            var target = 0
            for i in 1 ..< columns where heights[i] < heights[target] - 0.5 { target = i }
            frames.append(CGRect(x: CGFloat(target) * (columnWidth + gap), y: heights[target], width: columnWidth, height: height))
            heights[target] += height + gap
        }
        return (frames, (heights.max() ?? gap) - gap)
    }

    /// 稀疏段：一行等高，高度取「填满整行所需」与「目标列宽 × 1.2」的较小者
    private func sparseRow(aspects: [Double], width: CGFloat) -> (frames: [CGRect], height: CGFloat) {
        let ratios = aspects.map { CGFloat(Self.clampAspect($0)) }
        let sum = ratios.reduce(0, +)
        let fit = (width - gap * CGFloat(ratios.count - 1)) / max(sum, 0.01)
        let height = min(column * 1.2, fit)
        var x: CGFloat = 0
        let frames = ratios.map { ratio -> CGRect in
            defer { x += ratio * height + gap }
            return CGRect(x: x, y: 0, width: ratio * height, height: height)
        }
        return (frames, height)
    }

    /// 把一面墙切成带子；`idBase` 让同一个 LazyVStack 里不同段的带子 id 不撞
    static func bands(frames: [CGRect], height: CGFloat, bandHeight: CGFloat = 420, idBase: Int = 0) -> [Band] {
        guard height > 0 else { return [] }
        let count = max(1, Int((height / bandHeight).rounded(.up)))
        var buckets = [[Placed]](repeating: [], count: count)
        for (i, frame) in frames.enumerated() {
            let first = min(count - 1, Int(frame.minY / bandHeight))
            let last = min(count - 1, Int(max(frame.minY, frame.maxY - 0.5) / bandHeight))
            for band in first ... last {
                buckets[band].append(Placed(id: i, frame: frame.offsetBy(dx: 0, dy: -CGFloat(band) * bandHeight)))
            }
        }
        return buckets.enumerated().map { band, tiles in
            Band(id: idBase + band, height: min(bandHeight, height - CGFloat(band) * bandHeight), tiles: tiles)
        }
    }

    /// 一条带子的渲染：瓦片按算好的位置绝对定位，出界部分裁掉
    struct BandView<Tile: View>: View {
        let band: Band
        @ViewBuilder let tile: (Placed) -> Tile

        var body: some View {
            ZStack(alignment: .topLeading) {
                ForEach(band.tiles) { placed in
                    tile(placed)
                        .frame(width: placed.frame.width, height: placed.frame.height)
                        .offset(x: placed.frame.minX, y: placed.frame.minY)
                }
            }
            .frame(maxWidth: .infinity, minHeight: band.height, maxHeight: band.height, alignment: .topLeading)
            .clipped()
        }
    }
}

// MARK: - 图床瀑布流

/// 影视库 / 其他库 / 合集 / 全部收藏的「图床浏览模式」（对应 Web `components/video-gallery.tsx`）。
///
/// 把每部作品的海报、剧照、分集剧照与章节场景图铺成一面瀑布流：
/// - 分组按**作品**：一部作品一段标题（可点进详情）+ 一面墙；⋯ 菜单关掉分组后
///   整库的图混成一条瀑布流，纯看图最沉浸（`GalleryPrefs.grouped`）；
/// - 瓦片上只有「已收藏」那颗心一个常驻角标（用户决策 2026-09-07：分类角标满墙都是
///   会把视线牵走）；
/// - 点图开灯箱，顶栏是「播放 / 收藏 / 详情」：章节图从那一帧起播、分集剧照播那一集；
///   心收藏的是整部作品，与详情页那颗是同一颗；
/// - 按作品分页（offset/limit），滑近底部自动要下一页；灯箱翻到末尾也会要。
///
/// **本视图是滚动内容，不自带 ScrollView**：调用方把它放进自己页面的 ScrollView
/// （页头、筛选条在它上面），数据加载由本视图通过 `fetch` 闭包自己完成，
/// 三个来源（单库 / 合集 / 收藏）因此都能复用。`reloadKey` 变化（换筛选 / 排序）即从头重载。
///
/// 「回到上次位置」（同 Web jumpGalleryTo）：宿主改 `startOffset` 就把窗口换成从那部作品开始的一页；
/// 滚动时经 `onFirstVisible` 回报视口里第一部作品在整份排序里的位置（按作品计，与海报墙同一口径），
/// 宿主据此写同一条位置记录。
struct LibraryGalleryWall: View {
    let reloadKey: AnyHashable
    var startOffset: Int = 0
    let fetch: (_ offset: Int, _ limit: Int) async throws -> [API.LibraryGalleryGroupView]
    /// 点作品段标题（调用方一般 push 到条目详情）
    var onOpenItem: (API.LibraryGalleryGroupView) -> Void = { _ in }
    /// 视口里第一部作品的位置（整份排序里的 offset）
    var onFirstVisible: (Int) -> Void = { _ in }

    /// 一页的作品数：一部作品十来张图，24 部约一屏半（同 Web GALLERY_PAGE_SIZE）
    static let pageSize = 24

    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @State private var feed = GalleryFeed()
    @State private var width: CGFloat = 0
    @State private var lightbox: LightboxSession?
    /// 灯箱关闭后要执行的跳转（播放 / 进详情）：必须等全屏层收起，否则播放器弹不出来
    @State private var afterDismiss: (() -> Void)?
    /// 视口里的行（行下标 → 该行第一张图所属作品的分组下标）
    @State private var visibleRows: [Int: Int] = [:]
    private var prefs: GalleryPrefs { GalleryPrefs.shared }

    private struct LoadKey: Hashable {
        var reload: AnyHashable
        var start: Int
    }

    var body: some View {
        content
            .frame(maxWidth: .infinity)
            .onGeometryChange(for: CGFloat.self, of: { $0.size.width }) { width = $0 }
            .task(id: LoadKey(reload: reloadKey, start: startOffset)) {
                // `.task` 每次重新出现都会重跑：从条目详情返回时取数口径没变，窗口原样保留
                // （同 Web：图廊窗口按「来源 + 排序」记键，键没变就不重拉），不跳回墙首
                let key = AnyHashable(LoadKey(reload: reloadKey, start: startOffset))
                guard !feed.holdsWindow(for: key) else { return }
                visibleRows = [:]
                await feed.reload(fetch: fetch, from: startOffset)
                // 被更新的口径打断的这一轮不记键（窗口里可能还是旧口径的数据）
                if !Task.isCancelled { feed.markWindow(key) }
            }
            .fullScreenCover(item: $lightbox, onDismiss: {
                afterDismiss?()
                afterDismiss = nil
            }) { session in
                GalleryLightbox(
                    feed: feed,
                    index: session.index,
                    fetch: fetch,
                    onLeave: { afterDismiss = $0 }
                )
            }
    }

    @ViewBuilder
    private var content: some View {
        switch feed.phase {
        case .loading:
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, minHeight: 240)
        case let .failed(message):
            ErrorState(message: message, retry: { await feed.reload(fetch: fetch, from: startOffset) })
                .frame(minHeight: 280)
        case .loaded:
            if feed.entries.isEmpty {
                EmptyState(systemImage: "photo.on.rectangle", title: "暂无图片")
                    .frame(minHeight: 280)
            } else {
                wall
            }
        }
    }

    private var wall: some View {
        let rows = feed.rows(width: width, density: prefs.density, grouped: prefs.grouped)
        return LazyVStack(alignment: .leading, spacing: 0) {
            ForEach(Array(rows.enumerated()), id: \.element.id) { position, row in
                rowView(row)
                    .onAppear {
                        // 离底部还有几行就要下一页：图大、下载慢，等滑到底再要就是一屏空瓦片
                        if position >= rows.count - 4 { Task { await feed.loadMore(fetch: fetch) } }
                    }
                    .onScrollVisibilityChange(threshold: 0.2) { visible in
                        trackVisible(position: position, group: feed.firstGroup(of: row), visible: visible)
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

    /// 回报视口里第一部作品的位置（整份排序里的 offset = 窗口起点 + 分组下标）
    private func trackVisible(position: Int, group: Int?, visible: Bool) {
        if visible, let group { visibleRows[position] = group } else { visibleRows[position] = nil }
        guard let first = visibleRows.min(by: { $0.key < $1.key })?.value else { return }
        onFirstVisible(feed.start + first)
    }

    @ViewBuilder
    private func rowView(_ row: GalleryRow) -> some View {
        switch row.kind {
        case let .header(group):
            Button { onOpenItem(group) } label: {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text(Self.groupTitle(group))
                        .font(.body.weight(.semibold))
                        .foregroundStyle(.white.opacity(0.85))
                        .lineLimit(1)
                    Text("\(group.images.count) 张")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(Theme.textFaint)
                        .fixedSize()
                    Spacer(minLength: 0)
                }
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .padding(.top, row.isFirst ? 0 : 28)
            .padding(.bottom, 12)
        case let .band(band, base):
            GalleryDensity.BandView(band: band) { placed in
                tile(at: base + placed.id)
            }
        }
    }

    @ViewBuilder
    private func tile(at index: Int) -> some View {
        if feed.entries.indices.contains(index) {
            let entry = feed.entries[index]
            let group = feed.groups[entry.group]
            let image = group.images[entry.image]
            Button {
                lightbox = LightboxSession(index: index)
            } label: {
                RemoteImage(url: api.image(image.url, prefs.density.variant ?? .galleryTile), placeholderSymbol: "photo")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .clipped()
                    .overlay(alignment: .topTrailing) {
                        // 收藏是唯一的常驻角标：它是状态不是分类，只有被收藏的那几张才有
                        if group.isFavorite {
                            Image(systemName: "heart.fill")
                                .font(.system(size: 14))
                                .foregroundStyle(Theme.danger)
                                .shadow(color: .black.opacity(0.75), radius: 2, y: 1)
                                .padding(8)
                        }
                    }
                    .clipShape(.rect(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(.white.opacity(0.07)))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("查看 \(group.title) · \(image.label)\(group.isFavorite ? "（已收藏）" : "")")
        }
    }

    static func groupTitle(_ group: API.LibraryGalleryGroupView) -> String {
        if let year = group.year { return "\(group.title) (\(year))" }
        return group.title
    }
}

/// 灯箱的一次打开（fullScreenCover 的 item）
private struct LightboxSession: Identifiable {
    let id = UUID()
    var index: Int
}

/// 墙上的一行：段标题或一条带子
private struct GalleryRow: Identifiable {
    enum Kind {
        case header(API.LibraryGalleryGroupView)
        /// 带子 + 本段第一张在铺平列表里的下标
        case band(GalleryDensity.Band, base: Int)
    }

    var id: String
    var kind: Kind
    var isFirst = false
}

/// 图廊的数据窗口：已加载的作品分组 + 铺平后的整份图列表（墙与灯箱按同一列表、同一下标）。
///
/// 用引用类型挂在墙上、同一个实例交给灯箱：灯箱翻到末尾要下一页、点心改收藏，
/// 墙上的瓦片与角标跟着变，两边读的是同一份。
@Observable
private final class GalleryFeed {
    enum Phase { case loading, failed(String), loaded }

    /// 铺平后的一张：所属分组下标 + 组内图片下标
    struct Entry { var group: Int; var image: Int }

    private(set) var phase: Phase = .loading
    private(set) var groups: [API.LibraryGalleryGroupView] = []
    private(set) var entries: [Entry] = []
    private(set) var hasMore = false
    /// 窗口在整份排序里的起点（「回到上次位置」跳过来后不为 0）
    @ObservationIgnored private(set) var start = 0

    /// 服务端口径的已取条数（含被去重掉的空组）：下一页的 offset
    @ObservationIgnored private var loaded = 0
    @ObservationIgnored private var loading = false
    /// 重载代次：换筛选后晚回来的旧页直接丢弃
    @ObservationIgnored private var generation = 0
    /// 排版缓存：灯箱翻页等无关状态变化时不重算几千张瓦片的位置
    @ObservationIgnored private var rowsCache: (key: String, rows: [GalleryRow])?
    @ObservationIgnored private var version = 0
    /// 当前窗口是按哪个取数口径（来源 + 排序 + 起点）载入的
    @ObservationIgnored private var windowKey: AnyHashable?

    /// 已经按这个口径载好了窗口（失败或还在转圈的不算）
    func holdsWindow(for key: AnyHashable) -> Bool {
        guard windowKey == key, case .loaded = phase else { return false }
        return true
    }

    func markWindow(_ key: AnyHashable) {
        if case .loaded = phase { windowKey = key }
    }

    func reload(fetch: (Int, Int) async throws -> [API.LibraryGalleryGroupView], from offset: Int = 0) async {
        generation += 1
        let current = generation
        if case .failed = phase { phase = .loading }
        // 换窗口起点：旧窗口作废，先显示转圈
        if offset != start { phase = .loading; replace(with: []) }
        loading = true
        defer { if current == generation { loading = false } }
        do {
            let page = try await fetch(offset, LibraryGalleryWall.pageSize)
            guard current == generation else { return }
            start = offset
            loaded = offset + page.count
            hasMore = page.count >= LibraryGalleryWall.pageSize
            replace(with: Self.dedupe(page))
            phase = .loaded
        } catch is CancellationError {
        } catch {
            guard current == generation else { return }
            if groups.isEmpty { phase = .failed(error.localizedDescription) }
            hasMore = false
        }
    }

    /// 追加下一页。某一页全是空组（没图的作品只用来数页）时接着要，免得墙停在原地不动
    func loadMore(fetch: (Int, Int) async throws -> [API.LibraryGalleryGroupView]) async {
        guard hasMore, !loading, case .loaded = phase else { return }
        loading = true
        let current = generation
        defer { if current == generation { loading = false } }
        var attempts = 0
        while hasMore, attempts < 5 {
            attempts += 1
            do {
                let page = try await fetch(loaded, LibraryGalleryWall.pageSize)
                guard current == generation else { return }
                loaded += page.count
                hasMore = page.count >= LibraryGalleryWall.pageSize
                let before = entries.count
                replace(with: Self.dedupe(groups + page))
                if entries.count > before { return }
            } catch {
                // 追加失败：停止无限滚动（同 Web），下次重载再来
                if current == generation, !(error is CancellationError) { hasMore = false }
                return
            }
        }
    }

    /// 收藏 / 取消收藏整部作品：先翻本地（墙上角标与灯箱的心同时变）再落库，失败翻回来
    func setFavorite(_ mediaItemId: Int, _ favorite: Bool, api: APIClient) async throws {
        patchFavorite(mediaItemId, favorite)
        do {
            let marks = try await api.playbackMarksSet(body: API.PlaybackMarksRequest(mediaItemId: mediaItemId, favorite: favorite))
            patchFavorite(mediaItemId, marks.isFavorite)
        } catch {
            patchFavorite(mediaItemId, !favorite)
            throw error
        }
    }

    private func patchFavorite(_ mediaItemId: Int, _ favorite: Bool) {
        var next = groups
        for i in next.indices where next[i].mediaItemId == mediaItemId { next[i].isFavorite = favorite }
        replace(with: next)
    }

    private func replace(with next: [API.LibraryGalleryGroupView]) {
        groups = next
        entries = next.enumerated().flatMap { g, group in group.images.indices.map { Entry(group: g, image: $0) } }
        version += 1
    }

    /// 上墙前的统一口径：没图的作品不占位，同一部作品只留最前面那一组（同 Web dedupeGalleryGroups）
    private static func dedupe(_ groups: [API.LibraryGalleryGroupView]) -> [API.LibraryGalleryGroupView] {
        var seen = Set<Int>()
        return groups.filter { group in
            guard !group.images.isEmpty, !seen.contains(group.mediaItemId) else { return false }
            seen.insert(group.mediaItemId)
            return true
        }
    }

    /// 一行里第一张图所属作品的分组下标（段标题就是那部作品）
    func firstGroup(of row: GalleryRow) -> Int? {
        switch row.kind {
        case let .header(group):
            return groups.firstIndex { $0.mediaItemId == group.mediaItemId }
        case let .band(band, base):
            guard let first = band.tiles.min(by: { $0.id < $1.id }), entries.indices.contains(base + first.id) else { return nil }
            return entries[base + first.id].group
        }
    }

    /// 墙的行模型：分组时每部作品一个段标题 + 若干带子；不分组时整份列表一面墙
    func rows(width: CGFloat, density: GalleryDensity, grouped: Bool) -> [GalleryRow] {
        guard width > 0 else { return [] }
        let key = "\(version)|\(Int(width))|\(density.rawValue)|\(grouped)"
        if let rowsCache, rowsCache.key == key { return rowsCache.rows }
        var rows: [GalleryRow] = []
        if grouped {
            var base = 0
            for (g, group) in groups.enumerated() {
                rows.append(GalleryRow(id: "h-\(group.mediaItemId)", kind: .header(group), isFirst: g == 0))
                let layout = density.layout(aspects: group.images.map(\.aspect), width: width)
                for band in GalleryDensity.bands(frames: layout.frames, height: layout.height) {
                    rows.append(GalleryRow(id: "b-\(group.mediaItemId)-\(band.id)", kind: .band(band, base: base)))
                }
                base += group.images.count
            }
        } else {
            let aspects = entries.map { groups[$0.group].images[$0.image].aspect }
            let layout = density.layout(aspects: aspects, width: width)
            for band in GalleryDensity.bands(frames: layout.frames, height: layout.height) {
                rows.append(GalleryRow(id: "all-\(band.id)", kind: .band(band, base: 0)))
            }
        }
        rowsCache = (key, rows)
        return rows
    }
}

/// 图廊的灯箱（对应 Web VideoGalleryLightbox）：翻的是铺平后的整份图列表，
/// 顶栏右侧是「播放 / 收藏 / 详情」。
private struct GalleryLightbox: View {
    let feed: GalleryFeed
    @State var index: Int
    let fetch: (Int, Int) async throws -> [API.LibraryGalleryGroupView]
    /// 登记一个「灯箱收起后再做」的跳转，并收起灯箱
    let onLeave: (@escaping () -> Void) -> Void

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss
    @Environment(Router.self) private var router
    @State private var note: String?

    var body: some View {
        LibraryZoomableImage.Lightbox(
            slides: slides,
            index: $index,
            hasMore: feed.hasMore,
            onReachEnd: { Task { await feed.loadMore(fetch: fetch) } },
            note: note
        ) {
            if let current {
                let (group, image) = current
                let playLabel = image.tSeconds != nil ? "从此处播放" : "播放"
                LibraryZoomableImage.ActionButton(systemImage: "play.fill", label: playLabel) {
                    // 章节图从那一帧起播，分集剧照播那一集，海报 / 剧照从头（或续播）
                    leave {
                        router.play(PlayRequest(mediaItemId: group.mediaItemId, season: image.season, episode: image.episode, startSeconds: image.tSeconds))
                    }
                }
                // 收藏的是整部作品（与详情页那颗心同一落点），不是这张图或这一集
                LibraryZoomableImage.ActionButton(
                    systemImage: group.isFavorite ? "heart.fill" : "heart",
                    label: group.isFavorite ? "取消收藏" : "收藏",
                    tint: group.isFavorite ? Theme.danger : .white.opacity(0.85)
                ) {
                    toggleFavorite(group)
                }
                LibraryZoomableImage.ActionButton(systemImage: "arrow.up.forward.square", label: "前往影片详情") {
                    // 分集剧照 / 剧集章节图带季集号，详情页直接落到那一集；落点库取分组自带的
                    let unit = image.season != nil && image.episode != nil
                    leave {
                        router.push(.libraryItem(
                            libraryId: group.libraryId,
                            itemId: group.mediaItemId,
                            season: unit ? image.season : nil,
                            episode: unit ? image.episode : nil
                        ))
                    }
                }
            }
        }
    }

    private var current: (API.LibraryGalleryGroupView, API.LibraryGalleryImageView)? {
        guard feed.entries.indices.contains(index) else { return nil }
        let entry = feed.entries[index]
        let group = feed.groups[entry.group]
        return (group, group.images[entry.image])
    }

    private var slides: [LibraryZoomableImage.Slide] {
        feed.entries.enumerated().map { i, entry in
            let group = feed.groups[entry.group]
            let image = group.images[entry.image]
            // 墙上的派生图先铺底，主图走屏幕适配派生（长边 2048、转 WebP，翻页跟手）
            return LibraryZoomableImage.Slide(
                id: i,
                title: "\(LibraryGalleryWall.groupTitle(group)) · \(image.label)",
                thumbURL: api.image(image.url, .photoTile),
                screenURL: api.image(image.url, .photoScreen),
                aspect: image.aspect
            )
        }
    }

    private func leave(_ action: @escaping () -> Void) {
        onLeave(action)
        dismiss()
    }

    private func toggleFavorite(_ group: API.LibraryGalleryGroupView) {
        let next = !group.isFavorite
        Task {
            do {
                try await feed.setFavorite(group.mediaItemId, next, api: api)
            } catch is CancellationError {
            } catch {
                // 全屏灯箱盖住了全局 Toast，失败原因就地提示
                note = error.localizedDescription.isEmpty ? "收藏失败，请稍后重试" : error.localizedDescription
                try? await Task.sleep(for: .seconds(4))
                note = nil
            }
        }
    }
}
