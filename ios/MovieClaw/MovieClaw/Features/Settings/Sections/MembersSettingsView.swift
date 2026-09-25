import SwiftUI

/// 设置 → 成员（Web members-section.tsx）：成员账号、能力与资源范围的唯一管理入口。
///
/// 列表只负责扫描状态（头像、启用点、功能权限徽标、媒体库范围、最近活动），
/// 创建 / 编辑用独立弹层，避免在列表里展开长表单；每行的 ⋯ 菜单承载
/// 编辑 / 重置密码 / 停用或启用 / 删除。创建与重置产生的明文密码只在一次性结果弹层里出现，
/// 关掉后前端不再保留——与后端「仅返回一次」的凭据语义一致。
struct MembersSettingsView: View {
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback

    @State private var members: Loadable<[API.MemberView]> = .loading
    @State private var libraries: [API.LibraryView] = []
    @State private var sites: [API.CatalogItem] = []
    @State private var creating = false
    @State private var editing: API.MemberView?
    @State private var passwordResult: SettingsMemberPasswordResult?

    var body: some View {
        AsyncContent(members, retry: load) { rows in
            List {
                Section {
                    HStack(spacing: 12) {
                        SettingsRowText(title: "成员账号", detail: "管理登录状态、功能权限和可见媒体库")
                        Spacer(minLength: 8)
                        Button { creating = true } label: { Label("添加成员", systemImage: "plus") }
                            .settingsProminentButton()
                            .accessibilityIdentifier("member-add")
                    }
                }
                Section {
                    if rows.isEmpty {
                        VStack(spacing: 4) {
                            Text("还没有成员账号").font(.body.weight(.medium))
                            Text("添加后即可分别控制订阅、搜索和媒体库可见范围。")
                                .font(.subheadline).foregroundStyle(Theme.textMuted)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 20)
                    } else {
                        ForEach(rows) { member in
                            MemberRow(
                                member: member, libraries: libraries,
                                onEdit: { editing = member },
                                onResetPassword: { Task { await resetPassword(member) } },
                                onToggleStatus: { Task { await toggleStatus(member) } },
                                onDelete: { Task { await remove(member) } }
                            )
                        }
                    }
                }
            }
        }
        .appBackground()
        .task { await load() }
        .sheet(isPresented: $creating) {
            CreateMemberSheet { member, password in
                creating = false
                if case var .loaded(rows) = members {
                    rows.append(member)
                    members = .loaded(rows)
                }
                passwordResult = SettingsMemberPasswordResult(title: "成员已创建", username: member.username, password: password)
            }
            .sheetFeedback()
        }
        .sheet(item: $editing) { member in
            EditMemberSheet(member: member, libraries: libraries, sites: sites) { next in
                replace(next)
                editing = nil
                feedback.success("成员设置已保存")
            }
            .sheetFeedback()
        }
        .sheet(item: $passwordResult) { result in
            PasswordResultSheet(result: result)
                .sheetFeedback()
        }
    }

    private func load() async {
        await Loadable.load(into: $members, onRefreshError: { feedback.error("加载成员失败：\($0.localizedDescription)") }) {
            try await api.membersList()
        }
        async let libs = try? api.libraryList(scope: "all")
        async let catalog = try? api.siteCatalog()
        libraries = await libs ?? []
        sites = await catalog ?? []
    }

    private func replace(_ next: API.MemberView) {
        guard case var .loaded(rows) = members, let index = rows.firstIndex(where: { $0.id == next.id }) else { return }
        rows[index] = next
        members = .loaded(rows)
    }

    private func resetPassword(_ member: API.MemberView) async {
        let ok = await feedback.confirm(
            "重置「\(member.nickname)」的密码？",
            message: "旧密码和该成员的全部登录会立即失效。新密码只显示一次。",
            confirmTitle: "重置密码"
        )
        guard ok else { return }
        do {
            let result = try await api.membersPasswordReset(memberId: member.id)
            passwordResult = SettingsMemberPasswordResult(title: "密码已重置", username: result.username, password: result.password)
        } catch {
            feedback.error("重置失败：\(error.localizedDescription)")
        }
    }

