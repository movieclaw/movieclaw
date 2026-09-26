import SwiftUI

// 消息推送 →「接入通道」标签（对应 Web `ChannelsTab` + `AddChannelMenu` + `ChannelAccountRowView`）。

/// 全部可接入的通道：微信走扫码，Telegram/Discord 走 bot token + 配对码，飞书贴群机器人 Webhook。
///
/// 平台文案集中在这里（Web `CHANNEL_META`，本模块唯一的平台差异落点）：
/// summary 给「新增通道」菜单，howTo 给绑定弹层顶部，tokenHint 只有需要 bot token 的通道才有。
enum SettingsBPushChannel: String, CaseIterable, Identifiable {
    case weixin, telegram, discord, feishu

    var id: String { rawValue }

    var label: String {
        switch self {
        case .weixin: "微信"
        case .telegram: "Telegram"
        case .discord: "Discord"
        case .feishu: "飞书"
        }
    }

    var summary: String {
        switch self {
        case .weixin: "手机扫码绑定，无需自建机器人"
        case .telegram: "接入你用 @BotFather 创建的 bot"
        case .discord: "接入你在开发者后台创建的 bot"
        case .feishu: "粘贴群机器人 Webhook 地址，即绑即用"
        }
    }

    var howTo: String {
        switch self {
        case .weixin:
            "打开手机微信扫描下方二维码。扫码人即唯一可对话的用户，同时也是推送目标。"
        case .telegram:
            "在 Telegram 搜索 @BotFather 创建 bot 并复制 token；国内网络请先在「设置 → 网络」为 Telegram 开启代理。"
        case .discord:
            "在 discord.com/developers 创建应用 → Bot → Reset Token 复制；国内网络请先在「设置 → 网络」为 Discord 开启代理。"
        case .feishu:
            "在飞书群聊「设置 → 群机器人 → 添加机器人」里添加「自定义机器人」，把复制的 Webhook 地址粘贴到下面即可。安全设置建议选「签名校验」，并把密钥一并填入。"
        }
    }

    var tokenHint: String {
        switch self {
        case .telegram: "从 @BotFather 创建 bot 后获得的 token"
        case .discord: "Discord 开发者后台 Bot 页面的 token"
        default: ""
        }
    }

    /// 解绑确认框的说明
    var unbindMessage: String {
        switch self {
        case .weixin: "解绑后需重新扫码才能使用。"
        case .feishu: "解绑后群机器人不再收到推送，需重新粘贴 Webhook 地址。"
        default: "解绑将删除 bot 凭据并停止通道，需重新配对才能使用。"
        }
    }

    /// 绑定成功的 Toast
    var boundToast: String {
        self == .feishu ? "\(label) 已接入，去飞书群里看看欢迎消息吧" : "\(label) 已接入，现在就可以给它发消息试试"
    }
}

/// 列表行：四个平台的账号归一成同一形状，列表本身不再关心来源差异
struct SettingsBPushAccountRow: Identifiable {
    let channel: SettingsBPushChannel
    let accountId: String
    /// 完成绑定的用户 id（白名单，同时是推送目标）
    let boundUserId: String?
    /// active = 正常；stale = 凭据失效需重新绑定
    let status: String
    let running: Bool
    let lastError: String?
    let boundAt: String

    var id: String { "\(channel.rawValue):\(accountId)" }
}

/// 「接入通道」的全部 Section
struct SettingsBPushChannelsSections: View {
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(Router.self) private var router

    @State private var rows: [SettingsBPushAccountRow]?
    @State private var error: String?
    @State private var busy = false
    /// 正在绑定的通道（nil = 没开弹层）
    @State private var binding: SettingsBPushChannel?
    private var probe: LLMCapabilityProbe { .shared }

    var body: some View {
        Section {
            // 生命周期修饰符挂在常驻的说明行上：挂在 Section 上会被 List 分发到每一行，
            // 变成多份任务 / 多个 sheet 呈现者
            intro
                .task {
                    async let probing: Void = probe.ensure(api: api)
                    await load()
                    await probing
                }
                .sheet(item: $binding) { channel in
                    SettingsBPushBindSheet(channel: channel) {
                        binding = nil
                        feedback.success(channel.boundToast)
                        Task { await load() }
                    }
                    .sheetFeedback()
                }
            // 前置门禁：对话完全由模型驱动，未接入模型时隐藏新增入口并引导
            if probe.state == .missing {
                HStack(alignment: .firstTextBaseline) {
                    Text("接入 AI 模型后即可解锁通道中的 AI 对话能力。")
                        .font(.footnote)
                        .foregroundStyle(Theme.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 8)
                    Button("去接入") { router.push(.settingsSection(.llm)) }
                        .font(.footnote.weight(.medium))
                        .buttonStyle(.borderless)
                        .accessibilityIdentifier("push-llm-setup")
                }
            }
            if let error {
                SettingsBNotice(text: error, tone: .danger)
                    .accessibilityIdentifier("push-channels-error")
            }
        }

        Section {
            if let rows {
                if rows.isEmpty {
                    emptyState
                } else {
                    ForEach(rows) { row in
                        accountRow(row)
                    }
                }
            } else {
                ForEach(0..<2, id: \.self) { _ in
                    RoundedRectangle(cornerRadius: 10)
                        .fill(Color.white.opacity(0.04))
                        .frame(height: 48)
                }
            }
        } header: {
            HStack {
                Text(rows == nil ? "加载中…" : rows!.isEmpty ? "还没有接入任何通道。" : "已接入 \(rows!.count) 个账号。")
                    .accessibilityIdentifier("push-channels-count")
                Spacer()
                if probe.state != .missing {
                    addMenu
                }
            }
            .textCase(nil)
        }
    }

