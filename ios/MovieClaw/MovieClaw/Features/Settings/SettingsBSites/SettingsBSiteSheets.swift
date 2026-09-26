import SwiftUI

// 资源站点的三个弹层：添加站点、编辑授权、刷流设置（对应 Web AddSitePanel / SiteForm / BoostSettingsModal）。
// Web 的「添加」「编辑授权」是就地展开的面板，手机上改成 sheet：表单字段多（Cookie 是长文本），
// 在列表行里展开会把整页推得很长，也和系统键盘避让打架。
// 所有弹层里的确认框都用弹层自己环境里的 Feedback（`.sheetFeedback()` 提供），否则会被 sheet 盖住。

// MARK: - 授权表单

/// 授权表单（对应 Web `SiteForm`）：按目录声明渲染授权方式切换与必填字段。
/// 编辑时出于安全后端不回传敏感值，字段一律留空、占位提示「出于安全，请重新填写」。
struct SettingsBSiteAuthForm: View {
    let item: API.CatalogItem
    let isEdit: Bool
    @Binding var authType: String
    @Binding var values: [String: String]

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            // 授权方式（多于一种时才展示）
            if item.supportedAuthTypes.count > 1 {
                VStack(alignment: .leading, spacing: 6) {
                    Text("授权方式").font(.subheadline).foregroundStyle(Theme.textMuted)
                    Picker("授权方式", selection: $authType) {
                        ForEach(item.supportedAuthTypes, id: \.authType) { opt in
                            Text(SettingsBSiteText.authType(opt.authType)).tag(opt.authType)
                        }
                    }
                    .pickerStyle(.segmented)
                    .accessibilityIdentifier("site-auth-type")
                }
            }
            ForEach(Self.fields(item, authType), id: \.self) { field in
                fieldView(field)
            }
        }
    }

    /// 当前授权方式要填的字段（找不到时退回第一项，同 Web）
    static func fields(_ item: API.CatalogItem, _ authType: String) -> [String] {
        (item.supportedAuthTypes.first { $0.authType == authType } ?? item.supportedAuthTypes.first)?.requiredFields ?? []
    }

    /// 全部必填字段已填才可提交
    static func canSubmit(_ item: API.CatalogItem, _ authType: String, _ values: [String: String]) -> Bool {
        let fields = fields(item, authType)
        return !fields.isEmpty && fields.allSatisfy { !(values[$0] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }

    @ViewBuilder
    private func fieldView(_ field: String) -> some View {
        let meta = SettingsBSiteText.field(field)
        let binding = Binding(get: { values[field] ?? "" }, set: { values[field] = $0 })
        let placeholder = isEdit ? "出于安全，请重新填写" : ""
        VStack(alignment: .leading, spacing: 6) {
            Text(meta.label).font(.subheadline).foregroundStyle(Theme.textMuted)
            // Cookie 恰是插件的用武之地：就地提一句，不打断手动粘贴的用户
            if field == "cookie" {
                Text("手动粘贴的 Cookie 过期后需重填；推荐用本页下方的 MovieClaw 浏览器插件自动同步。")
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Group {
                switch meta.kind {
                case .textarea:
                    TextField(placeholder, text: binding, axis: .vertical)
                        .lineLimit(3...6)
                        .font(.footnote.monospaced())
                case .password:
                    SecureField(placeholder, text: binding)
                case .text:
                    TextField(placeholder, text: binding)
                }
            }
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .textContentType(meta.kind == .password ? .oneTimeCode : nil)
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .background(Color.white.opacity(0.05), in: .rect(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.08)))
            .accessibilityIdentifier("site-field-\(field)")
        }
    }

    /// 组装提交体里的授权字段（只带当前授权方式要求的字段，去首尾空白）
    static func trimmed(_ field: String, _ item: API.CatalogItem, _ authType: String, _ values: [String: String]) -> String? {
        guard fields(item, authType).contains(field) else { return nil }
        return (values[field] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

// MARK: - 添加站点

/// 添加站点（对应 Web `AddSitePanel`）：先选站点（带搜索，只列目录里还没接入的），再填授权表单。
/// 保存后后端异步验证，新站点以「待验证」出现在列表末尾，页面的 2.5 秒轮询会跟进到验证结果。
struct SettingsBSiteAddSheet: View {
    let available: [API.CatalogItem]
    let onCreated: (API.ConfiguredSite) -> Void

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var selected: API.CatalogItem?
    @State private var authType = ""
    @State private var values: [String: String] = [:]
    @State private var busy = false

    var body: some View {
        SubsSheetScaffold(title: "添加站点") {
            if let selected {
                formStep(selected)
            } else {
                pickStep
            }
        } footer: {
            if let selected {
                SubsPrimaryButton(title: busy ? "保存中…" : "保存并验证", busy: busy,
                                  enabled: SettingsBSiteAuthForm.canSubmit(selected, authType, values),
                                  identifier: "site-add-save") {
                    Task { await save(selected) }
                }
            }
        }
    }

    private var filtered: [API.CatalogItem] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return available }
        return available.filter {
            $0.displayName.lowercased().contains(q) || $0.siteId.lowercased().contains(q) || $0.baseUrl.lowercased().contains(q)
        }
    }

    private var pickStep: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(Theme.textFaint)
                TextField("搜索站点名称 / 地址", text: $query)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .accessibilityIdentifier("site-add-search")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .background(Color.white.opacity(0.05), in: .rect(cornerRadius: 12))

            if available.isEmpty {
                Text("所有支持的站点都已配置。").foregroundStyle(Theme.textMuted).frame(maxWidth: .infinity).padding(.vertical, 24)
            } else if filtered.isEmpty {
                Text("没有匹配「\(query)」的站点。").foregroundStyle(Theme.textMuted).frame(maxWidth: .infinity).padding(.vertical, 24)
            } else {
                VStack(spacing: 2) {
                    ForEach(filtered, id: \.siteId) { item in
                        Button {
                            pick(item)
                        } label: {
                            HStack(spacing: 10) {
                                SettingsBSiteBadge(item: item)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(item.displayName).font(.body.weight(.medium)).foregroundStyle(Theme.text).lineLimit(1)
                                    Text(item.baseUrl).font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
                                }
                                Spacer(minLength: 8)
                                Text(item.supportedAuthTypes.map { SettingsBSiteText.authType($0.authType) }.joined(separator: " / "))
                                    .font(.caption)
                                    .foregroundStyle(Theme.textMuted)
                                    .lineLimit(1)
                            }
                            .padding(.horizontal, 10)
                            .padding(.vertical, 9)
                            .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 10))
                            .contentShape(.rect)
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("site-add-item-\(item.siteId)")
                    }
                }
            }
        }
    }

    private func formStep(_ item: API.CatalogItem) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 10) {
                SettingsBSiteBadge(item: item)
                VStack(alignment: .leading, spacing: 1) {
                    Text(item.displayName).font(.body.weight(.semibold)).lineLimit(1)
                    Text(item.baseUrl).font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
                }
                Spacer(minLength: 8)
                Button("重新选择") { selected = nil }
                    .buttonStyle(.glass)
                    .disabled(busy)
                    .accessibilityIdentifier("site-add-repick")
            }
            SettingsBSiteAuthForm(item: item, isEdit: false, authType: $authType, values: $values)
        }
    }

    private func pick(_ item: API.CatalogItem) {
        selected = item
        authType = item.supportedAuthTypes.first?.authType ?? "cookie"
        values = [:]
    }

    private func save(_ item: API.CatalogItem) async {
        busy = true
        defer { busy = false }
        let t = { (f: String) in SettingsBSiteAuthForm.trimmed(f, item, authType, values) }
        do {
            let site = try await api.siteAdd(body: API.SiteConfigCreate(
                siteId: item.siteId, authType: authType,
                cookie: t("cookie"), apiKey: t("api_key"), username: t("username"), password: t("password"),
                enabled: true
            ))
            onCreated(site)
            dismiss()
        } catch {
            feedback.error(error)
        }
    }
}

