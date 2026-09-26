import SwiftUI

/// 订阅海报墙：订阅首页「剧集订阅 ›」「电影订阅 ›」压栈进来的二级页（`AppRoute.subscriptionWall`）。
///
/// 改版前的订阅首页本身就是这面墙；首页改成流媒体式横滑行后，完整清单降级到这一层。
/// 墙上的海报保留全部信息（状态斜标、洗版徽标、收录摘要、「规则组 → 媒体库」流向），
/// 顺序与首页那一排相同：此刻最要紧的在前，已收齐 / 已暂停的排在最后。
/// 数据同样直接消费全站订阅索引 `SubscriptionIndex.shared`，规则组名（管理员）与媒体库名补齐流向。
struct SubscriptionWallView: View {
    let kind: String

    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(AppModel.self) private var model

    @State private var ruleSets: [API.RuleSetView] = []
    @State private var libraries: [API.LibraryView] = []

    private var index: SubscriptionIndex { SubscriptionIndex.shared }
    private var title: String { kind == "movie" ? "电影订阅" : "剧集订阅" }

    /// 与首页那一排同一套排序（这里没有预告与刚刚入库的实时信息，下载 / 整理按订阅进度判断）
    private var shelf: SubsHomeShelf {
        SubscriptionsHome.shelf(kind: kind, subscriptions: index.subscriptions ?? [], groups: [], recent: [])
    }

    var body: some View {
        let shelf = shelf
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Text(summary(shelf))
                    .font(.subheadline)
                    .monospacedDigit()
                    .foregroundStyle(Theme.textMuted)
                    .accessibilityIdentifier("subscriptions-count")
                // 墙上的海报带完整收录摘要与流向，宽度沿用改版前的首页海报墙（两列），窄了数字会折行
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 140), spacing: 12, alignment: .top)], alignment: .leading, spacing: 20) {
                    ForEach(shelf.all) { item in
                        SubscriptionPosterCell(
                            sub: item.sub,
                            ruleSetName: ruleSets.first { $0.id == item.sub.ruleSetId }?.name,
                            libraryName: libraryName(for: item.sub)
                        )
                    }
                }
            }
            .padding(.horizontal, Theme.pagePadding)
            .padding(.top, 4)
            .padding(.bottom, 40)
        }
        .scrollIndicators(.hidden)
        .appBackground()
        .navigationTitle(title)
        .refreshable { _ = await index.refresh(api: api, owner: model.session?.username) }
        .task { await loadNames() }
        .tracksSubscriptionIndex()
    }

    /// 「共 12 部剧集 · 5 部追踪中」：与首页那一排的计数同一口径（按订阅状态数）
    private func summary(_ shelf: SubsHomeShelf) -> String {
        let total = shelf.all.count
        let tracking = shelf.all.filter { $0.sub.status == "active" }.count
        let noun = kind == "movie" ? "电影" : "剧集"
        guard tracking > 0, tracking < total else { return "共 \(total) 部\(noun)" }
        return "共 \(total) 部\(noun) · \(tracking) 部追踪中"
    }

    /// 规则组名（管理员可见边界同详情页）与媒体库名：只为拼「规则组 → 库」流向
    private func loadNames() async {
        libraries = (try? await api.libraryList(scope: "all")) ?? []
        ruleSets = permissions.canManageSubscriptions ? ((try? await api.rulesList()) ?? []) : []
    }

    /// 库名：订阅未指定库时显示该类型默认库
    private func libraryName(for sub: API.SubscriptionView) -> String? {
        if let id = sub.libraryId { return libraries.first { $0.id == id }?.name }
        return libraries.first { $0.isDefault && $0.kind == sub.media.kind }?.name
    }
}

// MARK: - 海报墙单元格

/// 订阅海报：左上状态斜标（已入库 / 已收齐 / 自动续订）、右上「● 洗版 N」、海报底部常驻收录摘要，
/// 海报下方是片名、年份与季范围、「规则组 → 媒体库」流向。点按进订阅详情；长按菜单可去影片详情。
/// 已经全部到手的卡片压暗一档：扫墙时亮着的就是还没到手的那些。
struct SubscriptionPosterCell: View {
    let sub: API.SubscriptionView
    var ruleSetName: String?
    var libraryName: String?

    @Environment(\.api) private var api
    @Environment(Router.self) private var router

