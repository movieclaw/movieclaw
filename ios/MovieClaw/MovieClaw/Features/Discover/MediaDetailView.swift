import SafariServices
import SwiftUI

/// 影片详情（对应 Web `components/media-detail-view.tsx`）：`/media/{type}/{id}` 与 `/media/douban/{id}`。
///
/// 数据：`GET /discover/titles/{titleRef}` 一次给全（资料、演职员、预告片、剧照海报、系列、相似推荐、在库入口）。
/// 版式自上而下：沉浸剧照 Hero → 标题与元信息 → 在库条 → 订阅 / 搜索资源 → 简介（4 行折叠）→
/// 演职员 → 预告片 → 剧照与海报（灯箱可「设为背景」）→ 系列 → 相似推荐 → 相关链接。
///
/// 按钮规则同 Web：已在库的电影收起「订阅」与「搜索资源」（已订阅时仍显示订阅状态键）；
/// 订阅键未订阅时打开订阅弹层，已订阅时显示「已订阅 · 状态」、点它同样打开订阅弹层（由弹层管理态接手）。
///
/// 首屏预存（同 Web `getMediaSeed`）：站内点卡片进来时先用列表字段（标题、海报、简介…）渲染，
/// 详情接口返回后原位替换；有预存时接口失败也不打断页面，只有直达（无预存）才进失败页。
struct MediaDetailView: View {
    let titleRef: String

    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Router.self) private var router
    @Environment(\.openURL) private var openURL
    @Environment(\.displayScale) private var displayScale

    @State private var state: Loadable<API.DiscoveredTitleDetailsView> = .loading
    /// 列表字段拼出的半份详情（没有预存为 nil）
    @State private var seed: API.DiscoveredTitleDetailsView?
    @State private var titleVisible = false
    @State private var overviewExpanded = false
    /// 简介完整排版高度与 4 行截断后的高度：前者更高才给「展开全文」（同 Web scrollHeight > clientHeight）
    @State private var overviewFullHeight: CGFloat = 0
    @State private var overviewShownHeight: CGFloat = 0
    @State private var trailer: WebLink?
    /// 正在探测 YouTube 可达性的预告片
    @State private var probingTrailer: String?
    /// 本机连不上 YouTube 时的说明（带「在 YouTube 打开」）
    @State private var blockedTrailer: API.MediaVideo?
    /// 顶部安全区高度：沉浸剧照用等量负边距顶到屏幕物理顶边
    @State private var topInset: CGFloat = 0
    /// 页面宽度（逻辑点）：判断沉浸大图是否值得取原图
    @State private var pageWidth: CGFloat = 0

    init(titleRef: String) {
        self.titleRef = titleRef
        _seed = State(initialValue: DiscoverMediaSeed.item(for: titleRef).map(API.DiscoveredTitleDetailsView.init(seed:)))
    }

    var body: some View {
        Group {
            switch state {
            case .loading:
                if let seed {
                    content(seed)
                } else {
                    VStack(spacing: 12) {
                        ProgressView().controlSize(.large)
                        Text("正在加载详情…").font(.subheadline).foregroundStyle(Theme.textMuted)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            case .failed where seed != nil:
                // 有预存时详情拉取失败不打断页面：列表字段仍可完整展示
                content(seed!)
            case let .failed(message):
                ContentUnavailableView {
                    Label("未能加载该影片详情", systemImage: "film")
                } description: {
                    Text("资源可能已下线，或网络暂时不可达。请返回后重试。\n\(message)")
                } actions: {
                    Button("重试") { Task { await load() } }.discoverProminentButton()
                    Button("返回") { router.pop() }.buttonStyle(.glass)
                }
                .accessibilityIdentifier("error-state")
            case let .loaded(detail):
                content(detail)
            }
        }
        .appBackground(.plain) // 氛围页：自带沉浸大图，不铺全站蒙版（Web isHomeRoute）
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                Text(state.value?.title.title ?? seed?.title.title ?? "")
                    .font(.headline)
                    .lineLimit(1)
                    .opacity(titleVisible ? 1 : 0)
                    .animation(.easeInOut(duration: 0.2), value: titleVisible)
            }
        }
        .tracksSubscriptionIndex()
        .task { if state.value == nil { await load() } }
        .sheet(item: $trailer) { link in
            SafariView(url: link.url)
                .ignoresSafeArea()
        }
        .alert("当前设备无法直连 YouTube", isPresented: Binding(get: { blockedTrailer != nil }, set: { if !$0 { blockedTrailer = nil } }), presenting: blockedTrailer) { video in
            if let url = WebLink(video.watchUrl)?.url {
                Button("在 YouTube 打开 ↗") { openURL(url) }
            }
            Button("好的", role: .cancel) {}
        } message: { _ in
            Text("预告片由 YouTube 提供，播放需要本机能访问它。服务端在「设置 → 网络」配的代理只作用于服务端自己抓数据，不经过播放器；给本机挂上代理后即可正常播放。")
        }
    }

    private func load() async {
        await Loadable.load(into: $state) { try await api.discoverGetTitleDetails(titleRef: titleRef) }
    }

    // MARK: 版式

    @ViewBuilder
    private func content(_ detail: API.DiscoveredTitleDetailsView) -> some View {
        let item = detail.title
        let heroURL = heroImage(detail)
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                if let heroURL {
                    Color.clear
                        .containerRelativeFrame(.vertical) { height, _ in min(height * 0.62, 460) }
                        .overlay { RemoteImage(url: heroURL) }
                        .clipped()
                        .overlay(alignment: .top) {
                            LinearGradient(colors: [.black.opacity(0.45), .clear], startPoint: .top, endPoint: .bottom).frame(height: 112)
                        }
                        .overlay(alignment: .bottom) {
                            LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: Theme.background.opacity(0.55), location: 0.5), .init(color: Theme.background, location: 1)], startPoint: .top, endPoint: .bottom)
                                .frame(height: 260)
                        }
                        .accessibilityHidden(true)
                }
                header(detail)
                    .padding(.horizontal, Theme.pagePadding)
                    .padding(.top, heroURL == nil ? 12 : -150)
                    .onScrollVisibilityChange(threshold: 0.2) { visible in titleVisible = !visible }

                VStack(alignment: .leading, spacing: 28) {
                    if !item.overview.isEmpty {
                        overview(item.overview)
                            .padding(.horizontal, Theme.pagePadding)
                    }
                    let people = castPeople(detail)
                    if !people.isEmpty {
                        DetailCastRow(people: people)
                    }
                    if !detail.videos.isEmpty {
                        trailerRow(detail)
                    }
                    if !detail.backdrops.isEmpty || !detail.posters.isEmpty {
                        DetailPhotoWall(title: item.title, backdrops: detail.backdrops, posters: detail.posters)
                    }
                    if let collection = detail.collection, collection.titles.count > 1 {
                        DiscoverPosterRow(title: collection.name, items: collection.titles.map(DiscoverPosterItem.init))
                    }
                    if !detail.recommendations.isEmpty {
                        DiscoverPosterRow(title: "相似推荐", items: detail.recommendations.map(DiscoverPosterItem.init))
                    }
                    externalLinks(detail)
                        .padding(.horizontal, Theme.pagePadding)
                }
                .padding(.top, 20)
                .padding(.bottom, 40)
            }
            .padding(.top, heroURL == nil ? 0 : -topInset)
        }
        .scrollEdgeEffectHidden(heroURL != nil && !titleVisible, for: .top)
        .onGeometryChange(for: CGFloat.self) { $0.safeAreaInsets.top } action: { topInset = $0 }
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { pageWidth = $0 }
        .refreshable { await load() }
        .accessibilityIdentifier("media-detail")
    }

    /// 沉浸大图：剧照（w1280），没有剧照时用海报兜底（豆瓣条目没有横版剧照）。
    /// 只有物理宽度超过 1280 像素的屏幕才换后端给的原图（同 Web `useWantsOriginalImage`：
    /// 手机 393pt × 3 = 1179 像素，w1280 已 1:1 覆盖，原图只是白白多下 1–3 MB）。
    private func heroImage(_ detail: API.DiscoveredTitleDetailsView) -> URL? {
        let wantsOriginal = pageWidth * displayScale > 1280
        if wantsOriginal, detail.title.provider != "douban", let original = detail.backdropOriginalUrl {
            return api.image(original)
        }
        let fallback = detail.title.backdropUrl ?? (detail.title.posterUrl.isEmpty ? nil : detail.title.posterUrl)
        return api.image(fallback)
    }

    private func header(_ detail: API.DiscoveredTitleDetailsView) -> some View {
        let item = detail.title
        let info = detail.metadata
        let isMovie = item.mediaType != "tv"
        let parts = TitleRefParts(item.titleRef)
        let sub = SubscriptionIndex.shared.subscription(source: item.provider, externalId: item.externalId, mediaType: item.mediaType ?? parts.mediaType)
        let ownedMovie = isMovie && !detail.libraryLinks.isEmpty
        let showSubscribe = permissions.canSubscribe && (sub != nil || !ownedMovie)
        let showSearch = permissions.canSearch && !ownedMovie

        return VStack(alignment: .leading, spacing: 10) {
            Text(item.title)
                .font(.system(size: 28, weight: .bold))
                .foregroundStyle(.white)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityAddTraits(.isHeader)

            HStack(spacing: 8) {
                if item.providerRating > 0 {
                    HStack(spacing: 4) {
                        Image(systemName: "star.fill").foregroundStyle(Theme.warning)
                        Text(String(format: "%.1f", item.providerRating)).font(.title3.bold()).foregroundStyle(.white)
                    }
                }
                let metas = [item.releaseYear.map(String.init), item.extentLabel.isEmpty ? nil : item.extentLabel].compactMap { $0 }
                ForEach(Array(metas.enumerated()), id: \.offset) { index, text in
                    if index > 0 || item.providerRating > 0 { Text("·") }
                    Text(text)
                }
            }
            .font(.subheadline)
            .monospacedDigit()
            .foregroundStyle(.white.opacity(0.8))

            let place = [info.country, info.language].filter { !$0.isEmpty }.joined(separator: " · ")
            if !place.isEmpty || !item.genres.isEmpty {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    if !place.isEmpty { Text(place).foregroundStyle(.white.opacity(0.65)) }
                    if !place.isEmpty, !item.genres.isEmpty { Text("｜").foregroundStyle(.white.opacity(0.25)) }
                    if !item.genres.isEmpty { Text(item.genres.joined(separator: " · ")) }
                }
                .font(.subheadline)
                .foregroundStyle(.white.opacity(0.72))
            }
            if !info.released.isEmpty {
                Text("\(isMovie ? "上映日期" : "首播日期") · \(info.released)")
                    .font(.subheadline)
                    .monospacedDigit()
                    .foregroundStyle(.white.opacity(0.55))
            }

            if !detail.libraryLinks.isEmpty {
                libraryBar(detail.libraryLinks)
                    .padding(.top, 4)
            }

            if showSubscribe || showSearch || sub != nil {
                DiscoverFlowLayout(spacing: 8, lineSpacing: 8) {
                    if showSubscribe {
                        if let sub {
                            Button {
                                // 已订阅也打开订阅弹层，由弹层管理态接手（同 Web `openSubscribe`）
                                router.present(.subscribe(SubscribeRequest(titleRef: item.titleRef, title: item.title)))
                            } label: {
                                Label {
                                    Text("已订阅 · \(SubscriptionStatusMeta.label(sub.status))")
                                } icon: {
                                    Image(systemName: "checkmark").foregroundStyle(SubscriptionStatusMeta.color(sub.status))
                                }
                                .font(.subheadline.weight(.semibold))
                            }
                            .buttonStyle(.glass)
                            .accessibilityIdentifier("detail-subscribed")
                        } else {
                            Button {
                                router.present(.subscribe(SubscribeRequest(titleRef: item.titleRef, title: item.title)))
                            } label: {
                                Label("订阅追踪", systemImage: "bell").font(.subheadline.weight(.semibold))
                            }
                            .discoverProminentButton()
                            .accessibilityIdentifier("detail-subscribe")
                        }
                    }
                    if showSearch {
                        Button {
                            router.push(.search(.init(q: item.title)))
                        } label: {
                            Label("搜索资源", systemImage: "magnifyingglass").font(.subheadline.weight(.semibold))
                        }
                        .buttonStyle(.glass)
                        .accessibilityIdentifier("detail-search")
                    }
                    if let sub {
                        HStack(spacing: 6) {
                            Circle().fill(SubscriptionStatusMeta.color(sub.status)).frame(width: 6, height: 6)
                            Text(SubscriptionStatusMeta.progressNote(sub))
                        }
                        .font(.subheadline)
                        .foregroundStyle(Theme.textMuted)
                        .frame(minHeight: 36)
                    }
                }
                .padding(.top, 6)
            }
        }
    }

    /// 在库条：列出每个包含它的媒体库，点进库内条目详情
    private func libraryBar(_ links: [API.MediaLibraryLink]) -> some View {
        DiscoverFlowLayout(spacing: 10, lineSpacing: 8) {
            Label("在库", systemImage: "folder")
                .font(.subheadline.weight(.medium))
                .foregroundStyle(Color(red: 0.6, green: 0.95, blue: 0.8))
            ForEach(links, id: \.mediaItemId) { link in
                Button(link.libraryName) {
                    router.push(.libraryItem(libraryId: link.libraryId, itemId: link.mediaItemId))
                }
                .font(.subheadline)
                .foregroundStyle(Color(red: 0.85, green: 1, blue: 0.93))
                .underline()
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(Color(red: 0.2, green: 0.8, blue: 0.55).opacity(0.08), in: .rect(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color(red: 0.4, green: 0.9, blue: 0.7).opacity(0.2)))
        .discoverContainer("detail-library-bar")
    }

    private func overview(_ text: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(text)
                .font(.subheadline)
                .lineSpacing(4)
                .foregroundStyle(.white.opacity(0.78))
                .lineLimit(overviewExpanded ? nil : 4)
                // 实测是否溢出：同宽度下完整排版的高度超过 4 行截断后的高度才给「展开全文」
                .background(alignment: .topLeading) {
                    if !overviewExpanded {
                        Text(text)
                            .font(.subheadline)
                            .lineSpacing(4)
                            .fixedSize(horizontal: false, vertical: true)
                            .hidden()
                            .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { fullHeight in
                                overviewFullHeight = fullHeight
                            }
                    }
                }
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { shownHeight in
                    overviewShownHeight = shownHeight
                }
            if overviewExpanded || overviewFullHeight > overviewShownHeight + 1 {
                Button {
                    withAnimation { overviewExpanded.toggle() }
                } label: {
                    HStack(spacing: 2) {
                        Text(overviewExpanded ? "收起" : "展开全文")
                        Image(systemName: "chevron.right").rotationEffect(.degrees(overviewExpanded ? -90 : 90)).font(.caption)
                    }
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.accent)
                }
                .accessibilityIdentifier("detail-overview-toggle")
            }
        }
    }

    private func castPeople(_ detail: API.DiscoveredTitleDetailsView) -> [DetailCastRow.Person] {
        let isMovie = detail.title.mediaType != "tv"
        let credit = isMovie ? "导演" : "主创"
        let info = detail.metadata
        var people: [DetailCastRow.Person] = []
        if !info.directorCredits.isEmpty {
            people += info.directorCredits.map { .init(name: $0.name, subtitle: credit, avatarUrl: $0.avatarUrl, tmdbPersonId: $0.tmdbPersonId) }
        } else {
            var seen = Set<String>()
            for name in info.directors where seen.insert(name).inserted {
                people.append(.init(name: name, subtitle: credit, avatarUrl: nil, tmdbPersonId: nil))
            }
        }
        people += info.cast.map { .init(name: $0.name, subtitle: $0.role.map { "饰 \($0)" }, avatarUrl: $0.avatarUrl, tmdbPersonId: $0.tmdbPersonId) }
        return people
    }

    private func trailerRow(_ detail: API.DiscoveredTitleDetailsView) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("预告片").font(.title3.weight(.semibold)).foregroundStyle(Theme.text)
                .padding(.horizontal, Theme.pagePadding)
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(alignment: .top, spacing: 12) {
                    ForEach(detail.videos, id: \.key) { video in
                        Button {
                            playTrailer(video)
                        } label: {
                            VStack(alignment: .leading, spacing: 6) {
                                Color.clear
                                    .aspectRatio(16 / 9, contentMode: .fit)
                                    .overlay { RemoteImage(url: api.image(video.thumbnailUrl)) }
                                    .overlay {
                                        Group {
                                            if probingTrailer == video.key {
                                                ProgressView().tint(.white)
                                            } else {
                                                Image(systemName: "play.fill").font(.title3)
                                            }
                                        }
                                        .foregroundStyle(.white)
                                        .frame(width: 44, height: 44)
                                        .background(.black.opacity(0.55), in: .circle)
                                    }
                                    .overlay(alignment: .topLeading) {
                                        Text(video.kind)
                                            .font(.caption.weight(.medium))
                                            .foregroundStyle(.white.opacity(0.85))
                                            .padding(.horizontal, 8).padding(.vertical, 2)
                                            .background(.black.opacity(0.6), in: .capsule)
                                            .padding(8)
                                    }
                                    .clipShape(.rect(cornerRadius: 12))
                                Text(video.name).font(.subheadline).foregroundStyle(Theme.textMuted).lineLimit(1)
                            }
                            .frame(width: 240)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("播放预告片：\(video.name)")
                    }
                }
                .padding(.horizontal, Theme.pagePadding)
            }
        }
        .discoverContainer("detail-trailers")
    }

    /// 点预告片：先探测本机能否直连 YouTube（同 Web `useYoutubeReachable`），
    /// 能连就在应用内 Safari 播放，连不上改为说明原因并给「在 YouTube 打开」，
    /// 而不是留给用户一个永远转圈的页面
    private func playTrailer(_ video: API.MediaVideo) {
        guard probingTrailer == nil else { return }
        probingTrailer = video.key
        Task {
            let reachable = await DiscoverYouTubeProbe.reachable(videoKey: video.key)
            probingTrailer = nil
            if reachable, let link = WebLink(video.watchUrl) {
                trailer = link
            } else {
                blockedTrailer = video
            }
        }
    }

    @ViewBuilder
    private func externalLinks(_ detail: API.DiscoveredTitleDetailsView) -> some View {
        let item = detail.title
        let tmdbURL = item.provider == "tmdb" ? URL(string: "https://www.themoviedb.org/\(item.mediaType ?? "movie")/\(item.externalId)") : nil
        let doubanURL = detail.metadata.sourceUrl.flatMap(URL.init(string:))
        if tmdbURL != nil || doubanURL != nil {
            HStack(spacing: 16) {
                Text("相关链接").foregroundStyle(Theme.textFaint)
                if let tmdbURL { Button("TMDB ↗") { openURL(tmdbURL) } }
                if let doubanURL { Button("豆瓣 ↗") { openURL(doubanURL) } }
            }
            .font(.subheadline)
            .foregroundStyle(Theme.textMuted)
            .padding(.top, 8)
            .discoverContainer("detail-links")
        }
    }
}

