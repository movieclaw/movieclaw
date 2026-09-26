import NukeUI
import SwiftUI

/// 站点行的全部操作（由分区根视图实现：确认框、弹层都挂在根上，行本身只发意图）
struct SettingsBSiteRowActions {
    var toggleEnabled: (API.ConfiguredSite) -> Void
    var toggleProtected: (API.ConfiguredSite) -> Void
    var enableBoost: (API.ConfiguredSite) -> Void
    var disableBoost: (API.ConfiguredSite) -> Void
    var toggleBoostPaused: (API.ConfiguredSite) -> Void
    var boostSettings: (API.ConfiguredSite) -> Void
    var editAuth: (API.ConfiguredSite) -> Void
    var reverify: (API.ConfiguredSite) -> Void
    var delete: (API.ConfiguredSite) -> Void
}

/// 站点行（对应 Web `SiteRow`）：一行一站，信息按优先级排——
/// - P0 常驻：徽标 + 保护盾 + 名称 + 验证状态（+ 已停用）；
/// - P1 条件行：验证失败时错误原因吃掉这一行（那一刻没有比它更重要的信息），否则开着刷流才出现刷流读数；
/// - P2 点行展开：刷流 / 账号 / 索引 / 授权四段详情（单开手风琴，同 Web）；
/// - 操作全收进右侧 ⋯ 菜单，卡面只留信息。停用的站点整行半透明。
struct SettingsBSiteRow: View {
    let item: API.CatalogItem
    let site: API.ConfiguredSite
    let stats: API.SiteSyncStatsView?
    let boost: API.SiteBoostStatsView?
    let expanded: Bool
    let busy: Bool
    let onToggle: () -> Void
    let actions: SettingsBSiteRowActions

    var body: some View {
        let failed = site.status == "failed" && site.lastError?.isEmpty == false
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Button(action: onToggle) {
                    HStack(spacing: 8) {
                        SettingsBSiteBadge(item: item)
                        if site.protected {
                            Image(systemName: "shield.fill")
                                .font(.footnote)
                                .foregroundStyle(Theme.info)
                                .accessibilityLabel("站点保护中")
                        }
                        Text(item.displayName)
                            .font(.body.weight(.semibold))
                            .foregroundStyle(Theme.text)
                            .lineLimit(1)
                        SettingsBSiteStatusBadge(status: site.status)
                        if !site.enabled { SettingsBBadge(text: "已停用") }
                        Spacer(minLength: 4)
                        Image(systemName: "chevron.down")
                            .font(.footnote.weight(.semibold))
                            .foregroundStyle(Theme.textFaint)
                            .rotationEffect(.degrees(expanded ? 180 : 0))
                    }
                    .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("site-row-\(site.siteId)")
                .accessibilityValue(expanded ? "已展开" : "已收起")

                menu
            }

            if failed {
                Text(site.lastError ?? "")
                    .font(.caption)
                    .foregroundStyle(Theme.danger)
                    .lineLimit(2)
                    .accessibilityIdentifier("site-error-\(site.siteId)")
            } else if site.boostEnabled {
                SettingsBSiteBoostReadout(boost: boost, paused: site.boostPaused)
            }

            if expanded {
                SettingsBSiteDetail(item: item, site: site, stats: stats, boost: boost)
                    .accessibilityIdentifier("site-detail-\(site.siteId)")
            }
        }
        .padding(.vertical, 4)
        .opacity(site.enabled ? 1 : 0.6)
        .animation(.easeOut(duration: 0.2), value: expanded)
    }

    /// ⋯ 菜单：启停 / 保护 / 刷流（开关、暂停、设置）/ 编辑授权 / 重新验证 / 删除，全部收口于此（同 Web SiteActionsMenu）
    private var menu: some View {
        Menu {
            Button(site.enabled ? "停用站点" : "启用站点", systemImage: site.enabled ? "pause.circle" : "play.circle") {
                actions.toggleEnabled(site)
            }
            Button(site.protected ? "取消保护" : "开启保护", systemImage: site.protected ? "shield.slash" : "shield") {
                actions.toggleProtected(site)
            }
            // 刷流启停都带二次确认（开启含预算设置）
            Button(site.boostEnabled ? "关闭刷流…" : "开启刷流…", systemImage: "arrow.up.circle") {
                site.boostEnabled ? actions.disableBoost(site) : actions.enableBoost(site)
            }
            if site.boostEnabled {
                // 暂停/恢复：临时给前台流量让出上行，任务保留、随时无损恢复，故不设二次确认
                Button(site.boostPaused ? "恢复刷流" : "暂停刷流", systemImage: site.boostPaused ? "play" : "pause") {
                    actions.toggleBoostPaused(site)
                }
                Button("刷流设置…", systemImage: "slider.horizontal.3") { actions.boostSettings(site) }
            }
            Button("编辑授权", systemImage: "key") { actions.editAuth(site) }
            Button("重新验证", systemImage: "arrow.clockwise") { actions.reverify(site) }
                .disabled(SettingsBSiteText.inProgress(site.status))
            if let url = URL(string: item.baseUrl), !item.baseUrl.isEmpty {
                Link(destination: url) { Label("打开站点网页", systemImage: "safari") }
            }
            Divider()
            Button("删除配置", systemImage: "trash", role: .destructive) { actions.delete(site) }
        } label: {
            Group {
                if busy {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: "ellipsis")
                }
            }
            .frame(width: 30, height: 30)
            .contentShape(.rect)
        }
        .buttonStyle(.glass)
        .buttonBorderShape(.circle)
        .disabled(busy)
        .accessibilityLabel("站点操作")
        .accessibilityIdentifier("site-menu-\(site.siteId)")
    }
}

