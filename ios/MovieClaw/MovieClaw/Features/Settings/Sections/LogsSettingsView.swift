import SwiftUI

/// 设置 → 系统日志（Web system-logs-section.tsx）：结构化的在线日志查看器。
///
/// - 按天列表（`GET /system/logs`）+ 日期选择；内容 `GET /system/logs/{day}?tail=`，超长日志默认只取末尾，可「加载全部」；
/// - 按后端固定格式「时间 | 级别 | 模块 | 内容」解析成条目，异常堆栈等续行归并进上一条；
/// - 级别筛选（全部 / 错误 / 警告 / 信息 / 调试，带条数）+ 关键字搜索高亮；
/// - 自动刷新 关 / 3s / 10s（默认）/ 30s，选择记在本机（键名同 Web localStorage），只对当天日志生效，
///   查看历史日期时暂停；App 退到后台自动停（`.polling`）；
/// - tail -f 式跟随：停在底部时新日志自动滚入；向上翻阅则暂停跟随，浮出「N 条新日志」一键回底；
/// - 全屏查看：同一份工具栏与日志窗口铺满屏幕。
struct LogsSettingsView: View {
    @Environment(\.api) private var api
    @State private var model = SettingsLogModel()
    @State private var fullscreen = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            LogToolbar(model: model)
            if let error = model.error { SettingsNotice(text: error) }
            LogWindow(model: model, fullscreen: $fullscreen)
            HStack(alignment: .top) {
                Text("日志按天存档在服务端 data/logs 目录（Docker 部署挂载 data 卷即可持久化），超过保留天数的旧日志会自动清理；保留天数与目录位置可通过 LOG_RETENTION_DAYS、LOG_DIR 环境变量调整。")
                    .font(.caption).foregroundStyle(Theme.textFaint)
                if !model.isLatestDay, model.activeDay != nil, model.refreshMs > 0 {
                    Text("正在查看历史日志，自动刷新已暂停").font(.caption).foregroundStyle(Theme.textFaint)
                }
            }
        }
        .padding(.horizontal, Theme.pagePadding)
        .padding(.top, 8)
        .appBackground()
        .task {
            model.api = api
            await model.refresh(prefer: nil)
        }
        .polling(every: model.refreshMs > 0 ? Double(model.refreshMs) / 1000 : 3) {
            if model.refreshMs > 0, model.isLatestDay, let day = model.activeDay, !fullscreen {
                await model.loadDay(day, silent: true)
            }
        }
        .fullScreenCover(isPresented: $fullscreen) {
            LogFullscreen(model: model, fullscreen: $fullscreen)
                .sheetFeedback()
        }
    }
}

// MARK: - 数据

/// 一条日志（时间只留当天时刻，日期由所选天数隐含）
struct SettingsLogEntry: Identifiable {
    enum Level: String, CaseIterable { case error = "ERROR", warning = "WARNING", info = "INFO", debug = "DEBUG" }
    let id: Int
    let time: String
    let level: Level
    let module: String
    var message: String
}

/// 日志查看器状态：普通态与全屏态共用同一份（Web 同一份 JSX 复用）
@Observable
final class SettingsLogModel {
    static let refreshKey = "movieclaw.log-refresh-interval"
    static let refreshOptions: [(label: String, value: Int)] = [("关闭", 0), ("3s", 3000), ("10s", 10000), ("30s", 30000)]
    static let levelFilters: [(id: SettingsLogEntry.Level?, label: String)] = [
        (nil, "全部"), (.error, "错误"), (.warning, "警告"), (.info, "信息"), (.debug, "调试"),
    ]

    var api: APIClient?
    var days: [API.LogDay] = []
    var activeDay: String?
    var content: API.LogContent?
    var entries: [SettingsLogEntry] = []
    var loading = true
    var error: String?
    var levelFilter: SettingsLogEntry.Level?
    var query = ""
    var refreshMs: Int {
        didSet { UserDefaults.standard.set(refreshMs, forKey: Self.refreshKey) }
    }
    /// 是否停在底部（决定新日志是否自动滚入）
    var atBottom = true
    var pendingNew = 0
    /// 「加载全部」后的 tail 口径（0 = 全量），自动刷新沿用同一口径
    private var tail: Int?
    private var inFlight = false

    init() {
        // 区分「从未设置」与「主动选了关闭(0)」
        let stored = UserDefaults.standard.object(forKey: Self.refreshKey) as? Int
        refreshMs = stored.flatMap { value in Self.refreshOptions.contains { $0.value == value } ? value : nil } ?? 10000
    }

    var isLatestDay: Bool { activeDay != nil && activeDay == days.first?.day }

