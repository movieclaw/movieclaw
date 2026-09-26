import NukeUI
import SwiftUI

// 会话时间线里的单轮渲染（对应 Web `agent-conversation-view.tsx` 的 TurnView 及其子组件）。
//
// 呈现取舍（与 Web 一致，刻意做减法）：
// - 用户消息右侧气泡；Agent 回应整栏正文，不挂头像、不套气泡（ChatGPT / Claude 同款版式）；
// - 思考与工具调用收进「处理过程」折叠块：进行中显示实时状态，完成后一句话总结；展开后工具调用
//   只列单行摘要，点某一行才展开参数与输出——一轮动辄十几次调用，全量平铺等于没有排版；
// - 轮次页脚只留耗时（进行中是进度环 + 实时秒数），模型/token 这些排查信息不上屏。

/// 单轮：用户气泡 + Agent 回应块。Equatable：流式时只有正在生成的那一轮变化，历史轮次整轮跳过重绘。
struct AgentTurnView: View, Equatable {
    let turn: AgentTurn
    let sessionId: String
    /// 已知技能名（小写）；nil = 名单未就绪（暂不过滤）
    let knownSkills: Set<String>?
    /// 改写本轮重问；nil（运行中）则气泡上不出现该入口
    let onEdit: ((String, String) -> Void)?

    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.turn == rhs.turn && lhs.sessionId == rhs.sessionId && lhs.knownSkills == rhs.knownSkills
            && (lhs.onEdit == nil) == (rhs.onEdit == nil)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            AgentUserBubble(
                text: turn.input,
                images: turn.images,
                sessionId: sessionId,
                knownSkills: knownSkills,
                onEdit: onEdit.flatMap { edit in turn.messageId.map { id in { edit(id, turn.input) } } }
            )

            VStack(alignment: .leading, spacing: 10) {
                ForEach(turn.segments.indices, id: \.self) { index in
                    let active = turn.isRunning && index == turn.segments.count - 1
                    switch turn.segments[index] {
                    case let .process(items):
                        // 生成式 UI：该块里 show_media_cards 画的卡片组紧跟在折叠块之后常显
                        AgentProcessBlock(items: items, active: active)
                        ForEach(AgentMediaCards.groups(in: items), id: \.id) { entry in
                            AgentMediaCardsBlock(group: entry.group)
                        }
                    case let .text(text):
                        VStack(alignment: .leading, spacing: 0) {
                            AgentMarkdownView(text: text)
                            if active { AgentStreamingCursor() }
                        }
                    case let .compaction(summary, before, after):
                        AgentCompactionCard(summary: summary, tokensBefore: before, tokensAfter: after)
                    }
                }

                if turn.status == .error, let error = turn.error {
                    Text(error)
                        .font(.system(size: 14))
                        .foregroundStyle(Theme.danger)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 14).padding(.vertical, 10)
                        .background(Theme.danger.opacity(0.1), in: .rect(cornerRadius: 12))
                        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.danger.opacity(0.3)))
                        .textSelection(.enabled)
                        .accessibilityIdentifier("agent-turn-error")
                }

                AgentTurnFooter(turn: turn)
            }
        }
    }
}

// MARK: - 用户气泡

/// 用户提问气泡 + 浮现的行内操作（复制 / 改写重问）。
///
/// 点一下气泡浮现操作键（同 Web 触屏的 tap-reveal），长按也有同样的系统菜单；
/// 操作键落在气泡下方、右对齐，样式与回答底部的「复制」一致——两种消息的操作都在消息下方
/// （原先放在气泡左侧，与回答的复制一左一下，2026-09-27 用户要求统一）。
/// `/skill:名字` 渲染成技能 chip，正文只留用户自己的话（复制与改写仍用完整 token 原文）。
struct AgentUserBubble: View {
    let text: String
    let images: [AgentTurnImage]
    let sessionId: String
    let knownSkills: Set<String>?
    let onEdit: (() -> Void)?