    private var intro: some View {
        let code: (String) -> Text = { Text(" \($0) ").font(.caption.monospaced()).foregroundStyle(Theme.text) }
        return Text("接入的通道都是推送目标；微信 / Telegram / Discord 里还能直接和 AI 助手对话：发消息即可搜片、订阅、查进度。发送\(code("/reset"))重置会话，\(code("/stop"))取消正在进行的处理。")
            .font(.footnote)
            .foregroundStyle(Theme.textMuted)
            .fixedSize(horizontal: false, vertical: true)
    }

    /// 「新增通道」菜单：平台名 + 一句话说明，点选即开绑定弹层
    private var addMenu: some View {
        Menu {
            ForEach(SettingsBPushChannel.allCases) { channel in
                Button {
                    binding = channel
                } label: {
                    Text("新增 \(channel.label) 渠道")
                    Text(channel.summary)
                }
                .accessibilityIdentifier("push-add-\(channel.rawValue)")
            }
        } label: {
            Label("新增通道", systemImage: "plus")
                .font(.footnote.weight(.semibold))
        }
        .buttonStyle(.glass)
        .disabled(rows == nil)
        .accessibilityIdentifier("push-add-channel")
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: "bubble.left.and.text.bubble.right")
                .font(.title2)
                .foregroundStyle(Theme.textMuted)
                .frame(width: 48, height: 48)
                .background(Color.white.opacity(0.06), in: .rect(cornerRadius: 14))
            Text("还没有接入任何通道").font(.body.weight(.medium))
            Text("点击右上角「新增通道」，支持微信、Telegram、Discord 和飞书。")
                .font(.footnote)
                .foregroundStyle(Theme.textMuted)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 24)
        .accessibilityIdentifier("push-channels-empty")
    }

    /// 一行已接入账号：平台名 + 运行状态 + 绑定人 / 失效原因 + 解绑
    private func accountRow(_ row: SettingsBPushAccountRow) -> some View {
        let badge: (text: String, tone: SettingsBTone) = row.status == "stale"
            ? ("需重新绑定", .danger)
            : row.running ? ("运行中", .ok) : ("未运行", .neutral)
        let detail: String = row.status == "stale"
            ? (row.lastError ?? "凭据已失效，请重新绑定")
            : row.channel == .feishu
                ? "群机器人 · 绑定于 \(SettingsBFormat.relative(row.boundAt))"
                : "\(row.boundUserId ?? row.accountId) · 绑定于 \(SettingsBFormat.relative(row.boundAt))"
        return HStack(spacing: 12) {
            Image(systemName: "bubble.left.and.text.bubble.right")
                .font(.body)
                .foregroundStyle(Theme.textMuted)
                .frame(width: 38, height: 38)
                .background(Color.white.opacity(0.06), in: .rect(cornerRadius: 11))
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 8) {
                    Text(row.channel.label).font(.body.weight(.semibold)).lineLimit(1)
                    HStack(spacing: 5) {
                        SettingsBDot(tone: badge.tone, size: 6)
                        Text(badge.text)
                    }
                    .font(.caption.weight(.medium))
                    .foregroundStyle(badge.tone == .neutral ? Theme.text.opacity(0.75) : badge.tone.color)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background((badge.tone == .neutral ? Color.white : badge.tone.color).opacity(0.12), in: .capsule)
                }
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            Spacer(minLength: 8)
            Button("解绑") {
                Task { await unbind(row) }
            }
            .font(.subheadline.weight(.medium))
            .foregroundStyle(Theme.danger)
            .buttonStyle(.glass)
            .disabled(busy)
            .accessibilityIdentifier("push-unbind-\(row.channel.rawValue)-\(row.accountId)")
        }
        .padding(.vertical, 4)
        .accessibilityIdentifier("push-account-\(row.channel.rawValue)-\(row.accountId)")
    }

    // MARK: 数据

    private func load() async {
        do {
            // 各平台同源同后端，任一失败都视作整体失败：与其展示半张列表让用户误以为
            // 「某个通道掉了」，不如明确报错让他重试（同 Web）
            async let weixin = api.channelsWeixinAccountsList()
            async let telegram = api.channelsImAccountsList(channel: "telegram")
            async let discord = api.channelsImAccountsList(channel: "discord")
            async let feishu = api.channelsImAccountsList(channel: "feishu")
            let (wx, tg, dc, fs) = try await (weixin, telegram, discord, feishu)
            error = nil
            rows = wx.map {
                SettingsBPushAccountRow(channel: .weixin, accountId: $0.accountId, boundUserId: $0.boundUserId,
                                        status: $0.status, running: $0.running, lastError: $0.lastError, boundAt: $0.boundAt)
            } + [(SettingsBPushChannel.telegram, tg), (.discord, dc), (.feishu, fs)].flatMap { channel, list in
                list.map {
                    SettingsBPushAccountRow(channel: channel, accountId: $0.accountId, boundUserId: $0.boundUserId,
                                            status: $0.status, running: $0.running, lastError: $0.lastError, boundAt: $0.boundAt)
                }
            }
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
            rows = []
        }
    }

    private func unbind(_ row: SettingsBPushAccountRow) async {
        guard await feedback.confirm(
            "解绑该 \(row.channel.label) 账号？",
            message: row.channel.unbindMessage,
            confirmTitle: "解绑",
            destructive: true
        ) else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            if row.channel == .weixin {
                _ = try await api.channelsWeixinAccountsUnbind(accountId: row.accountId)
            } else {
                _ = try await api.channelsImAccountsUnbind(channel: row.channel.rawValue, accountId: row.accountId)
            }
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