    var visible: [SettingsLogEntry] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        return entries.filter { entry in
            (levelFilter == nil || entry.level == levelFilter)
                && (q.isEmpty || entry.message.lowercased().contains(q) || entry.module.lowercased().contains(q))
        }
    }

    func count(_ level: SettingsLogEntry.Level?) -> Int {
        guard let level else { return entries.count }
        return entries.filter { $0.level == level }.count
    }

    /// 刷新日期列表；首次进入或手动刷新时选中最新一天（或保留当前天）并加载内容
    @MainActor
    func refresh(prefer: String?) async {
        guard let api else { return }
        loading = true
        error = nil
        do {
            days = try await api.logsDays().days
            let target = prefer.flatMap { day in days.contains { $0.day == day } ? day : nil } ?? days.first?.day
            activeDay = target
            if let target {
                await loadDay(target)
            } else {
                content = nil
                entries = []
                loading = false
            }
        } catch is CancellationError {
            loading = false
        } catch {
            self.error = error.localizedDescription
            loading = false
        }
    }

    /// 拉取某天内容；silent 为真时不动加载态（自动刷新不闪屏），失败也不打扰
    @MainActor
    func loadDay(_ day: String, tail newTail: Int? = nil, silent: Bool = false) async {
        guard let api, !inFlight else { return }
        inFlight = true
        defer { inFlight = false }
        if let newTail { tail = newTail }
        if !silent {
            loading = true
            error = nil
        }
        do {
            let next = try await api.logsRead(day: day, tail: tail)
            let added = next.totalLines - (content?.day == day ? content?.totalLines ?? 0 : 0)
            if silent, !atBottom, added > 0 { pendingNew += added }
            content = next
            entries = Self.parse(next.lines)
            error = nil
        } catch is CancellationError {
        } catch {
            if !silent {
                content = nil
                entries = []
                self.error = error.localizedDescription
            }
        }
        if !silent { loading = false }
    }

    /// 切换日期：重置 tail 口径与跟随状态，回到「贴底看最新」
    @MainActor
    func switchDay(_ day: String) async {
        activeDay = day
        tail = nil
        atBottom = true
        pendingNew = 0
        await loadDay(day)
    }

    private static let pattern = try! NSRegularExpression(pattern: #"^\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}) \| ([A-Z]+) \| (\S+) \| (.*)$"#)

    /// 行流 → 条目；无法匹配格式的行视为上一条的续行（堆栈等）
    static func parse(_ lines: [String]) -> [SettingsLogEntry] {
        var result: [SettingsLogEntry] = []
        result.reserveCapacity(lines.count)
        for line in lines {
            let ns = line as NSString
            if let match = pattern.firstMatch(in: line, range: NSRange(location: 0, length: ns.length)) {
                let raw = ns.substring(with: match.range(at: 2))
                let level = raw == "CRITICAL" ? .error : (SettingsLogEntry.Level(rawValue: raw) ?? .info)
                result.append(SettingsLogEntry(id: result.count, time: ns.substring(with: match.range(at: 1)), level: level,
                                       module: ns.substring(with: match.range(at: 3)), message: ns.substring(with: match.range(at: 4))))
            } else if !result.isEmpty {
                result[result.count - 1].message += "\n" + line
            } else if !line.trimmingCharacters(in: .whitespaces).isEmpty {
                // 文件开头就是续行（tail 截断把堆栈拦腰截断），单独成条兜底
                result.append(SettingsLogEntry(id: 0, time: "", level: .info, module: "", message: line))
            }
        }
        return result
    }
}

// MARK: - 工具栏

private struct LogToolbar: View {
    @Bindable var model: SettingsLogModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Menu {
                    ForEach(model.days, id: \.day) { day in
                        Button {
                            Task { await model.switchDay(day.day) }
                        } label: {
                            if day.day == model.activeDay { Label(day.day, systemImage: "checkmark") } else { Text(day.day) }
                        }
                    }
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "calendar")
                        Text(model.activeDay ?? "暂无日志").monospacedDigit().lineLimit(1).fixedSize()
                        Image(systemName: "chevron.up.chevron.down").font(.caption2)
                    }
                    .font(.subheadline)
                }
                .buttonStyle(.glass)
                .disabled(model.loading || model.days.isEmpty)
                .accessibilityLabel("选择日志日期")
                .accessibilityIdentifier("logs-day")
                if let meta = model.days.first(where: { $0.day == model.activeDay }) {
                    Text(Formatters.bytes(meta.sizeBytes) + (model.content.map { " · 共 \($0.totalLines) 行" } ?? ""))
                        .font(.caption).monospacedDigit().foregroundStyle(Theme.textMuted)
                        .accessibilityIdentifier("logs-meta")
                }
                Spacer(minLength: 0)
                Button(model.loading ? "加载中…" : "刷新") { Task { await model.refresh(prefer: model.activeDay) } }
                    .buttonStyle(.glass)
                    .font(.subheadline)
                    .disabled(model.loading)
                    .accessibilityIdentifier("logs-refresh")
            }
            HStack(spacing: 8) {
                HStack(spacing: 5) {
                    if model.refreshMs > 0, model.isLatestDay {
                        SettingsStatusDot(color: Theme.success, size: 6, glow: true)
                    }
                    Text("自动刷新").font(.subheadline).foregroundStyle(Theme.textMuted)
                }
                Picker("自动刷新频率", selection: $model.refreshMs) {
                    ForEach(SettingsLogModel.refreshOptions, id: \.value) { option in
                        Text(option.label).tag(option.value)
                    }
                }
                .pickerStyle(.segmented)
                .accessibilityIdentifier("logs-auto-refresh")
            }
        }
    }
}

