import SwiftUI

// 订阅详情主体的「一集一条履历」（对应 Web subscription-inspector-view.tsx 的
// SearchRoundBar / WantedBreakdown / WantedRow / MilestoneChain / ActivityLogSection）。
//
// 每集展开成一条固定形状的里程碑链（播出 → 搜索 → 投递 → 下载 → 入库 →（洗版）），
// 第一个非完成节点就是这集卡住的地方；集行上的胶囊 = 微型链（亮点指卡在第几站）+ 三字短名，
// 与展开链共用一套颜色。多季订阅默认只展开「最新一季 ∪ 有在途工单的季」，其余折成带进度的季头行。

/// 订阅级卡片底（半透明玻璃卡）
private struct InspectorCard: ViewModifier {
    func body(content: Content) -> some View {
        content
            .background(Color(red: 14 / 255, green: 16 / 255, blue: 22 / 255).opacity(0.45), in: .rect(cornerRadius: 18))
            .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(Color.white.opacity(0.07)))
            .clipShape(.rect(cornerRadius: 18))
    }
}

extension View {
    fileprivate func inspectorCard() -> some View { modifier(InspectorCard()) }
}

// MARK: - 搜索轮次摘要

/// 最近一轮搜索压成一行：「2 天前搜了一轮」+ 关键数字 + 下轮时间；失败轮或老记录退化为完整句子。
/// 没有任何搜索记录时整行不渲染。
struct SearchRoundBar: View {
    let activities: [API.ActivityView]
    let wanted: [API.WantedView]

    var body: some View {
        if let latest = activities.first(where: { $0.type == "searched" }) {
            let p = latest.payload
            // 与 Web `!p.failed` 同口径：缺省 / null / false / 0 / 空串都算没失败
            let failed = p["failed"].map { value in
                !(value.isNull || value == .bool(false) || value == .int(0) || value == .string(""))
            } ?? false
            let hasStats = p["results"]?.intValue != nil && !failed
            let nextDue = wanted.filter { $0.status == "wanted" && $0.nextSearchAt != nil }.compactMap(\.nextSearchAt).sorted().first
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 10) {
                    Circle().fill(SubsColor.info).frame(width: 6, height: 6)
                        .background(Circle().fill(SubsColor.info.opacity(0.14)).frame(width: 12, height: 12))
                    Text("\(SubsFormat.relative(latest.createdAt))搜了一轮").font(.subheadline.weight(.medium)).foregroundStyle(Theme.text.opacity(0.85))
                }
                Text(hasStats
                    ? "\(p["results"]?.intValue ?? 0) 结果 · 命中 \(p["identity_hits"]?.intValue ?? 0) · 拒绝 \(p["rejected"]?.intValue ?? 0) · 投递 \(p["dispatched_units"]?.intValue ?? 0) 个单元" + (nextDue.map { " · 下轮 \(SubsFormat.dateTime($0))" } ?? "")
                    : latest.message)
                    .font(.caption).monospacedDigit()
                    .foregroundStyle(Theme.textFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, 16).padding(.vertical, 11)
            .frame(maxWidth: .infinity, alignment: .leading)
            .inspectorCard()
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("search-round")
        }
    }
}

// MARK: - 按季分组的追踪项

struct WantedBreakdown: View {
    let wanted: [API.WantedView]
    let isMovie: Bool
    /// info_hash → 实时下载快照
    let downloads: [String: API.SubscriptionDownloadView]
    /// 工单 id → 仍未解决的失败活动
    let failures: [Int: API.ActivityView]
    /// 片源标注入口（管理员）
    let canAnnotate: Bool
    @Binding var openSeasons: Set<Int>
    let onAnnotate: (Int) -> Void

    @State private var openWanted: Int?

