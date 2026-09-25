import SwiftUI

/// 分享页的影片页（Web `components/share/shared-item-view.tsx`）：详情页的浏览面，去掉一切管理与站内入口。
///
/// 剧照铺底（固定在背后）→ 顶栏（「返回合集」+ 字标 + 链接到期提示）→ 标题、季集行、规格事实、类型、
/// 文件规格行、音轨 / 字幕（只读）→ 播放 → 简介 → （剧集）季集横滚 → 章节条 → 演职员 → 外部词条。
/// 播放走 `router.play(PlayRequest(… shareSlug:))`，播放器用 `/share/{slug}/playback` 接口族，进度只存本机。
struct ShareItemView: View {
    let slug: String
    /// 合集分享时看哪一部；条目分享不传（范围就那一个）
    let mediaItemId: Int?
    let onBack: (() -> Void)?

    /// 选中的一集及其文件（剧集才有）
    struct Selected: Equatable {
        var season: Int
        var episode: API.EpisodeView
        var files: [API.SharedFileView]
    }

    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @State private var item: API.SharedItemView?
    @State private var failed: String?
    @State private var selected: Selected?

    var body: some View {
        Group {
            if let failed {
                Text(failed)
                    .font(.body).foregroundStyle(.white.opacity(0.8))
                    .multilineTextAlignment(.center)
                    .padding(24)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let item {
                page(item)
            } else {
                HStack(spacing: 10) {
                    ProgressView()
                    Text("正在读取影片信息…").font(.subheadline).foregroundStyle(.white.opacity(0.6))
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .navigationTitle(item?.title ?? "")
        .task(id: mediaItemId) { await load() }
    }

    private var isMovie: Bool { item.map { $0.kind != "tv" } ?? true }

    // MARK: 页面

    private func page(_ item: API.SharedItemView) -> some View {
        let trackFiles = isMovie ? item.files : (selected?.files ?? [])
        let available = trackFiles.filter { $0.state == "in_place" }
        let current = available.first
        let canPlay = !available.isEmpty && (isMovie || selected != nil)
        let plot = isMovie ? item.localMeta?.plot : (selected?.episode.overview ?? item.localMeta?.plot)

        return ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                header(item)
                Color.clear.frame(height: 150)
                VStack(alignment: .leading, spacing: 10) {
                    Text(item.title)
                        .font(.system(size: 28, weight: .bold))
                        .foregroundStyle(.white)
                        .shadow(color: .black.opacity(0.5), radius: 8)
                        .accessibilityIdentifier("share-item-title")
                    if !isMovie, let selected {
                        Text("第 \(selected.season) 季 第 \(selected.episode.episodeNumber) 集" + (selected.episode.name.map { " - \($0)" } ?? ""))
                            .font(.subheadline).foregroundStyle(.white.opacity(0.65))
                    }
                    let facts = facts(item, available: available)
                    if !facts.isEmpty {
                        Text(facts.joined(separator: " · ")).font(.subheadline.monospacedDigit()).foregroundStyle(.white.opacity(0.8))
                    }
                    if let genres = item.localMeta?.genres, !genres.isEmpty {
                        Text(genres.joined(separator: " · ")).font(.subheadline).foregroundStyle(.white.opacity(0.72))
                    }
                    if let current {
                        Text([
                            current.container?.uppercased(),
                            current.videoCodec?.uppercased(),
                            libraryBytes(current.sizeBytes),
                            available.count > 1 ? "共 \(available.count) 个版本" : nil,
                        ].compactMap { $0 }.joined(separator: " · "))
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.white.opacity(0.5))
                        ShareTrackRows(audio: current.audioStreams, subtitles: current.subtitleStreams)
                            .padding(.top, 4)
                    }
                    if canPlay {
                        playButton(item)
                            .padding(.top, 8)
                    } else {
                        Text(isMovie || selected != nil ? "暂时没有可播放的文件。" : "请选择一集。")
                            .font(.subheadline).foregroundStyle(.white.opacity(0.55))
                            .padding(.top, 8)
                    }
                    if let plot, !plot.isEmpty {
                        ExpandablePlot(text: plot).padding(.top, 8)
                    }
                }
                .padding(.horizontal, Theme.pagePadding)

                VStack(alignment: .leading, spacing: 24) {
                    if !isMovie, !item.seasons.isEmpty {
                        ShareSeasonEpisodes(slug: slug, item: item, queryItem: mediaItemId) { selected = $0 }
                    }
                    if let chapters = current?.chapters, !chapters.isEmpty {
                        ChapterStripSection(
                            chapters: chapters,
                            onPlayFrom: { play(item, start: $0) },
                            resumeMs: resume(item).map(\.positionMs)
                        )
                    }
                    let people = cast(item)
                    if !people.isEmpty {
                        DetailCastRow(people: people)
                    }
                    externalLinks(item)
                }
                .padding(.top, 28)
                .padding(.bottom, 80)
            }
        }
        .background {
            // 剧照铺底：固定在视口，内容从下方的渐变板上浮出
            ZStack {
                RemoteImage(url: api.image(item.backdropUrl ?? item.posterUrl))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .clipped()
                LinearGradient(stops: [
                    .init(color: Color(red: 7 / 255, green: 8 / 255, blue: 12 / 255).opacity(0.15), location: 0),
                    .init(color: Color(red: 7 / 255, green: 8 / 255, blue: 12 / 255).opacity(0.75), location: 0.45),
                    .init(color: Color(red: 7 / 255, green: 8 / 255, blue: 12 / 255), location: 0.75),
                ], startPoint: .top, endPoint: .bottom)
            }
            .ignoresSafeArea()
        }
    }

    /// 顶栏只有字标与到期提示：没有登录入口、没有搜索；合集分享才有「返回合集」
    private func header(_ item: API.SharedItemView) -> some View {
        HStack(spacing: 10) {
            if let onBack {
                Button(action: onBack) {
                    Label("返回合集", systemImage: "chevron.left").font(.subheadline)
                }
                .buttonStyle(.glass)
                .accessibilityIdentifier("share-back-collection")
            }
            Text("MOVIECLAW").font(.footnote.weight(.semibold)).tracking(3).foregroundStyle(.white.opacity(0.7))
            Spacer()
            Text("链接 \(ShareLinkKit.expiryHint(item.expiresAt))")
                .font(.caption.monospacedDigit())
                .foregroundStyle(.white.opacity(0.55))
                .accessibilityIdentifier("share-expiry")
        }
        .padding(.horizontal, Theme.pagePadding)
        .padding(.top, 8)
    }

    /// 年份 · 片长 · 分辨率 · HDR。片长 NFO 优先，其次任意文件的实测时长；不足一分钟的短片不显示
    private func facts(_ item: API.SharedItemView, available: [API.SharedFileView]) -> [String] {
        var runtime = item.localMeta?.runtimeMinutes
        if runtime == nil || runtime == 0, let probed = item.files.compactMap(\.durationSeconds).first, probed >= 60 {
            runtime = Int((Double(probed) / 60).rounded())
        }
        let resolutions = Array(Set(available.compactMap(\.resolution)))
            .sorted { $0.localizedStandardCompare($1) == .orderedDescending }
            .map(Self.resolutionLabel)
        var hdr: [String] = []
        for value in available.compactMap(\.hdr) where !hdr.contains(value) { hdr.append(value) }
        return [
            item.year.map(String.init),
            runtime.flatMap { $0 > 0 ? Self.runtimeLabel($0) : nil },
            resolutions.isEmpty ? nil : resolutions.joined(separator: " / "),
            hdr.isEmpty ? nil : hdr.joined(separator: " / "),
        ].compactMap { $0 }
    }

    /// 同 Web `formatVideoResolution`
    static func resolutionLabel(_ resolution: String) -> String {
        let normalized = resolution.trimmingCharacters(in: .whitespaces).lowercased()
        let key = !normalized.isEmpty && normalized.allSatisfy(\.isNumber) ? "\(normalized)p" : normalized
        let labels = ["4320p": "8K", "2160p": "4K", "1440p": "2K", "1080p": "1080p", "720p": "720p", "4k": "4K", "2k": "2K"]
        return labels[key] ?? resolution
    }

    /// 同 Web `formatRuntimeMinutes`：「52 分钟」/「2 小时 8 分钟」
    static func runtimeLabel(_ minutes: Int) -> String {
        if minutes < 60 { return "\(minutes) 分钟" }
        let rest = minutes % 60
        return rest > 0 ? "\(minutes / 60) 小时 \(rest) 分钟" : "\(minutes / 60) 小时"
    }

    /// 演职员：导演在前（有头像的导演条目优先），演员随后；访客页不给影人跳转
    private func cast(_ item: API.SharedItemView) -> [DetailCastRow.Person] {
        guard let meta = item.localMeta else { return [] }
        var people: [DetailCastRow.Person] = []
        if !meta.directorCredits.isEmpty {
            people += meta.directorCredits.map { .init(name: $0.name, subtitle: "导演", avatarUrl: $0.thumbUrl) }
        } else {
            var seen: [String] = []
            for name in meta.directors where !seen.contains(name) { seen.append(name) }
            people += seen.map { .init(name: $0, subtitle: "导演") }
        }
        people += meta.actors.map { .init(name: $0.name, subtitle: $0.role, avatarUrl: $0.thumbUrl) }
        return people
    }

    @ViewBuilder
    private func externalLinks(_ item: API.SharedItemView) -> some View {
        if item.tmdbId != nil || item.imdbId != nil || item.doubanId != nil {
            HStack(spacing: 16) {
                Text("相关链接").foregroundStyle(Theme.textFaint)
                if let tmdb = item.tmdbId, let url = URL(string: "https://www.themoviedb.org/\(item.kind == "tv" ? "tv" : "movie")/\(tmdb)") {
                    Link("TMDB", destination: url)
                }
                if let imdb = item.imdbId, let url = URL(string: "https://www.imdb.com/title/\(imdb)/") {
                    Link("IMDb", destination: url)
                }
                if let douban = item.doubanId, let url = URL(string: "https://movie.douban.com/subject/\(douban)/") {
                    Link("豆瓣", destination: url)
                }
            }
            .font(.caption)
            .tint(.white.opacity(0.7))
            .padding(.top, 12)
            .overlay(alignment: .top) { Rectangle().fill(Color.white.opacity(0.04)).frame(height: 1) }
            .padding(.horizontal, Theme.pagePadding)
            .accessibilityIdentifier("share-external-links")
        }
    }

    // MARK: 播放

    private func unit(_ item: API.SharedItemView) -> PlaybackUnit? {
        if isMovie { return PlaybackUnit(mediaItemId: item.mediaItemId, season: 0, episode: 0) }
        guard let selected else { return nil }
        return PlaybackUnit(mediaItemId: item.mediaItemId, season: selected.season, episode: selected.episode.episodeNumber)
    }

    /// 续播点只在本机（访客播放不落服务端观看状态）
    private func resume(_ item: API.SharedItemView) -> ShareLocalProgress.Record? {
        guard let unit = unit(item), let record = ShareLocalProgress.read(slug, unit), record.positionMs > 0 else { return nil }
        return record
    }

    private func playButton(_ item: API.SharedItemView) -> some View {
        let resumable = resume(item) != nil
        return Button {
            play(item, start: nil)
        } label: {
            Label(resumable ? "继续观看" : "播放", systemImage: "play.fill")
                .font(.body.weight(.semibold))
                .frame(maxWidth: .infinity)
                .frame(height: 30)
        }
        .discoverProminentButton()
        .accessibilityIdentifier("share-play")
    }

    private func play(_ item: API.SharedItemView, start: Double?) {
        router.play(PlayRequest(
            mediaItemId: item.mediaItemId,
            season: isMovie ? nil : selected?.season,
            episode: isMovie ? nil : selected?.episode.episodeNumber,
            startSeconds: start,
            shareSlug: slug
        ))
    }

    private func load() async {
        do {
            item = try await api.shareGuestItem(slug: slug, item: mediaItemId)
            failed = nil
        } catch is CancellationError {
        } catch let error as APIError where error.status != nil {
            failed = error.localizedDescription.isEmpty ? "读取影片信息失败" : error.localizedDescription
        } catch {
            failed = "无法连接到服务器，请检查网络后重试"
        }
    }
}

// MARK: - 季集

/// 分享页的分集区：季选择 + 分集横滚卡（`GET /share/{slug}/episodes`）；默认落在第一个在库的季与集
private struct ShareSeasonEpisodes: View {
    let slug: String
    let item: API.SharedItemView
    /// 合集分享才带（`?item=`）
    let queryItem: Int?
    let onChange: (ShareItemView.Selected?) -> Void

