import SwiftUI

/// 设置 → 设备（Web devices-section.tsx，设计见 docs/design/device-auth.md §7）。
///
/// 这一页承担三件事：
/// 1. **批准是防钓鱼的唯一一道人工闸**：审批卡上的名称、类型、来源 IP、配对码（大号等宽、加字距，
///    方便逐字比对）与「将获得」的大白话权限说明就是用户做决定的全部依据；
/// 2. **吊销是唯一的事后止损手段**：已连接设备一台一行，最近活跃时间 + 一键吊销；
/// 3. **手工令牌**给没人能按批准的环境（NAS 定时任务、CI、无界面容器）：明文只在创建响应里出现一次，
///    给出可直接粘贴的两行环境变量，关闭前二次确认。
///
/// 待批准请求只活在服务端内存里，所以每 3 秒轮询一次（用户常常是先在设备上发起，再切回来批准）；
/// 轮询失败不弹错，不盖掉用户正在看的审批卡。
struct DevicesSettingsView: View {
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback

    @State private var requests: [API.DeviceRequestView] = []
    @State private var devices: Loadable<[API.ApiTokenView]> = .loading
    @State private var busy: String?
    @State private var error: String?

    // 手工令牌
    @State private var tokenStage: TokenStage = .idle
    @State private var tokenName = ""
    @State private var tokenNameError: String?
    @State private var creating = false
    @State private var created: API.ApiTokenCreatedView?
    @State private var externalUrl = ""

    enum TokenStage { case idle, form }

    var body: some View {
        List {
            if let error {
                Section { SettingsNotice(text: error) }
            }
            if !requests.isEmpty {
                Section("待批准的接入请求") {
                    ForEach(requests, id: \.userCode) { request in
                        ApprovalCard(request: request, busy: busy == request.userCode,
                                     onApprove: { Task { await approve(request) } },
                                     onDeny: { Task { await deny(request) } })
                    }
                }
            }
            Section("已连接的设备") {
                switch devices {
                case .loading:
                    SettingsLoadingRow()
                case let .failed(message):
                    Text(message).font(.subheadline).foregroundStyle(Theme.danger)
                case let .loaded(list) where list.isEmpty:
                    devicesEmpty
                case let .loaded(list):
                    ForEach(list, id: \.id) { device in
                        deviceRow(device)
                    }
                }
            }
            manualTokenSection
        }
        .appBackground()
        .task { await load() }
        .polling(every: 3) {
            if let next = try? await api.authDevicesRequests() { requests = next }
        }
        .task {
            // 对外访问地址：进入分区就先拉，等按下创建再拉会多等一个往返；拿不到就回落当前服务器地址
            if let config = try? await api.appShow() { externalUrl = config.externalUrl }
        }
    }

    private func load() async {
        error = nil
        do {
            async let pending = api.authDevicesRequests()
            async let granted = api.authTokensList()
            requests = try await pending
            devices = .loaded(try await granted)
        } catch {
            if devices.value == nil { devices = .failed(error.localizedDescription) }
            self.error = error.localizedDescription
        }
    }

    private func reloadDevices() async {
        if let list = try? await api.authTokensList() { devices = .loaded(list) }
    }

