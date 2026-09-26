import SwiftUI

// 设置（下）九个分区共用的小积木。
//
// 设置页统一用系统 `Form`（插入分组样式）承载：每个 Web 卡片对应一个 `Section`，
// 卡片标题 → Section 头，卡片说明 → Section 尾注或 `SettingsBIntro`。
// 这样手机上的层级、间距、点按区域都交给系统，信息与操作逐项对齐 Web 即可。
// 文件名与类型名一律带 `SettingsB` 前缀：设置（上）在另一个工作树并行开发，避免同名冲突。

// MARK: - 页面骨架

extension View {
    /// 设置分区页的统一外观：隐藏表单默认底色，露出 App 深色背景
    func settingsBFormStyle() -> some View {
        formStyle(.grouped)
            .scrollContentBackground(.hidden)
            .appBackground()
    }
}

/// 分区顶部的说明段落（对应 Web 标题下的灰色说明文字）
struct SettingsBIntro: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.footnote)
            .foregroundStyle(Theme.textMuted)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - 徽标与状态点

/// 语义色（与 Web 的 ok / warn / danger / info 同一套）
enum SettingsBTone {
    case ok, warn, danger, info, neutral, accent

    var color: Color {
        switch self {
        case .ok: Theme.success
        case .warn: Theme.warning
        case .danger: Theme.danger
        case .info: Theme.info
        case .neutral: Theme.textMuted
        case .accent: Theme.accent
        }
    }
}

/// 名字旁的小胶囊标签（「默认」「已停用」「验证中」）
struct SettingsBBadge: View {
    let text: String
    var tone: SettingsBTone = .neutral

    var body: some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(tone == .neutral ? Theme.text.opacity(0.75) : tone.color)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background((tone == .neutral ? Color.white : tone.color).opacity(tone == .neutral ? 0.08 : 0.14), in: .capsule)
            .overlay(Capsule().strokeBorder((tone == .neutral ? Color.white : tone.color).opacity(0.16)))
            .lineLimit(1)
            .fixedSize()
    }
}

/// 状态点
struct SettingsBDot: View {
    var tone: SettingsBTone
    var size: CGFloat = 7
    var body: some View {
        Circle().fill(tone.color).frame(width: size, height: size)
    }
}

/// 规格芯片（小灰底圆角文字，如规则组摘要「2160p > 1080p」）
struct SettingsBChip: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(Theme.text.opacity(0.75))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Color.white.opacity(0.07), in: .rect(cornerRadius: 6))
    }
}

/// 提示条（红 = 错误、琥珀 = 注意、蓝 = 信息、绿 = 正向）
struct SettingsBNotice: View {
    let text: String
    var tone: SettingsBTone = .info

    var body: some View {
        Text(text)
            .font(.footnote)
            .foregroundStyle(tone.color.mix(with: .white, by: 0.45))
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .background(tone.color.opacity(0.1), in: .rect(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(tone.color.opacity(0.25)))
    }
}

// MARK: - 表单行

/// 左标签右值的只读行（值可选等宽字体、可复制）
struct SettingsBValueRow: View {
    let label: String
    let value: String
    var mono = false
    var copyable = false
    @Environment(Feedback.self) private var feedback

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(label).foregroundStyle(Theme.textMuted)
            Spacer(minLength: 8)
            Text(value)
                .font(mono ? .footnote.monospaced() : .body)
                .foregroundStyle(Theme.text)
                .multilineTextAlignment(.trailing)
                .lineLimit(3)
                .truncationMode(.middle)
                .textSelection(.enabled)
            if copyable {
                Button {
                    UIPasteboard.general.string = value
                    feedback.success("已复制")
                } label: {
                    Image(systemName: "doc.on.doc")
                }
                .buttonStyle(.borderless)
                .accessibilityLabel("复制\(label)")
            }
        }
    }
}

/// 带标签的文本输入（标签在上，适合长地址 / 路径）
struct SettingsBTextField: View {
    let label: String
    @Binding var text: String
    var placeholder = ""
    var hint: String?
    var mono = false
    var secure = false
    var keyboard: UIKeyboardType = .default
    var identifier: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.subheadline).foregroundStyle(Theme.textMuted)
            Group {
                if secure {
                    SecureField(placeholder, text: $text)
                } else {
                    TextField(placeholder, text: $text, axis: .horizontal)
                }
            }
            .font(mono ? .body.monospaced() : .body)
            .keyboardType(keyboard)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .accessibilityIdentifier(identifier ?? label)
            if let hint {
                Text(hint).font(.caption).foregroundStyle(Theme.textFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 2)
    }
}

/// 数字输入（空串 = 不限 / 未填）
struct SettingsBNumberField: View {
    let label: String
    @Binding var text: String
    var placeholder = ""
    var unit: String?
    var identifier: String?