/// 本机能否直连 YouTube：取一张 YouTube 图床的小图作探针，6 秒无响应按不可达算。
///
/// 探针刻意不走后端图片代理：要测的正是「本机自己」的可达性——服务端在「设置 → 网络」配的代理
/// 只作用于服务端抓数据，帮不到播放器；走了代理就变成在测服务端，结论会反过来骗人。
enum DiscoverYouTubeProbe {
    private static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 6
        config.timeoutIntervalForResource = 6
        return URLSession(configuration: config)
    }()

    static func reachable(videoKey: String) async -> Bool {
        guard let url = URL(string: "https://i.ytimg.com/vi/\(videoKey)/default.jpg") else { return false }
        guard let (_, response) = try? await session.data(from: url) else { return false }
        return (response as? HTTPURLResponse).map { (200 ..< 300).contains($0.statusCode) } ?? false
    }
}

/// 可用于 `.sheet(item:)` 的外链（只接受 http/https：SFSafariViewController 遇到其它协议会崩溃）
struct WebLink: Identifiable {
    let url: URL
    var id: String { url.absoluteString }

    init?(_ raw: String?) {
        guard let raw, let url = URL(string: raw), ["http", "https"].contains(url.scheme?.lowercased() ?? "") else { return nil }
        self.url = url
    }
}