// MARK: - 日志窗口

private struct LogWindow: View {
    @Bindable var model: SettingsLogModel
    @Binding var fullscreen: Bool

    var body: some View {
        let visible = model.visible
        let query = model.query.trimmingCharacters(in: .whitespaces)
        VStack(spacing: 0) {
            // 过滤条：级别 chip（带条数）+ 搜索 + 全屏
            VStack(spacing: 8) {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 4) {
                        ForEach(SettingsLogModel.levelFilters, id: \.label) { filter in
                            let active = filter.id == model.levelFilter
                            let count = model.count(filter.id)
                            Button { model.levelFilter = filter.id } label: {
                                HStack(spacing: 3) {
                                    Text(filter.label)
                                    if count > 0 { Text("\(count)").monospacedDigit().opacity(0.7) }
                                }
                                .font(.subheadline.weight(active ? .medium : .regular))
                                .foregroundStyle(filter.id == .error && count > 0 ? Color(red: 1, green: 0.54, blue: 0.54) : (active ? Theme.text : Theme.textMuted))
                                .padding(.horizontal, 10).padding(.vertical, 4)
                                .background(active ? Color.white.opacity(0.14) : .clear, in: .capsule)
                            }
                            .buttonStyle(.plain)
                            .accessibilityAddTraits(active ? .isSelected : [])
                            .accessibilityIdentifier("logs-level-\(filter.label)")
                        }
                    }
                }
                HStack(spacing: 8) {
                    HStack(spacing: 6) {
                        Image(systemName: "magnifyingglass").font(.caption).foregroundStyle(Theme.textFaint)
                        TextField("搜索日志…", text: $model.query)
                            .font(.subheadline)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .accessibilityIdentifier("logs-search")
                        if !model.query.isEmpty {
                            Button { model.query = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(Theme.textFaint) }
                                .buttonStyle(.plain)
                        }
                    }
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 8))
                    .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.white.opacity(0.08)))
                    Button { fullscreen.toggle() } label: {
                        Image(systemName: fullscreen ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right")
                            .frame(width: 30, height: 30)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Theme.textMuted)
                    .accessibilityLabel(fullscreen ? "退出全屏" : "全屏查看")
                    .accessibilityIdentifier("logs-fullscreen")
                }
            }
            .padding(10)
            .background(Color.white.opacity(0.03))

            if let content = model.content, content.truncated {
                HStack {
                    Text("日志较长，仅加载末尾 \(content.lines.count) 行（全天共 \(content.totalLines) 行）")
                        .font(.caption).foregroundStyle(Theme.textFaint)
                    Spacer()
                    Button("加载全部") {
                        if let day = model.activeDay { Task { await model.loadDay(day, tail: 0) } }
                    }
                    .font(.caption.weight(.medium))
                    .disabled(model.loading)
                    .accessibilityIdentifier("logs-load-all")
                }
                .padding(.horizontal, 12).padding(.vertical, 6)
                .background(Color.white.opacity(0.02))
            }
            Divider().overlay(Color.white.opacity(0.06))

            ScrollViewReader { proxy in
                ScrollView([.vertical]) {
                    if visible.isEmpty {
                        Text(emptyText)
                            .font(.subheadline).foregroundStyle(Theme.textFaint)
                            .multilineTextAlignment(.center)
                            .padding(.horizontal, 24).padding(.vertical, 48)
                            .frame(maxWidth: .infinity)
                    } else {
                        LazyVStack(alignment: .leading, spacing: 0) {
                            ForEach(visible) { entry in
                                LogRow(entry: entry, query: query)
                            }
                            Color.clear.frame(height: 1).id("logs-bottom")
                        }
                        .textSelection(.enabled)
                    }
                }
                .background(Color.black.opacity(0.45))
                .onScrollGeometryChange(for: Bool.self) { geometry in
                    geometry.contentOffset.y + geometry.containerSize.height >= geometry.contentSize.height - 48
                } action: { _, bottom in
                    model.atBottom = bottom
                    if bottom { model.pendingNew = 0 }
                }
                .onChange(of: visible.count) { _, _ in
                    if model.atBottom { proxy.scrollTo("logs-bottom", anchor: .bottom) }
                }
                .onChange(of: model.content?.totalLines) { _, _ in
                    if model.atBottom { proxy.scrollTo("logs-bottom", anchor: .bottom) }
                }
                .onAppear { proxy.scrollTo("logs-bottom", anchor: .bottom) }
                .overlay(alignment: .bottomTrailing) {
                    if model.pendingNew > 0 {
                        Button {
                            model.atBottom = true
                            model.pendingNew = 0
                            withAnimation { proxy.scrollTo("logs-bottom", anchor: .bottom) }
                        } label: {
                            Label("\(model.pendingNew) 条新日志", systemImage: "arrow.down")
                                .font(.subheadline.weight(.medium))
                        }
                        .buttonStyle(.glass)
                        .padding(14)
                    }
                }
            }
        }
        .clipShape(.rect(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.white.opacity(0.08)))
        .frame(maxHeight: .infinity)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("logs-window")
    }

    private var emptyText: String {
        if model.loading { return "日志加载中…" }
        if model.days.isEmpty { return "还没有任何日志文件。日志随后端运行按天写入服务端 data/logs 目录，稍后再来看看。" }
        if model.entries.isEmpty { return "这一天的日志是空的。" }
        return "没有匹配的日志，换个级别或关键字试试。"
    }
}