// MARK: - 编辑授权

/// 编辑授权（对应 Web 详情「授权」段的 SiteForm）：保存即触发后端重新验证。
struct SettingsBSiteEditAuthSheet: View {
    let item: API.CatalogItem
    let site: API.ConfiguredSite
    let onSaved: (API.ConfiguredSite) -> Void

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(\.dismiss) private var dismiss
    @State private var authType: String
    @State private var values: [String: String] = [:]
    @State private var busy = false

    init(item: API.CatalogItem, site: API.ConfiguredSite, onSaved: @escaping (API.ConfiguredSite) -> Void) {
        self.item = item
        self.site = site
        self.onSaved = onSaved
        _authType = State(initialValue: site.authType)
    }

    var body: some View {
        SubsSheetScaffold(title: "编辑授权", subtitle: item.displayName) {
            SettingsBSiteAuthForm(item: item, isEdit: true, authType: $authType, values: $values)
        } footer: {
            SubsPrimaryButton(title: busy ? "保存中…" : "保存并重新验证", busy: busy,
                              enabled: SettingsBSiteAuthForm.canSubmit(item, authType, values),
                              identifier: "site-edit-save") {
                Task { await save() }
            }
        }
    }

    private func save() async {
        busy = true
        defer { busy = false }
        let t = { (f: String) in SettingsBSiteAuthForm.trimmed(f, item, authType, values) }
        do {
            let updated = try await api.siteUpdate(siteId: site.siteId, body: API.SiteConfigUpdate(
                authType: authType,
                cookie: t("cookie"), apiKey: t("api_key"), username: t("username"), password: t("password"),
                enabled: site.enabled
            ))
            onSaved(updated)
            dismiss()
        } catch {
            feedback.error(error)
        }
    }
}

// MARK: - 刷流设置

/// 刷流设置（对应 Web `BoostSettingsModal`）：预算 + 汰换保留期同窗，开启确认与运行中调整共用。
/// - enable：开启前讲清将发生什么（抢免费种、预算内汰换、索引同步提速），按钮「开启刷流」；
/// - adjust：运行中调整；调小预算的后果不可逆（超出部分连数据删除），保存前再二次确认。
struct SettingsBSiteBoostSheet: View {
    enum Mode { case enable, adjust }