    @Environment(\.api) private var api
    @State private var revealed = false
    @State private var copied = false
    @State private var lightbox: DiscoverLightboxContent?

    var body: some View {
        let parsed = AgentSkillText.parseTokens(text, allow: knownSkills)
        VStack(alignment: .trailing, spacing: 6) {
            HStack(spacing: 0) {
                Spacer(minLength: 40)
                bubble(parsed)
                    .onTapGesture { withAnimation(.easeOut(duration: 0.15)) { revealed.toggle() } }
                    .contextMenu {
                        Button("复制", systemImage: "doc.on.doc", action: copy)
                        if let onEdit {
                            Button("改写这条提问", systemImage: "pencil", action: onEdit)
                        }
                    }
            }
            if revealed {
                HStack(spacing: 8) {
                    if let onEdit {
                        Button {
                            revealed = false
                            onEdit()
                        } label: {
                            actionLabel("改写", systemImage: "pencil")
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("改写这条提问")
                        .accessibilityIdentifier("agent-edit-message")
                    }
                    Button(action: copy) {
                        actionLabel(copied ? "已复制" : "复制", systemImage: copied ? "checkmark" : "doc.on.doc")
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("agent-copy-message")
                }
                .font(.system(size: 13))
                .foregroundStyle(Theme.textFaint)
                .transition(.opacity)
            }
        }
        .fullScreenCover(item: $lightbox) { content in
            DiscoverLightbox(content: content).sheetFeedback()
        }
    }

    private func bubble(_ parsed: (names: [String], text: String)) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if !parsed.names.isEmpty {
                AgentFlowLayout(spacing: 6) {
                    ForEach(parsed.names, id: \.self) { name in
                        Text("⚡ \(name)")
                            .font(.system(size: 13))
                            .foregroundStyle(Theme.textMuted)
                            .padding(.horizontal, 8).padding(.vertical, 2)
                            .background(Color.white.opacity(0.08), in: .rect(cornerRadius: 8))
                    }
                }
            }
            if !images.isEmpty {
                AgentFlowLayout(spacing: 8) {
                    ForEach(Array(images.enumerated()), id: \.element.attachmentId) { index, image in
                        Button {
                            lightbox = DiscoverLightboxContent(
                                urls: images.map { api.agentAttachmentURL(sessionId: sessionId, attachmentId: $0.attachmentId) },
                                initialIndex: index,
                                title: image.name ?? "图片"
                            )
                        } label: {
                            AgentAttachmentThumb(image: image, url: api.agentAttachmentURL(sessionId: sessionId, attachmentId: image.attachmentId))
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("放大预览：\(image.name ?? "图片")")
                    }
                }
            }
            if !parsed.text.isEmpty {
                Text(parsed.text)
                    .font(.system(size: 16))
                    .foregroundStyle(Theme.text)
                    .lineSpacing(5)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(Color.white.opacity(0.1), in: .rect(cornerRadius: 18))
        .contentShape(.rect(cornerRadius: 18))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("agent-user-bubble")
    }

    private func copy() {
        UIPasteboard.general.string = text
        copied = true
        Task { try? await Task.sleep(for: .seconds(1.5)); copied = false }
    }

    /// 与回答底部「复制」（AgentTurnFooter）同一个样子：小图标 + 文字
    private func actionLabel(_ title: String, systemImage: String) -> some View {
        HStack(spacing: 3) {
            Image(systemName: systemImage).font(.system(size: 11))
            Text(title)
        }
        .padding(.horizontal, 4).padding(.vertical, 2)
        .expandedHitArea(vertical: 10)
    }
}

/// 气泡里的图片缩略图（按原比例，最高 160、最宽 200，同 Web max-h-40 max-w-[200px]）：
/// 刚发出的用本地预览，回放走会话附件下载接口
struct AgentAttachmentThumb: View {
    let image: AgentTurnImage
    let url: URL

    var body: some View {
        Group {
            if let local = AgentImagePreviews.images[image.attachmentId] {
                Image(uiImage: local).resizable().scaledToFit()
            } else {
                LazyImage(url: url) { state in
                    if let loaded = state.image {
                        loaded.resizable().scaledToFit()
                    } else {
                        ZStack {
                            Color.white.opacity(0.06)
                            if state.error != nil {
                                Image(systemName: "photo").foregroundStyle(Theme.textFaint)
                            }
                        }
                        .frame(width: 120, height: 120)
                    }
                }
            }
        }
        .frame(maxWidth: 200, maxHeight: 160)
        .clipShape(.rect(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.white.opacity(0.1)))
    }
}

// MARK: - 处理过程

/// 处理过程折叠块（仿 Claude）：头部进行中显示实时状态、完成后显示一句话总结；
/// 点击展开思考与工具调用的混合列表（按实际发生顺序）。
/// 生成式 UI 的绘制调用不算处理过程（它的产出就是紧随其后的卡片），整块只剩它时连折叠头也不出。
struct AgentProcessBlock: View {
    let items: [AgentProcessItem]
    let active: Bool
    @State private var open = false

    var body: some View {
        let visible = items.filter { item in
            if case let .tool(tool) = item { return !AgentMediaCards.isMediaCardsTool(tool.name) }
            return true
        }
        if !visible.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Button {
                    withAnimation(.easeOut(duration: 0.18)) { open.toggle() }
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "chevron.right")
                            .font(.system(size: 11, weight: .semibold))
                            .rotationEffect(.degrees(open ? 90 : 0))
                        Text(active ? AgentTimeline.processStatus(visible) : AgentTimeline.processSummary(visible))
                            .font(.system(size: 14, weight: .medium))
                            .modifier(AgentPulse(active: active))
                    }
                    .foregroundStyle(Theme.textFaint)
                    .expandedHitArea(vertical: 12)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("agent-process-toggle")

                if open {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(visible.indices, id: \.self) { i in
                            switch visible[i] {
                            case let .thinking(text):
                                Text(text)
                                    .font(.system(size: 14))
                                    .foregroundStyle(Theme.textFaint)
                                    .lineSpacing(3)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .textSelection(.enabled)
                            case let .tool(tool):
                                AgentToolRow(tool: tool)
                            }
                        }
                    }
                    .padding(.leading, 12)
                    .overlay(alignment: .leading) {
                        Rectangle().fill(Color.white.opacity(0.08)).frame(width: 2)
                    }
                }
            }
        }
    }
}

