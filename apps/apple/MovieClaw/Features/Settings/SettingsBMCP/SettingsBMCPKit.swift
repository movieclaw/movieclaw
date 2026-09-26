import SwiftUI

// 「设置 → MCP 服务」的共享状态与小件（对应 Web `components/mcp/ui.tsx` + `mcp-section.tsx` 的数据层）。
//
// 这一页的用户是要把端点接进 Claude Code / Cursor 的开发者，基调沿用 Web 的三条：
// 1. 标识符（地址、令牌、工具名、参数名）一律等宽字体，且配复制按钮——它们是要粘到别处去的；
// 2. 信息密度优先：列表每行把「状态 / 地址 / 服务 / 工具数 / 最近调用」一次交代完；
// 3. 破坏性操作分层：日常操作在行内，删除沉到详情「设置」页底部的危险区，且要打字确认。

// MARK: - 共享状态

/// 列表页与端点详情页共用的一份状态。
///
/// 为什么用一个 @Observable 对象而不是各页各拉各的：详情页的每个写操作（启停、保存、轮换、删除）
/// 之后都要整份重拉 `GET /mcp/status`（Web 的 `run()` 同样如此），列表页返回时应当立刻看到新状态；
/// 详情页按端点 id 从这里现取，不持有自己的副本，就不会出现两页数据不一致。
@Observable
final class SettingsBMCPStore {
    var status: API.StatusView?
    /// 错误横幅（列表、新建、详情三处共用同一条，同 Web banner）
    var error: String?
    /// 写操作进行中：禁用会触发写的按钮
    var busy = false

    func load(_ api: APIClient) async {
        do {
            status = try await api.mcpStatus()
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// 执行一个写操作：忙碌态 + 清错误 + 成功后整份重拉；失败把后端中文原因写进横幅并返回 nil
    func run<T>(_ api: APIClient, _ action: () async throws -> T) async -> T? {
        busy = true
        error = nil
        defer { busy = false }
        do {
            let result = try await action()
            await load(api)
            return result
        } catch {
            self.error = error.localizedDescription
            return nil
        }
    }

    func endpoint(id: String) -> API.EndpointView? {
        status?.endpoints.first { $0.id == id }
    }

    /// 端点完整地址：后端已给绝对地址就原样用；否则（未配外部地址）拼上前缀或占位
    func fullURL(_ endpoint: API.EndpointView) -> String {
        if endpoint.url.hasPrefix("http") { return endpoint.url }
        let base = status?.baseUrl ?? ""
        return (base.isEmpty ? "http://<你的地址>" : base) + endpoint.url
    }
}

// MARK: - 格式化

enum SettingsBMCPFormat {
    /// 定义体积：≥1KB 一位小数 KB，否则字节（同 Web formatBytes）
    static func bytes(_ n: Int) -> String {
        n >= 1024 ? String(format: "%.1f KB", Double(n) / 1024) : "\(n) B"
    }

    /// 最近调用（同 Web `devices-display.relativeTime`：分钟 / 小时 / 天三档，1 分钟内「刚刚活跃」）
    static func relative(_ raw: String?) -> String {
        guard let raw else { return "从未使用" }
        guard let date = Formatters.date(raw) else { return "未知" }
        let minutes = Int(Date.now.timeIntervalSince(date) / 60)
        if minutes < 1 { return "刚刚活跃" }
        if minutes < 60 { return "\(minutes) 分钟前" }
        let hours = minutes / 60
        if hours < 24 { return "\(hours) 小时前" }
        return "\(hours / 24) 天前"
    }

    static func mode(_ expand: Bool) -> String { expand ? "一命令一工具" : "一服务一工具" }
}

// MARK: - 小件

/// 状态点：绿 = 在跑，灰 = 停用
struct SettingsBMCPStatusDot: View {
    let on: Bool
    var body: some View {
        Circle()
            .fill(on ? Theme.success : Color.white.opacity(0.25))
            .frame(width: 6, height: 6)
            .accessibilityLabel(on ? "运行中" : "已停用")
    }
}

/// 等宽描边小标（「只读」「破坏性」）
struct SettingsBMCPBadge: View {
    let text: String
    var danger = false
    var body: some View {
        Text(text)
            .font(.system(size: 11).monospaced())
            .foregroundStyle(danger ? Theme.danger : Theme.textMuted)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(danger ? Theme.danger.opacity(0.4) : Color.white.opacity(0.12)))
            .fixedSize()
    }
}

/// 服务标签组：每个服务域一个描边小标，超过 max 的收成「+N」（同 Web ServiceChips）
struct SettingsBMCPServiceChips: View {
    let services: [String]
    var max = 4