// MARK: - 演职员

/// 演职员横滚条：2:3 头像卡 + 姓名 + 身份（导演/主创/饰 X）；有 TMDB 影人 ID 的进 TMDB 影人页
struct DetailCastRow: View {
    struct Person: Hashable {
        var name: String
        var subtitle: String?
        var avatarUrl: String?
        var tmdbPersonId: Int?
    }

    let people: [Person]
    var title = "演职员"
    @Environment(\.api) private var api
    @Environment(Router.self) private var router

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.title3.weight(.semibold)).foregroundStyle(Theme.text)
                .padding(.horizontal, Theme.pagePadding)
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(alignment: .top, spacing: 12) {
                    ForEach(Array(people.enumerated()), id: \.offset) { _, person in
                        card(person)
                    }
                }
                .padding(.horizontal, Theme.pagePadding)
            }
        }
        .discoverContainer("detail-cast")
    }

    @ViewBuilder
    private func card(_ person: Person) -> some View {
        let body = VStack(alignment: .leading, spacing: 2) {
            Color.clear
                .aspectRatio(2 / 3, contentMode: .fit)
                .overlay {
                    ZStack {
                        LinearGradient(colors: [.white.opacity(0.07), .white.opacity(0.02)], startPoint: .top, endPoint: .bottom)
                        Text(String(person.name.prefix(1))).font(.system(size: 26, weight: .semibold)).foregroundStyle(.white.opacity(0.3))
                        if person.avatarUrl != nil {
                            RemoteImage(url: api.image(person.avatarUrl), placeholderSymbol: "person.fill")
                        }
                    }
                }
                .clipShape(.rect(cornerRadius: 12))
                .padding(.bottom, 4)
            Text(person.name).font(.subheadline.weight(.medium)).foregroundStyle(Theme.text).lineLimit(1)
            if let subtitle = person.subtitle {
                Text(subtitle).font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
            }
        }
        .frame(width: 104)
        if let id = person.tmdbPersonId {
            Button { router.push(.discoveredPerson(tmdbId: id)) } label: { body }
                .buttonStyle(.plain)
                .accessibilityLabel("查看 \(person.name) 的影人页")
        } else {
            body
        }
    }
}