    var body: some View {
        HStack {
            Text(label)
            Spacer()
            TextField(placeholder, text: $text)
                .keyboardType(.numberPad)
                .multilineTextAlignment(.trailing)
                .frame(maxWidth: 120)
                .accessibilityIdentifier(identifier ?? label)
            if let unit { Text(unit).foregroundStyle(Theme.textMuted) }
        }
    }
}

// MARK: - 忙碌按钮

/// 执行异步操作的按钮：执行期间显示转圈并禁用（Web 端 busy 态的按钮）
struct SettingsBAsyncButton<Label: View>: View {
    var role: ButtonRole?
    let action: () async -> Void
    @ViewBuilder let label: () -> Label
    @State private var running = false

    var body: some View {
        Button(role: role) {
            guard !running else { return }
            running = true
            Task {
                await action()
                running = false
            }
        } label: {
            HStack(spacing: 8) {
                label()
                if running { ProgressView().controlSize(.small) }
            }
        }
        .disabled(running)
    }
}

extension SettingsBAsyncButton where Label == Text {
    init(_ title: String, role: ButtonRole? = nil, action: @escaping () async -> Void) {
        self.init(role: role, action: action) { Text(title) }
    }
}

// MARK: - 格式化

enum SettingsBFormat {
    /// 字节 → 「1.5 TB」（与 Web formatBytes 同口径：1024 进制、两位小数以内）
    static func bytes(_ value: Int?) -> String {
        guard let value else { return "—" }
        return bytes(Double(value))
    }

    static func bytes(_ value: Double) -> String {
        let units = ["B", "KB", "MB", "GB", "TB", "PB"]
        var v = value
        var i = 0
        while abs(v) >= 1024, i < units.count - 1 {
            v /= 1024
            i += 1
        }
        if i == 0 { return "\(Int(v)) B" }
        return String(format: v >= 100 ? "%.0f %@" : v >= 10 ? "%.1f %@" : "%.2f %@", v, units[i])
    }

    /// 速率 KB/s → 「1.2 MB/s」；0 或 nil = 不限
    static func speedKB(_ kb: Int?) -> String {
        guard let kb, kb > 0 else { return "不限" }
        return bytes(Double(kb) * 1024) + "/s"
    }

    /// 相对时间（空值给「—」）
    static func relative(_ raw: String?) -> String {
        let text = Formatters.relative(raw)
        return text.isEmpty ? "—" : text
    }
}

// MARK: - 目录选择器

/// 服务端目录选择器（对应 Web `components/directory-picker.tsx`）。
///
/// 下载器路径映射、自动入库源目录/目标目录都要填服务器上的绝对路径，手打极易出错：
/// 这里用 `GET /fs/browse?path=` 逐级下钻，顶部面包屑可跳回任意一层，
/// 右上角铅笔切换手动输入（记得路径时不必逐级点）。起始目录无效时回落根目录。
struct SettingsBDirectoryPicker: View {
    var initialPath: String?
    let onSelect: (String) -> Void

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss
    @State private var view: API.FsBrowseView?
    @State private var loading = false
    @State private var error: String?
    @State private var editing = false
    @State private var draft = ""

    var body: some View {
        NavigationStack {
            List {
                Section {
                    if editing {
                        TextField("输入绝对路径后回车跳转", text: $draft)
                            .font(.body.monospaced())
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .onSubmit { Task { await navigate(draft.trimmingCharacters(in: .whitespaces)) } }
                    } else {
                        breadcrumb
                    }
                    if let error {
                        Text(error).font(.footnote).foregroundStyle(Theme.danger)
                    }
                }
                Section {
                    if loading && view == nil {
                        ProgressView().frame(maxWidth: .infinity)
                    } else if let view, view.entries.isEmpty {
                        Text("该目录下没有子目录，可直接选择当前目录")
                            .foregroundStyle(Theme.textMuted)
                    } else if let view {
                        if let parent = view.parent {
                            Button {
                                Task { await navigate(parent) }
                            } label: {
                                Label("上一级", systemImage: "arrow.turn.left.up")
                            }
                        }
                        ForEach(view.entries, id: \.path) { entry in
                            Button {
                                Task { await navigate(entry.path) }
                            } label: {
                                HStack {
                                    Label(entry.name, systemImage: "folder")
                                        .foregroundStyle(Theme.text)
                                    Spacer()
                                    Image(systemName: "chevron.right").font(.caption).foregroundStyle(Theme.textFaint)
                                }
                            }
                            .disabled(loading)
                        }
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .navigationTitle("选择服务器目录")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消", systemImage: "xmark") { dismiss() }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("手动输入路径", systemImage: editing ? "list.bullet" : "pencil") {
                        draft = view?.path ?? "/"
                        editing.toggle()
                    }
                }
            }
            .safeAreaBar(edge: .bottom) {
                HStack(spacing: 12) {
                    Text(view?.path ?? "…")
                        .font(.footnote.monospaced())
                        .foregroundStyle(Theme.textMuted)
                        .lineLimit(1)
                        .truncationMode(.head)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Button {
                        if let path = view?.path {
                            onSelect(path)
                            dismiss()
                        }
                    } label: {
                        Label("选择此目录", systemImage: "checkmark").font(.body.weight(.semibold))
                    }
                    .discoverProminentButton()
                    .disabled(view == nil || loading)
                    .accessibilityIdentifier("directory-picker-select")
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 12)
            }
        }
        .presentationBackground(.regularMaterial)
        .task { await open() }
    }