    private func toggleStatus(_ member: API.MemberView) async {
        let enabling = member.status != "active"
        if !enabling {
            let ok = await feedback.confirm(
                "停用「\(member.nickname)」？",
                message: "该成员的全部设备会立即下线，个人数据和订阅保留，可随时重新启用。",
                confirmTitle: "停用成员",
                destructive: true
            )
            guard ok else { return }
        }
        do {
            replace(try await api.membersStatusSet(memberId: member.id, body: .init(enabled: enabling)))
            feedback.success(enabling ? "成员已启用" : "成员已停用")
        } catch {
            feedback.error("操作失败：\(error.localizedDescription)")
        }
    }

    private func remove(_ member: API.MemberView) async {
        let ok = await feedback.confirm(
            "删除成员「\(member.nickname)」？",
            message: "头像、播放进度等个人数据会被清理；订阅转由管理员接管，已下载内容不受影响。此操作不可恢复。",
            confirmTitle: "删除成员",
            destructive: true
        )
        guard ok else { return }
        do {
            try await api.membersDelete(memberId: member.id)
            if case var .loaded(rows) = members {
                rows.removeAll { $0.id == member.id }
                members = .loaded(rows)
            }
            feedback.success("成员已删除")
        } catch {
            feedback.error("删除失败：\(error.localizedDescription)")
        }
    }
}

/// 一次性明文密码结果（创建 / 重置）
struct SettingsMemberPasswordResult: Identifiable {
    let id = UUID()
    let title: String
    let username: String
    let password: String
}

// MARK: - 列表行

private struct MemberRow: View {
    let member: API.MemberView
    let libraries: [API.LibraryView]
    let onEdit: () -> Void
    let onResetPassword: () -> Void
    let onToggleStatus: () -> Void
    let onDelete: () -> Void

    /// 权限摘要；分级上限直接摆进摘要——这是「这个号是给谁用的」最要紧的一条
    private var permissionLabels: [String] {
        [
            member.allowSubscribe ? "订阅" : nil,
            member.allowSearch ? "搜索" : nil,
            member.allowDirectDownload ? "下载" : nil,
            member.contentAgeLimit.map { "\($0)+ 以下" },
        ].compactMap { $0 }
    }

    /// 「全部库」只自动包含对所有成员开放的库；「指定成员」的库要单独勾选，摘要里单独列出
    private var libraryScope: String {
        let visible = libraries.filter { member.libraryIds.contains($0.id) }.map(\.name)
        let granted = libraries.filter { $0.accessMode == "selected" && member.libraryIds.contains($0.id) }.map(\.name)
        if member.allLibraries {
            return granted.isEmpty ? "全部共享库" : "全部共享库 + 指定成员的库：\(granted.joined(separator: "、"))"
        }
        return visible.isEmpty ? "未分配媒体库" : visible.joined(separator: "、")
    }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            AvatarBadge(session: nil, avatarUrl: member.avatarUrl, nickname: member.nickname, size: 38)
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Text(member.nickname).font(.body.weight(.semibold)).lineLimit(1)
                    SettingsStatusDot(color: member.status == "active" ? Theme.success : Color.white.opacity(0.25), size: 6)
                        .accessibilityLabel(member.status == "active" ? "已启用" : "已停用")
                }
                Text("@\(member.username)").font(.caption).foregroundStyle(Theme.textFaint)
                if permissionLabels.isEmpty {
                    Text("仅浏览与播放").font(.caption).foregroundStyle(Theme.textFaint)
                } else {
                    DiscoverFlowLayout(spacing: 6, lineSpacing: 6) {
                        ForEach(permissionLabels, id: \.self) { label in
                            Text(label)
                                .font(.caption)
                                .foregroundStyle(.white.opacity(0.7))
                                .padding(.horizontal, 8)
                                .padding(.vertical, 3)
                                .background(Color.white.opacity(0.06), in: .rect(cornerRadius: 6))
                        }
                    }
                }
                Text(libraryScope).font(.subheadline).foregroundStyle(Theme.textMuted).lineLimit(2)
                Text(member.lastLoginAt.map { "最近活动 \(Formatters.relative($0))" } ?? "从未登录")
                    .font(.caption).foregroundStyle(Theme.textFaint)
            }
            Spacer(minLength: 4)
            Menu {
                Button("编辑成员", systemImage: "pencil", action: onEdit)
                Button("重置密码", systemImage: "key", action: onResetPassword)
                Divider()
                if member.status == "active" {
                    Button("停用成员", systemImage: "pause.circle", role: .destructive, action: onToggleStatus)
                } else {
                    Button("启用成员", systemImage: "play.circle", action: onToggleStatus)
                }
                Button("删除成员", systemImage: "trash", role: .destructive, action: onDelete)
            } label: {
                Image(systemName: "ellipsis")
                    .frame(width: 32, height: 32)
                    .contentShape(.rect)
            }
            .buttonStyle(.glass)
            .accessibilityLabel("\(member.nickname) 的更多操作")
            .accessibilityIdentifier("member-menu-\(member.username)")
        }
        .padding(.vertical, 4)
        .contentShape(.rect)
        .onTapGesture(perform: onEdit)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("member-row-\(member.username)")
    }
}