    @Environment(\.api) private var api
    @State private var season: Int?
    @State private var data: API.SeasonEpisodesView?
    @State private var failed = false
    @State private var selected: Int?

    private var ownedSeasons: Set<Int> { Set(item.files.map(\.seasonNumber)) }
    private var currentSeason: Int { season ?? item.seasons.first { ownedSeasons.contains($0) } ?? item.seasons.first ?? 1 }

    private func label(_ s: Int) -> String {
        let name = s == 0 ? "特别篇" : "第 \(s) 季"
        return ownedSeasons.contains(s) ? name : "\(name) · 未入库"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                Text("分集").font(.title3.weight(.semibold)).foregroundStyle(Theme.text)
                if item.seasons.count > 1 {
                    Menu {
                        Picker("季", selection: Binding(get: { currentSeason }, set: { season = $0 })) {
                            ForEach(item.seasons, id: \.self) { Text(label($0)).tag($0) }
                        }
                    } label: {
                        HStack(spacing: 4) {
                            Text(label(currentSeason))
                            Image(systemName: "chevron.up.chevron.down").font(.caption2)
                        }
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Theme.text)
                        .padding(.horizontal, 12).padding(.vertical, 6)
                        .glassEffect(.regular.interactive(), in: .capsule)
                    }
                    .accessibilityIdentifier("share-season-picker")
                } else {
                    Text(label(currentSeason)).font(.subheadline).foregroundStyle(Theme.textMuted)
                }
                if let data {
                    Text("在库 \(data.episodes.filter(\.owned).count) / \(data.episodes.count) 集")
                        .font(.subheadline.monospacedDigit()).foregroundStyle(Theme.textFaint)
                }
            }
            .padding(.horizontal, Theme.pagePadding)
            if failed {
                Text("分集信息加载失败，请稍后重试。").font(.subheadline).foregroundStyle(Theme.textMuted)
                    .padding(.horizontal, Theme.pagePadding)
            } else if let data {
                ScrollView(.horizontal, showsIndicators: false) {
                    LazyHStack(alignment: .top, spacing: 12) {
                        ForEach(data.episodes, id: \.episodeNumber) { episode in
                            card(episode)
                        }
                    }
                    .padding(.horizontal, Theme.pagePadding)
                }
            } else {
                HStack(spacing: 8) {
                    ProgressView()
                    Text("正在读取分集信息…")
                }
                .font(.subheadline).foregroundStyle(Theme.textMuted)
                .padding(.horizontal, Theme.pagePadding)
            }
        }
        .task(id: currentSeason) { await load() }
        .accessibilityIdentifier("share-season-episodes")
    }

    private func card(_ episode: API.EpisodeView) -> some View {
        let isSelected = episode.episodeNumber == selected
        return Button {
            selected = episode.episodeNumber
            report()
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                LibraryArtwork(url: api.image(episode.stillUrl, .landscapeCard), frameAspect: 16 / 9,
                               fallbackText: episode.stillUrl == nil ? "\(episode.episodeNumber)" : nil)
                    .clipShape(.rect(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(isSelected ? .white.opacity(0.85) : .white.opacity(0.08), lineWidth: isSelected ? 2 : 1))
                    .overlay(alignment: .topTrailing) {
                        if !episode.owned {
                            Text("缺").font(.caption2.weight(.semibold)).foregroundStyle(Theme.warning)
                                .padding(.horizontal, 5).padding(.vertical, 1)
                                .background(.black.opacity(0.6), in: .rect(cornerRadius: 4))
                                .padding(6)
                        }
                    }
                Text("\(episode.episodeNumber). \(episode.name ?? "第 \(episode.episodeNumber) 集")")
                    .font(.footnote.weight(isSelected ? .semibold : .regular))
                    .foregroundStyle(isSelected ? .white : Theme.text)
                    .lineLimit(1)
            }
            .frame(width: 200)
            .opacity(episode.owned ? 1 : 0.45)
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
        .accessibilityIdentifier("share-episode-\(episode.episodeNumber)")
    }

    private func load() async {
        let s = currentSeason
        data = nil
        failed = false
        do {
            let result = try await api.shareGuestEpisodes(slug: slug, seasonNumber: s, item: queryItem)
            guard s == currentSeason else { return }
            data = result
            selected = (result.episodes.first(where: \.owned) ?? result.episodes.first)?.episodeNumber
            report()
        } catch is CancellationError {
        } catch {
            failed = true
        }
    }

    private func report() {
        guard let data, let episode = data.episodes.first(where: { $0.episodeNumber == selected }) else {
            onChange(nil)
            return
        }
        let files = item.files.filter { $0.seasonNumber == data.seasonNumber && $0.episodeNumber == episode.episodeNumber }
        onChange(.init(season: data.seasonNumber, episode: episode, files: files))
    }
}

