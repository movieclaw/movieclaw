import SwiftUI

/// 活动页（管理员，Web `/activity?view=`，`components/activity-view.tsx`）。
///
/// 一级按**领域**分「观看 / 任务」，二级才是各自的切片：
/// - 观看：此刻谁在看什么（纯观察，没有处置语义）——正在播放 / 最近播放 / 观看统计；
/// - 任务：有什么需要我处理——全部 / 进行中 / 需要处理 / 已结束。
/// 两者维度不同，把观看塞进任务的状态切片会稀释「需要处理」的优先级（docs/design/activity.md）。
///
/// 一级切换是大标题下方通栏的原生分段控件，段名后带数字提示：观看=正在播放人数，
/// 任务=需要处理数（没有则为进行中数），与标签角标同源同数。
/// 数据来自外壳常驻的 `ShellBadges.tasks / media`，来回切换不打断轮询与 SSE。
struct ActivityView: View {
    var initialView: String?

    @Environment(ShellBadges.self) private var badges
    @State private var scope: Scope
    @State private var watchView: WatchSlice
    @State private var taskView: TaskSlice

    enum Scope: String { case media, tasks }

    @Environment(Router.self) private var router

    init(initialView: String?) {
        self.initialView = initialView
        let raw = initialView
        let task = raw.flatMap(TaskSlice.init(rawValue:))
        _scope = State(initialValue: task == nil ? .media : .tasks)
        _taskView = State(initialValue: task ?? .all)
        _watchView = State(initialValue: raw.flatMap(WatchSlice.init(rawValue:)) ?? .playing)
    }

    /// 站内链接 /activity?view=… 切到本标签时带来的视图参数。
    /// 与 Web「地址即状态」同口径（lib/task-center.ts）：任务切片名 → 任务视角；
    /// 其余（观看切片名、缺省、非法值）→ 观看视角，非法值落「正在播放」；
    /// 没被点名的那一侧回到各自默认（观看=正在播放、任务=全部）。
    private func apply(view raw: String) {
        let task = TaskSlice(rawValue: raw)
        scope = task == nil ? .media : .tasks
        taskView = task ?? .all
        watchView = WatchSlice(rawValue: raw) ?? .playing
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                header
                switch scope {
                case .media:
                    WatchPanel(store: badges.media, view: $watchView)
                case .tasks:
                    TaskCenterPanel(store: badges.tasks, view: $taskView)
                }
            }
            .padding(.horizontal, Theme.pagePadding)
            .padding(.top, 8)
            .padding(.bottom, 48)
        }
        // 标题同媒体库：左上角大字页面名（iOS 标签根页规范）；观看 / 任务切换固定在标题下方
        .navigationTitle("活动")
        .toolbarTitleDisplayMode(.inlineLarge)
        .safeAreaBar(edge: .top, alignment: .leading) {
            ActivityScopeSwitcher(
                scope: $scope,
                liveCount: badges.media.liveCount,
                taskBadge: TaskCenter.badge(badges.tasks.activity)
            )
            .padding(.horizontal, Theme.pagePadding)
            .padding(.bottom, 6)
        }
        .appBackground()
        .onChange(of: router.rootParameter, initial: true) { _, parameter in
            guard let parameter, parameter.tab == .activity else { return }
            apply(view: parameter.value)
            router.rootParameter = nil
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(scope == .media
                ? "谁在看什么、用哪台设备、速率如何，媒体库的实时动静都在这里。"
                : "观察下载、入库和后台作业的完整过程，需要处理的任务会优先出现。")
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.bottom, 4)
    }
}

/// 观看视角的三个切片（值与 Web `WATCH_VIEWS` 一致，共用 `view` 查询参数）
enum WatchSlice: String, CaseIterable {
    case playing, plays, stats

    var label: String {
        switch self {
        case .playing: "正在播放"
        case .plays: "最近播放"
        case .stats: "观看统计"
        }
    }
}

/// 任务视角的四个状态切片（Web `TASK_CENTER_VIEWS`；标签顺序同 Web：全部/进行中/需要处理/已结束）
enum TaskSlice: String, CaseIterable {
    case all, active, attention, history

    var label: String {
        switch self {
        case .all: "全部"
        case .active: "进行中"
        case .attention: "需要处理"
        case .history: "已结束"
        }
    }
}

/// 一级视角切换：系统原生分段控件（iOS 26 自带液态玻璃滑块——按住会鼓成透镜、可拖着换段、带触感），
/// 通栏铺满与下方「正在播放 / 最近播放…」这类二级切片胶囊拉开层级。
/// 原生分段只能放纯文字，提示改成段名后的数字：观看=正在播放的人数、任务=需要处理数（没有时为进行中数）；
/// 红/蓝之分留给标签栏的活动角标，二级切片里「需要处理」也有计数。
struct ActivityScopeSwitcher: View {
    @Binding var scope: ActivityView.Scope
    let liveCount: Int
    let taskBadge: TaskCenter.Badge

    var body: some View {
        Picker("活动视角", selection: $scope.animation(.snappy(duration: 0.2))) {
            Text(liveCount > 0 ? "观看 \(liveCount)" : "观看")
                .accessibilityLabel(liveCount > 0 ? "观看，\(liveCount) 人正在观看" : "观看")
                .tag(ActivityView.Scope.media)
            Text(taskBadge.count > 0 ? "任务 \(taskBadge.count)" : "任务")
                .accessibilityLabel(taskBadge.count > 0 ? "任务，\(taskBadge.hint)" : "任务")
                .tag(ActivityView.Scope.tasks)
        }
        .pickerStyle(.segmented)
        .accessibilityIdentifier("activity-scope")
    }
}

/// 切片选项卡（观看 / 任务两处同一形态）：选中白系胶囊，可带计数
struct ActivitySliceTabs<Slice: Hashable>: View {
    let slices: [Slice]
    let selection: Slice
    let label: (Slice) -> String
    let count: (Slice) -> Int?
    let identifier: (Slice) -> String
    let onSelect: (Slice) -> Void

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 4) {
                ForEach(slices, id: \.self) { slice in
                    let selected = slice == selection
                    Button {
                        onSelect(slice)
                    } label: {
                        HStack(spacing: 5) {
                            Text(label(slice))
                            if let count = count(slice), count > 0 {
                                Text("\(count)").font(.caption).monospacedDigit().foregroundStyle(Theme.textFaint)
                            }
                        }
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(selected ? Theme.text : Theme.textMuted)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 7)
                        .background(selected ? Color.white.opacity(0.14) : .clear, in: .capsule)
                        .contentShape(.capsule)
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier(identifier(slice))
                    .accessibilityAddTraits(selected ? .isSelected : [])
                }
            }
        }
        .scrollClipDisabled()
    }
}
