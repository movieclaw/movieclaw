import NukeUI
import SwiftUI

// AI 回复正文的 Markdown 渲染器（对应 Web `components/markdown.tsx` + globals.css 的 `.markdown` 排版）。
//
// 选型：自研轻量块级解析 + 系统 `AttributedString(markdown:)` 处理行内语法，不引入第三方依赖。
// - 块级：标题、段落、围栏代码块、有序/无序/任务列表（可嵌套）、引用、分隔线、GFM 表格——
//   覆盖 LLM 回复里实际出现的全部结构（系统解析器的 full 模式不给表格，也无法定制代码块/列表版式）；
// - 行内：粗体、斜体、删除线、行内代码、链接交给系统解析（inlineOnlyPreservingWhitespace），
//   再按 Web 的样式改写行内代码底色与链接颜色；
// - 图片：`![说明](地址)` 拆成独立的图片块（同 react-markdown 默认渲染 `<img>`），远程图经后端缓存代理；
// - 版式取 Web 移动端档：正文 17pt、行高约 28pt，标题 1.25/1.15/1.05em，代码块右上角常驻复制键
//   （手指划选代码是移动端最难用的操作之一）。正文代码块不着色（同 Web 正文的默认 `<pre>`）。
// 也负责工具调用参数的轻量高亮（bash / json，配色取 Shiki github-dark），见 `AgentCodeText`——
// 只用于工具参数，与 Web 只在工具参数上接 Shiki 一致。

// MARK: - 块级结构

indirect enum AgentMarkdownBlock: Equatable {
    case heading(level: Int, text: String)
    case paragraph(String)
    case code(language: String, text: String)
    case list(ordered: Bool, start: Int, items: [AgentMarkdownListItem])
    case quote([AgentMarkdownBlock])
    case rule
    case table(header: [String], rows: [[String]])
    /// 图片；`link` = 外面包着链接（`[![说明](图)](链接)`），点图打开链接
    case image(alt: String, url: String, link: String? = nil)
}

struct AgentMarkdownListItem: Equatable {
    /// 任务列表勾选状态；nil = 普通列表项
    var checked: Bool?
    var blocks: [AgentMarkdownBlock]
}

