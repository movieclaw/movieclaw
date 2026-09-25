import SwiftUI

/// 设置 → 概览（Web settings-overview-section.tsx）：管理员进设置的落地页。
///
/// 新手真正的问题只有三个——哪些必须配、现在有什么问题、下一步做什么。概览用一块面板回答全部：
/// - 有新版本时顶部一张安静的提示卡（`GET /app/update/pending`），点进「更新与维护」；
/// - 订阅链路体检（`GET /subscriptions/automation-readiness`，与 Web PipelineHealthPanel 同源判定）：
///   必要件（站点 / 下载器 / 媒体库）没配齐时呈现为开局清单；配齐后是逐库的链路体检，
///   红黄项按根因聚合成修复卡，每张卡给修复去处。体检只读，修复动作全部跳回原配置页。
/// 修完配置返回本页会自动重检（页面重新出现即重拉），形成「修复 → 回来 → 变绿」的闭环。
struct OverviewSettingsView: View {
    @Environment(\.api) private var api
    @Environment(Router.self) private var router

    @State private var pending: API.PendingUpdateView?
    @State private var health: API.PipelineHealthView?
    @State private var failed = false
    @State private var busy = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                if let pending, pending.appVersion != nil || pending.modelTag != nil {
                    updateCard(pending)
                }
                panel
            }
            .padding(Theme.pagePadding)
        }
        .appBackground()
        .onAppear { Task { await reload() } }
        .refreshable { await reload() }
    }

    private func reload() async {
        busy = true
        failed = false
        defer { busy = false }
        async let pendingResult = try? api.appUpdatePending()
        do {
            health = try await api.subscriptionsCheckAutomationReadiness()
        } catch is CancellationError {
        } catch {
            failed = true
        }
        pending = await pendingResult
    }

    // MARK: 更新提示卡

    /// 应用与模型可能同时有更新：只说更要紧的那个（应用版本）；用提示性的 info 色，不是告警色
    private func updateCard(_ pending: API.PendingUpdateView) -> some View {
        Button { router.push(.settingsSection(.app)) } label: {
            HStack(spacing: 12) {
                Image(systemName: "arrow.up.circle")
                VStack(alignment: .leading, spacing: 2) {
                    Text(pending.appVersion.map { "新版本 v\($0) 可用" } ?? "新识别模型 \(pending.modelTag ?? "") 可用")
                        .font(.body.weight(.medium))
                    Text("去「更新与维护」查看更新内容并一键升级").font(.caption).foregroundStyle(Theme.textMuted)
                }
                Spacer()
                Image(systemName: "chevron.right").font(.footnote).opacity(0.7)
            }
            .foregroundStyle(Theme.info)
            .padding(16)
            .cardStyle()
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("overview-update-card")
    }

    // MARK: 链路体检

    private var panel: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("订阅链路体检").font(.headline)
                Spacer()
                Button {
                    Task { await reload() }
                } label: {
                    HStack(spacing: 6) {
                        if busy { ProgressView().controlSize(.mini) }
                        Text("重新体检")
                    }
                }
                .buttonStyle(.glass)
                .controlSize(.small)
                .disabled(busy)
                .accessibilityIdentifier("overview-recheck")
            }
            Text("逐库预演「资源搜索 → 下载 → 投递 → 入库」的完整链路，与真实投递同一套判定。红点表示订阅会卡在那一步（工单不会丢，修好后自动重试）；黄点能转但有降级。修改配置后回到本页会自动重检。")
                .font(.subheadline).foregroundStyle(Theme.textMuted)

            if failed {
                Text("体检加载失败，请重试")
                    .font(.subheadline).foregroundStyle(Theme.textMuted)
                    .frame(maxWidth: .infinity).padding(.vertical, 18)
                    .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 12))
            } else if let health {
                if !health.sitesConfigured || !health.downloadersConfigured || health.libraries.isEmpty {
                    SetupChecklist(health: health)
                } else {
                    let hidden = Set(health.issues.map(\.key))
                    if !health.issues.isEmpty {
                        Text("需要处理 \(health.issues.count) 件事（下方库卡片的红黄点都来自这里）：")
                            .font(.subheadline.weight(.medium))
                        ForEach(Array(health.issues.enumerated()), id: \.offset) { _, issue in
                            IssueCard(issue: issue)
                        }
                    }
                    SharedSegmentsCard(health: health, hiddenKeys: hidden)
                    ForEach(health.libraries, id: \.libraryId) { pipeline in
                        LibraryPipelineCard(pipeline: pipeline, hiddenKeys: hidden)
                    }
                }
            } else {
                HStack(spacing: 10) {
                    ProgressView()
                    Text("正在体检…").font(.subheadline).foregroundStyle(Theme.textMuted)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(16)
                .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 12))
            }
        }
        .accessibilityIdentifier("pipeline-health")
    }
}

