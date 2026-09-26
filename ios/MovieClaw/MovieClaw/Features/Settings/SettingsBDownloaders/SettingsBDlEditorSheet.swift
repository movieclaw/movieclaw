import SwiftUI

/// 新建 / 编辑下载器（对应 Web `DownloaderForm`）：类型 + 名称 + 地址 + 可选凭证 / 保存目录 / 路径映射。
///
/// 设计要点（与 Web 一致）：
/// - 出于安全后端不回传密码，编辑时密码框留空、提示「出于安全，请重新填写」；
/// - 路径映射默认折叠（绝大多数部署用不到），已有映射时默认展开；
/// - 映射行要么删掉要么填完整（下载器侧必须是 / 开头的绝对路径），两端各自查重
///   （尾部斜杠归一后比较）——同一路径两条映射的翻译结果取决于遍历顺序，是错配；
/// - 保存成功后后端异步测试连接，清单靠轮询看到状态落定。
struct SettingsBDlEditorSheet: View {
    let downloader: API.DownloaderView?
    /// 体检跳转建议预填的映射本机侧路径（Web `?suggest_mapping=`）；无建议为 nil
    var suggestMapping: String?
    let onSaved: (API.DownloaderView) -> Void

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(\.dismiss) private var dismiss

    @State private var clientType: String
    @State private var name: String
    @State private var url: String
    @State private var username: String
    @State private var password = ""
    @State private var savePath: String
    @State private var mappings: [SettingsBDlMappingDraft]
    @State private var mappingsOpen: Bool
    @State private var busy = false

    init(downloader: API.DownloaderView?, suggestMapping: String? = nil, onSaved: @escaping (API.DownloaderView) -> Void) {
        self.downloader = downloader
        self.suggestMapping = suggestMapping
        self.onSaved = onSaved
        _clientType = State(initialValue: downloader?.clientType ?? "qbittorrent")
        _name = State(initialValue: downloader?.name ?? "")
        _url = State(initialValue: downloader?.url ?? "")
        _username = State(initialValue: downloader?.username ?? "")
        _savePath = State(initialValue: downloader?.savePath ?? "")
        let existing = (downloader?.pathMappings ?? []).map { SettingsBDlMappingDraft(local: $0.local, remote: $0.remote) }
        _mappings = State(initialValue: Self.withSuggestedMapping(existing, suggestMapping))
        // 已有映射或带预填建议时默认展开
        _mappingsOpen = State(initialValue: !existing.isEmpty || suggestMapping != nil)
    }

    /// 建议路径已被某条映射的本机侧覆盖（相同或是其子目录）就不再追加（Web withSuggestedMapping）
    static func withSuggestedMapping(_ existing: [SettingsBDlMappingDraft], _ suggest: String?) -> [SettingsBDlMappingDraft] {
        guard let suggest, !suggest.isEmpty else { return existing }
        let target = norm(suggest)
        let covered = existing.contains { mapping in
            let local = norm(mapping.local)
            return local != "/" && (target == local || target.hasPrefix(local + "/"))
        }
        return covered ? existing : existing + [SettingsBDlMappingDraft(local: suggest, remote: "")]
    }

    // MARK: - 校验（同 Web canSubmit）

    private static func norm(_ path: String) -> String {
        var p = path.trimmingCharacters(in: .whitespaces)
        while p.hasSuffix("/") { p.removeLast() }
        return p.isEmpty ? "/" : p
    }

    private var mappingsComplete: Bool {
        mappings.allSatisfy {
            !$0.local.trimmingCharacters(in: .whitespaces).isEmpty
                && $0.remote.trimmingCharacters(in: .whitespaces).hasPrefix("/")
        }
    }

    private var mappingsUnique: Bool {
        let locals = mappings.map { Self.norm($0.local) }.filter { $0 != "/" }
        let remotes = mappings.map { Self.norm($0.remote) }.filter { $0 != "/" }
        return Set(locals).count == locals.count && Set(remotes).count == remotes.count
    }

