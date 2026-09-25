import SwiftUI

/// 更新与维护 →「定时任务」页签（Web scheduled-tasks-section.tsx + lib/scheduled-tasks.ts）。
///
/// 每个后台任务的周期与启停：给两种人能说清的形状——「每 N 小时」「每天固定时刻」，
/// 其它 cron 照样能显示与保存（改表达式）。启停开关改即存；周期改完点「保存」才落库（`PUT /scheduled-tasks/{key}`）。
/// 「媒体库对账」多一条建议：有库在网络挂载上且周期比一小时长时，提示调到每 1 小时（只建议，不替用户改）。
struct ScheduledTasksPanel<Header: View>: View {
    @ViewBuilder let header: () -> Header
    @Environment(\.api) private var api

    @State private var tasks: [API.ScheduledTaskView]?
    @State private var anyNetwork = false
    @State private var error: String?
    @State private var busyKey: String?

    var body: some View {
        List {
            Section { header() }
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets(top: 0, leading: 4, bottom: 0, trailing: 4))
            Section {
                Text("后台任务各自按周期运行；改动立即生效，不用重启。周期与启停按任务记在服务器上。")
                    .font(.subheadline).foregroundStyle(Theme.textMuted)
                    .listRowBackground(Color.clear)
                if let error { SettingsNotice(text: error) }
                if let reconcile = tasks?.first(where: { $0.key == "library_reconcile" }),
                   ScheduleShape.suggestReconcile(reconcile, anyNetwork: anyNetwork) {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("有媒体库放在网络挂载上：实时监控收不到远端变化，新文件全靠「媒体库对账」发现。现在是\(ScheduleShape.describe(reconcile))，建议调到每 1 小时——增量对账通常只需几秒。")
                            .font(.subheadline)
                        Button("调到每 1 小时") {
                            Task { await save(reconcile, .init(enabled: true, triggerType: "interval", intervalSeconds: 3600, cronExpr: nil)) }
                        }
                        .buttonStyle(.glass).controlSize(.small)
                        .disabled(busyKey == reconcile.key)
                    }
                }
                if tasks == nil, error == nil {
                    SettingsLoadingRow(text: "正在加载…")
                }
            }
            ForEach(tasks ?? [], id: \.key) { task in
                Section {
                    TaskEditor(task: task, busy: busyKey == task.key) { body in
                        await save(task, body)
                    }
                }
            }
        }
        .task { await load() }
    }

    private func load() async {
        do {
            async let rows = api.appTasksList()
            async let libs = try? api.libraryList()
            tasks = try await rows
            anyNetwork = (await libs ?? []).contains(where: \.networkMount)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func save(_ task: API.ScheduledTaskView, _ body: API.ScheduledTaskUpdate) async {
        busyKey = task.key
        defer { busyKey = nil }
        do {
            let updated = try await api.appTasksUpdate(taskKey: task.key, body: body)
            tasks = tasks?.map { $0.key == task.key ? updated : $0 }
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: - 周期形状（Web lib/scheduled-tasks.ts）

enum ScheduleShape {
    /// 「分 时 * * *」形状的 cron → 每天固定时刻；其它形状返回 nil
    static func dailyTime(_ cron: String?) -> (hour: Int, minute: Int)? {
        guard let cron else { return nil }
        let parts = cron.split(whereSeparator: \.isWhitespace).map(String.init)
        guard parts.count == 5, parts[2] == "*", parts[3] == "*", parts[4] == "*" else { return nil }
        guard parts[0].count <= 2, parts[1].count <= 2, let minute = Int(parts[0]), let hour = Int(parts[1]),
              parts[0].allSatisfy(\.isNumber), parts[1].allSatisfy(\.isNumber),
              hour <= 23, minute <= 59 else { return nil }
        return (hour, minute)
    }

    static func dailyCron(hour: Int, minute: Int) -> String { "\(minute) \(hour) * * *" }

    static func describe(_ task: API.ScheduledTaskView) -> String {
        if task.triggerType == "interval" {
            let seconds = task.intervalSeconds ?? 0
            if seconds <= 0 { return "间隔未设置" }
            if seconds % 3600 == 0 { return "每 \(seconds / 3600) 小时" }
            if seconds % 60 == 0 { return "每 \(seconds / 60) 分钟" }
            return "每 \(seconds) 秒"
        }
        if let daily = dailyTime(task.cronExpr) {
            return String(format: "每天 %02d:%02d", daily.hour, daily.minute)
        }
        return task.cronExpr.map { "cron：\($0)" } ?? "未设置"
    }

    /// 固定时刻 = 一天一次，比一小时长；间隔超过一小时也建议
    static func suggestReconcile(_ task: API.ScheduledTaskView, anyNetwork: Bool) -> Bool {
        guard anyNetwork else { return false }
        if task.triggerType != "interval" { return true }
        return (task.intervalSeconds ?? 0) > 3600
    }
}

// MARK: - 单个任务

private struct TaskEditor: View {
    let task: API.ScheduledTaskView
    let busy: Bool
    let onSave: (API.ScheduledTaskUpdate) async -> Void

    enum Mode: String, CaseIterable { case interval, daily, cron }

    @State private var mode: Mode = .interval
    @State private var hours = 6
    @State private var time = Date()
    @State private var cron = ""

    private static func mode(of task: API.ScheduledTaskView) -> Mode {
        if task.triggerType == "interval" { return .interval }
        return ScheduleShape.dailyTime(task.cronExpr) != nil ? .daily : .cron
    }

    /// 服务器那份变了（保存成功 / 别处改了）就把编辑态对齐回去
    private func sync() {
        mode = Self.mode(of: task)
        if let seconds = task.intervalSeconds, seconds > 0 { hours = max(1, Int((Double(seconds) / 3600).rounded())) }
        let daily = ScheduleShape.dailyTime(task.cronExpr) ?? (3, 0)
        time = Calendar.current.date(bySettingHour: daily.hour, minute: daily.minute, second: 0, of: .now) ?? .now
        cron = task.cronExpr ?? ""
    }

    private var draft: API.ScheduledTaskUpdate {
        switch mode {
        case .interval:
            return .init(enabled: task.enabled, triggerType: "interval", intervalSeconds: hours * 3600, cronExpr: nil)
        case .daily:
            let parts = Calendar.current.dateComponents([.hour, .minute], from: time)
            return .init(enabled: task.enabled, triggerType: "cron", intervalSeconds: nil,
                         cronExpr: ScheduleShape.dailyCron(hour: parts.hour ?? 3, minute: parts.minute ?? 0))
        case .cron:
            return .init(enabled: task.enabled, triggerType: "cron", intervalSeconds: nil,
                         cronExpr: cron.trimmingCharacters(in: .whitespaces))
        }
    }

    private var dirty: Bool {
        let d = draft
        if d.triggerType != task.triggerType { return true }
        return d.triggerType == "interval" ? d.intervalSeconds != task.intervalSeconds : d.cronExpr != task.cronExpr
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Toggle(isOn: Binding(get: { task.enabled }, set: { value in
                var body = draft
                body.enabled = value
                Task { await onSave(body) }
            })) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 8) {
                        Text(task.title).font(.body.weight(.medium))
                        Text(ScheduleShape.describe(task)).font(.subheadline).foregroundStyle(Theme.textMuted)
                    }
                    if !task.description.isEmpty {
                        Text(task.description).font(.caption).foregroundStyle(Theme.textMuted)
                    }
                    Text("上次 \(task.lastRunAt.map(Formatters.relative) ?? "还没跑过")"
                         + (task.enabled && task.nextRunAt != nil ? " · 下次 \(Formatters.dateTime(task.nextRunAt))" : "")
                         + (task.enabled ? "" : " · 已停用"))
                        .font(.caption).foregroundStyle(Theme.textFaint)
                }
            }
            .disabled(busy)
            .accessibilityIdentifier("task-enabled-\(task.key)")

            Picker("周期方式", selection: $mode) {
                Text("每隔").tag(Mode.interval)
                Text("每天固定时刻").tag(Mode.daily)
                Text("cron 表达式").tag(Mode.cron)
            }
            .pickerStyle(.segmented)
            .accessibilityIdentifier("task-mode-\(task.key)")

            HStack(spacing: 10) {
                switch mode {
                case .interval:
                    Stepper(value: $hours, in: 1 ... 168) {
                        Text("\(hours) 小时").monospacedDigit()
                    }
                    .accessibilityIdentifier("task-hours-\(task.key)")
                case .daily:
                    DatePicker("每天几点", selection: $time, displayedComponents: .hourAndMinute)
                        .environment(\.locale, Locale(identifier: "en_GB"))
                case .cron:
                    TextField("分 时 日 月 周，如 0 3 * * *", text: $cron)
                        .font(.subheadline.monospaced())
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }
            }
            HStack {
                Spacer()
                Button(busy ? "保存中…" : "保存") { Task { await onSave(draft) } }
                    .buttonStyle(.glass).controlSize(.small)
                    .disabled(busy || !dirty)
                    .accessibilityIdentifier("task-save-\(task.key)")
            }
        }
        .padding(.vertical, 4)
        .onAppear(perform: sync)
        .onChange(of: task) { _, _ in sync() }
    }
}
