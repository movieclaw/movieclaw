import SwiftUI

/// 管理页的一行（Web `library-manage-row.tsx` 的手机卡片形态）。
///
/// 一行只有两个视觉重心：库名（白色、加粗）与状态（有事才带色）。类型、库存、配置备注一律是库名下的小字；
/// 根目录独占卡片一整行（拿到整个宽度，不必早早截尾）。行内不放独立按钮，所有操作收进右上的单一 ⋯ 菜单；
/// 唯一的例外是「待识别 / 缺失」胶囊本身可点——它是这页真正要你动手的信号，点它直达待处理清单。
struct ManageLibraryRow: View {
    /// 一行库能触发的全部操作；是否可用由行内按当前状态判定
    struct Actions {
        var toggleScan: (API.LibraryView) -> Void
        var openPending: (API.LibraryView) -> Void
        var organize: (API.LibraryView) -> Void
        var toggleRefresh: (API.LibraryView) -> Void
        var chapterImages: (API.LibraryView) -> Void
        var edit: (API.LibraryView) -> Void
        var setDefault: (API.LibraryView) -> Void
        var toggleHome: (API.LibraryView) -> Void
        var reorder: () -> Void
        var delete: (API.LibraryView) -> Void
    }

    let library: API.LibraryView
    let actions: Actions

    @Environment(Router.self) private var router

    private var status: ManageLibraryStatus { .of(library) }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .center, spacing: 12) {
                ManageLibraryThumb(library: library)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Button {
                            router.push(.library(id: library.id))
                        } label: {
                            Text(library.name)
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(.white)
                                .lineLimit(1)
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("manage-row-name-\(library.id)")
                        if library.isDefault { ManageBadge(text: "默认") }
                        if ManageLibraryRules.accessRestricted(library) {
                            ManageBadge(text: ManageLibraryRules.accessLabel(library), locked: !library.viewerAccess)
                        }
                    }
                    // 第二行小字：类型 · 库存 · 需要留意的配置
                    Text(([ManageKind.label(library.kind), ManageLibraryRules.inventory(library).primary]
                        + ManageLibraryRules.configNotes(library)).joined(separator: " · "))
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                        .lineLimit(2)
                }
                Spacer(minLength: 4)
                menu
            }

            // 根目录：主根（等宽）+ 多根时「+N 个根目录」
            HStack(spacing: 6) {
                Text(library.rootPaths.first ?? "—")
                    .font(.caption.monospaced())
                    .foregroundStyle(Theme.textMuted)
                    .lineLimit(1)
                    .truncationMode(.head)
                if library.rootPaths.count > 1 {
                    Text("+\(library.rootPaths.count - 1) 个根目录")
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                        .fixedSize()
                }
            }

            statusView
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("manage-row-\(library.id)")
    }

    // MARK: 状态

    /// 状态道三种形态：空闲只写「最近扫描 X 前」；任务中蓝点 + 百分比 + 进度条；待识别 / 缺失是带色胶囊
    @ViewBuilder
    private var statusView: some View {
        let status = self.status
        switch status.tone {
        case .idle:
            Text(status.detail)
                .font(.caption)
                .foregroundStyle(Theme.textFaint)
                .lineLimit(1)
        case .busy:
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Circle().fill(Theme.info).frame(width: 6, height: 6)
                        .background(Circle().fill(Theme.info.opacity(0.18)).frame(width: 12, height: 12))
                    Text(status.title).font(.footnote).foregroundStyle(.white.opacity(0.9)).lineLimit(1)
                }
                if let percent = status.percent {
                    GeometryReader { proxy in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.white.opacity(0.1))
                            Capsule().fill(Theme.info).frame(width: proxy.size.width * CGFloat(percent) / 100)
                        }
                    }
                    .frame(height: 3)
                    .animation(.easeOut(duration: 0.5), value: percent)
                }
                if !status.detail.isEmpty {
                    Text(status.detail).font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
                }
            }
        case .pending, .missing:
            let color = status.tone == .missing ? Theme.danger : Theme.warning
            ManageFlow(spacing: 12, lineSpacing: 4) {
                Button {
                    // 待处理清单只有刮削型库才有（与 ⋯ 菜单里「待处理」的显隐同一条件）
                    if library.capabilities.scraped { actions.openPending(library) }
                } label: {
                    HStack(spacing: 6) {
                        Circle().fill(color).frame(width: 6, height: 6)
                        Text(status.title).lineLimit(1)
                    }
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(color)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(color.opacity(0.1), in: .capsule)
                    .overlay(Capsule().strokeBorder(color.opacity(0.35)))
                }
                .buttonStyle(.plain)
                .disabled(!library.capabilities.scraped)
                .accessibilityIdentifier("manage-row-pending-\(library.id)")
                Text(status.detail).font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
            }
        }
    }

    // MARK: ⋯ 菜单

    /// 行尾菜单：首页卡片菜单与单库页菜单的并集，不新增功能（同 Web RowMenu）
    private var menu: some View {
        let scanning = library.scanning
        let organizing = library.organizing
        let refreshing = library.metadataRefresh?.refreshing == true
        let busy = scanning || organizing || refreshing
        // 重识别占同一把库级锁但不接受中途停止（后端会拒绝），入口置灰并如实标出
        let stoppable = scanning && library.scanProgress?.phase != "reidentifying"
        // 库快照只有文件级计数（待识别 + 缺失）；单位写明，免得与单库页按分组数的「待处理 N」混为一谈
        let pendingFiles = library.stats.unidentifiedCount + library.stats.missingCount
        let caps = library.capabilities
        let pct = status.percent.map { " \($0)%" } ?? ""

        return Menu {
            Section {
                Button(!scanning ? "扫描库" : stoppable ? "停止扫描\(pct)" : "\(ScanPhase.label(library.scanProgress?.phase))…") {
                    actions.toggleScan(library)
                }
                .disabled((busy && !scanning) || (scanning && !stoppable))
                // 待处理常驻：计数为 0 也可进（已忽略清单只有这里能到）
                if caps.scraped {
                    Button("待处理\(pendingFiles > 0 ? " · \(pendingFiles) 个文件" : "")") { actions.openPending(library) }
                }
                if caps.naming {
                    Button(organizing ? "整理中…\(pct)" : "整理文件名") { actions.organize(library) }
                        .disabled(busy && !organizing)
                }
                Button(refreshing ? "停止刷新\(pct)" : caps.scraped ? "刷新元数据" : caps.playable ? "重新读取 NFO 与封面" : "重新生成封面") {
                    actions.toggleRefresh(library)
                }
                .disabled(busy && !refreshing)
                if library.extractChapterImages {
                    // 作业排队 / 进行中时置灰并如实写状态（后端同库只跑一份）
                    Button(ManageLibraryStatus.chapterJobLabel(library.chapterJob)) { actions.chapterImages(library) }
                        .disabled(busy || library.chapterJob != nil)
                }
            }
            Section {
                // 扫描 / 整理正按当前根路径读写台账，期间不允许改库配置
                Button("编辑库") { actions.edit(library) }
                    .disabled(scanning || organizing)
                Button(library.isDefault ? "已是默认库" : "设为默认库") { actions.setDefault(library) }
                    .disabled(library.isDefault)
                Button(library.excludeFromHome ? "在首页展示" : "从首页排除") { actions.toggleHome(library) }
                Button("调整顺序") { actions.reorder() }
            }
            Section {
                Button(role: .destructive) {
                    actions.delete(library)
                } label: {
                    Text("删除库")
                    Text("不动磁盘")
                }
                .disabled(scanning || organizing)
            }
        } label: {
            Image(systemName: "ellipsis")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(.white.opacity(0.8))
                .frame(width: 32, height: 32)
                .background(Color.white.opacity(0.04), in: .circle)
                .overlay(Circle().strokeBorder(Color.white.opacity(0.09)))
                .contentShape(.circle)
        }
        .accessibilityLabel("「\(library.name)」的操作")
        .accessibilityIdentifier("manage-row-menu-\(library.id)")
    }
}