// MARK: - 音轨 / 字幕（只读）

/// 只读的音轨 / 字幕两行（Web `ReadOnlyTrackRows`）：按语言去重成芯片（「简体中文 ×2」），
/// 点芯片弹出该行的完整列表。访客页没有预览、删除、AI 生成
private struct ShareTrackRows: View {
    let audio: [API.AudioStreamView]?
    let subtitles: [API.SubtitleStreamView]

    @State private var showing: String?

    private static let languages: [String: String] = [
        "chs": "简体中文", "cht": "繁体中文", "chi": "中文", "zho": "中文", "cmn": "中文", "yue": "粤语",
        "eng": "英语", "jpn": "日语", "kor": "韩语", "fre": "法语", "fra": "法语", "ger": "德语", "deu": "德语",
        "spa": "西班牙语", "rus": "俄语", "ita": "意大利语", "por": "葡萄牙语", "tha": "泰语", "hin": "印地语",
    ]

    private static func language(_ code: String?, title: String? = nil) -> String {
        if let code, code != "und" { return languages[code.lowercased()] ?? code }
        if let title, !title.isEmpty { return title }
        return "未标语言"
    }

    private static func groups(_ names: [String]) -> [(name: String, count: Int)] {
        var result: [(name: String, count: Int)] = []
        for name in names {
            if let i = result.firstIndex(where: { $0.name == name }) { result[i].count += 1 } else { result.append((name, 1)) }
        }
        return result
    }