// MARK: - 剧照与海报

/// 剧照 / 海报两个分页的横滚图集；点开灯箱，剧照灯箱可「设为背景」
struct DetailPhotoWall: View {
    let title: String
    let backdrops: [API.MediaImage]
    let posters: [API.MediaImage]

    @Environment(\.api) private var api
    @State private var tab = "backdrops"
    @State private var lightbox: DiscoverLightboxContent?

    private var tabs: [(id: String, label: String, images: [API.MediaImage])] {
        [("backdrops", "剧照", backdrops), ("posters", "海报", posters)].filter { !$0.images.isEmpty }
    }

    var body: some View {
        let active = tabs.first { $0.id == tab } ?? tabs[0]
        let landscape = active.id == "backdrops"
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Text("剧照与海报").font(.title3.weight(.semibold)).foregroundStyle(Theme.text)
                if tabs.count > 1 {
                    ForEach(tabs, id: \.id) { item in
                        DiscoverChip(label: "\(item.label) \(item.images.count)", active: item.id == active.id) { tab = item.id }
                    }
                }
            }
            .padding(.horizontal, Theme.pagePadding)
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(spacing: 10) {
                    ForEach(Array(active.images.enumerated()), id: \.offset) { index, image in
                        Button {
                            open(active: active, index: index)
                        } label: {
                            RemoteImage(url: api.image(image.previewUrl), placeholderSymbol: "photo")
                                .frame(width: landscape ? 185 : 84, height: landscape ? 104 : 126)
                                .clipShape(.rect(cornerRadius: 10))
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("查看\(active.label)第 \(index + 1) 张")
                    }
                }
                .padding(.horizontal, Theme.pagePadding)
            }
        }
        .fullScreenCover(item: $lightbox) { DiscoverLightbox(content: $0).sheetFeedback() }
        .discoverContainer("detail-photos")
    }

    private func open(active: (id: String, label: String, images: [API.MediaImage]), index: Int) {
        let images = active.images
        let client = api
        var action: LightboxAction?
        if active.id == "backdrops" {
            action = MediaDetailView.setBackdropAction(
                upload: { try await client.uploadBackdrop(fromRemote: images[$0].fullUrl) },
                apply: { await AppBackdropStore.shared.apply(appearance: $0, api: client) }
            )
        }
        lightbox = DiscoverLightboxContent(
            urls: images.map { api.image($0.fullUrl) },
            initialIndex: index,
            title: "\(title) · \(active.label)",
            action: action,
            thumbnails: images.map { api.image($0.previewUrl) },
            thumbAspect: active.id == "backdrops" ? 16.0 / 9.0 : 2.0 / 3.0
        )
    }
}

