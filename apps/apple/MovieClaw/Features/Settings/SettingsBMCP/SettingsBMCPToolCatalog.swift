import SwiftUI

/// 工具目录：这个端点到底把什么交给了模型（对应 Web `mcp/tool-catalog.tsx`）。
///
/// 按开发者在这里真正要回答的三个问题组织，产出若干 `Section` 嵌进详情页的 `Form`：
/// 1. **有没有我要的那个工具** → 顶部搜索（工具名、说明、折叠模式下的命令名一起匹配）+「全部 / 只读 / 会改动」；
/// 2. **这个工具属于哪个服务** → 按服务分组，组头带数量，点组头整组折叠；
/// 3. **它要什么参数** → 点开一行：展开模式列参数（名称 / 类型 / 必填 / 落点 / 说明 / 可选值），
///    折叠模式列命令表（命令 / 说明 / params 字段 / 风险标记），并可复制工具名与调用样例。
/// Web 窄屏把表格改成堆叠块，这里同样一条一块，标识符宁可占满一行也不断词。
struct SettingsBMCPToolCatalog: View {
    let tools: [API.ToolPreview]

    private enum Filter: String, CaseIterable, Identifiable {
        case all = "全部", read = "只读", write = "会改动"
        var id: String { rawValue }
    }

    @State private var query = ""
    @State private var only: Filter = .all
    @State private var openTool: String?
    @State private var collapsed: Set<String> = []

    private var keyword: String { query.trimmingCharacters(in: .whitespaces).lowercased() }

    private var groups: [(service: String, tools: [API.ToolPreview])] {
        let matched = tools.filter { t in
            if only == .read && !t.readOnly { return false }
            if only == .write && t.readOnly { return false }
            if keyword.isEmpty { return true }
            return t.name.lowercased().contains(keyword)
                || t.description.lowercased().contains(keyword)
                // 折叠模式下用户搜的多半是命令名
                || t.commands.contains { "\($0.name) \($0.summary)".lowercased().contains(keyword) }
        }
        return Dictionary(grouping: matched, by: \.service)
            .map { (service: $0.key, tools: $0.value) }
            .sorted { $0.service < $1.service }
    }

    var body: some View {
        let groups = self.groups
        let shown = groups.reduce(0) { $0 + $1.tools.count }
        Section {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(Theme.textFaint)
                TextField("搜索工具名或说明", text: $query)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .accessibilityIdentifier("mcp-tools-search")
                if !query.isEmpty {
                    Button("清空搜索", systemImage: "xmark.circle.fill") { query = "" }
                        .labelStyle(.iconOnly)
                        .buttonStyle(.borderless)
                        .foregroundStyle(Theme.textFaint)
                }
            }
            // 只读 / 会改动：判断「这个端点危不危险」最快的一刀
            Picker("筛选", selection: $only) {
                ForEach(Filter.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .accessibilityIdentifier("mcp-tools-filter")
        } footer: {
            Text(shown == tools.count ? "\(tools.count) 个工具" : "\(shown) / \(tools.count) 个工具")
                .accessibilityIdentifier("mcp-tools-count")
        }

        if shown == 0 {
            Section {
                Text("没有匹配「\(query)」的工具")
                    .foregroundStyle(Theme.textMuted)
                    .frame(maxWidth: .infinity)
            }
        }

        ForEach(groups, id: \.service) { group in
            let folded = collapsed.contains(group.service)
            Section {
                if !folded {
                    ForEach(group.tools, id: \.name) { tool in
                        toolRow(tool)
                    }
                }
            } header: {
                Button {
                    if folded { collapsed.remove(group.service) } else { collapsed.insert(group.service) }
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: folded ? "chevron.right" : "chevron.down").font(.caption2)
                        Text(group.service).font(.subheadline.monospaced()).foregroundStyle(Theme.text)
                        Text("\(group.tools.count) 个工具").font(.caption).foregroundStyle(Theme.textFaint)
                        Spacer()
                    }
                    .textCase(nil)
                    .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("mcp-tools-group-\(group.service)")
            }
        }
    }

