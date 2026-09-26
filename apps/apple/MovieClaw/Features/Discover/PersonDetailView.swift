import SwiftUI

/// 库内影人页（对应 Web `person-detail-view.tsx`）：`GET /people/{tmdbPersonId}`，
/// 列出他在**我的库里**参演与执导的作品，点作品进库内条目详情。
/// 404 = 库内没有这位影人的作品（给出「刷新元数据」的解释与去媒体库入口）。
struct PersonDetailView: View {
    let tmdbId: Int

    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @State private var person: API.PersonView?
    @State private var failure: Failure?

    enum Failure { case missing, error }

    var body: some View {
        Group {
            if let failure {
                fallback(failure)
            } else if let person {
                content(person)
            } else {
                VStack(spacing: 12) {
                    ProgressView().controlSize(.large)
                    Text("正在读取影人档案…").font(.subheadline).foregroundStyle(Theme.textMuted)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .appBackground()
        .navigationTitle(person?.name ?? "")
        .navigationBarTitleDisplayMode(.inline)
        .task { if person == nil { await load() } }
    }

    private func load() async {
        failure = nil
        do {
            person = try await api.peopleShow(tmdbPersonId: tmdbId)
        } catch is CancellationError {
        } catch {
            failure = error.isDiscoverNotFound ? .missing : .error
        }
    }

    private func content(_ person: API.PersonView) -> some View {
        let cast = person.credits.filter { $0.department == "cast" }
        let directed = person.credits.filter { $0.department == "director" }
        return ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                PersonHeader(
                    name: person.name,
                    avatarUrl: person.avatarUrl,
                    eyebrow: "影人",
                    subtitle: person.originalName.flatMap { $0 != person.name ? $0 : nil },
                    summary: "库内 \(person.credits.count) 部" + (!cast.isEmpty && !directed.isEmpty ? "（参演 \(cast.count) · 执导 \(directed.count)）" : "")
                )
                creditGrid("参演", cast, showCharacter: true)
                creditGrid("执导", directed, showCharacter: false)
            }
            .padding(.horizontal, Theme.pagePadding)
            .padding(.bottom, 40)
        }
        .refreshable { await load() }
    }

    @ViewBuilder
    private func creditGrid(_ title: String, _ credits: [API.PersonCreditView], showCharacter: Bool) -> some View {
        if !credits.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(title).font(.title3.weight(.semibold)).foregroundStyle(Theme.text)
                    Text("\(credits.count)").font(.subheadline).foregroundStyle(Theme.textFaint)
                }
                LazyVGrid(columns: DiscoverGrid.columns, spacing: 16) {
                    ForEach(credits, id: \.self) { credit in
                        creditCard(credit, showCharacter: showCharacter)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func creditCard(_ credit: API.PersonCreditView, showCharacter: Bool) -> some View {
        let body = VStack(alignment: .leading, spacing: 2) {
            Color.clear
                .aspectRatio(2 / 3, contentMode: .fit)
                .overlay { RemoteImage(url: api.image(credit.posterUrl, .posterCard)) }
                .clipShape(.rect(cornerRadius: Theme.posterRadius))
                .padding(.bottom, 4)
            Text(credit.title).font(.subheadline.weight(.medium)).foregroundStyle(Theme.text).lineLimit(1)
            Text(showCharacter && credit.character != nil ? "饰 \(credit.character!)" : credit.year.map(String.init) ?? " ")
                .font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
        }
        if let libraryId = credit.libraryId {
            Button { router.push(.libraryItem(libraryId: libraryId, itemId: credit.mediaItemId)) } label: { body.contentShape(.rect) }
                .buttonStyle(.plain)
                .accessibilityLabel("查看《\(credit.title)》")
        } else {
            // 文件已全部删除、只剩档案：不可点
            body
        }
    }

    @ViewBuilder
    private func fallback(_ failure: Failure) -> some View {
        switch failure {
        case .missing:
            ContentUnavailableView {
                Label("库内没有这位影人的作品", systemImage: "person.crop.rectangle")
            } description: {
                Text("可能是他参演的片都已从库里移除；也可能这个库是早前扫描的——影人档案随入库刮削一并建立，对它执行一次「刷新元数据」即可补齐，之后这里就会列出他在库内的全部作品。")
            } actions: {
                Button("去媒体库") { router.open(.libraryHome) }.buttonStyle(.glass)
            }
        case .error:
            ContentUnavailableView {
                Label("未能加载影人档案", systemImage: "exclamationmark.triangle")
            } description: {
                Text("请稍后重试；若持续失败，请查看系统日志。")
            } actions: {
                Button("重试") { Task { await load() } }.buttonStyle(.glass)
            }
        }
    }
}

/// 影人页头部：头像（缺图显示首字）+ 眉题 + 姓名 + 原名 + 作品统计（两种影人页共用）
struct PersonHeader: View {
    let name: String
    let avatarUrl: String?
    let eyebrow: String
    var subtitle: String?
    let summary: String
    @Environment(\.api) private var api

    var body: some View {
        HStack(alignment: .bottom, spacing: 16) {
            ZStack {
                Color.white.opacity(0.06)
                Text(String(name.trimmingCharacters(in: .whitespaces).prefix(1)))
                    .font(.system(size: 34, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.3))
                if avatarUrl != nil {
                    RemoteImage(url: api.image(avatarUrl), placeholderSymbol: "person.fill")
                }
            }
            .frame(width: 92, height: 138)
            .clipShape(.rect(cornerRadius: 12))
            .shadow(color: .black.opacity(0.5), radius: 20, y: 12)
            VStack(alignment: .leading, spacing: 4) {
                Text(eyebrow)
                    .font(.caption.weight(.semibold))
                    .tracking(2.5)
                    .foregroundStyle(Theme.accent2)
                Text(name)
                    .font(.title2.bold())
                    .foregroundStyle(.white)
                if let subtitle {
                    Text(subtitle).font(.subheadline).foregroundStyle(.white.opacity(0.55)).lineLimit(1)
                }
                Text(summary)
                    .font(.subheadline)
                    .monospacedDigit()
                    .foregroundStyle(.white.opacity(0.7))
                    .padding(.top, 4)
            }
            .padding(.bottom, 4)
        }
        .padding(.top, 8)
    }
}