    private var canSubmit: Bool {
        !name.trimmingCharacters(in: .whitespaces).isEmpty
            && url.trimmingCharacters(in: .whitespaces).range(of: #"^https?://.+"#, options: .regularExpression) != nil
            && mappingsComplete
            && mappingsUnique
    }

    // MARK: - 界面

    var body: some View {
        NavigationStack {
            Form {
                Section("下载器类型") {
                    Picker("下载器类型", selection: $clientType) {
                        Text("qBittorrent").tag("qbittorrent")
                        Text("Transmission").tag("transmission")
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .accessibilityIdentifier("downloader-form-type")
                }

                Section {
                    SettingsBTextField(label: "名称", text: $name, placeholder: "如：家里的 qBittorrent",
                                       identifier: "downloader-form-name")
                    // qB 是 WebUI 地址，Tr 是 RPC 地址，端口不同
                    SettingsBTextField(
                        label: clientType == "qbittorrent" ? "WebUI 地址" : "RPC 地址",
                        text: $url,
                        placeholder: clientType == "qbittorrent" ? "http://192.168.1.10:8080" : "http://192.168.1.10:9091",
                        keyboard: .URL,
                        identifier: "downloader-form-url"
                    )
                    // 凭证：未开鉴权的下载器可整体留空
                    SettingsBTextField(label: "用户名（可选）", text: $username, identifier: "downloader-form-username")
                    SettingsBTextField(label: "密码（可选）", text: $password,
                                       placeholder: downloader != nil ? "出于安全，请重新填写" : "",
                                       secure: true, identifier: "downloader-form-password")
                }

                Section {
                    SettingsBPathField(
                        label: "默认保存目录（可选）",
                        path: $savePath,
                        placeholder: "浏览服务器目录并选择…",
                        hint: "提交下载时文件的保存位置，从 movieclaw 能看到的目录里选择；下载器看到的路径不同时，配合下方「路径映射」翻译。留空则使用下载器自己设置的默认下载目录——下载器与 movieclaw 没有共享目录的部署请留空。",
                        identifier: "downloader-form-save-path"
                    )
                }

                mappingSection
            }
            .scrollContentBackground(.hidden)
            .scrollDismissesKeyboard(.interactively)
            .navigationTitle(downloader == nil ? "添加下载器" : "编辑下载器")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消", systemImage: "xmark") { dismiss() }
                        .accessibilityIdentifier("sheet-close")
                }
            }
            .safeAreaBar(edge: .bottom) {
                SubsPrimaryButton(title: "保存", busy: busy, enabled: canSubmit, identifier: "downloader-form-save") {
                    Task { await submit() }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 12)
            }
        }
        .presentationBackground(.regularMaterial)
    }

