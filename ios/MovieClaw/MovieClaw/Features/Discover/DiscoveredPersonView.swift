import SwiftUI

/// TMDB 影人页（对应 Web `discovered-person-detail-view.tsx`）：`GET /discover/people/{id}`，
/// 参演与幕后合并去重的完整 TMDB 影视履历。海报自带「已入库」绿斜标，未入库但已订阅的打「已订阅」蓝斜标；
/// 点海报进发现详情。
struct DiscoveredPersonView: View {
    let tmdbId: Int

    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @State private var person: API.DiscoveredPersonDetailsView?
    @State private var failure: PersonDetailView.Failure?

    var body: some View {
        Group {
            if let failure {
                ContentUnavailableView {
                    Label(failure == .missing ? "TMDB 中没有这位影人" : "未能加载影人作品", systemImage: "person.crop.rectangle")
                } description: {
                    Text(failure == .missing ? "这条影人记录可能已被 TMDB 合并或移除。" : "请稍后重试；若持续失败，请检查 TMDB 网络连接。")
                } actions: {
                    if failure == .error {
                        Button("重试") { Task { await load() } }.discoverProminentButton()
                    }
                    Button("返回发现页") { router.open(.discover()) }.buttonStyle(.glass)
                }
                .accessibilityIdentifier("error-state")
            } else if let person {
                ScrollView {
                    VStack(alignment: .leading, spacing: 24) {
                        PersonHeader(
                            name: person.name,
                            avatarUrl: person.avatarUrl,
                            eyebrow: "TMDB 影人",
                            summary: "共 \(person.titles.count) 部影视作品"
                        )
                        if person.titles.isEmpty {
                            Text("TMDB 暂未收录这位影人的影视作品")
                                .foregroundStyle(Theme.textMuted)
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 32)
                                .cardStyle(radius: 12)
                        } else {
                            VStack(alignment: .leading, spacing: 12) {
                                HStack(alignment: .firstTextBaseline, spacing: 8) {
                                    Text("全部作品").font(.title3.weight(.semibold)).foregroundStyle(Theme.text)
                                    Text("\(person.titles.count)").font(.subheadline).foregroundStyle(Theme.textFaint)
                                }
                                LazyVGrid(columns: DiscoverGrid.columns, spacing: 20) {
                                    ForEach(person.titles.map(DiscoverPosterItem.init)) { item in
                                        DiscoverPosterCard(item: item, action: .none)
                                    }
                                }
                            }
                        }
                    }
                    .padding(.horizontal, Theme.pagePadding)
                    .padding(.bottom, 40)
                }
                .refreshable { await load() }
                .accessibilityIdentifier("discovered-person")
            } else {
                VStack(spacing: 12) {
                    ProgressView().controlSize(.large)
                    Text("正在读取 TMDB 影人作品…").font(.subheadline).foregroundStyle(Theme.textMuted)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .appBackground()
        .navigationTitle(person?.name ?? "")
        .navigationBarTitleDisplayMode(.inline)
        .tracksSubscriptionIndex()
        .task { if person == nil { await load() } }
    }

    private func load() async {
        failure = nil
        do {
            person = try await api.discoverGetPersonDetails(tmdbPersonId: tmdbId)
        } catch is CancellationError {
        } catch {
            failure = error.isDiscoverNotFound ? .missing : .error
        }
    }
}
