import CoreImage.CIFilterBuiltins
import SwiftUI

/// 「接入通道」绑定弹层（对应 Web `channel-bind-dialog.tsx`）——四个平台共用一层壳。
///
/// 为什么收进弹层：绑定是一次性动作（拿到二维码 / 配对码 → 去客户端确认 → 完成），
/// 设置页的常态是「看看已经接好了哪些」；只有点「新增通道」才进入这层，绑定成功即关闭并刷新列表。
///
/// 流程差异只落在 body 上，外壳（标题 / 指引 / 关闭）一致；各 body 直接产出表单分组，
/// 轮询 / 自动出码这类副作用只挂在一个常驻行上——列表会把修饰符分发给每一行，挂在整段上会变成多份：
/// - 微信：进弹层即请求二维码 → 扫码 →（可能）填手机上的配对码 → 完成；
/// - Telegram / Discord：先填 bot token → 拿 6 位配对码 → 私聊 bot 发码 → 完成；
/// - 飞书：粘贴群机器人 Webhook 地址 → 服务端发欢迎消息验真 → 即绑即用（无轮询）。
/// 前两者靠 2 秒一次的状态轮询推进（后端只读内存快照，毫秒级返回），与 Web 同一间隔。
struct SettingsBPushBindSheet: View {
    let channel: SettingsBPushChannel
    /// 绑定完成：由调用方负责关闭弹层并刷新通道列表
    let onBound: () -> Void

    var body: some View {
        SubsSheetScaffold(title: "接入 \(channel.label)", subtitle: channel.howTo, closeTitle: "关闭") {
            switch channel {
            case .weixin: SettingsBPushWeixinBody(onBound: onBound)
            case .feishu: SettingsBPushFeishuBody(onBound: onBound)
            case .telegram, .discord: SettingsBPushTokenBody(channel: channel, onBound: onBound)
            }
        }
    }
}

// MARK: - 共用小件

/// 弹层里的状态行（居中排版，行底由表单提供）
private struct SettingsBPushStatusCard<Content: View>: View {
    @ViewBuilder let content: () -> Content
    var body: some View {
        VStack(spacing: 14) { content() }
            .multilineTextAlignment(.center)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
    }
}

/// 单行输入（表单行里的原生输入框）
private struct SettingsBPushInput: View {
    let placeholder: String
    @Binding var text: String
    var keyboard: UIKeyboardType = .default
    var mono = false
    let identifier: String
    var onSubmit: () -> Void = {}

    var body: some View {
        TextField(placeholder, text: $text)
            .font(mono ? .body.monospaced() : .body)
            .keyboardType(keyboard)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .submitLabel(.go)
            .onSubmit(onSubmit)
            .accessibilityIdentifier(identifier)
    }
}

/// 本地生成二维码。
///
/// Web 直接显示服务端渲染好的 SVG data URL（`qrcode_image`），iOS 没有现成的 SVG 渲染，
/// 于是用 CoreImage 把二维码内容（`qrcode_url`，即 SVG 编码的同一串）本地画出来，
/// 最近邻放大保证边缘锐利。
enum SettingsBPushQRCode {
    static func image(for content: String) -> UIImage? {
        guard !content.isEmpty else { return nil }
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(content.utf8)
        filter.correctionLevel = "M"
        guard let output = filter.outputImage?.transformed(by: CGAffineTransform(scaleX: 10, y: 10)),
              let cg = CIContext().createCGImage(output, from: output.extent) else { return nil }
        return UIImage(cgImage: cg)
    }
}

// MARK: - 微信：扫码 + 可选配对码

private struct SettingsBPushWeixinBody: View {
    let onBound: () -> Void

    @Environment(\.api) private var api
    @State private var binding: API.WeixinBindingStatusView?
    @State private var qrImage: UIImage?
    @State private var verifyCode = ""
    @State private var error: String?
    @State private var busy = false
    /// 进弹层即出码只自动发起一次；失败 / 过期后由用户点按钮重来，不无限重试
    @State private var autoStarted = false
    @FocusState private var codeFocused: Bool

    private var polling: Bool {
        guard let status = binding?.status else { return false }
        return status == "pending" || status == "scanned" || status == "need_verify_code"
    }

    private var terminal: Bool { binding?.status == "expired" || binding?.status == "failed" }