    @ViewBuilder
    private func toolRow(_ tool: API.ToolPreview) -> some View {
        let open = openTool == tool.name
        Button {
            withAnimation(.snappy) { openTool = open ? nil : tool.name }
        } label: {
            HStack(alignment: .top, spacing: 10) {
                // 两行：工具名与说明各占一行；挤成一行说明必被截断，而它恰恰最该看清
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(tool.name).font(.subheadline.monospaced()).foregroundStyle(Theme.accent)
                            .fixedSize(horizontal: false, vertical: true)
                        if tool.readOnly { SettingsBMCPBadge(text: "只读") }
                        if tool.destructive { SettingsBMCPBadge(text: "破坏性", danger: true) }
                    }
                    Text(tool.summary.isEmpty ? tool.description : tool.summary)
                        .font(.caption)
                        .foregroundStyle(Theme.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 4)
                Text(tool.commands.isEmpty ? "\(tool.parameters.count) 参数" : "\(tool.commands.count) 命令")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(Theme.textFaint)
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("mcp-tool-\(tool.name)")

        if open {
            SettingsBMCPToolDetail(tool: tool, keyword: keyword)
                .listRowBackground(Color.black.opacity(0.2))
        }
    }
}

/// 点开一个工具后的详情块：完整说明 + 命令表 / 参数表 + 复制按钮
private struct SettingsBMCPToolDetail: View {
    let tool: API.ToolPreview
    let keyword: String
    @Environment(Feedback.self) private var feedback

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(tool.description)
                .font(.caption)
                .foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
            if !tool.commands.isEmpty {
                commandList
            } else if tool.parameters.isEmpty {
                Text("这个工具不需要参数。").font(.caption).foregroundStyle(Theme.textFaint)
            } else {
                parameterList
            }
            HStack(spacing: 8) {
                copyButton("复制工具名", tool.name, id: "mcp-tool-copy-name")
                copyButton("复制调用样例", sample, id: "mcp-tool-copy-sample")
            }
        }
        .padding(.vertical, 4)
    }

    private func copyButton(_ title: String, _ text: String, id: String) -> some View {
        Button(title) {
            UIPasteboard.general.string = text
            feedback.success("已复制")
        }
        .font(.caption.weight(.medium))
        .buttonStyle(.glass)
        .accessibilityIdentifier(id)
    }

    /// 折叠模式的命令表：与搜索联动，命中时只列命中的几条（一个服务动辄五十多条命令）
    private var commandList: some View {
        let matched = keyword.isEmpty ? tool.commands
            : tool.commands.filter { "\($0.name) \($0.summary)".lowercased().contains(keyword) }
        let list = matched.isEmpty ? tool.commands : matched
        return VStack(alignment: .leading, spacing: 0) {
            Text(list.count == tool.commands.count
                 ? "\(tool.commands.count) 条命令，填进 command 参数"
                 : "匹配「\(keyword)」的 \(list.count) / \(tool.commands.count) 条命令")
                .font(.caption)
                .foregroundStyle(Theme.textFaint)
                .padding(.bottom, 4)
            ForEach(list, id: \.name) { command in
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 4) {
                        Text(command.name).font(.caption.monospaced()).foregroundStyle(Theme.text)
                        if !command.dangerous.isEmpty {
                            Text("⚠")
                                .font(.caption)
                                .foregroundStyle(Theme.danger)
                                .accessibilityLabel(command.dangerous == "destructive" ? "破坏性：会删数据或磁盘文件" : "会清除配置或记录")
                        }
                    }
                    Text("\(command.summary.isEmpty ? "—" : command.summary)\(command.isJob ? Text("（后台任务，返回 job_id）").foregroundStyle(Theme.textFaint) : Text(verbatim: ""))")
                    .font(.caption)
                    .foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
                    if !command.params.isEmpty {
                        Text(command.params.joined(separator: ", "))
                            .font(.system(size: 11).monospaced())
                            .foregroundStyle(Theme.textFaint)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.vertical, 6)
                Divider().overlay(Color.white.opacity(0.05))
            }
        }
    }

    /// 展开模式的参数表（堆叠块）：名称 + 必填星号 + 类型 + 落点，下一行说明与可选值
    private var parameterList: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(tool.parameters, id: \.name) { param in
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("\(Text(param.name).foregroundStyle(Theme.text))\(param.required ? Text("*").foregroundStyle(Theme.danger) : Text(verbatim: ""))")
                            .font(.caption.monospaced())
                        Text(param.type).font(.system(size: 11).monospaced()).foregroundStyle(Theme.textMuted)
                        if !param.location.isEmpty {
                            Text(param.location).font(.system(size: 11)).foregroundStyle(Theme.textFaint)
                        }
                    }
                    Text(param.description.isEmpty ? "—" : param.description)
                        .font(.caption)
                        .foregroundStyle(Theme.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                    if !param.options.isEmpty {
                        Text("可选：\(param.options.joined(separator: " / "))")
                            .font(.system(size: 11).monospaced())
                            .foregroundStyle(Theme.textFaint)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.vertical, 6)
                Divider().overlay(Color.white.opacity(0.05))
            }
        }
    }

    /// 调用样例 JSON（与 Web 同结构、两空格缩进）：折叠模式给第一条命令 + 空 params；
    /// 展开模式列出必填参数并以「<类型>」占位
    private var sample: String {
        func q(_ s: String) -> String {
            let encoder = JSONEncoder()
            encoder.outputFormatting = .withoutEscapingSlashes
            return (try? encoder.encode(s)).flatMap { String(data: $0, encoding: .utf8) } ?? "\"\(s)\""
        }
        let arguments: String
        if let first = tool.commands.first {
            arguments = "{\n    \"command\": \(q(first.name)),\n    \"params\": {}\n  }"
        } else {
            let required = tool.parameters.filter(\.required)
            arguments = required.isEmpty ? "{}"
                : "{\n" + required.map { "    \(q($0.name)): \(q("<\($0.type)>"))" }.joined(separator: ",\n") + "\n  }"
        }
        return "{\n  \"name\": \(q(tool.name)),\n  \"arguments\": \(arguments)\n}"
    }
}