    var body: some View {
        if wanted.isEmpty {
            Text("当前没有追踪项。开启「自动续订」后，新集播出会自动加入。")
                .font(.subheadline).foregroundStyle(Theme.textMuted)
                .padding(18)
                .frame(maxWidth: .infinity, alignment: .leading)
                .inspectorCard()
        } else {
            let groups = Dictionary(grouping: wanted, by: \.seasonNumber)
            let annotatable = canAnnotate ? Set(wanted.filter { $0.upgrade?.indeterminate == true }.map(\.seasonNumber)) : []
            let collapsible = !isMovie && groups.count > 1
            VStack(spacing: 14) {
                // 倒序：最新一季在最上面（也就是默认展开的那一季）
                ForEach(groups.keys.sorted(by: >), id: \.self) { season in
                    let items = (groups[season] ?? []).sorted { $0.episodeNumber < $1.episodeNumber }
                    let open = !collapsible || openSeasons.contains(season)
                    VStack(spacing: 0) {
                        if !isMovie || annotatable.contains(season) {
                            seasonHeader(season, items: items, open: open, collapsible: collapsible, annotatable: annotatable.contains(season))
                            if open { Divider().overlay(Color.white.opacity(0.06)) }
                        }
                        if open {
                            ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                                if index > 0 { Divider().overlay(Color.white.opacity(0.05)) }
                                WantedRow(
                                    wanted: item,
                                    isMovie: isMovie,
                                    download: item.infoHash.flatMap { downloads[$0] },
                                    failure: failures[item.id],
                                    expanded: openWanted == item.id
                                ) {
                                    withAnimation(.snappy) { openWanted = openWanted == item.id ? nil : item.id }
                                }
                            }
                        }
                    }
                    .inspectorCard()
                    .accessibilityElement(children: .contain)
                    .accessibilityIdentifier("season-group-\(season)")
                }
            }
        }
    }

    private func seasonHeader(_ season: Int, items: [API.WantedView], open: Bool, collapsible: Bool, annotatable: Bool) -> some View {
        let arranged = items.filter { $0.status != "wanted" }.count
        let upgrading = items.filter { $0.upgrade?.active == true }.count
        return HStack(spacing: 8) {
            if collapsible {
                Image(systemName: "chevron.down")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.textFaint)
                    .rotationEffect(.degrees(open ? 180 : 0))
            }
            Text(isMovie ? "正片" : SubsFormat.seasonName(season)).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text.opacity(0.8))
            if !isMovie {
                Text("\(arranged)/\(items.count)").font(.subheadline).monospacedDigit().foregroundStyle(Theme.textFaint)
            }
            if upgrading > 0 {
                Text("洗版 \(upgrading)").font(.subheadline).monospacedDigit().foregroundStyle(SubsColor.upgrade.opacity(0.8))
            }
            Spacer(minLength: 4)
            if collapsible, !open {
                // 收起态迷你进度条：与同行 N/M 同一比值
                Capsule().fill(Color.white.opacity(0.1)).frame(width: 48, height: 4)
                    .overlay(alignment: .leading) {
                        Capsule().fill(arranged == items.count ? SubsColor.ok : SubsColor.warn)
                            .frame(width: 48 * CGFloat(arranged) / CGFloat(max(items.count, 1)), height: 4)
                    }
            }
            if annotatable {
                Button("标注片源") { onAnnotate(season) }
                    .font(.caption.weight(.medium))
                    .buttonStyle(.bordered)
                    .controlSize(.mini)
                    .accessibilityIdentifier("annotate-season-\(season)")
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 11)
        .contentShape(.rect)
        .onTapGesture {
            guard collapsible else { return }
            withAnimation(.snappy) {
                if openSeasons.contains(season) { openSeasons.remove(season) } else { openSeasons.insert(season) }
            }
        }
        .accessibilityAddTraits(collapsible ? .isButton : [])
        .accessibilityIdentifier("season-header-\(season)")
    }
}

// MARK: - 单个追踪项

/// 一行「集号 + 说明 + 状态胶囊」，点开展开该集的里程碑链（电影只有「正片」一条，链常展开）
struct WantedRow: View {
    let wanted: API.WantedView
    let isMovie: Bool
    let download: API.SubscriptionDownloadView?
    let failure: API.ActivityView?
    let expanded: Bool
    let onToggle: () -> Void