    /// 路径映射：默认折叠，仅跨容器/跨主机部署需要展开配置
    private var mappingSection: some View {
        Section {
            Button {
                withAnimation(.snappy) { mappingsOpen.toggle() }
            } label: {
                HStack {
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.semibold))
                        .rotationEffect(.degrees(mappingsOpen ? 90 : 0))
                    Text("路径映射（可选）").foregroundStyle(Theme.text)
                    Spacer()
                    if !mappingsOpen && !mappings.isEmpty {
                        Text("已配置 \(mappings.count) 条").font(.footnote).foregroundStyle(Theme.textFaint)
                    }
                }
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("downloader-form-mappings-toggle")

            if mappingsOpen {
                Text("movieclaw 与下载器不在同一容器/主机、同一块盘两边路径不同时才需要：提交下载前会把保存目录按前缀翻译成下载器视角。例如 movieclaw 看到的下载区是 `/data/downloads`、下载器容器里是 `/downloads`，则添加一条对照。留空表示两边路径一致。注意：配了映射后，所有下载保存目录（含媒体库目录）都必须被某条映射覆盖，否则会拒绝投递以防下载进下载器容器内的孤立路径；下载器能以相同路径直达的目录，添加一条两边相同的映射即可。")
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
                    .fixedSize(horizontal: false, vertical: true)

                if let suggestMapping {
                    Text("已按体检建议预填映射的本机侧 \(suggestMapping)——右侧填下载器视角的对应路径；下载器可直达同名路径时，点中间的 → 把左侧复制过去即可。")
                        .font(.caption)
                        .foregroundStyle(Theme.accent)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 8)
                        .background(Theme.accentSoft, in: .rect(cornerRadius: 8))
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("downloader-form-suggest-note")
                }

                ForEach($mappings) { $mapping in
                    SettingsBDlMappingEditorRow(mapping: $mapping) {
                        mappings.removeAll { $0.id == mapping.id }
                    }
                }

                Button {
                    mappings.append(SettingsBDlMappingDraft(local: "", remote: ""))
                } label: {
                    Label("添加映射", systemImage: "plus")
                }
                .accessibilityIdentifier("downloader-form-mapping-add")

                if !mappingsComplete {
                    Text("每条映射两边都要填：左边浏览选择，右边填下载器上以 / 开头的绝对路径；不需要的行请删除。")
                        .font(.caption)
                        .foregroundStyle(Theme.warning)
                } else if !mappingsUnique {
                    Text("映射的路径不能重复：同一 movieclaw 路径或同一下载器路径只能出现一次，请修改或删除重复的行。")
                        .font(.caption)
                        .foregroundStyle(Theme.warning)
                }
            }
        }
    }

    // MARK: - 提交

    private func submit() async {
        guard canSubmit, !busy else { return }
        busy = true
        defer { busy = false }
        let payload = API.DownloaderPayload(
            name: name.trimmingCharacters(in: .whitespaces),
            clientType: clientType,
            url: url.trimmingCharacters(in: .whitespaces),
            username: username.trimmingCharacters(in: .whitespaces).isEmpty ? nil : username.trimmingCharacters(in: .whitespaces),
            password: password.isEmpty ? nil : password,
            savePath: savePath.trimmingCharacters(in: .whitespaces).isEmpty ? nil : savePath.trimmingCharacters(in: .whitespaces),
            pathMappings: mappings.isEmpty ? nil : mappings.map {
                API.PathMapping(local: $0.local.trimmingCharacters(in: .whitespaces), remote: $0.remote.trimmingCharacters(in: .whitespaces))
            },
            enabled: downloader?.enabled ?? true
        )
        do {
            let saved: API.DownloaderView
            if let downloader {
                saved = try await api.dlUpdate(downloaderId: downloader.id, body: payload)
            } else {
                saved = try await api.dlAdd(body: payload)
            }
            onSaved(saved)
            dismiss()
        } catch {
            feedback.error(error)
        }
    }
}

/// 表单里的一条映射草稿（带稳定 id，删行时 ForEach 不串行）
struct SettingsBDlMappingDraft: Identifiable, Hashable {
    let id = UUID()
    var local: String
    var remote: String
}

/// 映射编辑行：上面选 MovieClaw 侧目录（服务端目录选择器），下面填下载器侧路径；
/// 中间「→ 同路径」一键把左侧复制到右侧（两边路径一致时省去手输，同 Web 的箭头按钮）。
private struct SettingsBDlMappingEditorRow: View {
    @Binding var mapping: SettingsBDlMappingDraft
    let onDelete: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top) {
                SettingsBPathField(
                    label: "MovieClaw 上的路径",
                    path: $mapping.local,
                    placeholder: "movieclaw 上的路径…",
                    identifier: "downloader-form-mapping-local"
                )
                Button("删除这条映射", systemImage: "xmark", role: .destructive, action: onDelete)
                    .labelStyle(.iconOnly)
                    .buttonStyle(.borderless)
                    .accessibilityIdentifier("downloader-form-mapping-delete")
            }
            HStack(spacing: 8) {
                Button {
                    mapping.remote = mapping.local
                } label: {
                    Image(systemName: "arrow.turn.down.right")
                }
                .buttonStyle(.borderless)
                .disabled(mapping.local.isEmpty)
                .accessibilityLabel("将左侧路径复制到右侧")
                .accessibilityIdentifier("downloader-form-mapping-copy")
                TextField("下载器上的路径，如 /downloads", text: $mapping.remote)
                    .font(.body.monospaced())
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .accessibilityIdentifier("downloader-form-mapping-remote")
            }
        }
        .padding(.vertical, 4)
    }
}