/// 一行日志：时间（暗淡等宽）+ 级别徽标 + 模块（银蓝）+ 正文（可换行、命中高亮）
private struct LogRow: View {
    let entry: SettingsLogEntry
    let query: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(entry.time.isEmpty ? " " : entry.time).foregroundStyle(Theme.textFaint)
            Text(badge)
                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                .foregroundStyle(badgeColor)
                .frame(width: 40)
                .padding(.vertical, 1)
                .background(badgeColor.opacity(0.14), in: .rect(cornerRadius: 3))
            Text(message).foregroundStyle(textColor)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .font(.caption.monospaced())
        .padding(.horizontal, 10)
        .padding(.vertical, 2)
        .background(entry.level == .error ? Theme.danger.opacity(0.05) : .clear)
    }

    private var badge: String {
        switch entry.level {
        case .error: "ERROR"
        case .warning: "WARN"
        case .info: "INFO"
        case .debug: "DEBUG"
        }
    }

    private var badgeColor: Color {
        switch entry.level {
        case .error: Color(red: 1, green: 0.54, blue: 0.54)
        case .warning: Theme.warning
        case .info: Theme.textMuted
        case .debug: Theme.textFaint
        }
    }

    private var textColor: Color {
        switch entry.level {
        case .error: Color(red: 1, green: 0.7, blue: 0.7)
        case .warning: Color(red: 1, green: 0.95, blue: 0.8).opacity(0.8)
        case .info: Theme.textMuted
        case .debug: Theme.textFaint
        }
    }

    /// 正文（手机窄屏同 Web 隐藏模块列，搜索仍匹配模块名），搜索命中片段用浅银底标出（大小写不敏感）
    private var message: AttributedString {
        var result = AttributedString()
        var body = AttributedString(entry.message)
        if !query.isEmpty {
            var searchStart = body.startIndex
            while let range = body[searchStart...].range(of: query, options: .caseInsensitive) {
                body[range].backgroundColor = Theme.accent.opacity(0.3)
                body[range].foregroundColor = Theme.text
                searchStart = range.upperBound
            }
        }
        result += body
        return result
    }
}

// MARK: - 全屏

private struct LogFullscreen: View {
    @Bindable var model: SettingsLogModel
    @Binding var fullscreen: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("系统日志").font(.headline)
                Spacer()
                Button("完成") { fullscreen = false }.buttonStyle(.glass)
                    .accessibilityIdentifier("logs-fullscreen-done")
            }
            LogToolbar(model: model)
            if let error = model.error { SettingsNotice(text: error) }
            LogWindow(model: model, fullscreen: $fullscreen)
        }
        .padding(Theme.pagePadding)
        .background(Theme.background.ignoresSafeArea())
        .polling(every: model.refreshMs > 0 ? Double(model.refreshMs) / 1000 : 3) {
            if model.refreshMs > 0, model.isLatestDay, let day = model.activeDay {
                await model.loadDay(day, silent: true)
            }
        }
    }
}