/// 单次工具调用：默认只占一行（工具名 + 参数摘要 + ✓/✗/进度环），点击展开参数（高亮）与输出。
/// 参数生成中时右锚定显示最新生成的尾部——长参数溢出时新字符持续从右侧推入，肉眼可见仍在进行。
struct AgentToolRow: View {
    let tool: AgentToolCall
    @State private var open = false

    static let nameColor = Color(red: 0x9F / 255, green: 0xC6 / 255, blue: 0xE8 / 255)

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button {
                withAnimation(.easeOut(duration: 0.15)) { open.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .semibold))
                        .rotationEffect(.degrees(open ? 90 : 0))
                        .foregroundStyle(Theme.textFaint)
                    Text(tool.name).foregroundStyle(Self.nameColor).layoutPriority(1)
                    if tool.streaming {
                        Text(String(tool.label.dropFirst(tool.name.count + 1).suffix(300)))
                            .foregroundStyle(Theme.textFaint)
                            .lineLimit(1)
                            .truncationMode(.head)
                            .frame(maxWidth: .infinity, alignment: .trailing)
                    } else {
                        Text(tool.summary)
                            .foregroundStyle(Theme.textFaint)
                            .lineLimit(1)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if tool.streaming || tool.output == nil {
                        ProgressView().controlSize(.mini)
                    } else {
                        Text(tool.isError ? "✗" : "✓").foregroundStyle(tool.isError ? Theme.danger : Theme.textFaint)
                    }
                }
                .font(.system(size: 13, design: .monospaced))
                .padding(.vertical, 2)
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .disabled(tool.streaming)
            .accessibilityIdentifier("agent-tool-row")