// MARK: - 状态语义与修复去处

/// 状态 → 颜色 / 文案（节点、检查项、库行共用一套语义）
enum PipelineStatus {
    static func color(_ status: String) -> Color {
        switch status {
        case "ok": Theme.success
        case "warn": Theme.warning
        default: Theme.danger
        }
    }

    static func label(_ status: String) -> String {
        switch status {
        case "ok": "正常"
        case "warn": "降级"
        default: "有问题"
        }
    }

    static func worst(_ statuses: [String]) -> String {
        if statuses.contains("error") { return "error" }
        if statuses.contains("warn") { return "warn" }
        return "ok"
    }

    /// 修复去处 → App 内路由与文案（Web fixTarget）。
    /// Web 修复选项还会带预填参数（fix_params，目标页读取后自动填表单）；App 的分区页面入口参数固定，只跳到分区。
    static func fixTarget(_ section: String?) -> (route: AppRoute, label: String)? {
        switch section {
        case "sites": (.settingsSection(.sites), "去接入站点")
        case "downloaders": (.settingsSection(.downloaders), "去下载器设置")
        case "import-watch": (.settingsSection(.importWatch), "去自动入库")
        case "libraries": (.libraryManage(), "去媒体库")
        default: nil
        }
    }
}

// MARK: - 开局清单

/// 三个必要件缺任一时，体检数据换一种读法——「下一步做什么」；配齐后自动让位给正常体检
private struct SetupChecklist: View {
    let health: API.PipelineHealthView
    @Environment(Router.self) private var router

    var body: some View {
        let steps: [(done: Bool, label: String, hint: String, route: AppRoute, action: String)] = [
            (health.sitesConfigured, "接入资源站点", "订阅从这里搜索资源", .settingsSection(.sites), "去接入"),
            (health.downloadersConfigured, "接入下载器", "qBittorrent / Transmission，找到的资源交给它下载", .settingsSection(.downloaders), "去接入"),
            (!health.libraries.isEmpty, "创建媒体库", "内容的家：下载完成后按「标题 (年份)」整理进库", .libraryManage(create: true), "去创建"),
        ]
        VStack(alignment: .leading, spacing: 12) {
            Text("把订阅跑起来需要三步（完成后这里会变成链路体检）：").font(.subheadline.weight(.medium))
            ForEach(Array(steps.enumerated()), id: \.offset) { index, step in
                HStack(spacing: 10) {
                    Text(step.done ? "✓" : "\(index + 1)")
                        .font(.caption.weight(.semibold))
                        .frame(width: 20, height: 20)
                        .foregroundStyle(step.done ? Theme.success : .white.opacity(0.7))
                        .background((step.done ? Theme.success.opacity(0.2) : Color.white.opacity(0.1)), in: .circle)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(step.label).font(.subheadline.weight(.medium))
                            .strikethrough(step.done)
                            .foregroundStyle(step.done ? .white.opacity(0.5) : .white.opacity(0.9))
                        Text(step.hint).font(.caption).foregroundStyle(Theme.textFaint)
                    }
                    Spacer(minLength: 6)
                    if !step.done {
                        Button(step.action) { router.push(step.route) }
                            .buttonStyle(.glass).controlSize(.small)
                    }
                }
            }
            Text("可选第四步：「自动入库」让下载区与媒体库分离（PT 保种推荐）——下载器落盘的内容自动硬链接进库，源文件继续做种。不配置则直接下载进库根，同样能自动入账。")
                .font(.caption).foregroundStyle(Theme.textFaint)
        }
        .padding(16)
        .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.white.opacity(0.08)))
        .accessibilityIdentifier("setup-checklist")
    }
}

// MARK: - 修复卡

/// 一张卡 = 一个根因：受影响的库 + 结构化的修复选项（多选项时由用户按自己的部署取舍）
private struct IssueCard: View {
    let issue: API.HealthIssueView
    @Environment(Router.self) private var router

