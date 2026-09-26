import SwiftUI

/// 「更多」页（左上角头像打开；Web `/my` 与 components/more-page.tsx）。
///
/// iOS 设置式分组列表（与 Web 同序同文案）：
/// - 用户头：头像 + 昵称 + `@用户名 · 角色`；
/// - 常用：个人信息 / 待处理（管理员且有事项时，30 秒轮询）/ 设置 / 应用更新（管理员且有待更新时，
///   文案「新版本 vX」或「新识别模型 X」）；
/// - 账号：切换账号 / 退出登录；
/// - 最近会话（管理员）：AI 会话（取最近 20 条），默认露出 5 条，其余就地展开、可再收起；
///   行尾常驻「⋯」菜单：在新会话中继续 / 复制会话 ID / 重命名 / 删除会话。
///
/// 两种打开方式：点头像以 sheet 弹出（`inSheet`，右上「完成」关闭）；站内链接 `/my` 压栈打开
/// （只有系统返回键，不再叠一个「完成」）。
struct MorePage: View {
    /// 是否以 sheet 形式弹出（决定右上角要不要「完成」）
    var inSheet = false

    @Environment(AppModel.self) private var model
    @Environment(Router.self) private var router
    @Environment(Feedback.self) private var feedback
    @Environment(ShellBadges.self) private var badges
    @Environment(\.permissions) private var permissions
    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss

    @State private var sessions: [API.SessionSummary] = []
    @State private var notices: [API.NoticeView] = []
    @State private var showAllSessions = false
    /// 默认露出的会话条数（Web RECENT_SESSIONS_LIMIT）
    private static let recentLimit = 5
    /// 会话列表取回条数（Web agent-conversations 的 PAGE_SIZE）
    private static let pageSize = 20