enum AgentMarkdownParser {
    /// `![说明](地址 "可选标题")`
    private static let imagePattern = try? NSRegularExpression(pattern: #"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)"#)

    /// 带链接的图片 `[![说明](图)](链接)`：整体算一张图，不能只拆里面的图、外层剩下 `[`、`](链接)` 残文
    private static let linkedImagePattern = try? NSRegularExpression(
        pattern: #"\[!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)"#
    )
    /// 行内代码（成对反引号包住的部分）：里面的 `![x](y)` 是字面文字，不是图片
    private static let codeSpanPattern = try? NSRegularExpression(pattern: #"(`+)[\s\S]*?\1"#)

    /// 段落里的图片拆成独立的图片块，前后文字仍是段落（系统行内解析不渲染图片）。
    /// 按语法树的口径处理两种边界（同 Web react-markdown）：行内代码里的不算图片；外面包着链接的整体算一张图
    static func splitImages(_ text: String) -> [AgentMarkdownBlock] {
        guard text.contains("!["), let regex = imagePattern else { return [.paragraph(text)] }
        let ns = text as NSString
        let whole = NSRange(location: 0, length: ns.length)
        let codeSpans = codeSpanPattern?.matches(in: text, range: whole).map(\.range) ?? []
        func inCode(_ range: NSRange) -> Bool {
            codeSpans.contains { NSIntersectionRange($0, range).length > 0 }
        }
        // 先认带链接的整体，再认裸图片；与已认下的区间重叠、或落在行内代码里的都跳过
        var found: [(range: NSRange, block: AgentMarkdownBlock)] = []
        for match in linkedImagePattern?.matches(in: text, range: whole) ?? [] where !inCode(match.range) {
            found.append((match.range, .image(
                alt: ns.substring(with: match.range(at: 1)),
                url: ns.substring(with: match.range(at: 2)),
                link: ns.substring(with: match.range(at: 3))
            )))
        }
        for match in regex.matches(in: text, range: whole) where !inCode(match.range) {
            guard !found.contains(where: { NSIntersectionRange($0.range, match.range).length > 0 }) else { continue }
            found.append((match.range, .image(alt: ns.substring(with: match.range(at: 1)), url: ns.substring(with: match.range(at: 2)))))
        }
        guard !found.isEmpty else { return [.paragraph(text)] }
        found.sort { $0.range.location < $1.range.location }
        var blocks: [AgentMarkdownBlock] = []
        var cursor = 0
        func appendText(_ range: NSRange) {
            let piece = ns.substring(with: range).trimmingCharacters(in: .whitespaces)
            if !piece.isEmpty { blocks.append(.paragraph(piece)) }
        }
        for item in found {
            appendText(NSRange(location: cursor, length: item.range.location - cursor))
            blocks.append(item.block)
            cursor = item.range.location + item.range.length
        }
        appendText(NSRange(location: cursor, length: ns.length - cursor))
        return blocks.isEmpty ? [.paragraph(text)] : blocks
    }

    static func parse(_ text: String) -> [AgentMarkdownBlock] {
        let lines = text.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
        return parse(lines: lines[...])
    }

    private static func parse(lines input: ArraySlice<String>) -> [AgentMarkdownBlock] {
        var blocks: [AgentMarkdownBlock] = []
        var lines = input
        var paragraph: [String] = []

        func flushParagraph() {
            guard !paragraph.isEmpty else { return }
            // 段内软换行按空格连接（与浏览器渲染一致）；行尾两空格或反斜杠是硬换行
            var text = ""
            for (i, line) in paragraph.enumerated() {
                let hard = line.hasSuffix("  ") || line.hasSuffix("\\")
                var content = line.trimmingCharacters(in: .whitespaces)
                if content.hasSuffix("\\") { content.removeLast() }
                text += content
                if i < paragraph.count - 1 { text += hard ? "\n" : " " }
            }
            blocks += splitImages(text)
            paragraph.removeAll()
        }

        while let line = lines.first {
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if trimmed.isEmpty {
                flushParagraph()
                lines = lines.dropFirst()
                continue
            }

            // 围栏代码块
            if let fence = fenceMarker(trimmed) {
                flushParagraph()
                let language = String(trimmed.dropFirst(fence.count)).trimmingCharacters(in: .whitespaces)
                let indent = line.prefix(while: { $0 == " " }).count
                var body: [String] = []
                lines = lines.dropFirst()
                while let next = lines.first {
                    lines = lines.dropFirst()
                    if next.trimmingCharacters(in: .whitespaces).hasPrefix(fence) { break }
                    // 列表内缩进的代码块：去掉与围栏同宽的前导空格
                    body.append(String(next.dropFirst(min(indent, next.prefix(while: { $0 == " " }).count))))
                }
                blocks.append(.code(language: language, text: body.joined(separator: "\n")))
                continue
            }

            // 标题
            if let heading = headingLevel(trimmed) {
                flushParagraph()
                let text = trimmed.drop(while: { $0 == "#" }).trimmingCharacters(in: .whitespaces)
                blocks.append(.heading(level: heading, text: text.replacingOccurrences(of: #"\s+#+$"#, with: "", options: .regularExpression)))
                lines = lines.dropFirst()
                continue
            }

            // 分隔线（放在列表判断前：「- - -」「***」不是列表）
            if isRule(trimmed) {
                // 段落后紧跟的「---」是 Setext 二级标题
                if !paragraph.isEmpty, trimmed.allSatisfy({ $0 == "-" }) {
                    let text = paragraph.joined(separator: " ").trimmingCharacters(in: .whitespaces)
                    paragraph.removeAll()
                    blocks.append(.heading(level: 2, text: text))
                } else {
                    flushParagraph()
                    blocks.append(.rule)
                }
                lines = lines.dropFirst()
                continue
            }

            // 引用
            if trimmed.hasPrefix(">") {
                flushParagraph()
                var body: [String] = []
                while let next = lines.first {
                    let t = next.trimmingCharacters(in: .whitespaces)
                    if t.hasPrefix(">") {
                        var content = t.dropFirst()
                        if content.first == " " { content = content.dropFirst() }
                        body.append(String(content))
                    } else if !t.isEmpty, !body.isEmpty, !(body.last ?? "").isEmpty, !isRule(t),
                              fenceMarker(t) == nil, headingLevel(t) == nil, listMarker(next) == nil {
                        body.append(t) // 惰性续行
                    } else {
                        break
                    }
                    lines = lines.dropFirst()
                }
                blocks.append(.quote(parse(lines: body[...])))
                continue
            }

            // 表格：表头行 + 分隔行
            if trimmed.contains("|"), lines.count >= 2, isTableSeparator(lines[lines.index(after: lines.startIndex)]) {
                flushParagraph()
                let header = tableCells(trimmed)
                lines = lines.dropFirst(2)
                var rows: [[String]] = []
                while let next = lines.first {
                    let t = next.trimmingCharacters(in: .whitespaces)
                    guard !t.isEmpty, t.contains("|") else { break }
                    var cells = tableCells(t)
                    if cells.count < header.count { cells += Array(repeating: "", count: header.count - cells.count) }
                    rows.append(Array(cells.prefix(header.count)))
                    lines = lines.dropFirst()
                }
                blocks.append(.table(header: header, rows: rows))
                continue
            }

            // 列表（段落中间的「1. 」不打断段落时 LLM 也几乎不会这么写，这里直接开列表）
            if let marker = listMarker(line) {
                flushParagraph()
                let (list, rest) = parseList(lines, first: marker)
                blocks.append(list)
                lines = rest
                continue
            }

            paragraph.append(line)
            lines = lines.dropFirst()
        }
        flushParagraph()
        return blocks
    }

    // MARK: 列表

    struct ListMarker {
        var indent: Int
        var ordered: Bool
        var number: Int
        /// 列表项正文相对行首的缩进（续行与嵌套内容以它为基准）
        var contentIndent: Int
        var content: String
    }

    static func listMarker(_ line: String) -> ListMarker? {
        let indent = line.prefix(while: { $0 == " " }).count
        let rest = line.dropFirst(indent)
        if let first = rest.first, "-*+".contains(first), rest.dropFirst().first == " " {
            let content = rest.dropFirst(2)
            return ListMarker(indent: indent, ordered: false, number: 0, contentIndent: indent + 2, content: String(content))
        }
        let digits = rest.prefix(while: \.isNumber)
        if !digits.isEmpty, digits.count <= 9 {
            let after = rest.dropFirst(digits.count)
            if let delimiter = after.first, delimiter == "." || delimiter == ")", after.dropFirst().first == " " {
                return ListMarker(indent: indent, ordered: true, number: Int(digits) ?? 1, contentIndent: indent + digits.count + 2, content: String(after.dropFirst(2)))
            }
        }
        return nil
    }

    private static func parseList(_ input: ArraySlice<String>, first: ListMarker) -> (AgentMarkdownBlock, ArraySlice<String>) {
        var lines = input
        var items: [AgentMarkdownListItem] = []
        var current: (marker: ListMarker, body: [String])?

        func closeItem() {
            guard let item = current else { return }
            var body = item.body
            var checked: Bool?
            if let head = body.first {
                if head.hasPrefix("[ ] ") { checked = false; body[0] = String(head.dropFirst(4)) }
                else if head.lowercased().hasPrefix("[x] ") { checked = true; body[0] = String(head.dropFirst(4)) }
            }
            while body.last?.trimmingCharacters(in: .whitespaces).isEmpty == true { body.removeLast() }
            items.append(AgentMarkdownListItem(checked: checked, blocks: parse(lines: body[...])))
            current = nil
        }

        while let line = lines.first {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if let marker = listMarker(line), marker.indent < (current?.marker.contentIndent ?? first.contentIndent), marker.indent >= first.indent - 1 {
                // 同级新列表项（有序/无序换了类型就结束本列表）
                if marker.ordered != first.ordered { break }
                closeItem()
                current = (marker, [marker.content])
                lines = lines.dropFirst()
                continue
            }
            guard var item = current else { break }
            let indent = line.prefix(while: { $0 == " " }).count
            if trimmed.isEmpty {
                // 空行：后面还有缩进内容或同类列表项才算列表继续
                let next = lines.dropFirst().first(where: { !$0.trimmingCharacters(in: .whitespaces).isEmpty })
                let continues = next.map { n in
                    let nIndent = n.prefix(while: { $0 == " " }).count
                    if nIndent >= item.marker.contentIndent { return true }
                    if let m = listMarker(n), m.ordered == first.ordered, m.indent < item.marker.contentIndent { return true }
                    return false
                } ?? false
                guard continues else { break }
                item.body.append("")
            } else if indent >= item.marker.contentIndent || (indent > first.indent && listMarker(line) != nil) {
                // 缩进续行 / 嵌套列表：去掉本项正文缩进后归入本项
                item.body.append(String(line.dropFirst(min(indent, item.marker.contentIndent))))
            } else if listMarker(line) == nil, fenceMarker(trimmed) == nil, headingLevel(trimmed) == nil,
                      !(item.body.last ?? "").trimmingCharacters(in: .whitespaces).isEmpty {
                item.body.append(trimmed) // 惰性续行
            } else {
                break
            }
            current = item
            lines = lines.dropFirst()
        }
        closeItem()
        return (.list(ordered: first.ordered, start: first.number, items: items), lines)
    }

    // MARK: 小工具

    private static func fenceMarker(_ trimmed: String) -> String? {
        if trimmed.hasPrefix("```") { return String(trimmed.prefix(while: { $0 == "`" })) }
        if trimmed.hasPrefix("~~~") { return String(trimmed.prefix(while: { $0 == "~" })) }
        return nil
    }

    private static func headingLevel(_ trimmed: String) -> Int? {
        let hashes = trimmed.prefix(while: { $0 == "#" }).count
        guard (1 ... 6).contains(hashes) else { return nil }
        let rest = trimmed.dropFirst(hashes)
        return rest.isEmpty || rest.first == " " ? hashes : nil
    }

    private static func isRule(_ trimmed: String) -> Bool {
        let compact = trimmed.replacingOccurrences(of: " ", with: "")
        guard compact.count >= 3, let first = compact.first, "-*_".contains(first) else { return false }
        return compact.allSatisfy { $0 == first }
    }

    private static func isTableSeparator(_ line: String) -> Bool {
        let t = line.trimmingCharacters(in: .whitespaces)
        guard t.contains("-") else { return false }
        return t.range(of: #"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$"#, options: .regularExpression) != nil
    }

    private static func tableCells(_ line: String) -> [String] {
        var t = line.trimmingCharacters(in: .whitespaces)
        if t.hasPrefix("|") { t.removeFirst() }
        if t.hasSuffix("|"), !t.hasSuffix("\\|") { t.removeLast() }
        // 单元格里转义的竖线 \| 不是分隔符
        let placeholder = "\u{E000}"
        return t.replacingOccurrences(of: "\\|", with: placeholder)
            .components(separatedBy: "|")
            .map { $0.replacingOccurrences(of: placeholder, with: "|").trimmingCharacters(in: .whitespaces) }
    }
}

// MARK: - 行内样式

enum AgentInline {
    static let linkColor = Color(red: 0x8A / 255, green: 0xB4 / 255, blue: 1)

    /// 行内 Markdown → 带样式的 AttributedString（行内代码等宽 + 浅底，链接蓝色下划线）
    static func attributed(_ text: String, size: CGFloat) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace, failurePolicy: .returnPartiallyParsedIfPossible)
        var result = (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
        for run in result.runs {
            if let intent = run.inlinePresentationIntent, intent.contains(.code) {
                result[run.range].font = .system(size: size * 0.86, design: .monospaced)
                result[run.range].backgroundColor = Color.white.opacity(0.09)
            }
            if run.link != nil {
                result[run.range].foregroundColor = linkColor
                result[run.range].underlineStyle = .single
            }
        }
        return result
    }
}

// MARK: - 视图

/// Markdown 正文。`size` 为正文字号（阅读正文档 17pt；卡片里的紧凑档可传更小值）。
struct AgentMarkdownView: View {
    let text: String
    var size: CGFloat = 17

    var body: some View {
        AgentMarkdownBlocks(blocks: AgentMarkdownParser.parse(text), size: size)
    }
}

struct AgentMarkdownBlocks: View {
    let blocks: [AgentMarkdownBlock]
    var size: CGFloat

    var body: some View {
        VStack(alignment: .leading, spacing: size * 0.75) {
            ForEach(blocks.indices, id: \.self) { i in
                block(blocks[i])
                    // 标题前多留一点空（Web：* + h1..h4 margin-top 1.4em）
                    .padding(.top, i > 0 && isHeading(blocks[i]) ? size * 0.65 : 0)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func isHeading(_ block: AgentMarkdownBlock) -> Bool {
        if case .heading = block { return true }
        return false
    }

    @ViewBuilder
    private func block(_ block: AgentMarkdownBlock) -> some View {
        switch block {
        case let .heading(level, text):
            let scale: CGFloat = level == 1 ? 1.25 : level == 2 ? 1.15 : 1.05
            Text(AgentInline.attributed(text, size: size * scale))
                .font(.system(size: size * scale, weight: .semibold))
                .foregroundStyle(Theme.text)
                .lineSpacing(3)
                .textSelection(.enabled)
        case let .paragraph(text):
            AgentMarkdownText(text: text, size: size)
        case let .code(_, text):
            AgentCodeBlock(code: text, size: size)
        case let .image(alt, url, link):
            AgentMarkdownImage(alt: alt, url: url, link: link)
        case let .list(ordered, start, items):
            VStack(alignment: .leading, spacing: size * 0.3) {
                ForEach(items.indices, id: \.self) { i in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Group {
                            if let checked = items[i].checked {
                                Image(systemName: checked ? "checkmark.square.fill" : "square")
                                    .font(.system(size: size * 0.85))
                            } else if ordered {
                                Text("\(start + i).").monospacedDigit()
                            } else {
                                Text("•")
                            }
                        }
                        .font(.system(size: size))
                        .foregroundStyle(Theme.textMuted)
                        .frame(minWidth: size * 1.1, alignment: .trailing)
                        AgentMarkdownBlocks(blocks: items[i].blocks, size: size)
                    }
                }
            }
        case let .quote(inner):
            AgentMarkdownBlocks(blocks: inner, size: size)
                .foregroundStyle(Theme.textMuted)
                .padding(.leading, size * 0.9)
                .overlay(alignment: .leading) {
                    Rectangle().fill(Color.white.opacity(0.14)).frame(width: 3)
                }
        case .rule:
            Rectangle().fill(Color.white.opacity(0.1)).frame(height: 1).padding(.vertical, size * 0.6)
        case let .table(header, rows):
            AgentMarkdownTable(header: header, rows: rows, size: size * 0.93)
        }
    }
}

/// 段落正文：可选中复制（长按选择，与网页可划选一致）
struct AgentMarkdownText: View {
    let text: String
    var size: CGFloat
    var color: Color = Theme.text

    var body: some View {
        Text(AgentInline.attributed(text, size: size))
            .font(.system(size: size))
            .foregroundStyle(color)
            .lineSpacing(size * 0.6)
            .fixedSize(horizontal: false, vertical: true)
            .textSelection(.enabled)
    }
}

/// GFM 表格：横向可滚动，单元格细描边，表头浅底加粗（同 Web `.markdown table`）
struct AgentMarkdownTable: View {
    let header: [String]
    let rows: [[String]]
    var size: CGFloat

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
                GridRow {
                    ForEach(header.indices, id: \.self) { i in
                        cell(header[i], bold: true)
                    }
                }
                .background(Color.white.opacity(0.04))
                ForEach(rows.indices, id: \.self) { r in
                    GridRow {
                        ForEach(rows[r].indices, id: \.self) { i in
                            cell(rows[r][i], bold: false)
                        }
                    }
                }
            }
            .overlay(Rectangle().strokeBorder(Color.white.opacity(0.1)))
        }
        .scrollBounceBehavior(.basedOnSize, axes: .horizontal)
    }

    private func cell(_ text: String, bold: Bool) -> some View {
        AgentCapWidth(maxWidth: 220) {
            Text(AgentInline.attributed(text.replacingOccurrences(of: "<br>", with: "\n"), size: size))
                .font(.system(size: size, weight: bold ? .semibold : .regular))
                .foregroundStyle(Theme.text)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .overlay(Rectangle().strokeBorder(Color.white.opacity(0.1), lineWidth: 0.5))
        .textSelection(.enabled)
    }
}

/// 限宽布局：子视图理想宽度超过上限时按上限折行（横向滚动容器里 `.frame(maxWidth:)` 拿不到有限宽度，无法折行）
struct AgentCapWidth: Layout {
    var maxWidth: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        guard let child = subviews.first else { return .zero }
        return child.sizeThatFits(ProposedViewSize(width: width(child, proposal), height: nil))
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        guard let child = subviews.first else { return }
        child.place(at: bounds.origin, proposal: ProposedViewSize(width: width(child, proposal), height: nil))
    }

    private func width(_ child: LayoutSubview, _ proposal: ProposedViewSize) -> CGFloat {
        let ideal = child.sizeThatFits(.unspecified).width
        let offered = (proposal.width ?? 0) > 40 ? proposal.width! : .infinity
        return min(ideal, maxWidth, offered)
    }
}

/// 正文里的图片（同 react-markdown 的 `<img>`）：按原比例铺满正文宽度、限高，加载前占位；
/// 远程图经后端缓存代理（本机直连图床常失败）
struct AgentMarkdownImage: View {
    let alt: String
    let url: String
    /// 外面包着的链接：点图打开（站内链接由会话页的 openURL 接管，走原生路由）
    var link: String?
    @Environment(\.api) private var api
    @Environment(\.openURL) private var openURL

    var body: some View {
        if let target = linkURL {
            Button { openURL(target) } label: { image }
                .buttonStyle(.plain)
        } else {
            image
        }
    }

    private var linkURL: URL? {
        guard let link, let parsed = URL(string: link) else { return nil }
        return parsed.scheme == nil ? api.server.resolve(link) : parsed
    }

    private var image: some View {
        LazyImage(url: api.image(url)) { state in
            if let image = state.image {
                image.resizable().scaledToFit()
            } else if state.error != nil {
                Label(alt.isEmpty ? "图片加载失败" : alt, systemImage: "photo")
                    .font(.footnote)
                    .foregroundStyle(Theme.textFaint)
                    .frame(maxWidth: .infinity, minHeight: 60)
                    .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 10))
            } else {
                Color.white.opacity(0.04).frame(height: 160)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: 420, alignment: .leading)
        .clipShape(.rect(cornerRadius: 10))
        .accessibilityLabel(alt.isEmpty ? "图片" : alt)
    }
}

/// 围栏代码块：横向滚动 + 右上角复制；不着色（同 Web 正文代码块）
struct AgentCodeBlock: View {
    let code: String
    var size: CGFloat = 17
    @State private var copied = false

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            AgentCodeText(code: code, language: .plain, size: size * 0.82)
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
                .padding(.trailing, 28)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.white.opacity(0.07)))
        .overlay(alignment: .topTrailing) {
            Button {
                UIPasteboard.general.string = code
                copied = true
                Task { try? await Task.sleep(for: .seconds(1.5)); copied = false }
            } label: {
                Image(systemName: copied ? "checkmark" : "doc.on.doc")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(copied ? Theme.success : Theme.textFaint)
                    .frame(width: 28, height: 28)
                    .background(Color(red: 28 / 255, green: 28 / 255, blue: 32 / 255).opacity(0.9), in: .rect(cornerRadius: 7))
                    .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(Color.white.opacity(0.08)))
            }
            .buttonStyle(.plain)
            .padding(6)
            .accessibilityLabel(copied ? "已复制" : "复制代码")
        }
    }
}

