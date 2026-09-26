import SwiftUI

/// 设置 → Webhook（对应 Web `webhook-section.tsx`，docs/design/webhook.md §4.3，Stripe 式管理页）。
///
/// 三层结构：
/// - 总开关 + 推送目标列表：一行一个 endpoint，状态点显示最近一次投递结果，行内给「发送测试 / 记录 / 编辑」与启用开关；
/// - 编辑弹层（新建与编辑共用）：显示名、URL、外发格式、订阅事件（按事件目录分组，目录随 GET 下发，
///   后端新增领域事件时前端零改动）、Jellyfin 模板与附加请求头、网络出口；编辑态带密钥轮换与删除；
/// - 一次性密钥展示条：新建 / 轮换后后端只在那一次响应里给明文，必须立刻展示并提示复制保存。
///
/// **保存是全量的**：`PUT /webhook` 一次提交总开关与全部 endpoint，任何增删改都把其余 endpoint 原样带上
/// （`payload(_:)` 只丢只读字段），响应即最新视图——与 Web 同一条路，避免两端各自拼出不同的配置。
struct WebhookSettingsView: View {
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @State private var config: API.WebhookConfigView?
    @State private var loadError: String?
    @State private var error: String?
    @State private var busy = false
    /// 编辑中的草稿；nil = 未打开
    @State private var draft: SettingsBWebhookDraft?
    /// 一次性密钥：(endpoint 名, 明文)；关闭即永远消失
    @State private var revealed: (name: String, secret: String)?
    /// 展开投递记录的 endpoint id 及其记录
    @State private var expanded: String?
    @State private var records: [API.DeliveryView] = []