    var body: some View {
        let meta = SubscriptionSummary.collectionMeta(sub)
        Button {
            router.push(.subscription(id: sub.id))
        } label: {
            VStack(alignment: .leading, spacing: 0) {
                poster(meta)
                VStack(alignment: .leading, spacing: 2) {
                    Text(sub.media.title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.text)
                        .lineLimit(1)
                    Text(metaLine)
                        .font(.caption).monospacedDigit()
                        .foregroundStyle(Theme.textMuted)
                        .lineLimit(1)
                    if let flow {
                        Text(flow)
                            .font(.caption)
                            .foregroundStyle(Theme.textFaint)
                            .lineLimit(1)
                    }
                }
                .padding(.top, 8)
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .opacity(SubscriptionSummary.fullyCollected(sub) ? 0.72 : 1)
        .contextMenu {
            Button("查看订阅详情", systemImage: "list.bullet.rectangle") { router.push(.subscription(id: sub.id)) }
            Button("查看影片详情", systemImage: "info.circle") {
                router.push(.mediaDetail(titleRef: "tmdb:\(sub.media.kind):\(sub.media.tmdbId)"))
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText(meta))
        .accessibilityAddTraits(.isButton)
        .accessibilityIdentifier("subscription-cell")
    }

    private var metaLine: String {
        let scope = sub.media.kind == "tv" ? SubscriptionSummary.compactSeasonRange(sub.selectedSeasons) : nil
        return [sub.media.year.map(String.init), scope].compactMap { $0 }.joined(separator: " · ")
    }

    private var flow: String? {
        let parts = [ruleSetName, libraryName].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: "  →  ")
    }

    private func accessibilityText(_ meta: SubscriptionCollectionMeta?) -> String {
        var parts = ["《\(sub.media.title)》"]
        if let ribbon = SubscriptionSummary.ribbon(sub) { parts.append(ribbon.label) }
        if let meta {
            parts.append("\(meta.label)，收录 \(meta.value)")
            if let count = meta.upgradingCount { parts.append("\(count) 集洗版中") }
        }
        if let flow { parts.append(flow) }
        return parts.joined(separator: "，")
    }

    private func poster(_ meta: SubscriptionCollectionMeta?) -> some View {
        Color.clear
            .aspectRatio(2.0 / 3.0, contentMode: .fit)
            .overlay { RemoteImage(url: api.image(sub.media.posterUrl, .posterCard)) }
            .overlay(alignment: .topLeading) {
                if let ribbon = SubscriptionSummary.ribbon(sub) { RibbonBadge(ribbon: ribbon) }
            }
            .overlay(alignment: .topTrailing) {
                if let meta, meta.upgrading, let count = meta.upgradingCount {
                    HStack(spacing: 4) {
                        Circle().fill(SubsColor.upgrade).frame(width: 6, height: 6)
                            .shadow(color: SubsColor.upgrade.opacity(0.7), radius: 3)
                        Text("洗版 \(count)").monospacedDigit()
                    }
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(SubsColor.upgrade)
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(.black.opacity(0.4), in: .rect(cornerRadius: 6))
                    .padding(8)
                }
            }
            .overlay(alignment: .bottom) {
                if let meta {
                    HStack(spacing: 8) {
                        Text(meta.label).lineLimit(1).foregroundStyle(.white.opacity(0.8))
                        Spacer(minLength: 4)
                        HStack(spacing: 5) {
                            if meta.tracking {
                                Circle().fill(SubsColor.ok).frame(width: 6, height: 6).shadow(color: SubsColor.ok.opacity(0.55), radius: 3)
                            } else if meta.upgrading {
                                Circle().fill(SubsColor.upgrade).frame(width: 6, height: 6)
                            }
                            Text(meta.value).monospacedDigit().fontWeight(.semibold).foregroundStyle(.white)
                        }
                    }
                    .font(.caption)
                    .padding(.horizontal, 10)
                    .padding(.bottom, 8)
                    .padding(.top, 30)
                    .background(LinearGradient(colors: [.clear, .black.opacity(0.55), .black.opacity(0.9)], startPoint: .top, endPoint: .bottom))
                }
            }
            .clipShape(.rect(cornerRadius: Theme.posterRadius))
            .overlay(RoundedRectangle(cornerRadius: Theme.posterRadius).strokeBorder(Color.white.opacity(0.08)))
            .shadow(color: .black.opacity(0.35), radius: 10, y: 6)
    }
}