// MARK: - 轻量代码高亮

/// 高亮支持的语言（与 Web shiki 配置一致只有 bash / json，其余按纯等宽文本）
enum AgentCodeLanguage {
    case bash, json, plain

    init(tag: String) {
        switch tag.lowercased() {
        case "bash", "sh", "shell", "zsh", "console": self = .bash
        case "json", "jsonc": self = .json
        default: self = .plain
        }
    }
}

/// 等宽代码 + 轻量着色（配色取 Shiki github-dark：键绿、字符串浅蓝、数字/常量蓝、关键字红、注释灰）
struct AgentCodeText: View {
    let code: String
    var language: AgentCodeLanguage
    var size: CGFloat = 13
    var color: Color = Theme.text.opacity(0.88)

    var body: some View {
        Text(AgentCodeHighlighter.highlight(code, language: language, base: color))
            .font(.system(size: size, design: .monospaced))
            .lineSpacing(size * 0.45)
            .fixedSize(horizontal: false, vertical: true)
            .textSelection(.enabled)
    }
}

enum AgentCodeHighlighter {
    static let keyColor = Color(red: 0x7E / 255, green: 0xE7 / 255, blue: 0x87 / 255)
    static let stringColor = Color(red: 0xA5 / 255, green: 0xD6 / 255, blue: 1)
    static let numberColor = Color(red: 0x79 / 255, green: 0xC0 / 255, blue: 1)
    static let keywordColor = Color(red: 1, green: 0x7B / 255, blue: 0x72 / 255)
    static let commandColor = Color(red: 0xD2 / 255, green: 0xA8 / 255, blue: 1)
    static let commentColor = Color(red: 0x8B / 255, green: 0x94 / 255, blue: 0x9E / 255)