    let mode: Mode
    let siteName: String
    let site: API.ConfiguredSite
    let onChanged: (API.ConfiguredSite) -> Void

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(\.dismiss) private var dismiss
    @State private var budgetGib: String
    @State private var holdDays: String
    @State private var error: String?
    @State private var busy = false

    init(mode: Mode, siteName: String, site: API.ConfiguredSite, onChanged: @escaping (API.ConfiguredSite) -> Void) {
        self.mode = mode
        self.siteName = siteName
        self.site = site
        self.onChanged = onChanged
        // 每次打开按当前生效值初始化（上次输入不残留）
        _budgetGib = State(initialValue: String(Int((Double(site.boostBudgetBytes) / Double(SettingsBSiteFormat.gib)).rounded())))
        _holdDays = State(initialValue: String(site.boostHoldDays))
    }

    var body: some View {
        SubsSheetScaffold(title: mode == .enable ? "开启自动刷分享率" : "刷流设置") {
            VStack(alignment: .leading, spacing: 8) {
                Text(mode == .enable ? "开启「\(siteName)」的自动刷分享率？" : "刷流设置 · \(siteName)")
                    .font(.title3.weight(.bold))
                Text(mode == .enable
                     ? "开启后将自动抢该站新发布的免费种子做种以提升分享率，占用空间在预算内自动汰换（下载完成、入池满保留期且上传效率过低的任务才会被连数据删除），该站的索引同步会提速到约 5 分钟一次。"
                     : "调小预算会按上传效率从低到高汰换在池任务（连数据删除），直到占用回到新预算内；保留期内的任务绝不会被提前删除。")
                    .font(.subheadline)
                    .foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let error {
                SubsNotice(text: error, tone: .error)
            }
            field(label: "存储预算", text: $budgetGib, unit: "GiB",
                  hint: "刷流任务占用磁盘的上限，预算内自动汰换", identifier: "boost-budget")
            field(label: "汰换保留期", text: $holdDays, unit: "天",
                  hint: "H&R 安全垫：有考核的站不小于考核时长；无考核可调 0 自由汰换", identifier: "boost-hold-days")
        } footer: {
            SubsPrimaryButton(title: busy ? "保存中…" : mode == .enable ? "开启刷流" : "保存", busy: busy,
                              identifier: "boost-save") {
                Task { await save() }
            }
        }
    }

    private func field(label: String, text: Binding<String>, unit: String, hint: String, identifier: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.subheadline).foregroundStyle(Theme.textMuted)
            HStack {
                TextField("", text: text)
                    .keyboardType(.numberPad)
                    .accessibilityIdentifier(identifier)
                Text(unit).font(.footnote.weight(.medium)).foregroundStyle(Theme.textFaint)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .background(Color.white.opacity(0.05), in: .rect(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.08)))
            .disabled(busy)
            Text(hint).font(.caption).foregroundStyle(Theme.textFaint).fixedSize(horizontal: false, vertical: true)
        }
    }

    private func save() async {
        guard let gibRaw = Double(budgetGib.trimmingCharacters(in: .whitespaces)), gibRaw.isFinite, gibRaw.rounded() >= 1 else {
            error = "刷流预算必须是不小于 1 的整数（单位 GiB）"
            return
        }
        // 粘贴超长数字时先挡住：Int 转换或换算成字节的乘法溢出会直接闪退
        guard gibRaw.rounded() <= Double(Int.max / SettingsBSiteFormat.gib) else {
            error = "刷流预算数值过大，请填写合理的 GiB 数"
            return
        }
        let gib = Int(gibRaw.rounded())
        guard let daysRaw = Double(holdDays.trimmingCharacters(in: .whitespaces)), daysRaw.isFinite,
              daysRaw.rounded() >= 0, daysRaw.rounded() <= 30 else {
            error = "汰换保留期须是 0～30 之间的整数（天）"
            return
        }
        let days = Int(daysRaw.rounded())
        let currentGib = Int((Double(site.boostBudgetBytes) / Double(SettingsBSiteFormat.gib)).rounded())
        if mode == .adjust, gib < currentGib {
            let ok = await feedback.confirm(
                "将「\(siteName)」的刷流预算从 \(currentGib) GiB 调小到 \(gib) GiB？",
                message: [
                    "在池占用超出新预算的部分将被汰换：连同已下载的数据一起删除，且同一种子不会再抢回",
                    "按上传效率从低到高删——死种和低效的先走，高效种子最后才会被动",
                    "汰换保留期内的任务不受影响，到期后才继续收敛",
                    "收敛期间暂停接新的免费种",
                ].map { "• " + $0 }.joined(separator: "\n"),
                confirmTitle: "确认调小",
                destructive: true
            )
            guard ok else { return }
        }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let updated = try await api.siteRatioBoostSet(
                siteId: site.siteId,
                body: API.SiteRatioBoostUpdate(enabled: true, budgetBytes: gib * SettingsBSiteFormat.gib, holdDays: days)
            )
            onChanged(updated)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