// MARK: - 弹层外壳

/// 设置弹层的统一外壳：导航栏左「取消」、右主操作（带忙碌态），内容是分组表单
struct SettingsSheetScaffold<Content: View>: View {
    let title: String
    var confirmTitle: String?
    var confirmDisabled = false
    var busy = false
    var cancelTitle = "取消"
    var confirmIdentifier = "sheet-confirm"
    var onConfirm: (() -> Void)?
    @ViewBuilder let content: () -> Content
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form { content() }
                .scrollDismissesKeyboard(.interactively)
                .navigationTitle(title)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button(cancelTitle) { dismiss() }
                            .accessibilityIdentifier("sheet-cancel")
                    }
                    if let confirmTitle, let onConfirm {
                        ToolbarItem(placement: .confirmationAction) {
                            if busy {
                                ProgressView()
                            } else {
                                Button(confirmTitle, action: onConfirm)
                                    .disabled(confirmDisabled)
                                    .accessibilityIdentifier(confirmIdentifier)
                            }
                        }
                    }
                }
        }
        .presentationBackground(Theme.background)
    }
}

// MARK: - 添加成员

/// 随机初始密码（前端生成，创建前即可复制；后端只存哈希）。去掉易混字符 0/O、1/l/I、8/B
private func generateMemberPassword() -> String {
    let alphabet = Array("abcdefghjkmnpqrstuvwxyzACDEFGHJKLMNPQRSTUVWXYZ2345679")
    return String((0 ..< 14).map { _ in alphabet[Int.random(in: 0 ..< alphabet.count)] })
}

private struct CreateMemberSheet: View {
    let onCreated: (API.MemberView, String) -> Void
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @State private var username = ""
    @State private var nickname = ""
    @State private var password = generateMemberPassword()
    @State private var busy = false

    var body: some View {
        SettingsSheetScaffold(
            title: "添加成员", confirmTitle: "创建成员", busy: busy,
            confirmIdentifier: "member-create-submit", onConfirm: { Task { await submit() } }
        ) {
            Section {
                LabeledTextField(label: "用户名", hint: "登录使用，创建后不可修改", placeholder: "如 yee", text: $username)
                    .accessibilityIdentifier("member-create-username")
                LabeledTextField(label: "昵称", hint: "页面展示名，可留空", placeholder: "如 小叶", text: $nickname)
                    .accessibilityIdentifier("member-create-nickname")
            } footer: {
                Text("新成员默认可以订阅和浏览全部媒体库，站点搜索默认关闭。")
            }
            Section {
                CredentialRow(label: "密码", value: password, mono: true)
                Button {
                    password = generateMemberPassword()
                } label: {
                    HStack {
                        Text("重新生成密码")
                        Spacer()
                        Text("换一个随机密码").font(.caption).foregroundStyle(Theme.textFaint)
                    }
                }
                .accessibilityIdentifier("member-create-regenerate")
            } header: {
                Text("初始密码")
            } footer: {
                Text("已随机生成。点击密码行即可复制，创建成功后还会显示一次。")
            }
        }
    }

