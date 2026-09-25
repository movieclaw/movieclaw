import SwiftUI

/// 影片分享页（Web `/s/[slug]`，`components/share/share-page.tsx`，设计见 docs/design/media-share.md §1.2）。
///
/// 状态机与 Web 一致：
///
///     探针 GET /share/{slug}
///       → 需要密码且未解锁 → 密码卡（密码之前不露片名海报）→ POST /share/{slug}/unlock
///       → 失效（不存在 / 已取消 / 已过期）→ 整页提示
///       → 条目分享 → 影片页（`ShareItemView`）
///       → 合集分享 → 名单网格（`ShareCollectionView`）→ 点一格进那一部的影片页（同一条链接，`?item=`）
///
/// 解锁凭据是服务端种的 Cookie，与会话 Cookie 同在共享 Cookie 存储里，App 内再打开同一条链接不必重输密码。
/// 访客页没有任何管理与站内入口（没有编辑、没有影人跳转），只能停在这部片 / 这个合集上。
struct SharePageView: View {
    let slug: String

    private enum Phase: Equatable {
        case loading
        case locked
        case unavailable(String)
        case collection
        /// 条目分享；合集分享点开某一部之后也是它（带着 mediaItemId）
        case item(Int?)
    }

    @Environment(\.api) private var api
    @State private var phase: Phase = .loading
    /// 解锁之后要知道这条链接是「一部片」还是「一个合集」——两者的落地页不一样
    @State private var isCollection = false

    var body: some View {
        Group {
            switch phase {
            case .loading:
                HStack(spacing: 10) {
                    ProgressView()
                    Text("正在打开分享…").font(.subheadline).foregroundStyle(.white.opacity(0.6))
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .locked:
                ShareGateView(slug: slug) { phase = isCollection ? .collection : .item(nil) }
            case let .unavailable(message):
                ShareUnavailableView(message: message)
            case .collection:
                ShareCollectionView(slug: slug) { phase = .item($0) }
            case let .item(mediaItemId):
                ShareItemView(
                    slug: slug,
                    mediaItemId: mediaItemId,
                    // 合集分享里给一条回名单的路；条目分享没有「上一层」
                    onBack: isCollection ? { phase = .collection } : nil
                )
            }
        }
        .background(Color(red: 7 / 255, green: 8 / 255, blue: 12 / 255).ignoresSafeArea())
        .navigationBarTitleDisplayMode(.inline)
        .task(id: slug) { await probe() }
    }

    private func probe() async {
        phase = .loading
        do {
            let info = try await api.shareGuestProbe(slug: slug)
            isCollection = info.collectionId != nil
            phase = info.requiresPassword && !info.unlocked ? .locked : isCollection ? .collection : .item(nil)
        } catch is CancellationError {
        } catch {
            phase = .unavailable(Self.unavailableMessage(error))
        }
    }

    /// 探针失败 → 访客看得懂的一句话（后端 message 已是中文，直接用）
    static func unavailableMessage(_ error: Error) -> String {
        if let error = error as? APIError, let status = error.status {
            let message = error.localizedDescription
            if status == 404 { return message.isEmpty ? "分享不存在或已取消" : message }
            return message.isEmpty ? "暂时无法打开这条分享，请稍后再试" : message
        }
        return "无法连接到服务器，请检查网络后重试"
    }
}

// MARK: - 密码卡 / 失效页

/// 密码卡片：与登录页同一种外壳，但没有任何站内入口；标题与海报在解锁前一律不露
private struct ShareGateView: View {
    let slug: String
    let onUnlocked: () -> Void

    @Environment(\.api) private var api
    @State private var password = ""
    @State private var error: String?
    @State private var busy = false

    private var empty: Bool { password.trimmingCharacters(in: .whitespaces).isEmpty }

    var body: some View {
        ShareCardShell(title: "需要密码", subtitle: "这是一条受密码保护的分享，输入密码后即可观看。") {
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("密码").font(.subheadline.weight(.medium)).foregroundStyle(.white.opacity(0.7))
                    SecureField("", text: $password)
                        .submitLabel(.go)
                        .onSubmit { Task { await submit() } }
                        .padding(.horizontal, 12)
                        .frame(height: 44)
                        .background(Color.white.opacity(0.06), in: .rect(cornerRadius: 12))
                        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.1)))
                        .accessibilityIdentifier("share-password")
                }
                if let error {
                    Text(error)
                        .font(.footnote)
                        .foregroundStyle(Color(red: 1, green: 0.75, blue: 0.75))
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.red.opacity(0.1), in: .rect(cornerRadius: 10))
                        .accessibilityIdentifier("share-password-error")
                }
                Button {
                    Task { await submit() }
                } label: {
                    Text(busy ? "正在验证…" : "打开")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(.black)
                        .frame(maxWidth: .infinity)
                        .frame(height: 44)
                        .background(.white, in: .rect(cornerRadius: 12))
                }
                .buttonStyle(.plain)
                .disabled(busy || empty)
                .opacity(busy || empty ? 0.5 : 1)
                .accessibilityIdentifier("share-unlock")
            }
        }
        .accessibilityIdentifier("share-gate")
    }

    private func submit() async {
        let value = password.trimmingCharacters(in: .whitespaces)
        guard !value.isEmpty, !busy else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            _ = try await api.shareGuestUnlock(slug: slug, password: value)
            onUnlocked()
        } catch let apiError as APIError where apiError.status != nil {
            let message = apiError.localizedDescription
            error = message.isEmpty ? "密码不对，请重新输入" : message
        } catch {
            self.error = "无法连接到服务器，请检查网络后重试"
        }
    }
}

