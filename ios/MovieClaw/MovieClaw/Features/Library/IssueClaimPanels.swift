import SwiftUI

/*
 人工认领的共享面板（对应 Web components/claim-panels.tsx）。
 待处理抽屉「待识别」与「修正识别结果」共用同一套：

 - 认领一律走「详情确认面板」：点候选/搜索结果先看海报、简介、季数再确认——
   只看名字不足以在同名双版本（正片 46 集 vs 送审版 51 集）之间下判断；
 - 候选全不对时走 TMDB 搜索（按片名检索、支持直接粘 ID），不让用户手抄 TMDB ID。

 这里的类型需要跨文件共用，所以是 internal，统一加 Issue 前缀避免与其它模块重名。
 */

/// 进入确认面板的最小信息：来自候选 chip 或搜索结果，详情由确认面板异步补全。
struct IssueClaimSeed: Hashable {
    var tmdbId: Int
    var title: String
    var year: Int?
    var posterUrl: String?
    var episodeCount: Int?
    var reasons: [String] = []

    init(tmdbId: Int, title: String, year: Int? = nil, posterUrl: String? = nil, episodeCount: Int? = nil, reasons: [String] = []) {
        self.tmdbId = tmdbId
        self.title = title
        self.year = year
        self.posterUrl = posterUrl
        self.episodeCount = episodeCount
        self.reasons = reasons
    }

    init(candidate: API.UnidentifiedCandidateView) {
        self.init(
            tmdbId: candidate.tmdbId, title: candidate.title, year: candidate.year,
            episodeCount: candidate.episodeCount, reasons: candidate.reasons
        )
    }