    private func submit() async {
        let name = username.trimmingCharacters(in: .whitespaces)
        if name.count < 3 { return feedback.error("用户名至少 3 个字符") }
        if password.count < 8 { return feedback.error("密码至少 8 位") }
        busy = true
        do {
            let nick = nickname.trimmingCharacters(in: .whitespaces)
            let member = try await api.membersCreate(body: .init(username: name, password: password, nickname: nick))
            onCreated(member, password)
        } catch {
            feedback.error("创建失败：\(error.localizedDescription)")
            busy = false
        }
    }
}

private struct LabeledTextField: View {
    let label: String
    let hint: String
    let placeholder: String
    @Binding var text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(label).font(.subheadline.weight(.medium))
                Text(hint).font(.caption).foregroundStyle(Theme.textFaint)
            }
            TextField(placeholder, text: $text)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
        }
    }
}

/// 可点击复制的凭据行（Web CredentialRow）
private struct CredentialRow: View {
    let label: String
    let value: String
    var mono = false
    @Environment(Feedback.self) private var feedback

    var body: some View {
        Button {
            SettingsClipboard.copy(value, feedback: feedback, message: "\(label)已复制")
        } label: {
            HStack(spacing: 14) {
                Text(label).font(.caption).foregroundStyle(Theme.textFaint).frame(width: 34, alignment: .leading)
                Text(value)
                    .font(mono ? .body.monospaced() : .body)
                    .foregroundStyle(Theme.text)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Text("点击复制").font(.caption).foregroundStyle(Theme.textFaint)
            }
        }
        .accessibilityIdentifier("credential-\(label)")
    }
}

// MARK: - 编辑成员

/// 年龄上限档位：对着家里真实的年龄段，不让用户猜「填几」
private let ageLimits: [(value: Int?, label: String)] = [
    (nil, "不限"), (6, "6 岁以下"), (12, "12 岁以下"), (16, "16 岁以下"), (18, "18 岁以下"),
]

private struct EditMemberSheet: View {
    let member: API.MemberView
    let libraries: [API.LibraryView]
    let sites: [API.CatalogItem]
    let onSaved: (API.MemberView) -> Void
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback

    @State private var nickname: String
    @State private var allowSubscribe: Bool
    @State private var allowSearch: Bool
    @State private var allowDirectDownload: Bool
    @State private var allLibraries: Bool
    @State private var libraryIds: [Int]
    @State private var allSites: Bool
    @State private var siteIds: [String]
    @State private var ageLimit: Int?
    @State private var allowUnrated: Bool
    @State private var busy = false

    init(member: API.MemberView, libraries: [API.LibraryView], sites: [API.CatalogItem], onSaved: @escaping (API.MemberView) -> Void) {
        self.member = member
        self.libraries = libraries
        self.sites = sites
        self.onSaved = onSaved
        _nickname = State(initialValue: member.nickname)
        _allowSubscribe = State(initialValue: member.allowSubscribe)
        _allowSearch = State(initialValue: member.allowSearch)
        _allowDirectDownload = State(initialValue: member.allowDirectDownload)
        _allLibraries = State(initialValue: member.allLibraries)
        _libraryIds = State(initialValue: member.libraryIds)
        _allSites = State(initialValue: member.allSites)
        _siteIds = State(initialValue: member.siteIds)
        _ageLimit = State(initialValue: member.contentAgeLimit)
        _allowUnrated = State(initialValue: member.allowUnrated)
    }