/// 验证状态胶囊：小圆点 + 文案（同 Web 状态徽章，颜色走语义色）
struct SettingsBSiteStatusBadge: View {
    let status: String

    var body: some View {
        let meta = SettingsBSiteText.status(status)
        HStack(spacing: 4) {
            SettingsBDot(tone: meta.tone, size: 5)
            Text(meta.label).font(.caption2.weight(.semibold))
        }
        .foregroundStyle(meta.tone.color)
        .padding(.horizontal, 7)
        .padding(.vertical, 2)
        .background(meta.tone.color.opacity(0.12), in: .capsule)
        .fixedSize()
    }
}

/// 刷流行级读数：只回答「刷流在产出吗」——近 24h 上传量一个数字；
/// 刚开启还没有产出时退回显示已用量；暂停态吃掉读数（那一刻「为什么没产出」更重要）。
struct SettingsBSiteBoostReadout: View {
    let boost: API.SiteBoostStatsView?
    let paused: Bool

    var body: some View {
        HStack(spacing: 6) {
            Text("刷流").font(.caption.weight(.medium)).foregroundStyle(Theme.accent)
            if paused {
                Text("已暂停 · 做种限速让出上行").font(.caption.weight(.medium)).foregroundStyle(Theme.warning)
            } else {
                Text(readout).font(.caption).foregroundStyle(Theme.textMuted)
            }
        }
        .lineLimit(1)
        .accessibilityElement(children: .combine)
    }

    private var readout: String {
        let up24 = boost?.uploadedBytes24h ?? 0
        let used = boost?.usedBytes ?? 0
        if up24 > 0 { return "24h ↑\(SettingsBSiteFormat.bytes(up24))" }
        if used > 0 { return "已用 \(SettingsBSiteFormat.bytes(used))" }
        return "等待首个免费种"
    }
}

/// 站点徽标：优先取站点真实 favicon（域名 + /favicon.ico），失败回落首字母。
/// 经后端 `/images/proxy` 统一图片代理回源（`api.image`）：favicon 也是站点流量，必须走统一出口
/// 受代理路由管控，且服务端落盘缓存后不再反复触达站点（同 Web SiteBadge）。
struct SettingsBSiteBadge: View {
    let item: API.CatalogItem
    @Environment(\.api) private var api

    var body: some View {
        let origin = Self.origin(item.baseUrl)
        ZStack {
            if let origin {
                LazyImage(url: api.image("\(origin)/favicon.ico")) { state in
                    if let image = state.image {
                        image.resizable().scaledToFit().frame(width: 18, height: 18)
                    } else if state.error != nil {
                        letter
                    } else {
                        Color.clear
                    }
                }
            } else {
                letter
            }
        }
        .frame(width: 30, height: 30)
        .background(Color.white.opacity(0.05), in: .rect(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.white.opacity(0.08)))
    }

    private var letter: some View {
        Text(item.displayName.prefix(1).uppercased())
            .font(.footnote.weight(.semibold))
            .foregroundStyle(Theme.textMuted)
    }

    private static func origin(_ raw: String) -> String? {
        guard let url = URL(string: raw), let scheme = url.scheme, let host = url.host() else { return nil }
        return url.port.map { "\(scheme)://\(host):\($0)" } ?? "\(scheme)://\(host)"
    }
}

// MARK: - 展开详情