    var body: some View {
        Group {
            if let config {
                content(config)
            } else if let loadError {
                ErrorState(message: loadError) { await load() }
            } else {
                ProgressView().controlSize(.large).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .appBackground()
        .task { await load() }
        .sheet(item: $draft) { initial in
            SettingsBWebhookEditor(
                initial: initial,
                catalog: config?.catalog ?? [],
                secretMasked: config?.endpoints.first { $0.id == initial.id }?.secretMasked ?? "",
                onSubmit: submit,
                onRotate: { id in try await rotate(id) },
                onDelete: { id in await remove(id) }
            )
            .sheetFeedback()
        }
    }

    private func content(_ config: API.WebhookConfigView) -> some View {
        Form {
            Section {
                SettingsBIntro(text: "播放、收藏等事件发生后，MovieClaw 会向下面配置的地址推送 JSON（自有协议带 HMAC-SHA256 签名，头 X-MovieClaw-Signature），供 Home Assistant、观影记录等外部服务实时订阅。")
                if let error {
                    SettingsBNotice(text: error, tone: .danger)
                }
            }

            if let revealed {
                Section {
                    Text("「\(revealed.name)」的签名密钥（仅显示这一次，请立即保存）")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Theme.success)
                    Text(revealed.secret)
                        .font(.footnote.monospaced())
                        .textSelection(.enabled)
                        .accessibilityIdentifier("webhook-secret")
                    Button("复制", systemImage: "doc.on.doc") {
                        UIPasteboard.general.string = revealed.secret
                        feedback.success("已复制")
                    }
                    Button("我已保存，关闭") { self.revealed = nil }
                        .accessibilityIdentifier("webhook-secret-dismiss")
                }
            }

            Section {
                Toggle(isOn: Binding(get: { config.enabled }, set: { value in Task { await toggleGlobal(value) } })) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("启用事件推送")
                        Text("关闭后所有 endpoint 都不再收到事件").font(.caption).foregroundStyle(Theme.textFaint)
                    }
                }
                .disabled(busy)
                .accessibilityIdentifier("webhook-global-toggle")
            }

            Section {
                if config.endpoints.isEmpty {
                    VStack(spacing: 10) {
                        Image(systemName: "paperplane").font(.title2).foregroundStyle(Theme.accent)
                        Text("还没有推送目标").font(.body.weight(.medium))
                        Text("点击右上角「新增 Endpoint」，把播放事件推给你的自动化服务。")
                            .font(.footnote).foregroundStyle(Theme.textMuted).multilineTextAlignment(.center)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 20)
                } else {
                    ForEach(config.endpoints, id: \.id) { endpoint in
                        row(endpoint)
                        if expanded == endpoint.id {
                            deliveries
                        }
                    }
                }
            } header: {
                HStack {
                    Text(config.endpoints.isEmpty ? "还没有配置推送目标。" : "已配置 \(config.endpoints.count) 个推送目标。")
                    Spacer()
                    Button("新增 Endpoint", systemImage: "plus") {
                        draft = SettingsBWebhookDraft.empty(config.catalog)
                    }
                    .font(.footnote.weight(.semibold))
                    .discoverProminentButton()
                    .disabled(busy)
                    .textCase(nil)
                    .accessibilityIdentifier("webhook-create")
                }
            }
        }
        .settingsBFormStyle()
        .refreshable { await load() }
    }

    // MARK: 行

    private func row(_ endpoint: API.WebhookEndpointView) -> some View {
        let status = status(of: endpoint)
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 8) {
                        Text(endpoint.name.isEmpty ? endpoint.url : endpoint.name)
                            .font(.body.weight(.semibold)).lineLimit(1)
                        SettingsBBadge(text: endpoint.format == "movieclaw" ? "自有协议" : "Jellyfin 兼容")
                    }
                    HStack(spacing: 6) {
                        SettingsBDot(tone: status.tone, size: 6)
                        Text("\(status.label) · \(endpoint.url)")
                            .font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1).truncationMode(.middle)
                    }
                }
                Spacer(minLength: 4)
                Toggle("启用 \(endpoint.name.isEmpty ? endpoint.url : endpoint.name)", isOn: Binding(
                    get: { endpoint.enabled },
                    set: { value in Task { await toggleEndpoint(endpoint.id, value) } }
                ))
                .labelsHidden()
                .disabled(busy)
                .accessibilityIdentifier("webhook-enabled-\(endpoint.name)")
            }
            HStack(spacing: 8) {
                SettingsBAsyncButton("发送测试") { await test(endpoint) }
                    .disabled(busy)
                    .accessibilityIdentifier("webhook-test-\(endpoint.name)")
                Button(expanded == endpoint.id ? "收起记录" : "记录") { Task { await toggleRecords(endpoint.id) } }
                    .accessibilityIdentifier("webhook-records-\(endpoint.name)")
                Button("编辑") { draft = SettingsBWebhookDraft(endpoint) }
                    .accessibilityIdentifier("webhook-edit-\(endpoint.name)")
            }
            .font(.footnote.weight(.medium))
            .buttonStyle(.glass)
        }
        .padding(.vertical, 4)
    }

    private func status(of endpoint: API.WebhookEndpointView) -> (label: String, tone: SettingsBTone) {
        guard endpoint.enabled else { return ("已停用", .neutral) }
        guard let last = endpoint.lastDelivery else { return ("未投递过", .neutral) }
        let when = Formatters.relative(last.at)
        return last.ok ? ("投递成功 · \(when)", .ok) : ("投递失败 · \(when)", .danger)
    }

    /// 最近投递记录（内存环形缓冲，重启清空）
    @ViewBuilder
    private var deliveries: some View {
        if records.isEmpty {
            Text("还没有投递记录（重启后记录会清空）。").font(.caption).foregroundStyle(Theme.textFaint)
        } else {
            ForEach(records, id: \.self) { r in
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        SettingsBDot(tone: r.ok ? .ok : .danger, size: 6)
                        Text(r.event).font(.caption.monospaced())
                        Spacer()
                        Text(Formatters.relative(r.at)).font(.caption).foregroundStyle(Theme.textFaint)
                    }
                    Text("\(r.statusCode.map { "HTTP \($0)" } ?? "未送达") · \(r.durationMs)ms · 尝试 \(r.attempts) 次")
                        .font(.caption).foregroundStyle(Theme.textMuted)
                    if !r.error.isEmpty {
                        Text(r.error).font(.caption).foregroundStyle(Theme.danger).lineLimit(2)
                    }
                }
            }
        }
    }

    // MARK: 数据

    private func load() async {
        do {
            config = try await api.webhookShow()
            loadError = nil
        } catch is CancellationError {
        } catch {
            if config == nil { loadError = error.localizedDescription } else { self.error = error.localizedDescription }
        }
    }

    /// 视图 → 保存载荷（丢掉 secret_masked / last_delivery 等只读字段）
    private func payload(_ ep: API.WebhookEndpointView) -> API.WebhookEndpointPayload {
        .init(id: ep.id, name: ep.name, url: ep.url, format: ep.format, enabled: ep.enabled,
              events: ep.events, template: ep.template, headers: ep.headers, egressScope: ep.egressScope)
    }

    private var currentPayloads: [API.WebhookEndpointPayload] {
        (config?.endpoints ?? []).map(payload)
    }

    /// 全量保存：任何变更（开关 / 增删改）都走这一条路，响应即最新视图
    @discardableResult
    private func save(enabled: Bool, endpoints: [API.WebhookEndpointPayload]) async -> API.WebhookConfigView? {
        busy = true
        error = nil
        defer { busy = false }
        do {
            let view = try await api.webhookSet(body: .init(enabled: enabled, endpoints: endpoints))
            config = view
            return view
        } catch {
            self.error = error.localizedDescription
            return nil
        }
    }

    private func toggleGlobal(_ enabled: Bool) async {
        guard let config else { return }
        let previous = config
        self.config?.enabled = enabled // 乐观更新
        if await save(enabled: enabled, endpoints: currentPayloads) == nil {
            self.config = previous // 失败回滚，避免开关停在与后端不一致的状态
        }
    }

    private func toggleEndpoint(_ id: String, _ enabled: Bool) async {
        guard let config else { return }
        await save(enabled: config.enabled, endpoints: currentPayloads.map { p in
            var p = p
            if p.id == id { p.enabled = enabled }
            return p
        })
    }

    /// 编辑弹层提交；成功返回 nil，失败返回错误文案（弹层内展示）
    private func submit(_ draft: SettingsBWebhookDraft) async -> String? {
        guard let config else { return "配置尚未加载" }
        let url = draft.url.trimmingCharacters(in: .whitespaces)
        if url.isEmpty { return "请填写目标地址" }
        var next = draft.payload
        next.url = url
        next.name = draft.name.trimmingCharacters(in: .whitespaces)
        let rest = currentPayloads.filter { $0.id != draft.id }
        guard let view = await save(enabled: config.enabled, endpoints: rest + [next]) else {
            return error ?? "保存失败"
        }
        error = nil
        // 响应里带明文 secret 的两种来路都要展示：新建 movieclaw endpoint，
        // 以及既有 endpoint 从 jellyfin 切换到 movieclaw（后端补发密钥）
        if let ep = view.endpoints.first(where: { $0.secret?.isEmpty == false }), let secret = ep.secret {
            revealed = (ep.name.isEmpty ? ep.url : ep.name, secret)
        }
        return nil
    }

    /// 轮换密钥（确认框由编辑弹层自己弹）：返回新明文，编辑弹层就地展示；关窗后列表顶部仍保留展示条
    private func rotate(_ id: String) async throws -> String? {
        busy = true
        defer { busy = false }
        let rotated = try await api.webhookRotateSecret(endpointId: id)
        if let secret = rotated.secret { revealed = (rotated.name.isEmpty ? rotated.url : rotated.name, secret) }
        await load()
        return rotated.secret
    }

    /// 删除：确认框在编辑弹层里弹（sheet 盖住根部时根部 alert 弹不出），确认后关弹层并全量保存
    private func remove(_ id: String) async -> Bool {
        guard let config, let ep = config.endpoints.first(where: { $0.id == id }) else { return false }
        return await save(enabled: config.enabled, endpoints: currentPayloads.filter { $0.id != ep.id }) != nil
    }

    private func test(_ endpoint: API.WebhookEndpointView) async {
        busy = true
        defer { busy = false }
        do {
            let result = try await api.webhookTest(endpointId: endpoint.id)
            if result.ok {
                feedback.success("测试事件已送达（HTTP \(result.statusCode.map(String.init) ?? "-")，\(result.durationMs)ms）")
            } else {
                feedback.error(result.error.isEmpty ? "测试投递失败" : result.error)
            }
            await load()
        } catch {
            feedback.error(error)
        }
    }

    private func toggleRecords(_ id: String) async {
        if expanded == id {
            expanded = nil
            return
        }
        do {
            records = try await api.webhookDeliveries(endpointId: id)
            expanded = id
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: - 草稿

/// 编辑中的 endpoint（新建与编辑共用）；请求头用「Key: Value」每行一条的文本形态编辑
struct SettingsBWebhookDraft: Identifiable {
    var id: String
    var name: String
    var url: String
    var format: String
    var enabled: Bool
    var events: [String]
    var template: String
    var headersText: String
    var egressScope: String

    var isNew: Bool { id.isEmpty }

    init(_ ep: API.WebhookEndpointView) {
        id = ep.id
        name = ep.name
        url = ep.url
        format = ep.format
        enabled = ep.enabled
        events = ep.events
        template = ep.template
        headersText = ep.headers.sorted { $0.key < $1.key }.map { "\($0.key): \($0.value)" }.joined(separator: "\n")
        egressScope = ep.egressScope
    }

    private init(catalog: [API.EventCatalogEntry]) {
        id = ""
        name = ""
        url = ""
        format = "movieclaw"
        enabled = true
        // 默认勾选 default_on 的事件：progress 这类高频事件按目录约定默认关闭
        events = catalog.filter(\.defaultOn).map(\.event)
        template = ""
        headersText = ""
        egressScope = "lan"
    }

    static func empty(_ catalog: [API.EventCatalogEntry]) -> Self { Self(catalog: catalog) }

    var headers: [String: String] {
        var out: [String: String] = [:]
        for line in headersText.split(separator: "\n", omittingEmptySubsequences: true) {
            guard let idx = line.firstIndex(of: ":"), idx > line.startIndex else { continue }
            let key = line[..<idx].trimmingCharacters(in: .whitespaces)
            let value = line[line.index(after: idx)...].trimmingCharacters(in: .whitespaces)
            if !key.isEmpty { out[key] = value }
        }
        return out
    }

    var payload: API.WebhookEndpointPayload {
        .init(id: id, name: name, url: url, format: format, enabled: enabled, events: events,
              template: template, headers: headers, egressScope: egressScope)
    }
}

// MARK: - 编辑弹层

/// 新增 / 编辑 Endpoint。编辑态附带签名密钥（打码值 + 轮换）与删除入口。
private struct SettingsBWebhookEditor: View {
    let initial: SettingsBWebhookDraft
    let catalog: [API.EventCatalogEntry]
    let secretMasked: String
    let onSubmit: (SettingsBWebhookDraft) async -> String?
    let onRotate: (String) async throws -> String?
    let onDelete: (String) async -> Bool

    @Environment(\.dismiss) private var dismiss
    @Environment(Feedback.self) private var feedback
    @State private var draft: SettingsBWebhookDraft
    @State private var busy = false
    @State private var error: String?
    /// 刚轮换出的新明文（仅本次展示）
    @State private var rotatedSecret: String?

    init(initial: SettingsBWebhookDraft, catalog: [API.EventCatalogEntry], secretMasked: String,
         onSubmit: @escaping (SettingsBWebhookDraft) async -> String?,
         onRotate: @escaping (String) async throws -> String?,
         onDelete: @escaping (String) async -> Bool) {
        self.initial = initial
        self.catalog = catalog
        self.secretMasked = secretMasked
        self.onSubmit = onSubmit
        self.onRotate = onRotate
        self.onDelete = onDelete
        _draft = State(initialValue: initial)
    }

    /// 事件目录按 group 分组，组内顺序即目录顺序
    private var groups: [(name: String, entries: [API.EventCatalogEntry])] {
        var order: [String] = []
        var map: [String: [API.EventCatalogEntry]] = [:]
        for entry in catalog {
            if map[entry.group] == nil { order.append(entry.group) }
            map[entry.group, default: []].append(entry)
        }
        return order.map { ($0, map[$0] ?? []) }
    }

    private func selectable(_ entry: API.EventCatalogEntry) -> Bool {
        draft.format != "jellyfin" || entry.jellyfinSupported
    }

    var body: some View {
        NavigationStack {
            Form {
                if let error {
                    Section { SettingsBNotice(text: error, tone: .danger) }
                }
                Section {
                    SettingsBTextField(label: "显示名", text: $draft.name, placeholder: "如 Home Assistant", identifier: "webhook-name")
                    SettingsBTextField(label: "目标地址", text: $draft.url, placeholder: "http://192.168.1.10:8123/api/webhook/xxx",
                                       mono: true, keyboard: .URL, identifier: "webhook-url")
                    Toggle("启用", isOn: $draft.enabled)
                        .accessibilityIdentifier("webhook-draft-enabled")
                }

                Section {
                    Picker("外发格式", selection: Binding(get: { draft.format }, set: setFormat)) {
                        Text("自有协议（HMAC 签名）").tag("movieclaw")
                        Text("Jellyfin 兼容（模板渲染）").tag("jellyfin")
                    }
                    .pickerStyle(.inline)
                    .labelsHidden()
                } header: {
                    Text("外发格式")
                } footer: {
                    if draft.format == "jellyfin" {
                        Text("与 Jellyfin Webhook 插件同一套模板变量：下游文档里「贴进 Jellyfin 插件」的模板可直接贴到下方，实现免适配接入。此格式不签名，鉴权用附加请求头。")
                    }
                }

                if draft.format == "jellyfin" {
                    Section("Handlebars 模板（支持 {{Var}} 与 if_equals / if_exist / link_to / url_encode / json_encode）") {
                        TextField("{\n  \"event\": \"{{NotificationType}}\",\n  \"title\": {{json_encode Name}}\n}", text: $draft.template, axis: .vertical)
                            .font(.footnote.monospaced())
                            .lineLimit(6...14)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                    }
                    Section("附加请求头（每行一条，如 Authorization: Bearer xxx；可留空）") {
                        TextField("", text: $draft.headersText, axis: .vertical)
                            .font(.footnote.monospaced())
                            .lineLimit(2...6)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                    }
                }

                ForEach(groups, id: \.name) { group in
                    let usable = group.entries.filter(selectable)
                    let allOn = !usable.isEmpty && usable.allSatisfy { draft.events.contains($0.event) }
                    Section {
                        ForEach(group.entries, id: \.event) { entry in
                            let enabled = selectable(entry)
                            Toggle(isOn: Binding(
                                get: { enabled && draft.events.contains(entry.event) },
                                set: { on in toggleEvent(entry.event, on) }
                            )) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(entry.label)
                                    Text(enabled ? entry.event : "\(entry.event) · 没有 Jellyfin 对应物，仅自有协议可订阅")
                                        .font(.caption.monospaced()).foregroundStyle(Theme.textFaint)
                                }
                            }
                            .disabled(!enabled)
                        }
                    } header: {
                        HStack {
                            Text(group.name)
                            Spacer()
                            Button(allOn ? "全不选" : "全选") { toggleGroup(group.entries, !allOn) }
                                .font(.footnote)
                                .textCase(nil)
                                .disabled(usable.isEmpty)
                        }
                    }
                }

                Section("网络出口") {
                    Picker("网络出口", selection: $draft.egressScope) {
                        Text("内网直连（默认）").tag("lan")
                        Text("跟随代理配置").tag("wan")
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                }

                if !draft.isNew && draft.format == "movieclaw" {
                    Section {
                        SettingsBValueRow(label: "签名密钥", value: secretMasked.isEmpty ? "（无）" : secretMasked, mono: true)
                        if let rotatedSecret {
                            VStack(alignment: .leading, spacing: 6) {
                                Text("新的签名密钥（仅显示这一次，请立即保存）")
                                    .font(.footnote.weight(.medium)).foregroundStyle(Theme.success)
                                Text(rotatedSecret).font(.footnote.monospaced()).textSelection(.enabled)
                                Button("复制", systemImage: "doc.on.doc") {
                                    UIPasteboard.general.string = rotatedSecret
                                    feedback.success("已复制")
                                }
                                .buttonStyle(.borderless)
                            }
                        }
                        SettingsBAsyncButton("轮换密钥") { await rotate() }
                            .disabled(busy)
                    } footer: {
                        Text("明文仅在创建时展示过一次，丢失只能轮换")
                    }
                }

                if !draft.isNew {
                    Section {
                        Button("删除", role: .destructive) { Task { await delete() } }
                            .disabled(busy)
                            .accessibilityIdentifier("webhook-delete")
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .navigationTitle(draft.isNew ? "新增 Endpoint" : "编辑 Endpoint")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消", systemImage: "xmark") { dismiss() }
                        .accessibilityIdentifier("sheet-close")
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button {
                        Task { await save() }
                    } label: {
                        if busy { ProgressView() } else { Text("保存") }
                    }
                    .disabled(busy)
                    .accessibilityIdentifier("webhook-save")
                }
            }
        }
        .presentationBackground(.regularMaterial)
    }

    private func setFormat(_ format: String) {
        guard format != draft.format else { return }
        // 切到 jellyfin 时剔除没有 Jellyfin 对应物的已选事件
        if format == "jellyfin" {
            draft.events = draft.events.filter { e in catalog.first { $0.event == e }?.jellyfinSupported == true }
        }
        draft.format = format
    }

    private func toggleEvent(_ event: String, _ on: Bool) {
        if on {
            if !draft.events.contains(event) { draft.events.append(event) }
        } else {
            draft.events.removeAll { $0 == event }
        }
    }

    private func toggleGroup(_ entries: [API.EventCatalogEntry], _ on: Bool) {
        let ids = entries.filter(selectable).map(\.event)
        if on {
            for id in ids where !draft.events.contains(id) { draft.events.append(id) }
        } else {
            draft.events.removeAll { ids.contains($0) }
        }
    }

    private func save() async {
        busy = true
        defer { busy = false }
        if let message = await onSubmit(draft) {
            error = message
        } else {
            dismiss()
        }
    }

    private func rotate() async {
        guard await feedback.confirm("轮换签名密钥？", message: "旧密钥立刻作废，下游需要更新为新密钥后才能继续验签。", confirmTitle: "轮换", destructive: true) else { return }
        do {
            rotatedSecret = try await onRotate(draft.id)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func delete() async {
        let name = draft.name.isEmpty ? draft.url : draft.name
        guard await feedback.confirm("删除「\(name)」？", message: "删除后该地址不再收到任何事件，签名密钥同时作废。", confirmTitle: "删除", destructive: true) else { return }
        busy = true
        defer { busy = false }
        if await onDelete(draft.id) { dismiss() } else { error = "删除失败，请稍后重试" }
    }
}