    var body: some View {
        let isError = issue.status == "error"
        let tint = isError ? Theme.danger : Theme.warning
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 10) {
                SettingsStatusDot(color: PipelineStatus.color(issue.status)).padding(.top, 6)
                VStack(alignment: .leading, spacing: 4) {
                    Text(issue.title).font(.subheadline.weight(.semibold))
                    if !issue.affectedLibraries.isEmpty {
                        Text("影响 \(issue.affectedLibraries.count) 个库：\(issue.affectedLibraries.joined(separator: "、"))")
                            .font(.caption).foregroundStyle(Theme.textFaint)
                    }
                    Text(issue.detail).font(.subheadline).foregroundStyle(Theme.textMuted)
                }
            }
            ForEach(Array(issue.options.enumerated()), id: \.offset) { index, option in
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 6) {
                        if issue.options.count > 1 {
                            Text("\(index + 1)").font(.caption2.weight(.semibold))
                                .frame(width: 18, height: 18).background(Color.white.opacity(0.1), in: .circle)
                        }
                        Text(option.title).font(.subheadline.weight(.semibold))
                    }
                    if !option.why.isEmpty {
                        Text(option.why).font(.caption).foregroundStyle(Theme.textFaint)
                    }
                    Text(option.steps).font(.subheadline).foregroundStyle(Theme.textMuted)
                    if let target = PipelineStatus.fixTarget(option.fixSection) {
                        Button("\(option.fixLabel) →") { router.push(target.route) }
                            .buttonStyle(.glass).controlSize(.small)
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 10))
            }
        }
        .padding(14)
        .background(tint.opacity(0.06), in: .rect(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(tint.opacity(0.28)))
    }
}

// MARK: - 步骤条

private struct FlowNode {
    let key: String
    let label: String
    let sub: String?
    let status: String
}

/// 节点胶囊 + 连接线，红黄节点自然显眼（窄屏自动折行）
private struct FlowStepper: View {
    let nodes: [FlowNode]

    var body: some View {
        DiscoverFlowLayout(spacing: 6, lineSpacing: 6) {
            ForEach(Array(nodes.enumerated()), id: \.element.key) { index, node in
                HStack(spacing: 6) {
                    if index > 0 { Rectangle().fill(Color.white.opacity(0.2)).frame(width: 12, height: 1) }
                    HStack(spacing: 6) {
                        SettingsStatusDot(color: PipelineStatus.color(node.status), size: 6)
                        Text(node.label).font(.subheadline.weight(.medium)).foregroundStyle(.white.opacity(0.9)).fixedSize()
                        if let sub = node.sub, !sub.isEmpty {
                            // 路径类长值保留末尾（最有辨识度的部分）
                            Text(sub).font(.caption.monospaced()).foregroundStyle(Theme.textFaint)
                                .lineLimit(1).truncationMode(.head).frame(maxWidth: 160)
                        }
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(background(node.status), in: .rect(cornerRadius: 8))
                    .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(border(node.status)))
                }
            }
        }
    }

    private func background(_ status: String) -> Color {
        status == "ok" ? Color.white.opacity(0.03) : PipelineStatus.color(status).opacity(0.1)
    }

    private func border(_ status: String) -> Color {
        status == "ok" ? Color.white.opacity(0.08) : PipelineStatus.color(status).opacity(0.32)
    }
}

/// 检查项明细（红黄项默认展示；「查看全部」时含绿项），红黄项带修复去处
private struct ProblemList: View {
    let checks: [API.HealthCheckView]
    @Environment(Router.self) private var router

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider().overlay(Color.white.opacity(0.06))
            ForEach(Array(checks.enumerated()), id: \.offset) { _, check in
                HStack(alignment: .top, spacing: 8) {
                    SettingsStatusDot(color: PipelineStatus.color(check.status), size: 6).padding(.top, 6)
                    VStack(alignment: .leading, spacing: 4) {
                        (Text(check.label).fontWeight(.medium).foregroundStyle(Theme.text) + Text("  \(check.detail)"))
                            .font(.subheadline).foregroundStyle(Theme.textMuted)
                        if check.status != "ok", let fix = PipelineStatus.fixTarget(check.fixSection) {
                            Button("\(fix.label) →") { router.push(fix.route) }
                                .font(.subheadline.weight(.medium))
                                .foregroundStyle(Theme.accent)
                                .buttonStyle(.plain)
                        }
                    }
                }
            }
        }
        .padding(.top, 4)
    }
}