    /// 组名/条目名 → 搜索词：去掉 [tmdbid=…] 等标记块与尾部年份括号（同 Web searchSeedFromLabel）。
    static func searchText(fromLabel label: String) -> String {
        label
            .replacingOccurrences(of: #"[\[{][^\]}]*[\]}]"#, with: " ", options: .regularExpression)
            .replacingOccurrences(of: #"\((?:18|19|20)\d{2}\)\s*$"#, with: "", options: .regularExpression)
            .replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
    }
}

/// 卡片内联面板：确认（看详情再定）或搜索；同时只开一个。
enum IssueClaimPanelState: Equatable {
    case confirm(IssueClaimSeed)
    case search

    var selectedTmdbId: Int? {
        if case let .confirm(seed) = self { return seed.tmdbId }
        return nil
    }
}

/// TMDB 页面地址（「在 TMDB 打开核对」「去 TMDB 补录」）
enum IssueTMDBLink {
    static func title(movie: Bool, id: Int) -> URL {
        URL(string: "https://www.themoviedb.org/\(movie ? "movie" : "tv")/\(id)")!
    }

    static func new(movie: Bool) -> URL {
        URL(string: "https://www.themoviedb.org/\(movie ? "movie" : "tv")/new")!
    }
}

// MARK: - 候选 chip

/// 机器给出的可能匹配：点一下进详情确认面板，不直接认领；再点同一个收起。
struct IssueCandidateChips: View {
    let candidates: [API.UnidentifiedCandidateView]
    let selectedId: Int?
    /// 待识别清单上显示「N 集」（同名双版本靠它一眼区分）；修正识别里不显示（同 Web）
    var showsEpisodeCount = true
    var disabled = false
    let onTap: (API.UnidentifiedCandidateView) -> Void

    var body: some View {
        IssueFlowLayout(spacing: 6) {
            ForEach(candidates, id: \.tmdbId) { candidate in
                let selected = selectedId == candidate.tmdbId
                Button {
                    onTap(candidate)
                } label: {
                    HStack(spacing: 6) {
                        Text(candidate.title + (candidate.year.map { " (\($0))" } ?? ""))
                            .foregroundStyle(selected ? Theme.text : Theme.text.opacity(0.9))
                            .lineLimit(1)
                        if showsEpisodeCount, let count = candidate.episodeCount, count > 0 {
                            Text("\(count) 集")
                                .font(.caption)
                                .foregroundStyle(Theme.textMuted)
                        }
                    }
                    .font(.subheadline)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    .background(Theme.accent.opacity(selected ? 0.28 : 0.12), in: .rect(cornerRadius: 8))
                    .overlay(
                        RoundedRectangle(cornerRadius: 8)
                            .strokeBorder(Theme.accent.opacity(selected ? 1 : 0.4))
                    )
                }
                .buttonStyle(.plain)
                .disabled(disabled)
                .opacity(disabled ? 0.4 : 1)
            }
        }
    }
}

/// 自动换行的横向排布（候选 chip 用）；单个超宽的 chip 压到整行宽度截断。
private struct IssueFlowLayout: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0, widest: CGFloat = 0
        for subview in subviews {
            let size = fitted(subview, maxWidth: maxWidth)
            if x > 0, x + size.width > maxWidth {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
            widest = max(widest, x - spacing)
        }
        return CGSize(width: min(widest, maxWidth), height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowHeight: CGFloat = 0
        for subview in subviews {
            let size = fitted(subview, maxWidth: bounds.width)
            if x > bounds.minX, x + size.width > bounds.maxX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            subview.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }

    private func fitted(_ subview: LayoutSubview, maxWidth: CGFloat) -> CGSize {
        let ideal = subview.sizeThatFits(.unspecified)
        guard ideal.width > maxWidth else { return ideal }
        return subview.sizeThatFits(ProposedViewSize(width: maxWidth, height: nil))
    }
}

// MARK: - 认领确认面板

/// 认领确认面板：海报 + 简介 + 季数 + 演职员，看清楚是谁再点认领。
/// 详情加载失败不挡认领（给出「在 TMDB 打开核对」兜底）。
struct IssueClaimConfirmPanel: View {
    let seed: IssueClaimSeed
    let movie: Bool
    let fileCount: Int
    let busy: Bool
    let onConfirm: () -> Void
    let onCancel: () -> Void

    @Environment(\.api) private var api
    @State private var detail: API.DiscoveredTitleDetailsView?
    @State private var failed = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 12) {
                RemoteImage(url: api.image(detail.map { $0.title.posterUrl }.flatMap { $0.isEmpty ? nil : $0 } ?? seed.posterUrl, .posterCard))
                    .frame(width: 70, height: 104)
                    .clipShape(.rect(cornerRadius: 8))
                VStack(alignment: .leading, spacing: 4) {
                    headline
                    if !seed.reasons.isEmpty {
                        Text("与本地文件的佐证：\(seed.reasons.joined(separator: "、"))")
                            .font(.caption)
                            .foregroundStyle(Theme.textFaint)
                    }
                    if let overview = detail?.title.overview, !overview.isEmpty {
                        Text(overview)
                            .font(.caption)
                            .foregroundStyle(Theme.textMuted)
                            .lineLimit(3)
                    } else if failed {
                        Text("详情加载失败（不影响认领，可打开 TMDB 页面核对）")
                            .font(.caption)
                            .foregroundStyle(Theme.textFaint)
                    } else if detail == nil {
                        Text("正在加载详情…")
                            .font(.caption)
                            .foregroundStyle(Theme.textFaint)
                    }
                    if !credits.isEmpty {
                        Text(credits)
                            .font(.caption)
                            .foregroundStyle(Theme.textFaint)
                            .lineLimit(1)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            Divider().overlay(Theme.line)

            HStack(spacing: 8) {
                Link("在 TMDB 打开核对 ↗", destination: IssueTMDBLink.title(movie: movie, id: seed.tmdbId))
                    .font(.caption)
                    .foregroundStyle(Theme.textMuted)
                Spacer(minLength: 0)
                if movie && fileCount > 1 {
                    Text("整组视为同一部片的多个版本")
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                        .multilineTextAlignment(.trailing)
                }
            }
            HStack(spacing: 8) {
                Spacer(minLength: 0)
                Button("取消", action: onCancel)
                    .buttonStyle(.glass)
                Button(fileCount > 1 ? "认领全部 \(fileCount) 个文件" : "认领", action: onConfirm)
                    .discoverProminentButton()
                    .fontWeight(.semibold)
            }
            .controlSize(.small)
            .disabled(busy)
        }
        .padding(12)
        .background(Theme.surfaceInset, in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.line))
        .task(id: seed.tmdbId) { await loadDetail() }
    }

    /// 片名 · 年份 · 规模 · 对应季集数 · 评分
    private var headline: some View {
        let item = detail?.title
        let year = item?.releaseYear ?? seed.year
        return VStack(alignment: .leading, spacing: 2) {
            Text(item?.title ?? seed.title)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(Theme.text)
            let meta = [
                year.map { "\($0)" },
                item.flatMap { $0.extentLabel.isEmpty ? nil : $0.extentLabel },
                seed.episodeCount.flatMap { $0 > 0 ? "对应季 \($0) 集" : nil },
            ].compactMap { $0 }
            if !meta.isEmpty || (item?.providerRating ?? 0) > 0 {
                HStack(spacing: 8) {
                    if !meta.isEmpty {
                        Text(meta.joined(separator: "  "))
                            .foregroundStyle(Theme.textMuted)
                    }
                    if let rating = item?.providerRating, rating > 0 {
                        Text("★ \(rating, specifier: "%.1f")")
                            .foregroundStyle(Theme.warning)
                    }
                }
                .font(.subheadline)
            }
        }
    }

    /// 「导演 a / b · 主演 c / d / e / f」
    private var credits: String {
        guard let metadata = detail?.metadata else { return "" }
        return [
            metadata.directors.isEmpty ? "" : "导演 \(metadata.directors.prefix(2).joined(separator: " / "))",
            metadata.cast.isEmpty ? "" : "主演 \(metadata.cast.prefix(4).map(\.name).joined(separator: " / "))",
        ]
        .filter { !$0.isEmpty }
        .joined(separator: " · ")
    }

    private func loadDetail() async {
        detail = nil
        failed = false
        let ref = "tmdb:\(movie ? "movie" : "tv"):\(seed.tmdbId)"
        do {
            let result = try await api.discoverGetTitleDetails(titleRef: ref)
            if !Task.isCancelled { detail = result }
        } catch {
            if !Task.isCancelled { failed = true }
        }
    }
}

// MARK: - TMDB 搜索面板

/// TMDB 搜索面板：候选全不对时按片名自己检索（比手抄 TMDB ID 体面）。
/// 输入预填从组名剥出的片名；直接粘一串数字则按 ID 进确认面板。
struct IssueClaimSearchPanel: View {
    let movie: Bool
    let onPick: (IssueClaimSeed) -> Void

    @Environment(\.api) private var api
    @State private var query: String
    @State private var results: [API.DiscoveredTitleView]?
    @State private var searching = false
    @State private var error: String?
    @FocusState private var focused: Bool

    init(movie: Bool, initialQuery: String, onPick: @escaping (IssueClaimSeed) -> Void) {
        self.movie = movie
        self.onPick = onPick
        _query = State(initialValue: initialQuery)
    }

    private var trimmed: String { query.trimmingCharacters(in: .whitespaces) }
    private var idOnly: Bool { trimmed.range(of: #"^\d{1,10}$"#, options: .regularExpression) != nil }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                TextField("片名关键词，或直接粘 TMDB ID", text: $query)
                    .textFieldStyle(.plain)
                    .font(.subheadline)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 7)
                    .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 8))
                    .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Theme.line))
                    .focused($focused)
                    .submitLabel(.search)
                    .autocorrectionDisabled()
                    .onSubmit { Task { await search() } }
                if idOnly {
                    Button("查看该 ID") {
                        if let id = Int(trimmed) { onPick(IssueClaimSeed(tmdbId: id, title: "TMDB #\(trimmed)")) }
                    }
                    .discoverProminentButton()
                    .fontWeight(.semibold)
                } else {
                    Button(searching ? "搜索中…" : "搜索") { Task { await search() } }
                        .buttonStyle(.glass)
                        .disabled(searching || trimmed.isEmpty)
                }
            }
            .controlSize(.small)

            if let error {
                Text(error).font(.caption).foregroundStyle(Theme.danger)
            }
            if let results, results.isEmpty, error == nil {
                emptyHint
            }
            if let results, !results.isEmpty {
                VStack(spacing: 2) {
                    ForEach(results, id: \.titleRef) { item in
                        resultRow(item)
                    }
                }
            }
        }
        .padding(12)
        .background(Theme.surfaceInset, in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.line))
        .onAppear { focused = true }
    }

    private var emptyHint: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("没有找到\(movie ? "电影" : "剧集")结果，换个关键词试试（TMDB 对中文名支持有限时可试英文/原名）。确认 TMDB 没收录的话，可以")
            Link("去 TMDB 补录该条目 ↗", destination: IssueTMDBLink.new(movie: movie))
                .underline()
            Text("——它是维基式社区库，收录后回来搜索或粘 ID 即可认领")
        }
        .font(.caption)
        .foregroundStyle(Theme.textMuted)
    }

    private func resultRow(_ item: API.DiscoveredTitleView) -> some View {
        Button {
            guard let id = Int(item.externalId) else { return }
            onPick(IssueClaimSeed(
                tmdbId: id, title: item.title, year: item.releaseYear,
                posterUrl: item.posterUrl.isEmpty ? nil : item.posterUrl
            ))
        } label: {
            HStack(spacing: 10) {
                RemoteImage(url: api.image(item.posterUrl, .posterCard))
                    .frame(width: 32, height: 48)
                    .clipShape(.rect(cornerRadius: 4))
                HStack(spacing: 6) {
                    Text(item.title)
                        .font(.subheadline)
                        .foregroundStyle(Theme.text.opacity(0.9))
                        .lineLimit(1)
                    if let year = item.releaseYear {
                        Text("\(year)").font(.caption).foregroundStyle(Theme.textMuted)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                if item.providerRating > 0 {
                    Text("★ \(item.providerRating, specifier: "%.1f")")
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
    }

    private func search() async {
        let q = trimmed
        guard !q.isEmpty, !searching, !idOnly else { return }
        searching = true
        error = nil
        defer { searching = false }
        do {
            let view = try await api.searchTitles(body: .init(query: q, provider: "tmdb", saveHistory: false))
            // multi 搜索混排两种类型，认领只关心本库的类型
            let kind = movie ? "movie" : "tv"
            results = view.titles.filter { $0.mediaType == kind }
        } catch {
            results = nil
            if !(error is CancellationError) { self.error = error.localizedDescription }
        }
    }
}