    var body: some View {
        if services.isEmpty {
            Text("未选服务").font(.caption).foregroundStyle(Theme.textFaint)
        } else {
            SettingsBFlow(spacing: 4, lineSpacing: 4) {
                ForEach(services.prefix(max), id: \.self) { service in
                    Text(service)
                        .font(.system(size: 11).monospaced())
                        .foregroundStyle(Theme.textMuted)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1)
                        .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 6))
                        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.white.opacity(0.1)))
                }
                if services.count > max {
                    Text("+\(services.count - max)")
                        .font(.system(size: 11).monospaced())
                        .foregroundStyle(Theme.textFaint)
                }
            }
        }
    }
}

/// 可复制的等宽值框（地址、令牌）：左值右「复制」
struct SettingsBMCPCopyField: View {
    let value: String
    let label: String
    var identifier: String?
    @Environment(Feedback.self) private var feedback

    var body: some View {
        HStack(spacing: 8) {
            Text(value)
                .font(.footnote.monospaced())
                .lineLimit(2)
                .truncationMode(.middle)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button(label) {
                UIPasteboard.general.string = value
                feedback.success("已复制")
            }
            .font(.caption.weight(.medium))
            .buttonStyle(.glass)
            .accessibilityIdentifier(identifier ?? label)
        }
        .padding(.leading, 12)
        .padding(.trailing, 6)
        .padding(.vertical, 6)
        .background(Color.black.opacity(0.25), in: .rect(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.white.opacity(0.08)))
    }
}

/// 带语言标签与复制按钮的代码块（接入指引里的命令、认证头）
struct SettingsBMCPCodeBlock: View {
    let code: String
    let lang: String
    var identifier: String?
    @Environment(Feedback.self) private var feedback

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(lang.uppercased())
                    .font(.system(size: 11).monospaced())
                    .tracking(1)
                    .foregroundStyle(Theme.textFaint)
                Spacer()
                Button("复制") {
                    UIPasteboard.general.string = code
                    feedback.success("已复制")
                }
                .font(.caption.weight(.medium))
                .buttonStyle(.glass)
                .accessibilityIdentifier(identifier ?? "copy-code")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            Divider().overlay(Color.white.opacity(0.06))
            ScrollView(.horizontal, showsIndicators: false) {
                Text(code)
                    .font(.caption.monospaced())
                    .foregroundStyle(Theme.text)
                    .textSelection(.enabled)
                    .fixedSize()
                    .padding(.horizontal, 12)
                    .padding(.vertical, 10)
            }
        }
        .background(Color.black.opacity(0.35), in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.08)))
    }
}

/// 令牌专屏：签发（新建 / 轮换）后必须先过这一关。
///
/// 令牌明文只在这一次响应里出现、服务端只存哈希，所以它独占整个弹层、禁止下滑关闭，
/// 只有一个出口「我已保存，去看端点」（同 Web TokenIssued）。
struct SettingsBMCPTokenIssuedView: View {
    let name: String
    let url: String
    let token: String
    let onDone: () -> Void

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("保存「\(name)」的令牌").font(.title3.weight(.medium))
                        Text("令牌只显示这一次。离开这一屏就再也看不到明文——服务端只保存哈希。丢了不要紧，随时可以轮换出一枚新的（旧的立即失效）。")
                            .font(.subheadline)
                            .foregroundStyle(Theme.danger)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        Text("端点地址").font(.caption).foregroundStyle(Theme.textMuted)
                        SettingsBMCPCopyField(value: url, label: "复制地址", identifier: "mcp-issued-copy-url")
                        Text("访问令牌").font(.caption).foregroundStyle(Theme.textMuted).padding(.top, 4)
                        SettingsBMCPCopyField(value: token, label: "复制令牌", identifier: "mcp-issued-copy-token")
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        Text("在客户端加上它").font(.caption).foregroundStyle(Theme.textMuted)
                        SettingsBMCPCodeBlock(
                            code: "claude mcp add --transport http movieclaw \\\n  \(url) \\\n  --header \"Authorization: Bearer \(token)\"",
                            lang: "bash",
                            identifier: "mcp-issued-copy-command"
                        )
                    }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .safeAreaBar(edge: .bottom) {
                SubsPrimaryButton(title: "我已保存，去看端点", identifier: "mcp-issued-done", action: onDone)
                    .padding(.horizontal, 20)
                    .padding(.vertical, 12)
            }
            .navigationTitle("访问令牌")
            .navigationBarTitleDisplayMode(.inline)
            .background(Theme.background.opacity(0.35))
        }
        .presentationBackground(.regularMaterial)
        .interactiveDismissDisabled()
    }
}