    /// 「指定成员」的库：成员即使是「全部库」也要单独勾选才可见
    private var selectedModeIds: [Int] { libraries.filter { $0.accessMode == "selected" }.map(\.id) }

    var body: some View {
        SettingsSheetScaffold(
            title: "编辑成员", confirmTitle: "保存设置", busy: busy,
            confirmIdentifier: "member-edit-save", onConfirm: { Task { await save() } }
        ) {
            Section {
                HStack(spacing: 12) {
                    AvatarBadge(session: nil, avatarUrl: member.avatarUrl, nickname: member.nickname, size: 44)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(member.nickname).font(.headline)
                        Text("@\(member.username)").font(.subheadline).foregroundStyle(Theme.textMuted)
                    }
                    Spacer()
                    Text(member.status == "active" ? "已启用" : "已停用")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(member.status == "active" ? Theme.success : Theme.textFaint)
                        .padding(.horizontal, 10).padding(.vertical, 4)
                        .background((member.status == "active" ? Theme.success : Color.white).opacity(0.1), in: .capsule)
                }
            }
            Section("基本信息") {
                TextField("昵称", text: $nickname)
                    .accessibilityIdentifier("member-edit-nickname")
            }
            Section {
                Toggle(isOn: $allowSubscribe) { SettingsRowText(title: "订阅追踪", detail: "发起订阅并管理自己的订阅") }
                    .accessibilityIdentifier("member-allow-subscribe")
                Toggle(isOn: Binding(get: { allowSearch }, set: { allowSearch = $0; if !$0 { allowDirectDownload = false } })) {
                    SettingsRowText(title: "站点搜索", detail: "搜索被分配的 PT 站点资源")
                }
                .accessibilityIdentifier("member-allow-search")
                Toggle(isOn: $allowDirectDownload) { SettingsRowText(title: "一键下载", detail: "从搜索结果直接提交下载，依赖站点搜索") }
                    .disabled(!allowSearch)
                    .accessibilityIdentifier("member-allow-download")
            } header: {
                Text("功能权限")
            } footer: {
                Text("关闭后相关入口会从成员页面隐藏，后端同时拒绝调用。")
            }
            Section {
                DiscoverFlowLayout(spacing: 6, lineSpacing: 6) {
                    ForEach(ageLimits, id: \.label) { option in
                        MemberChip(label: option.label, on: ageLimit == option.value) { ageLimit = option.value }
                    }
                }
                .padding(.vertical, 4)
                if ageLimit != nil {
                    Toggle(isOn: $allowUnrated) {
                        SettingsRowText(title: "未分级的作品也给看",
                                        detail: "大量中文影片在 TMDB 上没有分级信息。默认一并隐藏——「我不确定的一律不给看」更稳妥；打开之后这些片会出现在这个成员面前。")
                    }
                }
            } header: {
                Text("内容分级")
            } footer: {
                Text("给孩子用的档案设一个年龄上限：超过这个分级的作品在海报墙、搜索、合集、播放器和详情页都看不到，直接改地址栏也进不去。")
            }
            Section {
                AccessPicker(
                    all: $allLibraries, allLabel: "全部媒体库", limitedLabel: "指定媒体库",
                    options: libraries.map { ($0.id, $0.accessMode == "selected" ? "\($0.name) · 指定成员" : $0.name) },
                    selected: $libraryIds
                )
                if allLibraries, !selectedModeIds.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("指定成员的库（需单独勾选）").font(.caption).foregroundStyle(Theme.textFaint)
                        DiscoverFlowLayout(spacing: 8, lineSpacing: 8) {
                            ForEach(libraries.filter { $0.accessMode == "selected" }, id: \.id) { library in
                                MemberChip(label: library.name, on: libraryIds.contains(library.id)) {
                                    libraryIds = libraryIds.contains(library.id) ? libraryIds.filter { $0 != library.id } : libraryIds + [library.id]
                                }
                            }
                        }
                    }
                }
            } header: {
                Text("媒体库范围")
            } footer: {
                Text("「全部媒体库」自动包含对所有成员开放的库，含以后新建的；「指定成员」的库需要单独勾选。")
            }
            if allowSearch {
                Section("可搜索站点") {
                    AccessPicker(
                        all: $allSites, allLabel: "全部启用站点", limitedLabel: "指定站点",
                        options: sites.map { ($0.siteId, $0.displayName) },
                        selected: $siteIds
                    )
                }
            }
        }
    }

    private func save() async {
        busy = true
        let body = API.MemberUpdateRequest(
            nickname: nickname.trimmingCharacters(in: .whitespaces),
            allowSubscribe: allowSubscribe,
            allowSearch: allowSearch,
            allowDirectDownload: allowSearch && allowDirectDownload,
            allLibraries: allLibraries,
            // 「全部库」时只保留「指定成员」库的显式授权，不能当白名单一起清掉
            libraryIds: allLibraries ? libraryIds.filter { selectedModeIds.contains($0) } : libraryIds,
            allSites: allSites,
            siteIds: allSites ? [] : siteIds,
            // -1 = 取消上限；不传是「不改动」，两者在协议上必须分开
            contentAgeLimit: ageLimit ?? -1,
            allowUnrated: allowUnrated
        )
        do {
            onSaved(try await api.membersUpdate(memberId: member.id, body: body))
        } catch {
            feedback.error("保存失败：\(error.localizedDescription)")
            busy = false
        }
    }
}