    private var audioLines: [String] {
        (audio ?? []).map { s in
            [Self.language(s.language), s.profile ?? s.codec?.uppercased(), s.channelLayout ?? s.channels.map { "\($0) 声道" }]
                .compactMap { $0 }.joined(separator: " · ") + (s.default ? " · 默认" : "")
        }
    }

    private var subtitleLines: [String] {
        subtitles.map { s in
            [Self.language(s.language, title: s.title), s.codec?.uppercased(), s.external ? "外挂" : "内封", s.forced ? "强制" : nil]
                .compactMap { $0 }.joined(separator: " · ") + (s.default ? " · 默认" : "")
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            row("音轨", groups: Self.groups((audio ?? []).map { Self.language($0.language) }),
                empty: audio == nil ? "尚未探测" : "文件内没有音轨",
                token: { name in
                    // 只有一条的语言，芯片上直接写格式（同 Web）
                    let hits = (audio ?? []).filter { Self.language($0.language) == name }
                    guard hits.count == 1, let codec = hits[0].codec?.lowercased() else { return nil }
                    return Self.audioTokens[codec] ?? codec.uppercased()
                })
            if let spec = topAudioSpec {
                Text(spec)
                    .font(.caption.weight(.semibold).monospacedDigit())
                    .foregroundStyle(.white.opacity(0.8))
                    .padding(.horizontal, 10)
                    .frame(height: 30)
                    .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(Color.white.opacity(0.14)))
                    .padding(.leading, 38)
            }
            row("字幕", groups: Self.groups(subtitles.map { Self.language($0.language, title: $0.title) }),
                empty: "无内封或外挂字幕")
        }
        .sheet(item: Binding(get: { showing.map(ShareTrackSheetTarget.init) }, set: { showing = $0?.kind })) { target in
            NavigationStack {
                List(target.kind == "音轨" ? audioLines : subtitleLines, id: \.self) { Text($0).font(.subheadline) }
                    .scrollContentBackground(.hidden)
                    .navigationTitle(target.kind)
                    .navigationBarTitleDisplayMode(.inline)
            }
            .presentationDetents([.medium, .large])
            .presentationBackground(.regularMaterial)
        }
    }

    private static let audioTokens = [
        "aac": "AAC", "ac3": "AC3", "eac3": "EAC3", "truehd": "TrueHD", "dts": "DTS",
        "flac": "FLAC", "opus": "Opus", "mp3": "MP3", "vorbis": "Vorbis",
    ]
    private static let audioLabels = [
        "aac": "AAC", "ac3": "Dolby Digital", "eac3": "Dolby Digital+", "truehd": "Dolby TrueHD",
        "dts": "DTS", "flac": "FLAC", "opus": "Opus", "mp3": "MP3", "vorbis": "Vorbis",
    ]
    private static let audioTier = ["truehd": 5, "dts": 4, "eac3": 3, "flac": 3, "ac3": 2, "opus": 1, "aac": 1]

    /// 多条音轨时行尾常显最高规格（同 Web `topAudioSpec`：先比编码档次再比声道数）
    private var topAudioSpec: String? {
        let streams = audio ?? []
        guard streams.count >= 2,
              let best = streams.max(by: {
                  (Self.audioTier[$0.codec?.lowercased() ?? ""] ?? 0) * 100 + ($0.channels ?? 0)
                      < (Self.audioTier[$1.codec?.lowercased() ?? ""] ?? 0) * 100 + ($1.channels ?? 0)
              }) else { return nil }
        let generic: Set<String> = ["lc", "main", "high", "baseline", "main 10"]
        let profile = best.profile.flatMap { generic.contains($0.lowercased()) || $0.isEmpty ? nil : $0 }
        let codec = profile ?? best.codec.map { Self.audioLabels[$0.lowercased()] ?? $0.uppercased() }
        var channels: String?
        if let layout = best.channelLayout?.split(separator: "(").first?.trimmingCharacters(in: .whitespaces),
           let first = layout.first, first.isNumber {
            channels = layout
        } else if let n = best.channels {
            channels = [1: "单声道", 2: "2.0", 6: "5.1", 7: "6.1", 8: "7.1"][n] ?? "\(n) 声道"
        }
        let text = [codec, channels].compactMap { $0 }.joined(separator: " ")
        return text.isEmpty ? nil : text
    }

    private func row(_ label: String, groups: [(name: String, count: Int)], empty: String, token: @escaping (String) -> String? = { _ in nil }) -> some View {
        HStack(spacing: 8) {
            Text(label).font(.caption.weight(.semibold)).foregroundStyle(.white.opacity(0.5)).frame(width: 30, alignment: .leading)
            if groups.isEmpty {
                Text(empty).font(.caption).foregroundStyle(.white.opacity(0.45))
            } else {
                // 手机上最多 2 枚芯片，其余折成「+N 种」
                ForEach(groups.prefix(2), id: \.name) { group in
                    chip(group.count > 1 ? "\(group.name) ×\(group.count)" : [group.name, token(group.name)].compactMap { $0 }.joined(separator: " "), label)
                }
                if groups.count > 2 { chip("+\(groups.count - 2) 种", label) }
            }
            Spacer(minLength: 0)
        }
    }

    private func chip(_ text: String, _ kind: String) -> some View {
        Button {
            showing = kind
        } label: {
            Text(text)
                .font(.caption.weight(.medium).monospacedDigit())
                .foregroundStyle(.white.opacity(0.85))
                .padding(.horizontal, 10)
                .frame(height: 30)
                .background(Color.white.opacity(0.075), in: .rect(cornerRadius: 7))
        }
        .buttonStyle(.plain)
    }
}

private struct ShareTrackSheetTarget: Identifiable {
    let kind: String
    var id: String { kind }
}
