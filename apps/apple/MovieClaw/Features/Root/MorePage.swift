import SwiftUI

/// 「更多」页：标签栏最右的头像页签（Web `/my` 与 components/more-page.tsx）。
///
/// iOS 设置式分组列表（与 Web 同序同文案）：
/// - 用户头：头像 + 昵称 + `@用户名 · 角色`；
/// - 常用：个人信息 / 待处理（管理员且有事项时，30 秒轮询）/ 设置 / 应用更新（管理员且有待更新时，
///   文案「新版本 vX」或「新识别模型 X」）；
/// - 账号：切换账号 / 退出登录；
/// - 最近会话（管理员）：首行「新会话」（顶栏的「+」已去掉，这里是发起新会话的入口），下面是 AI 会话，
///   每页 20 条、滑到末尾自动加载下一页（用户决定不要「显示全部 / 收起」，与 Web 的差异）；
///   操作走 iOS 列表惯例：左滑删除 / 重命名、右滑在新会话中继续、长按出完整菜单（与会话页右上角同图标、同顺序）。
///
/// 原先是点左上角头像弹出的 sheet（右上「完成」关闭），2026-09-26 头像挪进标签栏后改为标签根页；
/// 站内链接 `/my` 也切到这个标签（Router.tabRoot）。
struct MorePage: View {
    @Environment(AppModel.self) private var model
    @Environment(Router.self) private var router
    @Environment(Feedback.self) private var feedback
    @Environment(ShellBadges.self) private var badges
    @Environment(\.permissions) private var permissions
    @Environment(\.api) private var api

    @State private var sessions: [API.SessionSummary] = []
    @State private var notices: [API.NoticeView] = []
    /// 还有更早的会话没取回（上一页取满了一整页）
    @State private var hasMoreSessions = false
    @State private var loadingMoreSessions = false
    /// 会话列表每页条数（Web agent-conversations 的 PAGE_SIZE）
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
                    Task { await model.logout() }
                } label: {
                    Label("退出登录", systemImage: "rectangle.portrait.and.arrow.right")
                }
                .accessibilityIdentifier("logout")
            }

            if permissions.isAdmin {
                Section("最近会话") {
                    MoreRouteRow(routes: [.newSession], tint: Theme.accentStrong) {
                        Label("新会话", systemImage: "square.and.pencil").fontWeight(.medium)
                    }
                    .accessibilityIdentifier("more-new-session")
                    if sessions.isEmpty {
                        Text("还没有会话，点上方的「新会话」开始。")
                            .font(.caption)
                            .foregroundStyle(Theme.textFaint)
                    }
                    ForEach(sessions, id: \.id) { item in
                        sessionRow(item)
                            // 滑到最后一条时接着取下一页，不再要「显示全部」
                            .onAppear {
                                if item.id == sessions.last?.id { Task { await loadMoreSessions() } }
                            }
                    }
                    if loadingMoreSessions {
                        HStack { Spacer(); ProgressView(); Spacer() }
                    }
                }
            }
        }
        .appBackground()
        .navigationTitle("更多")
        .navigationBarTitleDisplayMode(.inline)
        .task { await loadSessions() }
        // 待处理事项与 Web NoticeCenter 同频 30 秒轮询（首轮立即拉）
        .polling(every: 30, immediately: true) { await loadNotices() }
    }

    /// 会话行按 iOS 列表惯例处理操作（同邮件 / 信息）：行上不放「⋯」，左滑出「删除 / 重命名」，
    /// 右滑出「在新会话中继续」，长按出完整菜单（与会话页右上角同图标、同顺序）。
    /// 删除不允许一滑到底直接触发——删除要二次确认，全滑手势容易误触。
    private func sessionRow(_ item: API.SessionSummary) -> some View {
        MoreRouteRow(routes: [.session(id: item.id)]) {
            HStack(spacing: 10) {
                if item.running {
                    MoreRunningDot()
                }
                Text(title(of: item)).lineLimit(1)
            }
        }
        .accessibilityIdentifier("more-session-row")
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            Button("删除", systemImage: "trash", role: .destructive) { Task { await remove(item) } }
                .tint(.red)
            Button("重命名", systemImage: "pencil") { Task { await rename(item) } }
                .tint(.gray)
        }
        .swipeActions(edge: .leading, allowsFullSwipe: false) {
            Button("在新会话中继续", systemImage: "arrow.triangle.branch") { Task { await fork(item) } }
                .tint(.blue)
        }
        .contextMenu { sessionActions(item) }
    }

    @ViewBuilder
    private func sessionActions(_ item: API.SessionSummary) -> some View {
        Button("在新会话中继续", systemImage: "arrow.triangle.branch") { Task { await fork(item) } }
        Button("重命名", systemImage: "pencil") { Task { await rename(item) } }
        Divider()
        Button("删除会话", systemImage: "trash", role: .destructive) { Task { await remove(item) } }
    }

    private func title(of item: API.SessionSummary) -> String {
        if let title = item.title, !title.isEmpty { return title }
        if let prompt = item.lastPrompt, !prompt.isEmpty { return prompt }
        return "未命名会话"
    }

    /// 取第一页（进页、从会话里回来时刷新）；失败保留上次结果
    private func loadSessions() async {
        guard permissions.isAdmin, let first = try? await api.sessionList(limit: Self.pageSize) else { return }
        sessions = first
        hasMoreSessions = first.count == Self.pageSize
    }

    /// 接着取下一页；按 id 去重（翻页期间有新会话插到最前，偏移会错开一条）
    private func loadMoreSessions() async {
        guard hasMoreSessions, !loadingMoreSessions else { return }
        loadingMoreSessions = true
        defer { loadingMoreSessions = false }
        guard let page = try? await api.sessionList(limit: Self.pageSize, offset: sessions.count) else { return }
        let known = Set(sessions.map(\.id))
        sessions += page.filter { !known.contains($0.id) }
        hasMoreSessions = page.count == Self.pageSize
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

/// 更多页的跳转行：经 Router 在主导航里打开目标页（设置、会话这类不归属任何标签的页面，
/// 就压在「更多」标签自己的栈里）。
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