    /// 面包屑：根 "/" + 逐级路径段，点任意一段跳回该层
    private var breadcrumb: some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 2) {
                    Button("/") { Task { await navigate("/") } }
                        .font(.body.monospaced())
                    ForEach(segments, id: \.path) { seg in
                        Image(systemName: "chevron.right").font(.caption2).foregroundStyle(Theme.textFaint)
                        Button(seg.name) { Task { await navigate(seg.path) } }
                            .fontWeight(seg.path == view?.path ? .semibold : .regular)
                            .foregroundStyle(seg.path == view?.path ? Theme.text : Theme.textMuted)
                            .id(seg.path)
                    }
                }
                .buttonStyle(.borderless)
            }
            .onChange(of: view?.path) { _, path in
                if let path { proxy.scrollTo(path, anchor: .trailing) }
            }
        }
    }

    private var segments: [(name: String, path: String)] {
        guard let path = view?.path else { return [] }
        let parts = path.split(separator: "/").map(String.init)
        return parts.indices.map { i in (parts[i], "/" + parts[0...i].joined(separator: "/")) }
    }

    private func open() async {
        loading = true
        defer { loading = false }
        do {
            view = try await api.fsBrowse(path: initialPath?.isEmpty == false ? initialPath : nil)
        } catch {
            do { view = try await api.fsBrowse() } catch { self.error = error.localizedDescription }
        }
    }

    private func navigate(_ path: String) async {
        loading = true
        error = nil
        defer { loading = false }
        do {
            view = try await api.fsBrowse(path: path)
            editing = false
        } catch {
            // 跳转失败保留当前列表，只提示错误（如手动输入了不存在的路径）
            self.error = error.localizedDescription
        }
    }
}

/// 路径输入 + 「浏览」按钮（打开目录选择器）
struct SettingsBPathField: View {
    let label: String
    @Binding var path: String
    var placeholder = "/"
    var hint: String?
    var identifier: String?
    @State private var picking = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.subheadline).foregroundStyle(Theme.textMuted)
            HStack {
                TextField(placeholder, text: $path)
                    .font(.body.monospaced())
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .accessibilityIdentifier(identifier ?? label)
                Button("浏览", systemImage: "folder") { picking = true }
                    .labelStyle(.iconOnly)
                    .buttonStyle(.borderless)
            }
            if let hint {
                Text(hint).font(.caption).foregroundStyle(Theme.textFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .sheet(isPresented: $picking) {
            SettingsBDirectoryPicker(initialPath: path) { path = $0 }
                .sheetFeedback()
        }
    }
}

// MARK: - 流式换行布局

/// 芯片墙用的换行布局（SwiftUI 没有内置 flex-wrap）
struct SettingsBFlow: Layout {
    var spacing: CGFloat = 6
    var lineSpacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, lineHeight: CGFloat = 0, maxX: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width {
                x = 0
                y += lineHeight + lineSpacing
                lineHeight = 0
            }
            x += size.width + spacing
            maxX = max(maxX, x - spacing)
            lineHeight = max(lineHeight, size.height)
        }
        return CGSize(width: proposal.width ?? maxX, height: y + lineHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, lineHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX {
                x = bounds.minX
                y += lineHeight + lineSpacing
                lineHeight = 0
            }
            view.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            lineHeight = max(lineHeight, size.height)
        }
    }
}

/// 可多选芯片（选中高亮），用于分类、事件、能力等多选
struct SettingsBSelectChip: View {
    let title: String
    let selected: Bool
    var identifier: String?
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                if selected { Image(systemName: "checkmark").font(.caption2.weight(.bold)) }
                Text(title).font(.footnote.weight(.medium))
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .foregroundStyle(selected ? Theme.background : Theme.text.opacity(0.85))
            .background(selected ? Theme.accentStrong : Color.white.opacity(0.07), in: .capsule)
            .overlay(Capsule().strokeBorder(Color.white.opacity(selected ? 0 : 0.1)))
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier(identifier ?? title)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}