    var body: some View {
        // 入库失败是死局：胶囊自己变红，否则这一行与正常等待入库毫无区别
        let stuckOnImport = failure?.type == "import_failed" ? failure : nil
        let presentation = stuckOnImport.map { WantedPresentation(label: "入库失败", color: SubsColor.danger, note: $0.message) } ?? WantedLogic.presentation(wanted)
        let live = stuckOnImport == nil && (wanted.status == "grabbed" || wanted.status == "downloaded") ? download : nil
        let chain = WantedLogic.milestones(wanted, isMovie: isMovie, live: live, failure: failure)
        let lit = WantedLogic.stuckIndex(chain)
        let open = isMovie || expanded
        VStack(spacing: 0) {
            Button(action: { if !isMovie { onToggle() } }) {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 10) {
                        Text(isMovie ? "正片" : "E\(SubsFormat.pad(wanted.episodeNumber))")
                            .font(.subheadline.weight(.semibold)).monospacedDigit()
                            .foregroundStyle(Theme.text.opacity(0.9))
                        Spacer(minLength: 4)
                        pill(presentation, chain: chain, lit: lit)
                        if !isMovie {
                            Image(systemName: "chevron.down")
                                .font(.caption2.weight(.semibold))
                                .foregroundStyle(expanded ? Theme.textMuted : Color.white.opacity(0.3))
                                .rotationEffect(.degrees(expanded ? 180 : 0))
                        }
                    }
                    Text(live.map(WantedLogic.downloadNote) ?? presentation.note)
                        .font(.subheadline).monospacedDigit()
                        .foregroundStyle(Theme.textMuted)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    if let live, let progress = live.progress, live.state != "missing" {
                        ProgressView(value: min(1, max(0.01, progress)))
                            .tint(SubsColor.ok)
                            .accessibilityIdentifier("wanted-progress")
                    }
                }
                .padding(.horizontal, 16).padding(.vertical, 11)
                .background(expanded && !isMovie ? Color.white.opacity(0.05) : .clear)
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("wanted-row")
            if open {
                MilestoneChainView(chain: chain, color: presentation.color)
            }
        }
    }

    private func pill(_ presentation: WantedPresentation, chain: [Milestone], lit: Int) -> some View {
        HStack(spacing: 7) {
            HStack(spacing: 3) {
                ForEach(Array(chain.enumerated()), id: \.offset) { index, milestone in
                    Circle()
                        .fill(index == lit ? presentation.color : milestone.state == .done ? Color.white.opacity(0.38) : Color.white.opacity(0.14))
                        .frame(width: 4.5, height: 4.5)
                        .shadow(color: index == lit ? presentation.color.opacity(0.6) : .clear, radius: 2.5)
                }
            }
            .frame(width: 42, alignment: .leading)
            Text(presentation.shortLabel)
        }
        .font(.system(size: 11, weight: .semibold))
        .foregroundStyle(presentation.color)
        .padding(.horizontal, 10).padding(.vertical, 3)
        .background(presentation.color.opacity(0.14), in: .capsule)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("第 \(lit + 1)/\(chain.count) 站：\(chain[safe: lit]?.label ?? "")，\(presentation.label)")
        .accessibilityIdentifier("wanted-pill")
    }
}

extension Array {
    fileprivate subscript(safe index: Int) -> Element? { indices.contains(index) ? self[index] : nil }
}

// MARK: - 里程碑链

/// 展开的里程碑链：竖轨节点 + 站名 + 时间列 + 说明。卡点节点发光、站名染状态色，
/// 走过的轨线亮、没走的暗——链断在哪一眼可见。
struct MilestoneChainView: View {
    let chain: [Milestone]
    let color: Color

