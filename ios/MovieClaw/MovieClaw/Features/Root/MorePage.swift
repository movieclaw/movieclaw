import SwiftUI

/// 「更多」页（左上角头像打开；Web `/my` 与 components/more-page.tsx）。
///
/// iOS 设置式分组列表：
/// - 用户头：头像 + 昵称 + `@用户名 · 角色`；
/// - 常用：个人信息 / 待处理事项（管理员且有通知时）/ 设置 / 应用更新（管理员且有待更新时）；
/// - 账号：切换账号 / 退出登录；
/// - 最近会话（管理员）：AI 会话，默认 5 条，其余就地展开；每行菜单：
///   在新会话中继续 / 复制会话 ID / 重命名 / 删除。
struct MorePage: View {
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
    private static let recentLimit = 5

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
                MoreRouteRow(route: .settingsSection(.profile)) {
                    Label("个人信息", systemImage: "person.crop.circle")
                }
                if permissions.isAdmin, !notices.isEmpty {
                    NavigationLink {
                        NoticeCenterView()
                    } label: {
                        Label {
                            HStack {
                                Text("待处理事项")
                                Spacer()
                                Text("\(notices.count)").foregroundStyle(Theme.textMuted)
                            }
                        } icon: {
                            Image(systemName: "bell.badge").foregroundStyle(Theme.danger)
                        }
                    }
                }
                MoreRouteRow(route: .settings) {
                    Label("设置", systemImage: "gearshape")
                }
                .accessibilityIdentifier("more-settings")
                if permissions.isAdmin, badges.updatePending {
                    MoreRouteRow(route: .settingsSection(.app)) {
                        Label {
                            Text("有可用更新")
                        } icon: {
                            Image(systemName: "arrow.down.app").foregroundStyle(Theme.info)
                        }
                    }
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
                        Text("还没有会话。点右上角「+」开始一个新任务。")
                            .font(.subheadline)
                            .foregroundStyle(Theme.textMuted)
                    }
                    ForEach(visibleSessions, id: \.id) { item in
                        sessionRow(item)
                    }
                    if !showAllSessions, sessions.count > Self.recentLimit {
                        Button("显示全部 \(sessions.count) 个会话") { showAllSessions = true }
                            .foregroundStyle(Theme.textMuted)
                    }
                }
            }
        }
        .navigationTitle("更多")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("完成") { dismiss() }
            }
        }
        .task { await load() }
    }

    private var visibleSessions: [API.SessionSummary] {
        showAllSessions ? sessions : Array(sessions.prefix(Self.recentLimit))
    }

    @ViewBuilder
    private func sessionRow(_ item: API.SessionSummary) -> some View {
        MoreRouteRow(route: .session(id: item.id)) {
            HStack {
                if item.running {
                    Circle().fill(Theme.success).frame(width: 7, height: 7)
                        .symbolEffect(.pulse)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(title(of: item)).lineLimit(1)
                    Text(Formatters.relative(item.updatedAt))
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                }
            }
        }
        .contextMenu {
            Button("在新会话中继续", systemImage: "arrow.triangle.branch") { Task { await fork(item) } }
            Button("复制会话 ID", systemImage: "doc.on.doc") {
                UIPasteboard.general.string = item.id
                feedback.success("已复制会话 ID")
            }
            Button("重命名", systemImage: "pencil") { Task { await rename(item) } }
            Button("删除会话", systemImage: "trash", role: .destructive) { Task { await remove(item) } }
        }
    }

    private func title(of item: API.SessionSummary) -> String {
        if let title = item.title, !title.isEmpty { return title }
        if let prompt = item.lastPrompt, !prompt.isEmpty { return prompt }
        return "未命名会话"
    }

    private func load() async {
        guard permissions.isAdmin else { return }
        async let sessionList = try? api.sessionList(limit: 50)
        async let noticeList = try? api.noticesList()
        sessions = await sessionList ?? []
        notices = NoticeCenterView.visible(await noticeList ?? [])
    }

    private func fork(_ item: API.SessionSummary) async {
        do {
            let forked = try await api.sessionFork(sessionId: item.id)
            dismiss()
            router.open(.session(id: forked.session.id))
        } catch {
            feedback.error(error)
        }
    }

    private func rename(_ item: API.SessionSummary) async {
        guard let name = await feedback.prompt("重命名会话", placeholder: "会话标题（最多 80 字）", initial: item.title ?? ""),
              !name.trimmingCharacters(in: .whitespaces).isEmpty
        else { return }
        do {
            let updated = try await api.sessionRename(sessionId: item.id, body: .init(title: String(name.prefix(80))))
            if let index = sessions.firstIndex(where: { $0.id == item.id }) { sessions[index] = updated }
        } catch {
            feedback.error(error)
        }
    }

    private func remove(_ item: API.SessionSummary) async {
        guard await feedback.confirm("删除这个会话？", message: "会话记录将被永久删除，无法恢复。", confirmTitle: "删除", destructive: true) else { return }
        do {
            _ = try await api.sessionDelete(sessionId: item.id)
            sessions.removeAll { $0.id == item.id }
        } catch {
            feedback.error(error)
        }
    }
}

/// 更多页的跳转行：先关掉「更多」弹层，再在主导航里打开目标页
/// （弹层自带的导航栈里打开会话页时隐藏不了标签栏，页内跳转也会压错栈）。
private struct MoreRouteRow<Content: View>: View {
    let route: AppRoute
    @ViewBuilder let label: () -> Content
    @Environment(Router.self) private var router

    var body: some View {
        Button {
            router.open(route)
        } label: {
            HStack {
                label()
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(Theme.textFaint)
            }
            .contentShape(Rectangle())
        }
        .foregroundStyle(Theme.text)
    }
}