/// 全部 / 指定 两态 + 多选 chips（Web AccessPicker）
private struct AccessPicker<ID: Hashable>: View {
    @Binding var all: Bool
    let allLabel: String
    let limitedLabel: String
    let options: [(ID, String)]
    @Binding var selected: [ID]

    var body: some View {
        Picker(allLabel, selection: $all) {
            Text(allLabel).tag(true)
            Text(limitedLabel).tag(false)
        }
        .pickerStyle(.segmented)
        if !all {
            if options.isEmpty {
                Text("暂无可选项").font(.caption).foregroundStyle(Theme.textFaint)
            } else {
                DiscoverFlowLayout(spacing: 8, lineSpacing: 8) {
                    ForEach(options, id: \.0) { id, label in
                        MemberChip(label: label, on: selected.contains(id)) {
                            selected = selected.contains(id) ? selected.filter { $0 != id } : selected + [id]
                        }
                    }
                }
                .padding(.vertical, 4)
            }
        }
    }
}

private struct MemberChip: View {
    let label: String
    let on: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(label)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(on ? Theme.accent : Theme.textMuted)
                .padding(.horizontal, 12)
                .padding(.vertical, 7)
                .background(on ? Theme.accentSoft : Color.white.opacity(0.035), in: .rect(cornerRadius: 9))
                .overlay(RoundedRectangle(cornerRadius: 9).strokeBorder(on ? Theme.accent.opacity(0.5) : Color.white.opacity(0.09)))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}

// MARK: - 一次性密码结果

private struct PasswordResultSheet: View {
    let result: SettingsMemberPasswordResult
    @Environment(\.dismiss) private var dismiss
    @Environment(Feedback.self) private var feedback

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    CredentialRow(label: "账号", value: result.username)
                    CredentialRow(label: "密码", value: result.password, mono: true)
                    Button("复制全部登录信息", systemImage: "doc.on.doc") {
                        SettingsClipboard.copy("账号：\(result.username)\n密码：\(result.password)", feedback: feedback, message: "账号和密码已复制")
                    }
                    .accessibilityIdentifier("credential-copy-all")
                } footer: {
                    Text("密码只在这里显示一次。点击账号或密码即可复制，关闭弹窗后无法再次查看。")
                }
            }
            .navigationTitle(result.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") { dismiss() }.accessibilityIdentifier("password-result-done")
                }
            }
        }
        .presentationDetents([.medium, .large])
        .presentationBackground(Theme.background)
    }
}
