import SwiftUI

/// 活动页（管理员，Web `/activity?view=`，`components/activity-view.tsx`）。
///
/// 一级按**领域**分「观看 / 任务」，二级才是各自的切片：
/// - 观看：此刻谁在看什么（纯观察，没有处置语义）——正在播放 / 最近播放 / 观看统计；
/// - 任务：有什么需要我处理——全部 / 进行中 / 需要处理 / 已结束。
/// 两者维度不同，把观看塞进任务的状态切片会稀释「需要处理」的优先级（docs/design/activity.md）。
///
/// 一级切换挂在顶栏中间（同 Web 手机端挂进全局顶栏那一行），两边各带提示：
/// 观看是「此刻有人在播」的绿点；任务是数字——需要处理时红、只是进行中时蓝，与标签角标同源同数。
/// 数据来自外壳常驻的 `ShellBadges.tasks / media`，来回切换不打断轮询与 SSE。
struct ActivityView: View {
    var initialView: String?

    @Environment(ShellBadges.self) private var badges
    @State private var scope: Scope
    @State private var watchView: WatchSlice
    @State private var taskView: TaskSlice

    enum Scope: String { case media, tasks }

    init(initialView: String?) {
        self.initialView = initialView
        var raw = initialView
        #if DEBUG
        // 开发期截图直达：外壳切到活动标签时不会带查询参数，这里从 -mcRoute 里补读 view
        if raw == nil, let route = DebugLaunch.route, route.hasPrefix("/activity") || route.hasPrefix("/tasks") {
            raw = URLComponents(string: route)?.queryItems?.first { $0.name == "view" }?.value
        }
        #endif
        let task = raw.flatMap(TaskSlice.init(rawValue:))
        _scope = State(initialValue: task == nil ? .media : .tasks)
        _taskView = State(initialValue: task ?? .all)
        _watchView = State(initialValue: raw.flatMap(WatchSlice.init(rawValue:)) ?? .playing)
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
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                ActivityScopeSwitcher(
                    scope: $scope,
                    liveCount: badges.media.liveCount,
                    taskBadge: TaskCenter.badge(badges.tasks.activity)
                )
            }
        }
        .appBackground()
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                Image(systemName: "waveform.path.ecg").font(.title2).foregroundStyle(Theme.info)
                Text("活动").font(.system(size: 24, weight: .bold)).foregroundStyle(Theme.text)
            }
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

/// 一级视角切换胶囊：观看旁呼吸绿点（有人在播），任务旁数字（红=需要处理 / 蓝=进行中）
struct ActivityScopeSwitcher: View {
    @Binding var scope: ActivityView.Scope
    let liveCount: Int
    let taskBadge: TaskCenter.Badge

    var body: some View {
        HStack(spacing: 2) {
            segment(.media) {
                Text("观看")
                if liveCount > 0 {
                    ActivityStatusDot(color: Theme.success, pulse: true, size: 6, label: "有人正在观看")
                }
            }
            segment(.tasks) {
                Text("任务")
                if taskBadge.count > 0 {
                    Text("\(taskBadge.count)")
                        .font(.system(size: 11, weight: .semibold))
                        .monospacedDigit()
                        .foregroundStyle(taskBadge.alert ? Theme.danger : Theme.info)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background((taskBadge.alert ? Theme.danger : Theme.info).opacity(0.2), in: .capsule)
                        .accessibilityLabel(taskBadge.hint)
                        .accessibilityIdentifier("activity-task-badge")
                }
            }
        }
        .padding(3)
        .glassEffect(.regular, in: .capsule)
    }

    private func segment(_ value: ActivityView.Scope, @ViewBuilder label: () -> some View) -> some View {
        let selected = scope == value
        return Button {
            withAnimation(.snappy(duration: 0.2)) { scope = value }
        } label: {
            HStack(spacing: 6) { label() }
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(selected ? Theme.text : Theme.textMuted)
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .background(selected ? Color.white.opacity(0.15) : .clear, in: .capsule)
                .contentShape(.capsule)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("activity-scope-\(value.rawValue)")
        .accessibilityAddTraits(selected ? .isSelected : [])
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