private struct ShareUnavailableView: View {
    let message: String

    var body: some View {
        ShareCardShell(title: message, subtitle: "请向分享者确认链接是否仍然有效。") {
            Text("分享链接有有效期，到期或被取消后就无法再打开。")
                .font(.caption)
                .foregroundStyle(.white.opacity(0.4))
        }
        .accessibilityIdentifier("share-unavailable")
    }
}

/// 居中卡片外壳（Web `AuthScreen`）：字标 + 标题 + 副标题 + 内容
private struct ShareCardShell<Content: View>: View {
    let title: String
    let subtitle: String
    @ViewBuilder var content: () -> Content

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("MOVIECLAW")
                    .font(.footnote.weight(.semibold))
                    .tracking(3)
                    .foregroundStyle(.white.opacity(0.7))
                VStack(alignment: .leading, spacing: 6) {
                    Text(title).font(.title2.weight(.bold)).foregroundStyle(.white)
                    Text(subtitle).font(.subheadline).foregroundStyle(.white.opacity(0.6))
                }
                content()
            }
            .padding(24)
            .frame(maxWidth: 420)
            .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 24))
            .overlay(RoundedRectangle(cornerRadius: 24).strokeBorder(Color.white.opacity(0.08)))
            .padding(.horizontal, 20)
            .padding(.top, 80)
            .frame(maxWidth: .infinity)
        }
        .scrollDismissesKeyboard(.interactively)
    }
}

// MARK: - 合集名单

/// 合集分享的名单（Web `SharedCollectionView`）。成员由服务端每次现算：规则驱动的合集会自己长。
/// 与站内合集网格刻意不复用——访客看到的只该是「这几部片」，没有隐藏、系列分组这些登录用户才有的概念
private struct ShareCollectionView: View {
    let slug: String
    let onOpen: (Int) -> Void

    @Environment(\.api) private var api
    @State private var data: API.SharedCollectionView?
    @State private var failed = false

    private let columns = [GridItem(.adaptive(minimum: 104, maximum: 170), spacing: 12, alignment: .top)]

    var body: some View {
        Group {
            if failed {
                Text("这条分享暂时打不开，请稍后再试。")
                    .font(.subheadline).foregroundStyle(.white.opacity(0.6))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let data {
                ScrollView {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(data.name).font(.title2.weight(.semibold)).foregroundStyle(.white)
                        Text("\(data.itemCount) 部").font(.subheadline).foregroundStyle(.white.opacity(0.5))
                        if data.items.isEmpty {
                            Text("这个合集现在一部都没有。")
                                .font(.subheadline).foregroundStyle(.white.opacity(0.5))
                                .frame(maxWidth: .infinity)
                                .padding(.top, 60)
                        } else {
                            LazyVGrid(columns: columns, spacing: 18) {
                                ForEach(data.items, id: \.mediaItemId) { item in
                                    cell(item)
                                }
                            }
                            .padding(.top, 16)
                        }
                    }
                    .padding(.horizontal, Theme.pagePadding)
                    .padding(.vertical, 16)
                }
            } else {
                Text("正在打开分享…")
                    .font(.subheadline).foregroundStyle(.white.opacity(0.5))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .navigationTitle(data?.name ?? "")
        .task {
            do {
                data = try await api.shareGuestCollection(slug: slug)
            } catch is CancellationError {
            } catch {
                failed = true
            }
        }
    }

    private func cell(_ item: API.SharedCollectionItemView) -> some View {
        Button {
            onOpen(item.mediaItemId)
        } label: {
            VStack(alignment: .leading, spacing: 4) {
                ZStack {
                    Color.white.opacity(0.04)
                    if item.posterUrl != nil {
                        RemoteImage(url: api.image(item.posterUrl, .posterCard))
                    } else {
                        Text("暂无海报").font(.caption).foregroundStyle(.white.opacity(0.3))
                    }
                }
                .aspectRatio(2 / 3, contentMode: .fit)
                .clipShape(.rect(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.06)))
                Text(item.title).font(.subheadline.weight(.medium)).foregroundStyle(.white).lineLimit(1)
                if let year = item.year {
                    Text(String(year)).font(.caption).foregroundStyle(.white.opacity(0.4))
                }
            }
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("share-collection-item-\(item.mediaItemId)")
    }
}
