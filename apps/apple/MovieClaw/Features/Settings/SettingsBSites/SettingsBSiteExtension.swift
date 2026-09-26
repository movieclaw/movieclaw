import SwiftUI

/// 浏览器插件卡片（对应 Web `ExtensionCard`）：嵌在「站点接入」页签底部——插件是站点 Cookie 同步的配套工具，不单设分区。
///
/// 卡面极简：一段说明 + 安装状态 + 两个按钮；安装指引与同步令牌都收进弹层。
/// 安装状态：Web 靠探测 chrome-extension:// 资源判断，手机浏览器上恒为「未检测到」；
/// App 里同样无从探测，照 Web 手机端显示「未检测到」并保留「安装插件」入口（指引可分享到电脑上操作）。
///
/// 注意：弹层不挂在本 Section 上（List 会把 Section 上的修饰符分发给每一行，变成多个呈现者），
/// 由分区根视图统一呈现，这里只回调意图。
struct SettingsBSiteExtSection: View {
    let onInstall: () -> Void
    let onToken: () -> Void

    var body: some View {
        Section {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: "puzzlepiece.extension")
                    .font(.title3)
                    .foregroundStyle(Theme.text)
                    .frame(width: 40, height: 40)
                    .background(Color.white.opacity(0.07), in: .rect(cornerRadius: 11))
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text("MovieClaw 浏览器插件").font(.body.weight(.semibold))
                        Spacer(minLength: 6)
                        HStack(spacing: 5) {
                            SettingsBDot(tone: .neutral)
                            Text("未检测到").font(.footnote).foregroundStyle(Theme.textMuted)
                        }
                        .fixedSize()
                    }
                    Text("在站点页面一键读取登录 Cookie（含 httpOnly）并同步到本服务，免去手动复制粘贴，还能随 Cookie 变化自动保持最新。")
                        .font(.footnote)
                        .foregroundStyle(Theme.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.vertical, 4)

            HStack(spacing: 10) {
                Button {
                    onInstall()
                } label: {
                    Label("安装插件", systemImage: "arrow.down.circle").font(.subheadline.weight(.semibold))
                }
                .discoverProminentButton()
                .accessibilityIdentifier("ext-install")
                Button {
                    onToken()
                } label: {
                    Label("同步令牌", systemImage: "checkmark.shield").font(.subheadline.weight(.medium))
                }
                .buttonStyle(.glass)
                .accessibilityIdentifier("ext-token")
            }
            .padding(.vertical, 2)
        }
    }
}

/// 安装指引（对应 Web `InstallModal`）：四步明确操作。Chrome 政策不允许商店外插件静默安装，
/// 须在 chrome://extensions 手动加载——插件只能装在电脑浏览器上，所以第 1 步给「分享下载地址」（隔空投送到电脑）。
struct SettingsBSiteExtInstallSheet: View {
    let onOpenToken: () -> Void
    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        SubsSheetScaffold(
            title: "安装浏览器插件",
            subtitle: "按下面四步操作，全程约一分钟。Chrome 应用商店政策不允许商店外插件一键安装，所以需要手动加载一次，之后升级会自动提示。",
            closeTitle: "完成"
        ) {
            Section {
                step(1, Text("下载插件包并解压，得到 \(Text("chrome-mv3").font(.footnote.monospaced())) 文件夹。")) {
                    ShareLink(item: api.server.origin.appending(path: "extension/movieclaw-extension.zip")) {
                        Label("下载插件包", systemImage: "arrow.down.circle").font(.subheadline.weight(.semibold))
                    }
                    .discoverProminentButton()
                    .accessibilityIdentifier("ext-download")
                }
                step(2, Text("浏览器地址栏打开 \(Text("chrome://extensions").font(.footnote.monospaced()))，右上角开启「开发者模式」。"))
                step(3, Text("点「加载已解压的扩展程序」，选择第 1 步解压出的文件夹。"))
                step(4, Text("生成同步令牌并填入插件设置，之后切回本页会自动识别为「已安装」。")) {
                    Button {
                        onOpenToken()
                    } label: {
                        Label("去生成令牌", systemImage: "checkmark.shield").font(.subheadline.weight(.medium))
                    }
                    .buttonStyle(.glass)
                    .accessibilityIdentifier("ext-goto-token")
                }
            } footer: {
                Text("支持 Chrome / Edge 等 Chromium 内核浏览器；安装检测同样仅对 Chromium 生效。")
            }
        }
    }

    private func step(_ n: Int, _ text: Text) -> some View {
        step(n, text) { EmptyView() }
    }

    /// 一步一行：序号圆点 + 说明 + 可选的动作按钮
    private func step<A: View>(_ n: Int, _ text: Text, @ViewBuilder action: () -> A) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Text("\(n)")
                .font(.footnote.weight(.semibold))
                .frame(width: 24, height: 24)
                .background(Color.white.opacity(0.1), in: .circle)
            VStack(alignment: .leading, spacing: 8) {
                text.font(.subheadline).foregroundStyle(Theme.text.opacity(0.88)).fixedSize(horizontal: false, vertical: true)
                action()
            }
            .padding(.top, 2)
            Spacer(minLength: 0)
        }
        .padding(.vertical, 4)
    }
}