            if open, !tool.streaming {
                VStack(alignment: .leading, spacing: 6) {
                    if let input = tool.inputDetail {
                        AgentCodeText(code: input.code, language: input.lang, size: 13)
                    }
                    if let output = tool.output {
                        ScrollView {
                            Text(output)
                                .font(.system(size: 13, design: .monospaced))
                                .foregroundStyle(tool.isError ? Theme.danger : Theme.textMuted)
                                .lineSpacing(2)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .textSelection(.enabled)
                        }
                        .frame(maxHeight: 176)
                        .fixedSize(horizontal: false, vertical: output.count < 400)
                    } else {
                        Text("执行中…").font(.system(size: 13)).foregroundStyle(Theme.textFaint)
                    }
                }
                .padding(.horizontal, 10).padding(.vertical, 6)
                .background(Color.white.opacity(0.02), in: .rect(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.white.opacity(0.05)))
            }
        }
    }
}

// MARK: - 压缩 / 来源 / 页脚

/// 上下文压缩分隔卡片：横线分隔 + 居中标签，点击展开交接摘要。
/// 它同时是「多次压缩会降低准确性」的可见信号——卡片越多，会话越该另起。
struct AgentCompactionCard: View {
    let summary: String
    let tokensBefore: Int?
    let tokensAfter: Int?
    @State private var open = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                withAnimation(.easeOut(duration: 0.18)) { open.toggle() }
            } label: {
                HStack(spacing: 8) {
                    Rectangle().fill(Color.white.opacity(0.08)).frame(height: 1)
                    Image(systemName: "chevron.right").font(.system(size: 9, weight: .semibold)).rotationEffect(.degrees(open ? 90 : 0))
                    Text(label).fixedSize()
                    Rectangle().fill(Color.white.opacity(0.08)).frame(height: 1)
                }
                .font(.system(size: 13))
                .foregroundStyle(Theme.textFaint)
                .expandedHitArea(vertical: 12)
            }
            .buttonStyle(.plain)
            if open {
                VStack(alignment: .leading, spacing: 6) {
                    Text(summary).font(.system(size: 14)).foregroundStyle(Theme.textFaint).textSelection(.enabled)
                    Text("此前的对话已由交接摘要替代。多次压缩可能降低模型准确性，建议适时开启新会话。")
                        .font(.system(size: 13)).foregroundStyle(Theme.textFaint)
                }
                .padding(.leading, 12)
                .overlay(alignment: .leading) { Rectangle().fill(Color.white.opacity(0.08)).frame(width: 2) }
            }
        }
    }

    private var label: String {
        if let tokensBefore, let tokensAfter { return "已压缩上下文（\(tokensBefore) → \(tokensAfter) tokens）" }
        return "已压缩上下文"
    }
}

/// 派生会话的来源卡片：只展示来源关系，不把旧消息重复画成当前会话里可重试的轮次
struct AgentHandoffCard: View {
    let sourceId: String
    let sourceTitle: String?
    @Environment(Router.self) private var router

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text("已从「\(sourceTitle?.isEmpty == false ? sourceTitle! : "会话 \(sourceId.prefix(8))")」续接上下文")
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(Theme.text)
                Text("这是一个独立的新会话；后续操作不会改写原会话。")
                    .font(.system(size: 13))
                    .foregroundStyle(Theme.textFaint)
            }
            Spacer(minLength: 0)
            Button {
                router.push(.session(id: sourceId))
            } label: {
                HStack(spacing: 2) {
                    Text("查看原会话")
                    Image(systemName: "chevron.right").font(.system(size: 11))
                }
                .font(.system(size: 13))
                .foregroundStyle(Theme.textMuted)
                .expandedHitArea(vertical: 12)
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 16).padding(.vertical, 12)
        .background(Color.white.opacity(0.025), in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.08)))
        .accessibilityIdentifier("agent-handoff")
    }
}