    private func approve(_ request: API.DeviceRequestView) async {
        busy = request.userCode
        error = nil
        defer { busy = nil }
        do {
            try await api.authDevicesApprove(userCode: request.userCode)
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func deny(_ request: API.DeviceRequestView) async {
        busy = request.userCode
        error = nil
        defer { busy = nil }
        do {
            try await api.authDevicesDeny(userCode: request.userCode)
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func revoke(_ device: API.ApiTokenView) async {
        let ok = await feedback.confirm(
            "吊销「\(device.name)」？",
            message: "这台设备会立即失去访问权限，需要重新配对才能再次接入。其他设备不受影响。",
            confirmTitle: "吊销",
            destructive: true
        )
        guard ok else { return }
        busy = device.id
        error = nil
        defer { busy = nil }
        do {
            try await api.authTokensRevoke(tokenId: device.id)
            await reloadDevices()
        } catch {
            self.error = error.localizedDescription
        }
    }

    // MARK: 已连接设备

    private func deviceRow(_ device: API.ApiTokenView) -> some View {
        let live = SettingsTime.isLive(device.lastUsedAt)
        return HStack(spacing: 12) {
            SettingsStatusDot(color: live ? Theme.success : Color.white.opacity(0.25), glow: live)
            VStack(alignment: .leading, spacing: 3) {
                Text(device.name).font(.body.weight(.medium)).lineLimit(1)
                Text("\(DeviceText.clientType(device.clientType)) · \(DeviceText.grantBadge(device.clientType)) · \(SettingsTime.deviceRelative(device.lastUsedAt))")
                    .font(.caption).foregroundStyle(Theme.textFaint)
            }
            Spacer(minLength: 8)
            Button("吊销") { Task { await revoke(device) } }
                .buttonStyle(.glass)
                .controlSize(.small)
                .disabled(busy == device.id)
                .accessibilityIdentifier("device-revoke-\(device.name)")
        }
        .accessibilityIdentifier("device-row-\(device.name)")
    }

    /// 空态：直接告诉用户下一步在哪做
    private var devicesEmpty: some View {
        VStack(spacing: 8) {
            Image(systemName: "terminal").font(.title2).foregroundStyle(Theme.textMuted)
            Text("还没有设备接入").font(.body.weight(.medium))
            Text(requests.isEmpty
                 ? "在 Mac 转码 Worker 里点「在局域网中查找」或填好地址，或在终端运行 mclaw login，设备会显示一段配对码，回到这里批准即可。没人能按批准的环境（定时任务、CI、无界面容器）改用下面的手工令牌。"
                 : "上面有一条待批准的请求，核对配对码后即可批准。")
                .font(.subheadline).foregroundStyle(Theme.textMuted).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 16)
    }

    // MARK: 手工令牌

    @ViewBuilder
    private var manualTokenSection: some View {
        Section {
            if let created {
                createdCard(created)
            } else if tokenStage == .form {
                VStack(alignment: .leading, spacing: 6) {
                    Text("名字").font(.subheadline.weight(.medium)).foregroundStyle(Theme.textMuted)
                    TextField("nas-cron", text: $tokenName)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .submitLabel(.done)
                        .onSubmit { Task { await createToken() } }
                        .onChange(of: tokenName) { _, value in
                            if value.count > 64 { tokenName = String(value.prefix(64)) }
                            tokenNameError = nil
                        }
                        .accessibilityIdentifier("token-name")
                    Text(tokenNameError ?? "日后在上面的设备列表里就靠它认出这枚令牌、决定要不要吊销。")
                        .font(.caption)
                        .foregroundStyle(tokenNameError == nil ? Theme.textFaint : Theme.danger)
                }
                grantNote(DeviceText.manualGrant)
                HStack(spacing: 10) {
                    Button(creating ? "创建中…" : "创建令牌") { Task { await createToken() } }
                        .settingsProminentButton()
                        .disabled(creating)
                        .accessibilityIdentifier("token-create-submit")
                    Button("取消") {
                        tokenStage = .idle
                        tokenName = ""
                        tokenNameError = nil
                    }
                    .buttonStyle(.glass)
                    .disabled(creating)
                    .accessibilityIdentifier("token-create-cancel")
                }
            } else {
                Text("没法在浏览器里按下批准的环境——NAS 上的定时任务、CI、无界面容器——在这里创建一枚令牌，用 MOVIECLAW_SERVER 和 MOVIECLAW_TOKEN 两个环境变量注入给 mclaw。能打开浏览器的机器请直接运行 mclaw login 配对，不必走这里。")
                    .font(.subheadline).foregroundStyle(Theme.textMuted)
            }
        } header: {
            HStack {
                Text("手工创建令牌")
                Spacer()
                if tokenStage == .idle, created == nil {
                    Button { tokenStage = .form } label: { Label("创建令牌", systemImage: "plus") }
                        .font(.subheadline)
                        .textCase(nil)
                        .accessibilityIdentifier("token-create-open")
                }
            }
        }
    }

    private func grantNote(_ grant: DeviceText.Grant) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(grant.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.accent)
            Text(grant.body).font(.subheadline).foregroundStyle(Theme.textMuted)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.accentSoft, in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.accent.opacity(0.2)))
    }

    /// 注入地址：优先「对外访问地址」（用户明确声明的），没配时回落 App 当前连接的地址并说破
    private var serverAddress: (url: String, configured: Bool) {
        var configured = externalUrl.trimmingCharacters(in: .whitespaces)
        while configured.hasSuffix("/") { configured.removeLast() }
        if !configured.isEmpty { return (configured, true) }
        var origin = api.server.origin.absoluteString
        while origin.hasSuffix("/") { origin.removeLast() }
        return (origin, false)
    }

    /// 一次性凭据卡：全站唯一一处「现在不存就永远没了」的地方
    @ViewBuilder
    private func createdCard(_ token: API.ApiTokenCreatedView) -> some View {
        let address = serverAddress
        let snippet = "MOVIECLAW_SERVER=\(address.url)\nMOVIECLAW_TOKEN=\(token.token)"
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "checkmark").foregroundStyle(Theme.success)
            VStack(alignment: .leading, spacing: 3) {
                Text("已创建「\(token.name)」").font(.body.weight(.semibold))
                Text("令牌明文只显示这一次。关掉这张卡就再也读不到，只能吊销后重建。")
                    .font(.subheadline).foregroundStyle(Theme.warning)
            }
        }
        .accessibilityIdentifier("token-created-card")
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("粘贴到目标环境").font(.subheadline.weight(.medium)).foregroundStyle(Theme.textMuted)
                Spacer()
                SettingsCopyButton(text: snippet, title: "复制两行")
                    .buttonStyle(.glass).controlSize(.small)
            }
            (Text("MOVIECLAW_SERVER=").foregroundStyle(Theme.accent2) + Text(address.url).foregroundStyle(Theme.text)
                + Text("\nMOVIECLAW_TOKEN=").foregroundStyle(Theme.accent2) + Text(token.token).foregroundStyle(Theme.warning))
                .font(.footnote.monospaced())
                .textSelection(.enabled)
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.black.opacity(0.28), in: .rect(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.08)))
        }
        if address.configured {
            Text("地址取自「设置 → 网络」里填写的对外访问地址。").font(.caption).foregroundStyle(Theme.textFaint)
        } else {
            SettingsNotice(
                text: "上面这行地址取自 App 当前连接的服务器地址，只是猜测——目标机器不一定连得到。请到「设置 → 网络」填写对外访问地址，之后这里会直接给出正确的一行。",
                tone: .warn
            )
        }
        HStack {
            SettingsCopyButton(text: token.token, title: "仅复制令牌")
                .buttonStyle(.glass)
            Spacer()
            Button("我已保存，关闭") { Task { await dismissCreated() } }
                .settingsProminentButton()
                .accessibilityIdentifier("token-created-dismiss")
        }
    }

    private func createToken() async {
        let name = tokenName.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else {
            tokenNameError = "先给它起个名字，否则日后没法在列表里认出是哪台机器。"
            return
        }
        creating = true
        error = nil
        defer { creating = false }
        do {
            created = try await api.authTokensCreate(body: .init(name: name))
            tokenStage = .idle
            tokenName = ""
            tokenNameError = nil
            await reloadDevices()
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// 关闭一次性凭据卡要过确认：明文关掉就再也读不到，误点的代价是吊销重建
    private func dismissCreated() async {
        let ok = await feedback.confirm(
            "关闭后就看不到这枚令牌了？",
            message: "令牌明文只显示这一次。确认你已经把它存进目标机器，或者复制到了安全的地方。",
            confirmTitle: "我已保存"
        )
        if ok { created = nil }
    }
}

// MARK: - 审批卡

/// 用户做决定的全部依据都在这张卡上；配对码大号等宽字，便于和设备屏幕逐字比对
private struct ApprovalCard: View {
    let request: API.DeviceRequestView
    let busy: Bool
    let onApprove: () -> Void
    let onDeny: () -> Void

    var body: some View {
        let grant = DeviceText.grant(request.clientType)
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text(request.clientName).font(.body.weight(.semibold))
                Spacer()
                Text(request.userCode)
                    .font(.system(size: 22, weight: .semibold, design: .monospaced))
                    .tracking(3.5)
                    .foregroundStyle(Theme.accent)
                    .accessibilityIdentifier("device-request-code")
            }
            Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 6) {
                GridRow {
                    Text("类型").foregroundStyle(Theme.textFaint)
                    Text(DeviceText.clientType(request.clientType)).foregroundStyle(Theme.textMuted)
                }
                GridRow {
                    Text("来源").foregroundStyle(Theme.textFaint)
                    if request.sourceIp.isEmpty {
                        // 桥接网络的容器看到的是网桥网关，与其给个误导地址不如直说，把判断依据推回配对码
                        Text("无法确定 ").foregroundStyle(Theme.textFaint)
                            + Text("容器网络改写了源地址，请以配对码为准").font(.caption).foregroundStyle(Theme.textFaint)
                    } else {
                        Text(request.sourceIp).font(.subheadline.monospaced()).foregroundStyle(Theme.textMuted)
                    }
                }
            }
            .font(.subheadline)
            VStack(alignment: .leading, spacing: 4) {
                Text(grant.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.accent)
                Text(grant.body).font(.subheadline).foregroundStyle(Theme.textMuted)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.accentSoft, in: .rect(cornerRadius: 12))
            Text("请确认上面的配对码与设备上显示的完全一致。如果这不是你刚发起的操作，选择拒绝。")
                .font(.caption).foregroundStyle(Theme.textFaint)
            HStack(spacing: 10) {
                Button("批准接入", systemImage: "checkmark", action: onApprove)
                    .settingsProminentButton()
                    .accessibilityIdentifier("device-approve-\(request.userCode)")
                Button("拒绝", systemImage: "xmark", action: onDeny)
                    .buttonStyle(.glass)
                    .tint(Theme.danger)
                    .accessibilityIdentifier("device-deny-\(request.userCode)")
            }
            .disabled(busy)
        }
        .padding(.vertical, 6)
    }
}

// MARK: - 展示口径（Web lib/devices-display.ts，措辞是安全设计的一部分，照搬不改）

enum DeviceText {
    struct Grant {
        let title: String
        let body: String
    }

    static func clientType(_ type: String) -> String {
        switch type {
        case "worker": "转码 Worker"
        case "cli": "命令行 / Agent"
        case "manual": "手工令牌"
        default: "未知类型"
        }
    }

    static func grant(_ type: String) -> Grant {
        if type == "worker" {
            return Grant(title: "将获得：仅限转码", body: "这台机器不能查看或修改你的订阅、媒体库和设置。")
        }
        return Grant(
            title: "将获得：与你相同的完全权限",
            body: "这台机器上的程序将能做你在网页上能做的一切，包括删除媒体文件。只在你清楚这台机器上正在运行什么程序时才批准。"
        )
    }

    static let manualGrant = Grant(
        title: "将获得：与你相同的完全权限",
        body: "持有这枚令牌的程序将能做你在网页上能做的一切，包括删除媒体文件。令牌不会自动过期，只能在这里吊销——只把它放进你自己掌握的机器。"
    )

    static func grantBadge(_ type: String) -> String {
        type == "worker" ? "仅转码" : "完全权限"
    }
}