/// 展开详情（对应 Web `SiteDetail`）：刷流 / 账号 / 索引 / 授权四段，段与段之间发丝线分隔；
/// 手机上统计走两列等宽网格（同 Web 窄屏 grid-cols-2），数值用等宽数字纵向对齐。
struct SettingsBSiteDetail: View {
    let item: API.CatalogItem
    let site: API.ConfiguredSite
    let stats: API.SiteSyncStatsView?
    let boost: API.SiteBoostStatsView?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // ─ 刷流 ─ 只读运行统计；启停与预算在 ⋯ 菜单
            if site.boostEnabled {
                segment("刷流") {
                    if site.boostPaused {
                        Text("已暂停：在池做种压到极低上传限速，停止汰换与拉新种（任务与数据保留）。可在 ⋯ 菜单里「恢复刷流」。")
                            .font(.caption)
                            .foregroundStyle(Theme.warning)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    SettingsBSiteStatGrid {
                        SettingsBSiteStat(label: "已用 / 预算", value: "\(SettingsBSiteFormat.bytes(boost?.usedBytes ?? 0)) / \(SettingsBSiteFormat.bytes(site.boostBudgetBytes))")
                        SettingsBSiteStat(label: "在池做种", value: "\(boost?.activeCount ?? 0)")
                        SettingsBSiteStat(label: "累计上传", value: SettingsBSiteFormat.bytes(boost?.uploadedBytesTotal ?? 0))
                        SettingsBSiteStat(label: "汰换保留期", value: site.boostHoldDays > 0 ? "\(site.boostHoldDays) 天" : "不保护")
                        if let boost, boost.evictedCount > 0 {
                            SettingsBSiteStat(label: "已汰换", value: "\(boost.evictedCount)")
                        }
                    }
                    if let boost, boost.avgUsedBytes24h > 0 {
                        Text(boostWindowText(boost))
                            .font(.caption)
                            .foregroundStyle(Theme.textMuted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }

            // ─ 账号 ─ 身份与持有 / 流量指标两组
            if let profile = site.profile {
                segment("账号") {
                    SettingsBSiteStatGrid {
                        SettingsBSiteStat(label: "用户名", value: profile.username)
                        if !profile.userClass.isEmpty {
                            SettingsBSiteStat(label: "等级", value: profile.userClass)
                        }
                        SettingsBSiteStat(label: "做种", value: "\(profile.seedingCount)")
                        if let bonus = profile.bonus {
                            SettingsBSiteStat(label: "魔力", value: SettingsBSiteFormat.compact(bonus))
                        }
                    }
                    SettingsBSiteStatGrid {
                        SettingsBSiteStat(label: "上传量", value: SettingsBSiteFormat.bytes(profile.uploadedBytes))
                        SettingsBSiteStat(label: "下载量", value: SettingsBSiteFormat.bytes(profile.downloadedBytes))
                        SettingsBSiteStat(label: "分享率", value: SettingsBSiteFormat.ratio(profile.ratio))
                    }
                    // Web 把「资料更新于」放在悬停提示里，手机没有悬停，改成一行小字
                    Text("资料更新于 \(SettingsBSiteFormat.relative(profile.fetchedAt))")
                        .font(.caption2)
                        .foregroundStyle(Theme.textFaint)
                }
            }

            // ─ 索引 ─
            if let stats {
                segment("索引") {
                    SettingsBSiteStatGrid {
                        SettingsBSiteStat(label: "已缓存种子", value: stats.torrentCount.formatted(.number.locale(Locale(identifier: "zh_CN"))))
                        SettingsBSiteStat(label: "上次同步", value: SettingsBSiteFormat.relative(stats.lastSyncAt))
                        SettingsBSiteStat(label: "下次同步", value: SettingsBSiteFormat.nextSync(stats.nextSyncAt))
                        if let interval = stats.syncIntervalSeconds {
                            SettingsBSiteStat(label: "同步间隔", value: SettingsBSiteFormat.duration(interval))
                        }
                    }
                    if let error = stats.lastError, !error.isEmpty {
                        Text("上次同步失败：\(error)")
                            .font(.caption)
                            .foregroundStyle(Theme.danger)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }

            // ─ 授权 ─ 编辑入口在 ⋯ 菜单（编辑授权），这里只读
            segment("授权", last: true) {
                Text("\(SettingsBSiteText.authType(site.authType)) · 上次检查 \(SettingsBSiteFormat.relative(site.lastCheckedAt))")
                    .font(.subheadline)
                    .foregroundStyle(Theme.textMuted)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 4)
        .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 12))
    }

    private func boostWindowText(_ boost: API.SiteBoostStatsView) -> String {
        var text = "近 24 小时：\(SettingsBSiteFormat.bytes(boost.avgUsedBytes24h)) 在池种子贡献了 \(SettingsBSiteFormat.bytes(boost.uploadedBytes24h)) 上传"
        if boost.avgUsedBytes7d > 0 {
            text += " · 近 7 天：\(SettingsBSiteFormat.bytes(boost.avgUsedBytes7d)) 贡献 \(SettingsBSiteFormat.bytes(boost.uploadedBytes7d))"
        }
        return text
    }

    /// 分段：小标签在上、内容在下（Web 窄屏同款），段间发丝线
    private func segment<C: View>(_ label: String, last: Bool = false, @ViewBuilder content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(label)
                .font(.caption2.weight(.medium))
                .foregroundStyle(Theme.textFaint)
            content()
        }
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(alignment: .bottom) {
            if !last { Rectangle().fill(Color.white.opacity(0.06)).frame(height: 0.5) }
        }
    }
}

/// 统计网格：两列等宽，刻意不加底色小卡——层级靠对齐与字重撑住
struct SettingsBSiteStatGrid<Content: View>: View {
    @ViewBuilder let content: () -> Content
    var body: some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: 16, alignment: .leading), GridItem(.flexible(), spacing: 16, alignment: .leading)],
                  alignment: .leading, spacing: 10, content: content)
    }
}

/// 单个统计：淡色小标签在上、数值在下（等宽数字）
struct SettingsBSiteStat: View {
    let label: String
    let value: String
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.caption2).foregroundStyle(Theme.textFaint).lineLimit(1)
            Text(value)
                .font(.subheadline.weight(.semibold).monospacedDigit())
                .foregroundStyle(Theme.text)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}