/// 一轮的收尾行：进行中 = 进度环 + 实时耗时（秒级跳动）；完成 = 已停止/已中断 + 最终耗时 + 复制
struct AgentTurnFooter: View {
    let turn: AgentTurn
    @State private var copied = false

    var body: some View {
        if turn.status == .error {
            EmptyView()
        } else if turn.isRunning {
            TimelineView(.periodic(from: .now, by: 1)) { context in
                HStack(spacing: 8) {
                    ProgressView().controlSize(.mini)
                    Text(AgentTimeline.duration(context.date.timeIntervalSince(turn.startedAt), precise: false))
                        .monospacedDigit()
                }
                .font(.system(size: 13))
                .foregroundStyle(Theme.textFaint)
                .accessibilityElement(children: .combine)
                .accessibilityLabel("执行中…")
                .accessibilityIdentifier("agent-turn-running")
            }
        } else {
            // 本轮跑完时有精确耗时；从轨迹回放的历史轮次按消息时间戳估算
            let duration: String? = turn.result.map { AgentTimeline.duration(Double($0.elapsedMs) / 1000, precise: true) }
                ?? turn.endedAt.map { AgentTimeline.duration($0.timeIntervalSince(turn.startedAt), precise: false) }
            let answer = turn.answerText
            if duration != nil || turn.stopped || turn.interrupted || !answer.isEmpty {
                HStack(spacing: 8) {
                    if turn.stopped {
                        Text("已停止").accessibilityIdentifier("agent-turn-stopped")
                    } else if turn.interrupted {
                        Text("已中断").accessibilityIdentifier("agent-turn-interrupted")
                    }
                    if let duration { Text(duration).monospacedDigit() }
                    if !answer.isEmpty {
                        Button {
                            UIPasteboard.general.string = answer
                            copied = true
                            Task { try? await Task.sleep(for: .seconds(1.5)); copied = false }
                        } label: {
                            HStack(spacing: 3) {
                                Image(systemName: copied ? "checkmark" : "doc.on.doc").font(.system(size: 11))
                                Text(copied ? "已复制" : "复制")
                            }
                            .padding(.horizontal, 4).padding(.vertical, 2)
                            .expandedHitArea(vertical: 10)
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("agent-copy-answer")
                    }
                }
                .font(.system(size: 13))
                .foregroundStyle(Theme.textFaint)
            }
        }
    }
}

/// 流式光标：正文尾部的呼吸圆点
struct AgentStreamingCursor: View {
    var body: some View {
        Circle().fill(Theme.textMuted).frame(width: 12, height: 12)
            .modifier(AgentPulse(active: true))
            .padding(.top, 4)
    }
}

/// 进行中文案的呼吸效果
struct AgentPulse: ViewModifier {
    let active: Bool
    @State private var dim = false

    func body(content: Content) -> some View {
        content
            .opacity(active && dim ? 0.45 : 1)
            .onAppear { if active { withAnimation(.easeInOut(duration: 0.9).repeatForever()) { dim = true } } }
            .onChange(of: active) { _, now in
                if now { withAnimation(.easeInOut(duration: 0.9).repeatForever()) { dim = true } } else { withAnimation(.default) { dim = false } }
            }
    }
}

/// 简单的流式换行布局（技能 chip、图片缩略图）
struct AgentFlowLayout: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0, maxX: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width { x = 0; y += rowHeight + spacing; rowHeight = 0 }
            x += size.width + spacing
            maxX = max(maxX, x - spacing)
            rowHeight = max(rowHeight, size.height)
        }
        return CGSize(width: maxX, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX { x = bounds.minX; y += rowHeight + spacing; rowHeight = 0 }
            view.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}