// MARK: - Safari

/// 应用内 Safari（预告片：YouTube 必须由本机直连，服务端代理帮不上）
struct SafariView: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> SFSafariViewController {
        SFSafariViewController(url: url)
    }

    func updateUIViewController(_ controller: SFSafariViewController, context: Context) {}
}

// MARK: - 首屏预存

extension API.DiscoveredTitleDetailsView {
    /// 列表字段拼出的半份详情（同 Web 详情页 `detail?.item ?? listItem`）：只有标题区可用，
    /// 演职员、预告片、剧照、推荐等分区为空不渲染，等详情接口返回后整体替换。
    init(seed item: DiscoverPosterItem) {
        self.init(
            title: API.DiscoveredTitleView(
                titleRef: item.resolvedTitleRef,
                provider: item.source,
                externalId: item.externalId,
                mediaType: item.mediaType,
                title: item.title,
                originalTitle: item.originalTitle,
                releaseYear: item.year,
                providerRating: item.rating,
                genres: item.genres,
                extentLabel: item.extent,
                overview: item.overview,
                posterUrl: item.posterUrl ?? "",
                backdropUrl: item.backdropUrl,
                libraryStatus: item.libraryStatus
            ),
            metadata: API.DiscoveredTitleMetadata(
                directors: [], directorCredits: [], cast: [], country: "", language: "", released: "",
                network: nil, aliases: [], sourceUrl: nil
            ),
            backdropOriginalUrl: nil,
            videos: [],
            backdrops: [],
            posters: [],
            collection: nil,
            recommendations: [],
            libraryLinks: []
        )
    }
}

extension MediaDetailView {
    /// 剧照灯箱「设为背景」：上传成功后**立即**把后端回显的外观视图应用到全站背景
    /// （同 Web lib/backdrop.tsx 上传后 applyView）。曾经丢掉回显，提示「已设为背景」
    /// 背景却要重启才变（第二轮审计 N-03-1）。拆成静态函数便于单元测试核对这条接线
    static func setBackdropAction(
        upload: @escaping (Int) async throws -> API.AppearanceView,
        apply: @escaping (API.AppearanceView) async -> Void
    ) -> LightboxAction {
        LightboxAction(label: "设为背景", busyLabel: "正在下载并设置…", doneLabel: "已设为背景", systemImage: "photo") { index in
            let view = try await upload(index)
            await apply(view)
        }
    }
}