    var body: some View {
        let stuck = WantedLogic.stuckIndex(chain)
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(chain.enumerated()), id: \.element.id) { index, milestone in
                let isNow = milestone.state == .now || milestone.state == .fail
                HStack(alignment: .top, spacing: 12) {
                    VStack(spacing: 4) {
                        Group {
                            if isNow {
                                Circle().strokeBorder(color, lineWidth: 2.5).frame(width: 10, height: 10)
                                    .shadow(color: color.opacity(0.4), radius: 4)
                            } else if milestone.state == .done {
                                Circle().fill(Color.white.opacity(0.3)).frame(width: 9, height: 9)
                            } else {
                                Circle().strokeBorder(Color.white.opacity(0.15), lineWidth: 1.5).frame(width: 9, height: 9)
                            }
                        }
                        .padding(.top, 5)
                        if index < chain.count - 1 {
                            Capsule().fill(index < stuck ? Color.white.opacity(0.16) : Color.white.opacity(0.07))
                                .frame(width: 1.5)
                                .frame(maxHeight: .infinity)
                        }
                    }
                    .frame(width: 12)
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(milestone.label)
                                .font(.subheadline.weight(isNow ? .semibold : .medium))
                                .foregroundStyle(isNow ? color : milestone.state == .done ? Color.white.opacity(0.65) : Theme.textFaint)
                            Spacer(minLength: 4)
                            if !milestone.time.isEmpty {
                                Text(milestone.time).font(.caption).monospacedDigit().foregroundStyle(Theme.textFaint)
                            }
                        }
                        Text(milestone.detail)
                            .font(.subheadline).monospacedDigit()
                            .foregroundStyle(milestone.state == .done ? Theme.textFaint : milestone.state == .todo ? Color.white.opacity(0.25) : Color.white.opacity(0.8))
                            .fixedSize(horizontal: false, vertical: true)
                        if let why = milestone.why {
                            Text(why).font(.caption).foregroundStyle(SubsColor.reason.opacity(0.9)).fixedSize(horizontal: false, vertical: true)
                        }
                        ForEach(milestone.sources, id: \.self) { source in
                            Text(source).font(.caption).foregroundStyle(Theme.textFaint).lineLimit(2)
                        }
                    }
                    .padding(.bottom, index == chain.count - 1 ? 4 : 14)
                }
            }
        }
        .padding(.leading, 20).padding(.trailing, 16).padding(.vertical, 12)
        .background(Color.white.opacity(0.05))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("milestone-chain")
    }
}

// MARK: - 排查记录

/// 全量活动的折叠区，默认收起：链上看不出为什么时才下钻，按时间回放系统做过的每一步
struct ActivityLogSection: View {
    let activities: [API.ActivityView]
    @State private var open = false

    var body: some View {
        if !activities.isEmpty {
            VStack(spacing: 0) {
                Button {
                    withAnimation(.snappy) { open.toggle() }
                } label: {
                    HStack(spacing: 8) {
                        Text("排查记录").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text.opacity(0.7))
                        Text("\(activities.count)").font(.subheadline).monospacedDigit().foregroundStyle(Theme.textFaint)
                        Spacer()
                        Image(systemName: "chevron.down").font(.caption.weight(.semibold)).foregroundStyle(Theme.textFaint)
                            .rotationEffect(.degrees(open ? 180 : 0))
                    }
                    .padding(.horizontal, 16).padding(.vertical, 11)
                    .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("activity-log-toggle")
                if open {
                    Divider().overlay(Color.white.opacity(0.06))
                    ActivityTimelineView(activities: activities)
                        .padding(.horizontal, 16).padding(.vertical, 16)
                }
            }
            .inspectorCard()
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("activity-log")
        }
    }
}

/// 活动时间线：圆点定性（颜色）+ 后端写好的中文句子 + 相对时间
struct ActivityTimelineView: View {
    let activities: [API.ActivityView]

    var body: some View {
        LazyVStack(alignment: .leading, spacing: 0) {
            ForEach(Array(activities.enumerated()), id: \.element.id) { index, activity in
                let color = WantedLogic.activityColor(activity.type)
                let last = index == activities.count - 1
                HStack(alignment: .top, spacing: 14) {
                    VStack(spacing: 6) {
                        Circle().fill(color).frame(width: 8, height: 8).shadow(color: color.opacity(0.33), radius: 4).padding(.top, 6)
                        if !last { Rectangle().fill(Color.white.opacity(0.08)).frame(width: 1).frame(maxHeight: .infinity) }
                    }
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        Text(activity.message).font(.subheadline).foregroundStyle(Theme.text.opacity(0.85)).fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                        Text(SubsFormat.relative(activity.createdAt)).font(.caption).monospacedDigit().foregroundStyle(Theme.textFaint)
                    }
                    .padding(.bottom, last ? 0 : 16)
                }
            }
        }
    }
}