// MARK: - 公共段 / 分库卡片

/// 公共段（资源搜索 + 下载器）：所有库共享，单独一行不逐库重复
private struct SharedSegmentsCard: View {
    let health: API.PipelineHealthView
    let hiddenKeys: Set<String>

    var body: some View {
        let downloaderCheck = health.libraries.first?.checks.first { $0.key == "downloader" }
        let nodes = [
            FlowNode(key: "sites", label: "资源搜索", sub: nil, status: health.siteCheck.status),
            FlowNode(key: "downloader", label: "下载器", sub: nil,
                     status: downloaderCheck?.status ?? (health.downloaderOk ? "ok" : "error")),
        ]
        let problems = ([health.siteCheck] + (downloaderCheck.map { [$0] } ?? []))
            .filter { $0.status != "ok" && !hiddenKeys.contains($0.key) }
        VStack(alignment: .leading, spacing: 8) {
            Text("公共链路").font(.caption).foregroundStyle(Theme.textFaint)
            FlowStepper(nodes: nodes)
            if !problems.isEmpty { ProblemList(checks: problems) }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.white.opacity(0.08)))
    }
}

/// 一个库的流水线卡片：步骤条 + 正向叙事 + 红黄项（已聚合进修复卡的让位，状态点保留）
private struct LibraryPipelineCard: View {
    let pipeline: API.LibraryPipelineView
    let hiddenKeys: Set<String>
    @State private var showAll = false

    private var nodes: [FlowNode] {
        func byKey(_ keys: [String]) -> [API.HealthCheckView] { pipeline.checks.filter { keys.contains($0.key) } }
        let dispatch = byKey(["dispatch_dir", "mapping"])
        let transfer = byKey(["transfer_disk", "watch_active"])
        let ingestError = pipeline.checks.contains { $0.key == "dispatch_dir" && $0.fixSection == "libraries" && $0.status != "ok" }
        var result = [FlowNode(key: "dispatch", label: pipeline.mode == "watch" ? "投递到监听目录" : "投递",
                               sub: pipeline.path, status: PipelineStatus.worst(dispatch.map(\.status)))]
        if pipeline.mode == "watch" {
            result.append(FlowNode(key: "transfer", label: pipeline.stagingPath != nil ? "整理输出" : "转移进库",
                                   sub: pipeline.stagingPath, status: PipelineStatus.worst(transfer.map(\.status))))
        }
        if pipeline.stagingPath != nil {
            // 自定义目录规则：整理后隔着用户自己的外部流转，movieclaw 不判它的死活，节点仅作说明
            result.append(FlowNode(key: "external", label: "外部流转", sub: nil, status: "ok"))
        }
        result.append(FlowNode(key: "ingest", label: pipeline.stagingPath != nil ? "回流入账" : "入库",
                               sub: pipeline.libraryRoot, status: ingestError ? "error" : "ok"))
        return result
    }

    var body: some View {
        let problems = pipeline.checks.filter { $0.status != "ok" && !hiddenKeys.contains($0.key) }
        let listed = showAll ? pipeline.checks.filter { $0.key != "downloader" } : problems
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text(pipeline.libraryName).font(.subheadline.weight(.semibold))
                Text((pipeline.kind == "movie" ? "电影" : "剧集") + (pipeline.isDefault ? " · 默认" : ""))
                    .font(.caption).foregroundStyle(Theme.textFaint)
                Spacer()
                Text(PipelineStatus.label(pipeline.status)).font(.subheadline.weight(.medium))
                    .foregroundStyle(PipelineStatus.color(pipeline.status))
            }
            FlowStepper(nodes: nodes)
            // 正向叙事：订阅命中本库会发生什么
            if !pipeline.narrative.isEmpty {
                Text(pipeline.narrative).font(.caption).foregroundStyle(Theme.textFaint)
            }
            Button(showAll ? "收起" : "查看全部") { withAnimation { showAll.toggle() } }
                .font(.caption).foregroundStyle(Theme.textFaint).buttonStyle(.plain)
            if !listed.isEmpty { ProblemList(checks: listed) }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.white.opacity(0.08)))
        .accessibilityIdentifier("pipeline-library-\(pipeline.libraryId)")
    }
}