    static func highlight(_ code: String, language: AgentCodeLanguage, base: Color) -> AttributedString {
        var result = AttributedString(code)
        result.foregroundColor = base
        guard language != .plain, code.utf16.count < 20_000 else { return result }
        let rules: [(String, Color)]
        switch language {
        case .json:
            rules = [
                (#"-?\b\d+(\.\d+)?([eE][+-]?\d+)?\b"#, numberColor),
                (#"\b(true|false|null)\b"#, numberColor),
                (#""(?:[^"\\]|\\.)*""#, stringColor),
                (#""(?:[^"\\]|\\.)*"(?=\s*:)"#, keyColor),
            ]
        case .bash:
            rules = [
                (#"(?<=\s|^)--?[A-Za-z][\w-]*"#, numberColor),
                (#"(?m)(?:^|(?<=[|;&]\s?))\s*[A-Za-z_][\w.-]*"#, commandColor),
                (#"\b(if|then|else|fi|for|do|done|while|case|esac|in|function|export|sudo)\b"#, keywordColor),
                (#""(?:[^"\\]|\\.)*"|'[^']*'"#, stringColor),
                (#"(?m)(?:^|\s)#.*$"#, commentColor),
            ]
        case .plain:
            rules = []
        }
        let ns = code as NSString
        for (pattern, color) in rules {
            guard let regex = try? NSRegularExpression(pattern: pattern) else { continue }
            for match in regex.matches(in: code, range: NSRange(location: 0, length: ns.length)) {
                guard let range = Range(match.range, in: code),
                      let lower = AttributedString.Index(range.lowerBound, within: result),
                      let upper = AttributedString.Index(range.upperBound, within: result) else { continue }
                result[lower ..< upper].foregroundColor = color
            }
        }
        return result
    }
}
