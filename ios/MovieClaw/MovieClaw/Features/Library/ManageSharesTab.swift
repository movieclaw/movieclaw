import SwiftUI

/// 媒体库管理页的「分享」页签（Web `library-shares.tsx`，设计见 docs/design/media-share.md §5.4）。
///
/// 全部有效分享一行一条——海报、片名、有无密码、形态 / 年份 / 有效期、打开次数与最近打开，
/// 行尾「复制」「取消」。分享多了只有这里能一眼管住；过期 / 已取消的后端不列。
/// 30 秒轮询，列表回报条数给页签计数。
struct ManageSharesTab: View {
    var onCountChange: (Int) -> Void = { _ in }

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(Router.self) private var router

    @State private var shares: [API.ShareView]?
    @State private var failed = false
    @State private var busyId: Int?
    /// 待确认取消的分享：确认框按钮是「取消分享 / 先不」（同 Web），全局确认框的取消键固定叫「取消」会与之混淆
    @State private var revoking: API.ShareView?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let shares {
                if shares.isEmpty {
                    EmptyState(
                        systemImage: "link",
                        title: "还没有分享任何影片",
                        message: "在影片详情页的 ⋯ 菜单里可以创建分享：拿到链接的人不用登录就能看这一部影片。"
                    )
                    .padding(.top, 30)
                } else {
                    if failed {
                        ManageBanner(text: "与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据")
                    }
                    ForEach(shares, id: \.id) { share in
                        row(share)
                    }
                }
            } else if failed {
                ErrorState(message: "分享列表加载失败") { await reload() }
            } else {
                HStack(spacing: 10) {
                    ProgressView()
                    Text("正在读取分享…").font(.subheadline).foregroundStyle(Theme.textMuted)
                }
                .padding(.vertical, 30)
            }
        }
        .padding(.horizontal, Theme.pagePadding)
        .task { await reload() }
        .polling(every: 30) { await reload() }
        .alert("取消《\(revoking?.title ?? "")》的分享？", isPresented: Binding(
            get: { revoking != nil },
            set: { if !$0 { revoking = nil } }
        ), presenting: revoking) { share in
            Button("先不", role: .cancel) {}
            Button("取消分享", role: .destructive) { Task { await revoke(share) } }
        } message: { _ in
            Text("链接立即失效，正在播放的访客会在一分钟内中断。")
        }
    }

    private func row(_ share: API.ShareView) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .center, spacing: 14) {
                Button {
                    openItem(share)
                } label: {
                    RemoteImage(url: api.image(share.posterUrl, .posterCard))
                        .frame(width: 44, height: 66)
                        .clipShape(.rect(cornerRadius: 8))
                }
                .buttonStyle(.plain)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 8) {
                        Button {
                            openItem(share)
                        } label: {
                            Text(share.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text).lineLimit(1)
                        }
                        .buttonStyle(.plain)
                        if share.password != nil {
                            Label("密码", systemImage: "lock.fill")
                                .labelStyle(.titleAndIcon)
                                .font(.caption)
                                .foregroundStyle(Theme.textFaint)
                                .accessibilityIdentifier("share-row-locked")
                        }
                    }
                    Text(facts(share))
                        .font(.caption)
                        .foregroundStyle(Theme.textMuted)
                        .lineLimit(2)
                    Text((share.viewCount > 0 ? "已打开 \(share.viewCount) 次" : "还没有人打开")
                        + (share.lastAccessedAt.map { " · 最近 \(libraryFromNow($0))" } ?? ""))
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(Theme.textFaint)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            HStack(spacing: 6) {
                Spacer()
                Button {
                    copy(share)
                } label: {
                    Label("复制", systemImage: "doc.on.doc").font(.footnote)
                }
                .buttonStyle(.glass)
                .accessibilityIdentifier("share-copy-\(share.slug)")
                Button {
                    revoking = share
                } label: {
                    Text("取消").font(.footnote).foregroundStyle(Color(red: 1, green: 0.62, blue: 0.62))
                }
                .buttonStyle(.glass)
                .disabled(busyId == share.id)
                .accessibilityIdentifier("share-revoke-\(share.slug)")
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 18))
        .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(Theme.line))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("share-row-\(share.slug)")
    }

    /// 「形态 · 年份 · N 天后失效（绝对时间）」；合集分享没有形态，写它此刻有几部
    private func facts(_ share: API.ShareView) -> String {
        var parts: [String] = []
        if share.collectionId != nil {
            parts.append("合集 · \(share.itemCount ?? 0) 部")
        } else if let kind = share.kind {
            parts.append(LibraryKindMeta.label(kind))
        }
        if let year = share.year { parts.append(String(year)) }
        parts.append("\(ShareLinkKit.expiryHint(share.expiresAt))（\(Formatters.dateTime(share.expiresAt))）")
        return parts.joined(separator: " · ")
    }

    private func openItem(_ share: API.ShareView) {
        if let libraryId = share.libraryId, let itemId = share.mediaItemId {
            router.push(.libraryItem(libraryId: libraryId, itemId: itemId))
        } else if let collectionId = share.collectionId {
            router.push(.collection(libraryId: share.libraryId, collectionId: collectionId))
        }
    }

    private func copy(_ share: API.ShareView) {
        let link = ShareLinkKit.absoluteURL(share.url, origin: api.server.origin)
        UIPasteboard.general.string = ShareLinkKit.copyText(title: share.title, url: link, password: share.password)
        feedback.success(share.password != nil ? "已复制链接和密码" : "已复制链接")
    }

    private func revoke(_ share: API.ShareView) async {
        busyId = share.id
        defer { busyId = nil }
        do {
            _ = try await api.sharesRevoke(shareId: share.id)
            feedback.success("分享已取消")
            await reload()
        } catch {
            feedback.error(error.localizedDescription.isEmpty ? "取消分享失败，请稍后重试" : error.localizedDescription)
        }
    }

    private func reload() async {
        do {
            let rows = try await api.sharesList()
            failed = false
            if rows != shares { shares = rows }
            onCountChange(rows.count)
        } catch is CancellationError {
        } catch {
            failed = true
        }
    }
}
