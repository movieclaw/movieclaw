import SwiftUI

/// 更新与维护 →「缓存管理」页签（Web app-storage-section.tsx）。
///
/// 这一页是后端登记表（services/storage/registry.py）的视图：每个 data/ 目录的名称、一句话用途、完整说明、
/// 占用、能否清理都由后端给出，前端只负责分组渲染与确认交互——业务新增一种缓存不需要改这里。
///
/// 打开页面**不会**触发实时统计（遍历 data/ 在大库上要几十秒）：后端给「上一次的快照 + 是否正在重算」，
/// 页面秒开并标出「统计于 N 分钟前」；点「刷新」才重算，统计期间每 2 秒轮询，旧数据留在页面上直到新快照落地。
/// 清理动作收进行尾 ⋯ 菜单（清理孤儿条目 / 全部清空），二次确认，重建代价高的目录用红色确认键加重提醒。
struct AppStoragePanel<Header: View>: View {
    @ViewBuilder let header: () -> Header
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback

    @State private var usage: API.StorageUsageView?
    @State private var computing = false
    @State private var error: String?
    @State private var busyKey: String?
    @State private var notice: (key: String, text: String, ok: Bool)?
    @State private var loaded = false

    var body: some View {
        List {
            Section { header() }
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets(top: 0, leading: 4, bottom: 0, trailing: 4))
            overviewSection
            if let usage, !usage.unregistered.isEmpty {
                Section("未登记目录") {
                    Text("数据目录下出现了程序未登记的条目，不会被统计或清理。请把路径反馈给开发者。")
                        .font(.subheadline).foregroundStyle(Theme.warning)
                    ForEach(usage.unregistered, id: \.path) { entry in
                        HStack {
                            Text(relative(entry.path, usage.dataRoot)).font(.footnote.monospaced()).lineLimit(1).truncationMode(.middle)
                            Spacer()
                            Text(Formatters.bytes(entry.bytes)).font(.footnote).monospacedDigit()
                        }
                        .foregroundStyle(Theme.warning.opacity(0.8))
                    }
                }
            }
            dirSection(
                title: "可清理的缓存", group: "cache",
                trailing: usage.map { "合计 \(Formatters.bytes($0.cacheBytes))" }
            )
            dirSection(
                title: "应用数据", group: "data",
                trailing: usage.map { "合计 \(Formatters.bytes($0.dataBytes)) · 只展示，不提供删除" }
            )
        }
        .task { await load(refresh: false) }
        .polling(every: 2) { if computing { await load(refresh: false) } }
    }

    /// 读一次状态：拿到新快照才替换页面数据，没算完就只更新「统计中」标记
    private func load(refresh: Bool) async {
        do {
            let state = try await api.appStorageUsage(refresh: refresh ? true : nil)
            if let next = state.usage { usage = next }
            computing = state.computing
            error = state.error
        } catch is CancellationError {
        } catch {
            computing = false
            self.error = error.localizedDescription
        }
        loaded = true
    }

    // MARK: 磁盘概览

    private var statusText: String {
        if let usage {
            let time = Formatters.relative(Date(timeIntervalSince1970: TimeInterval(usage.computedAt)).ISO8601Format())
            return "统计于 \(time)\(computing ? " · 更新中" : "")"
        }
        return computing ? "首次统计中，可能要几十秒…" : "尚未统计"
    }

    private var overviewSection: some View {
        let total = usage?.diskTotal ?? 0
        let cache = usage?.cacheBytes ?? 0
        let data = usage?.dataBytes ?? 0
        let free = usage?.diskFree ?? 0
        // data/ 之外的其它占用（系统、其它应用）= 已用 − 本应用数据 − 缓存，负数按 0
        let other = max(0, (usage?.diskUsed ?? 0) - cache - data)
        let segments: [(label: String, value: Int, color: Color)] = [
            ("应用数据", data, Color(red: 0.22, green: 0.74, blue: 0.97).opacity(0.85)),
            ("可回收缓存", cache, Color(red: 0.98, green: 0.75, blue: 0.14).opacity(0.85)),
            ("其他占用", other, Color.white.opacity(0.2)),
            ("剩余", free, Color.white.opacity(0.07)),
        ]
        return Section {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .bottom) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("数据目录所在磁盘").font(.subheadline.weight(.medium))
                        Text(usage?.dataRoot ?? "…").font(.caption.monospaced()).foregroundStyle(Theme.textFaint).lineLimit(1)
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 2) {
                        Text(usage.map { _ in Formatters.bytes(free) } ?? "—").font(.title2.weight(.semibold)).monospacedDigit()
                        Text("剩余 · 共 \(usage.map { _ in Formatters.bytes(total) } ?? "—")").font(.caption).foregroundStyle(Theme.textFaint)
                    }
                }
                GeometryReader { proxy in
                    HStack(spacing: 1) {
                        ForEach(segments.prefix(3), id: \.label) { segment in
                            Rectangle().fill(segment.color)
                                .frame(width: total > 0 ? proxy.size.width * CGFloat(segment.value) / CGFloat(total) : 0)
                        }
                        Spacer(minLength: 0)
                    }
                }
                .frame(height: 8)
                .background(Color.white.opacity(0.07))
                .clipShape(.capsule)
                DiscoverFlowLayout(spacing: 16, lineSpacing: 6) {
                    ForEach(segments, id: \.label) { segment in
                        HStack(spacing: 6) {
                            SettingsStatusDot(color: segment.color)
                            Text(segment.label).font(.caption).foregroundStyle(Theme.textMuted)
                            Text(usage.map { _ in Formatters.bytes(segment.value) } ?? "—").font(.subheadline.weight(.medium)).monospacedDigit()
                        }
                    }
                }
                if let error {
                    Text(error).font(.subheadline).foregroundStyle(Theme.danger)
                }
            }
            .padding(.vertical, 4)
        } header: {
            HStack {
                Text("磁盘概览")
                Spacer()
                Text(statusText).textCase(nil).lineLimit(1)
                Button {
                    Task { await load(refresh: true) }
                } label: {
                    Label(computing ? "统计中" : "刷新", systemImage: "arrow.clockwise")
                        .font(.caption.weight(.medium))
                }
                .buttonStyle(.glass)
                .controlSize(.mini)
                .disabled(computing)
                .textCase(nil)
                .accessibilityIdentifier("storage-refresh")
            }
        }
    }

    // MARK: 目录行

    private func dirSection(title: String, group: String, trailing: String?) -> some View {
        Section {
            if let usage {
                ForEach(usage.dirs.filter { $0.group == group }, id: \.key) { dir in
                    dirRow(dir)
                }
            } else if !loaded || computing {
                SettingsLoadingRow()
            }
        } header: {
            HStack {
                Text(title)
                Spacer()
                if let trailing { Text(trailing).textCase(nil).monospacedDigit() }
            }
        }
    }

    private func dirRow(_ dir: API.DirUsageView) -> some View {
        let expensive = dir.rebuildCost == "expensive"
        let actionable = dir.group == "cache" && (dir.orphanAware || dir.clearable)
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(dir.title).font(.body.weight(.medium)).lineLimit(1)
                        SettingsHelpTip(text: "\(dir.description)\n\n\(dir.path)", label: "「\(dir.title)」的说明")
                    }
                    HStack(spacing: 6) {
                        if expensive {
                            Text("重建代价高").font(.caption2.weight(.medium)).foregroundStyle(Theme.warning)
                                .padding(.horizontal, 6).padding(.vertical, 1)
                                .background(Theme.warning.opacity(0.1), in: .capsule)
                        }
                        Text(dir.summary).font(.caption).foregroundStyle(Theme.textFaint).lineLimit(2)
                    }
                }
                Spacer(minLength: 6)
                Text(dir.exists ? Formatters.bytes(dir.bytes) : "—")
                    .font(.subheadline.weight(.semibold)).monospacedDigit()
                    .foregroundStyle(dir.exists && dir.bytes > 0 ? Theme.text : Theme.textFaint)
                if actionable {
                    Menu {
                        if dir.orphanAware {
                            Button("清理孤儿条目") { Task { await askClean(dir, mode: "orphans") } }
                        }
                        if dir.clearable {
                            Button("全部清空", role: expensive ? .destructive : nil) { Task { await askClean(dir, mode: "all") } }
                        }
                    } label: {
                        Group {
                            if busyKey == dir.key { ProgressView().controlSize(.small) } else { Image(systemName: "ellipsis") }
                        }
                        .frame(width: 30, height: 30)
                        .contentShape(.rect)
                    }
                    .buttonStyle(.glass)
                    .disabled(busyKey == dir.key || !dir.exists)
                    .accessibilityLabel("「\(dir.title)」的清理操作")
                    .accessibilityIdentifier("storage-menu-\(dir.key)")
                }
            }
            if let notice, notice.key == dir.key {
                Text(notice.text).font(.caption).foregroundStyle(notice.ok ? Theme.success : Theme.danger)
            }
        }
    }

    /// 先确认再清理，文案按模式与重建代价分档
    private func askClean(_ dir: API.DirUsageView, mode: String) async {
        let ok: Bool
        if mode == "orphans" {
            ok = await feedback.confirm("清理「\(dir.title)」的孤儿条目？",
                                        message: "只删除媒体库里已不存在的条目，正在使用的内容不受影响。",
                                        confirmTitle: "清理孤儿条目")
        } else {
            ok = await feedback.confirm("清空「\(dir.title)」？",
                                        message: dir.description,
                                        confirmTitle: dir.bytes > 0 ? "清空并释放 \(Formatters.bytes(dir.bytes))" : "全部清空",
                                        destructive: dir.rebuildCost == "expensive")
        }
        guard ok else { return }
        busyKey = dir.key
        notice = nil
        defer { busyKey = nil }
        do {
            let result = try await api.appStorageClean(key: dir.key, body: .init(mode: mode))
            var parts = ["已释放 \(Formatters.bytes(result.freedBytes))，删除 \(result.removed) 项"]
            if result.skippedBusy > 0 { parts.append("跳过正在使用的 \(result.skippedBusy) 项") }
            notice = (dir.key, parts.joined(separator: "，"), true)
            // 清理让后端快照标脏：这一次读取会拉起后台重算，占用数字随后被轮询替换
            await load(refresh: false)
        } catch {
            notice = (dir.key, error.localizedDescription, false)
        }
    }

    /// 未登记条目只显示相对 data/ 的短路径
    private func relative(_ path: String, _ root: String) -> String {
        let prefix = root.hasSuffix("/") ? root : root + "/"
        return path.hasPrefix(prefix) ? String(path.dropFirst(prefix.count)) : path
    }
}