    var body: some View {
        if let error {
            Section {
                SubsNoticeRow(text: error, tone: .error).accessibilityIdentifier("push-bind-error")
            }
        }
        Section {
            statusRow
                .task {
                    guard !autoStarted else { return }
                    autoStarted = true
                    await begin()
                }
                .polling(every: 2) { await poll() }
        }
        // 微信要求补填手机上显示的配对码时才出现
        if binding?.status == "need_verify_code" {
            Section {
                HStack(spacing: 8) {
                    SettingsBPushInput(
                        placeholder: "手机微信上显示的数字",
                        text: $verifyCode,
                        keyboard: .numberPad,
                        identifier: "push-weixin-verify-code",
                        onSubmit: { Task { await submitVerify() } }
                    )
                    .focused($codeFocused)
                    Button("确认") { Task { await submitVerify() } }
                        .font(.subheadline.weight(.semibold))
                        .discoverProminentButton()
                        .disabled(busy || verifyCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        .accessibilityIdentifier("push-weixin-verify-submit")
                }
                .onAppear { codeFocused = true }
            } header: {
                Text("配对码")
            }
        }
    }

    /// 二维码 / 状态常驻一行（副作用挂在这里，行身份不随分支变化）
    private var statusRow: some View {
        VStack(spacing: 0) {
            if let binding {
                SettingsBPushStatusCard {
                    if !terminal {
                        Group {
                            if let qrImage {
                                Image(uiImage: qrImage)
                                    .interpolation(.none)
                                    .resizable()
                                    .scaledToFit()
                            } else {
                                ProgressView().tint(.black)
                            }
                        }
                        .frame(width: 176, height: 176)
                        .padding(12)
                        .background(.white, in: .rect(cornerRadius: 16))
                        .accessibilityLabel("微信绑定二维码")
                        .accessibilityIdentifier("push-weixin-qrcode")
                    }
                    VStack(spacing: 4) {
                        Text(binding.status == "scanned" ? "已扫码" : terminal ? "绑定未完成" : "等待扫码")
                            .font(.body.weight(.medium))
                            .accessibilityIdentifier("push-weixin-status")
                        Text(binding.message).font(.subheadline).foregroundStyle(Theme.textMuted)
                    }
                    if terminal {
                        Button("重新生成二维码") { Task { await begin() } }
                            .font(.subheadline.weight(.semibold))
                            .discoverProminentButton()
                            .disabled(busy)
                            .accessibilityIdentifier("push-weixin-regenerate")
                    }
                }
            } else if busy {
                ProgressView().frame(maxWidth: .infinity, minHeight: 240)
            } else {
                // 发起失败（网关不可达等）：留一个手动重试入口
                SettingsBPushStatusCard {
                    Text("二维码没能生成").font(.body.weight(.medium))
                    Button("重试") { Task { await begin() } }
                        .font(.subheadline.weight(.semibold))
                        .discoverProminentButton()
                        .accessibilityIdentifier("push-weixin-retry")
                }
            }
        }
    }

    private func begin() async {
        error = nil
        verifyCode = ""
        busy = true
        defer { busy = false }
        do {
            let start = try await api.channelsWeixinBindingsStart()
            binding = API.WeixinBindingStatusView(
                challengeId: start.challengeId,
                status: "pending",
                message: start.message,
                qrcodeUrl: start.qrcodeUrl,
                qrcodeImage: start.qrcodeImage,
                account: nil
            )
            qrImage = SettingsBPushQRCode.image(for: start.qrcodeUrl)
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func poll() async {
        guard polling, let current = binding else { return }
        // 轮询失败静默重试（challenge 过期由状态自己表达）
        guard let snap = try? await api.channelsWeixinBindingsStatus(challengeId: current.challengeId) else { return }
        if snap.status == "confirmed" || snap.status == "already_bound" {
            onBound()
            return
        }
        // 二维码过期后服务端会换新码：内容变了才重画
        if snap.qrcodeUrl != binding?.qrcodeUrl {
            qrImage = SettingsBPushQRCode.image(for: snap.qrcodeUrl)
        }
        binding = snap
    }

    private func submitVerify() async {
        let code = verifyCode.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let current = binding, !code.isEmpty else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            _ = try await api.channelsWeixinBindingsVerify(challengeId: current.challengeId, body: .init(code: code))
            verifyCode = ""
            // 提交后先把本地状态推回 scanned，等下一次轮询的服务端判定
            var next = current
            next.status = "scanned"
            next.message = "正在校验配对码…"
            binding = next
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: - 飞书：Webhook 地址即绑即用

private struct SettingsBPushFeishuBody: View {
    let onBound: () -> Void

    @Environment(\.api) private var api
    @State private var webhookUrl = ""
    @State private var secret = ""
    @State private var error: String?
    @State private var busy = false

    private var canSubmit: Bool { !busy && !webhookUrl.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    var body: some View {
        if let error {
            Section {
                SubsNoticeRow(text: error, tone: .error).accessibilityIdentifier("push-bind-error")
            }
        }
        Section {
            SettingsBPushInput(
                placeholder: "https://open.feishu.cn/open-apis/bot/v2/hook/…",
                text: $webhookUrl,
                keyboard: .URL,
                identifier: "push-feishu-webhook",
                onSubmit: { Task { await bind() } }
            )
            // 确认键挂在导航栏右上角（与其它表单弹层一致）；只挂在这一行上，避免分发成多份
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    if busy {
                        ProgressView().accessibilityLabel("接入中")
                    } else {
                        Button("完成接入", systemImage: "checkmark", role: .confirm) { Task { await bind() } }
                            .discoverProminentButton()
                            .disabled(!canSubmit)
                            .accessibilityIdentifier("push-feishu-submit")
                    }
                }
            }
            SettingsBPushInput(
                placeholder: "签名密钥（未开启签名校验可留空）",
                text: $secret,
                identifier: "push-feishu-secret",
                onSubmit: { Task { await bind() } }
            )
        } footer: {
            Text("接入成功会立即向群里发一条欢迎消息，收到即说明通道可用。安全设置若选了「自定义关键词」，推送文案需包含该关键词才会送达，建议改用「签名校验」。")
        }
    }

    private func bind() async {
        let url = webhookUrl.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !busy, !url.isEmpty else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            _ = try await api.channelsImFeishuBind(body: .init(webhookUrl: url, secret: secret.trimmingCharacters(in: .whitespacesAndNewlines)))
            onBound()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: - Telegram / Discord：bot token + 配对码

private struct SettingsBPushTokenBody: View {
    let channel: SettingsBPushChannel
    let onBound: () -> Void

    @Environment(\.api) private var api
    @State private var token = ""
    @State private var binding: API.ImBindingView?
    @State private var error: String?
    @State private var busy = false

    private var canSubmit: Bool { !busy && !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    var body: some View {
        if let error {
            Section {
                SubsNoticeRow(text: error, tone: .error).accessibilityIdentifier("push-bind-error")
            }
        }
        Section {
            mainRow.polling(every: 2) { await poll() }
        }
    }

    /// 输入 bot token → 拿到配对码后换成配对状态；常驻一行，轮询挂在这里
    private var mainRow: some View {
        VStack(spacing: 0) {
            if let binding {
                SettingsBPushStatusCard {
                    if binding.status == "pending" {
                        Text("私聊 @\(binding.botName) 发送配对码").font(.body.weight(.medium))
                        Text(binding.pairCode)
                            .font(.system(size: 32, weight: .bold, design: .monospaced))
                            .tracking(9)
                            .foregroundStyle(Theme.accent)
                            .textSelection(.enabled)
                            .accessibilityIdentifier("push-pair-code")
                        Text("10 分钟内有效 · 发码人将成为唯一可对话的用户与推送目标")
                            .font(.footnote)
                            .foregroundStyle(Theme.textMuted)
                    } else {
                        Text("绑定未完成").font(.body.weight(.medium))
                        Text(binding.message).font(.subheadline).foregroundStyle(Theme.textMuted)
                        Button("重新发起") { self.binding = nil }
                            .font(.subheadline.weight(.semibold))
                            .discoverProminentButton()
                            .accessibilityIdentifier("push-token-restart")
                    }
                }
            } else {
                SettingsBPushInput(
                    placeholder: channel.tokenHint,
                    text: $token,
                    mono: true,
                    identifier: "push-\(channel.rawValue)-token",
                    onSubmit: { Task { await start() } }
                )
            }
        }
        .toolbar {
            if binding == nil {
                ToolbarItem(placement: .confirmationAction) {
                    if busy {
                        ProgressView().accessibilityLabel("校验中")
                    } else {
                        Button("获取配对码", systemImage: "checkmark", role: .confirm) { Task { await start() } }
                            .discoverProminentButton()
                            .disabled(!canSubmit)
                            .accessibilityIdentifier("push-token-submit")
                    }
                }
            }
        }
    }

    private func start() async {
        let value = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !busy, !value.isEmpty else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            binding = try await api.channelsImBindingsStart(channel: channel.rawValue, body: .init(token: value))
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func poll() async {
        guard let current = binding, current.status == "pending" else { return }
        // 轮询失败静默重试（过期由状态自己表达）
        guard let snap = try? await api.channelsImBindingsStatus(channel: channel.rawValue, challengeId: current.challengeId) else { return }
        if snap.status == "confirmed" {
            onBound()
            return
        }
        binding = snap
    }
}