    var body: some View {
        List {
            if let session = model.session {
                Section {
                    HStack(spacing: 14) {
                        AvatarBadge(session: session, size: 56)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(session.nickname).font(.title3.weight(.semibold))
                            Text("@\(session.username) · \(session.roleLabel)")
                                .font(.subheadline)
                                .foregroundStyle(Theme.textMuted)
                        }
                    }
                    .padding(.vertical, 4)
                }
            }

            Section("常用") {
                // Web 的返回链是 /settings/profile → /settings，这里同样先压「设置」再压「个人信息」
                MoreRouteRow(routes: [.settings, .settingsSection(.profile)]) {
                    Label("个人信息", systemImage: "person.crop.circle")
                }
                if permissions.isAdmin, !notices.isEmpty {
                    NavigationLink {
                        NoticeCenterView()
                    } label: {
                        Label {
                            HStack {
                                Text("待处理").fontWeight(.medium)
                                Spacer()
                                Text("\(notices.count)")
                                    .font(.caption2.weight(.semibold))
                                    .foregroundStyle(.white)
                                    .padding(.horizontal, 6)
                                    .padding(.vertical, 2)
                                    .background(Theme.danger, in: .capsule)
                            }
                        } icon: {
                            Image(systemName: "bell")
                        }
                        .foregroundStyle(Theme.danger)
                    }
                    .accessibilityIdentifier("more-notices")
                }
                MoreRouteRow(routes: [.settings]) {
                    Label("设置", systemImage: "gearshape")
                }
                .accessibilityIdentifier("more-settings")
                if permissions.isAdmin, let label = badges.updateLabel {
                    MoreRouteRow(routes: [.settingsSection(.app)], tint: Theme.info) {
                        Label(label, systemImage: "arrow.down.app")
                    }
                    .accessibilityIdentifier("more-update")
                }
            }

            Section("账号") {
                Button {
                    router.present(.accountSwitcher)
                } label: {
                    Label("切换账号", systemImage: "person.2")
                }
                Button(role: .destructive) {
                    Task {
                        dismiss()
                        await model.logout()
                    }
                } label: {
                    Label("退出登录", systemImage: "rectangle.portrait.and.arrow.right")
                }
                .accessibilityIdentifier("logout")
            }

            if permissions.isAdmin {
                Section("最近会话") {
                    if sessions.isEmpty {
                        Text("还没有会话，点上方的「新会话」开始。")
                            .font(.caption)
                            .foregroundStyle(Theme.textFaint)
                    }
                    ForEach(visibleSessions, id: \.id) { item in
                        sessionRow(item)
                    }
                    if sessions.count > Self.recentLimit {
                        // 展开/收起行：文字居中弱化 + 上下箭头（Web 同款，区别于「点进去」的右箭头）
                        Button {
                            withAnimation { showAllSessions.toggle() }
                        } label: {
                            HStack(spacing: 6) {
                                Spacer()
                                Text(showAllSessions ? "收起" : "显示全部 \(sessions.count) 个会话")
                                Image(systemName: "chevron.down")
                                    .font(.footnote.weight(.semibold))
                                    .rotationEffect(.degrees(showAllSessions ? 180 : 0))
                                Spacer()
                            }
                        }
                        .foregroundStyle(Theme.textMuted)
                        .accessibilityIdentifier("more-sessions-toggle")
                    }
                }
            }
        }
        .appBackground()
        .navigationTitle("更多")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if inSheet {
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") { dismiss() }
                }
            }
        }
        .task { await loadSessions() }
        // 待处理事项与 Web NoticeCenter 同频 30 秒轮询（首轮立即拉）
        .polling(every: 30, immediately: true) { await loadNotices() }
    }

    private var visibleSessions: [API.SessionSummary] {
        showAllSessions ? sessions : Array(sessions.prefix(Self.recentLimit))
    }

    @ViewBuilder
    private func sessionRow(_ item: API.SessionSummary) -> some View {
        HStack(spacing: 8) {
            MoreRouteRow(routes: [.session(id: item.id)], showsChevron: false) {
                HStack(spacing: 10) {
                    if item.running {
                        MoreRunningDot()
                    }
                    Text(title(of: item)).lineLimit(1)
                }
            }
            // 一行里有两个可点控件：必须各自 borderless，否则 List 会把整行点击同时派给两者
            .buttonStyle(.borderless)
            // 行尾常驻「⋯」（Web ConversationMenu），不再只能靠长按发现
            Menu {
                sessionActions(item)
            } label: {
                Image(systemName: "ellipsis")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(Theme.textMuted)
                    .frame(width: 32, height: 32)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("会话操作")
        }
        .contextMenu { sessionActions(item) }
    }

    @ViewBuilder
    private func sessionActions(_ item: API.SessionSummary) -> some View {
        Button("在新会话中继续", systemImage: "bubble.left") { Task { await fork(item) } }
        Button("复制会话 ID", systemImage: "doc.on.doc") {
            UIPasteboard.general.string = item.id
            feedback.success("会话 ID 已复制")
        }
        Button("重命名", systemImage: "pencil") { Task { await rename(item) } }
        Button("删除会话", systemImage: "trash", role: .destructive) { Task { await remove(item) } }
    }

    private func title(of item: API.SessionSummary) -> String {
        if let title = item.title, !title.isEmpty { return title }
        if let prompt = item.lastPrompt, !prompt.isEmpty { return prompt }
        return "未命名会话"
    }

    private func loadSessions() async {
        guard permissions.isAdmin else { return }
        sessions = (try? await api.sessionList(limit: Self.pageSize)) ?? sessions
    }

    private func loadNotices() async {
        guard permissions.isAdmin else { return }
        // 拉取失败保留上次结果，下一轮轮询自愈（同 Web）
        if let list = try? await api.noticesList() {
            notices = NoticeCenterView.visible(list)
        }
    }

    private func fork(_ item: API.SessionSummary) async {
        do {
            let forked = try await api.sessionFork(sessionId: item.id)
            dismiss()
            router.open(.session(id: forked.session.id))
        } catch {
            feedback.error("创建续接会话失败：\(error.localizedDescription)")
        }
    }

    private func rename(_ item: API.SessionSummary) async {
        // 初值是界面上显示的标题；去空白、截 80 字，没变化就不发请求（同 Web）
        let current = title(of: item)
        guard let input = await feedback.prompt("重命名会话", placeholder: "会话标题（最多 80 字）", initial: current, maxLength: 80) else { return }
        let name = String(input.trimmingCharacters(in: .whitespacesAndNewlines).prefix(80))
        guard !name.isEmpty, name != current else { return }
        do {
            let updated = try await api.sessionRename(sessionId: item.id, body: .init(title: name))
            if let index = sessions.firstIndex(where: { $0.id == item.id }) { sessions[index] = updated }
        } catch {
            feedback.error("重命名失败：\(error.localizedDescription)")
        }
    }

    private func remove(_ item: API.SessionSummary) async {
        guard await feedback.confirm(
            "彻底删除会话「\(title(of: item))」？",
            message: "服务器上的完整对话记录将一并删除，此操作不可恢复。",
            confirmTitle: "彻底删除", destructive: true
        ) else { return }
        do {
            _ = try await api.sessionDelete(sessionId: item.id)
            sessions.removeAll { $0.id == item.id }
        } catch {
            feedback.error("删除失败：\(error.localizedDescription)")
        }
    }
}

/// 运行中会话的提示点：信息蓝 + 呼吸（Web `bg-[var(--info)] animate-pulse`）
private struct MoreRunningDot: View {
    @State private var dim = false

    var body: some View {
        Circle()
            .fill(Theme.info)
            .frame(width: 6, height: 6)
            .opacity(dim ? 0.35 : 1)
            .animation(.easeInOut(duration: 1).repeatForever(autoreverses: true), value: dim)
            .onAppear { dim = true }
            .accessibilityLabel("运行中")
    }
}

/// 更多页的跳转行：先关掉「更多」弹层，再在主导航里打开目标页
/// （弹层自带的导航栈里打开会话页时隐藏不了标签栏，页内跳转也会压错栈）。
/// `routes` 依次压栈（第一个走 `open` 定标签，其余 `push`），用于还原 Web 的返回链。
private struct MoreRouteRow<Content: View>: View {
    let routes: [AppRoute]
    var tint: Color = Theme.text
    var showsChevron = true
    @ViewBuilder let label: () -> Content
    @Environment(Router.self) private var router

    var body: some View {
        Button {
            guard let first = routes.first else { return }
            router.open(first)
            for route in routes.dropFirst() { router.push(route) }
        } label: {
            HStack {
                label()
                Spacer()
                if showsChevron {
                    Image(systemName: "chevron.right")
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(Theme.textFaint)
                }
            }
            .contentShape(Rectangle())
        }
        .foregroundStyle(tint)
    }
}