/// 库名旁的小标签：「默认」与可见范围胶囊共用；你本人不在浏览范围内时前面带一把锁
struct ManageBadge: View {
    let text: String
    var locked = false

    var body: some View {
        HStack(spacing: 3) {
            if locked { Image(systemName: "lock.fill").font(.system(size: 8)) }
            Text(text)
        }
        .font(.system(size: 10, weight: .semibold))
        .foregroundStyle(.white.opacity(0.75))
        .padding(.horizontal, 6)
        .padding(.vertical, 1)
        .background(Color.white.opacity(0.08), in: .capsule)
        .overlay(Capsule().strokeBorder(Color.white.opacity(0.14)))
        .fixedSize()
        .accessibilityLabel(locked ? "\(text)，你不在浏览范围内" : text)
    }
}

/// 小缩略图：服务端封面（自定义图或拼贴，与首页卡片同源），空库 / 你不在浏览范围内退回类型图标或锁
struct ManageLibraryThumb: View {
    let library: API.LibraryView
    @Environment(\.api) private var api

    var body: some View {
        let hasCover = library.customCover || library.stats.itemCount > 0
        ZStack {
            LinearGradient(colors: [Color(red: 0.11, green: 0.13, blue: 0.19), Color(red: 0.06, green: 0.07, blue: 0.11)],
                           startPoint: .topLeading, endPoint: .bottomTrailing)
            if library.viewerAccess, hasCover {
                RemoteImage(url: api.image("/libraries/\(library.id)/cover?v=\(library.updatedAt)"),
                            placeholderSymbol: LibraryKindMeta.symbol(library.kind))
            } else {
                Image(systemName: library.viewerAccess ? LibraryKindMeta.symbol(library.kind) : "lock.fill")
                    .font(.system(size: 14))
                    .foregroundStyle(.white.opacity(0.25))
            }
        }
        .frame(width: 72, height: 44)
        .clipShape(.rect(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Theme.line))
    }
}