/// 同步令牌管理（对应 Web `TokenModal`）：打开时才拉取（低频配置不随页面加载）。
/// 生成 / 查看 / 复制 / 重新生成 / 关闭同步都收在这里；令牌默认打码，点「显示」才明文。
struct SettingsBSiteExtTokenSheet: View {
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @State private var token: API.SyncTokenView?
    @State private var loading = true
    @State private var busy = false
    @State private var error: String?
    @State private var revealed = false

    var body: some View {
        SubsSheetScaffold(
            title: "同步令牌",
            subtitle: "在浏览器插件的设置里填入此令牌，即可把站点 Cookie 同步到本服务。令牌长期有效，除非你重新生成。",
            closeTitle: "完成",
            ready: !loading
        ) {
            if let error {
                Section { SubsNoticeRow(text: error, tone: .error) }
            }
            Section {
                LabeledContent("状态") {
                    HStack(spacing: 5) {
                        SettingsBDot(tone: token?.enabled == true ? .ok : .neutral)
                        Text(token?.enabled == true ? "已启用" : "未启用")
                    }
                }
                .accessibilityIdentifier("ext-token-status")
                if loading {
                    ProgressView().frame(maxWidth: .infinity)
                } else if let token, token.enabled {
                    tokenRow(token.token ?? "")
                }
            } footer: {
                if !loading {
                    if let token, token.enabled, let createdAt = token.createdAt {
                        Text("生成于 \(SettingsBSiteFormat.dateTime(createdAt))")
                    } else if token?.enabled != true {
                        Text("尚未启用同步。点击下方「生成令牌」创建一个。")
                    }
                }
            }
            Section {
                Button {
                    Task { await generate() }
                } label: {
                    HStack {
                        Label(token?.enabled == true ? "重新生成" : "生成令牌", systemImage: "key")
                        Spacer()
                        if busy { ProgressView() }
                    }
                }
                .disabled(busy || loading)
                .accessibilityIdentifier("ext-token-generate")
                if token?.enabled == true {
                    Button(role: .destructive) {
                        Task { await revoke() }
                    } label: {
                        Label("关闭同步", systemImage: "xmark.shield")
                    }
                    .disabled(busy || loading)
                    .accessibilityIdentifier("ext-token-revoke")
                }
            }
        }
        .task { await load() }
    }

    /// 令牌行：默认打码，「显示」才明文；复制键就在旁边
    private func tokenRow(_ value: String) -> some View {
        HStack(spacing: 8) {
            Text(revealed ? value : String(repeating: "•", count: min(value.count, 28)))
                .font(.footnote.monospaced())
                .lineLimit(1)
                .truncationMode(.middle)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityIdentifier("ext-token-value")
            Button(revealed ? "隐藏" : "显示") { revealed.toggle() }
                .buttonStyle(.glass)
                .accessibilityIdentifier("ext-token-reveal")
            Button {
                UIPasteboard.general.string = value
                feedback.success("已复制")
            } label: {
                Image(systemName: "doc.on.doc")
            }
            .buttonStyle(.glass)
            .accessibilityLabel("复制令牌")
            .accessibilityIdentifier("ext-token-copy")
        }
    }

    private func load() async {
        loading = true
        error = nil
        defer { loading = false }
        do {
            token = try await api.extensionTokenShow()
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func generate() async {
        if token?.enabled == true {
            let ok = await feedback.confirm("重新生成同步令牌？", message: "重新生成将使旧令牌立即失效，已配置的插件需要更新令牌。",
                                            confirmTitle: "重新生成", destructive: true)
            guard ok else { return }
        }
        busy = true
        error = nil
        defer { busy = false }
        do {
            token = try await api.extensionTokenCreate()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func revoke() async {
        let ok = await feedback.confirm("关闭 Cookie 同步？", message: "关闭同步将撤销令牌，所有插件都将无法再同步。",
                                        confirmTitle: "关闭同步", destructive: true)
        guard ok else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            token = try await api.extensionTokenRevoke()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
