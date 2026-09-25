import SwiftUI

/// 消息推送 →「推送内容」标签（对应 Web `PushContentTab`）。
///
/// 事件开关对所有已接入通道统一生效，逐项即时保存（`PUT /channels/im/push-config`）：
/// 乐观更新，失败回滚并在顶部报错——与 Web 同一交互，没有「保存」按钮。
/// 测试推送（`POST /channels/im/push-test`）会真的给所有已接入通道发一条消息。
struct SettingsBPushContentSections: View {
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback

    @State private var config: API.ChannelPushConfigView?
    @State private var error: String?
    @State private var pushBusy = false

    private let rows: [(keyPath: WritableKeyPath<API.ChannelPushConfigView, Bool>, id: String, label: String, desc: String)] = [
        (\.pushDispatch, "dispatch", "开始下载", "订阅命中资源并提交下载器时"),
        (\.pushImported, "imported", "入库完成", "下载完成整理进媒体库时"),
    ]

    var body: some View {
        Section {
            SettingsBIntro(text: "这里的开关对所有已接入通道统一生效——关掉某个事件，任何通道都不会再收到它。")
                .task { await load() } // 挂在具体行上，避免 Section 修饰符被分发到每一行
            if let error {
                SettingsBNotice(text: error, tone: .danger)
                    .accessibilityIdentifier("push-content-error")
            }
        }

        Section {
            if let config {
                ForEach(rows, id: \.id) { row in
                    Toggle(isOn: Binding(
                        get: { config[keyPath: row.keyPath] },
                        set: { value in Task { await toggle(row.keyPath, value) } }
                    )) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(row.label).font(.body.weight(.medium))
                            Text(row.desc).font(.caption).foregroundStyle(Theme.textFaint)
                        }
                    }
                    .accessibilityLabel("\(row.label)推送")
                    .accessibilityIdentifier("push-toggle-\(row.id)")
                }
            } else {
                ProgressView().frame(maxWidth: .infinity, minHeight: 88)
            }
        }

        // 测试推送：验证已接入通道确实能收到系统事件
        Section {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("测试推送").font(.body.weight(.medium))
                    Text("向所有已接入通道发送一条测试消息").font(.caption).foregroundStyle(Theme.textFaint)
                }
                Spacer(minLength: 8)
                SettingsBAsyncButton("发送测试") { await testPush() }
                    .font(.subheadline.weight(.medium))
                    .buttonStyle(.glass)
                    .disabled(pushBusy)
                    .accessibilityIdentifier("push-test")
            }
        }
    }

    private func load() async {
        do {
            config = try await api.channelsImPushConfigGet()
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func toggle(_ keyPath: WritableKeyPath<API.ChannelPushConfigView, Bool>, _ value: Bool) async {
        guard let previous = config else { return }
        var next = previous
        next[keyPath: keyPath] = value
        config = next // 乐观更新，失败回滚
        error = nil
        do {
            _ = try await api.channelsImPushConfigUpdate(body: next)
        } catch {
            config = previous
            self.error = error.localizedDescription
        }
    }

    private func testPush() async {
        pushBusy = true
        defer { pushBusy = false }
        do {
            let result = try await api.channelsImPushTest(body: .init(text: ""))
            feedback.success("已推送到 \(result["sent"]?.intValue ?? 0) 个账号，去客户端看看吧")
        } catch {
            feedback.error(error.localizedDescription)
        }
    }
}
