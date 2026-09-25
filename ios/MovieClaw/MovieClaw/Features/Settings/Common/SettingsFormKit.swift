import SwiftUI
import UIKit

// 设置分区共用的小组件（概览 / 个人信息 / 外观 / 成员 / 设备 / 播放 / AI 设定 / 更新与维护 / 网络 / 日志）。
//
// 设计取向：设置页一律用系统分组列表（insetGrouped List）承载——它自带液态玻璃分组底、
// 行分隔与键盘避让，信息与操作逐条对应 Web 的 `css-glass` 字段组；这里只补 Web 有而系统
// 列表没有的几样东西：提示条、ⓘ 说明气泡、复制按钮、状态点、胶囊页签。
// 类型名统一带 `Settings` 前缀，避免与其它模块的同名小组件冲突。

// MARK: - 提示条

/// 行内提示条（对应 Web 各分区的红色错误条 / 琥珀色提醒 / 绿色完成提示）。
struct SettingsNotice: View {
    enum Tone { case error, warn, ok, info }
    let text: String
    var tone: Tone = .error

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon).font(.subheadline).padding(.top, 1)
            Text(text).font(.subheadline).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .foregroundStyle(color)
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(color.opacity(0.10), in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(color.opacity(0.28)))
    }

    private var icon: String {
        switch tone {
        case .error: "exclamationmark.triangle.fill"
        case .warn: "exclamationmark.circle.fill"
        case .ok: "checkmark.circle.fill"
        case .info: "info.circle.fill"
        }
    }

    var color: Color {
        switch tone {
        case .error: Theme.danger
        case .warn: Theme.warning
        case .ok: Theme.success
        case .info: Theme.info
        }
    }
}

// MARK: - ⓘ 说明气泡

/// 字段旁的 ⓘ：点按弹出说明（Web 的 Tooltip openOnClick）。
/// iPhone 上 popover 默认会变成全屏 sheet，这里强制保持气泡形态，读完点空白即关。
struct SettingsHelpTip: View {
    let text: String
    var label: String = "说明"
    @State private var shown = false

    var body: some View {
        Button { shown = true } label: {
            Image(systemName: "info.circle")
                .font(.footnote)
                .foregroundStyle(Theme.textFaint)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .popover(isPresented: $shown) {
            Text(text)
                .font(.footnote)
                .foregroundStyle(Theme.text)
                .fixedSize(horizontal: false, vertical: true)
                .padding(14)
                .frame(idealWidth: 300, maxWidth: 320)
                .presentationCompactAdaptation(.popover)
        }
    }
}

/// 字段名 + ⓘ（Web LabelWithHelp）
struct SettingsLabelWithHelp: View {
    let label: String
    let help: String

    var body: some View {
        HStack(spacing: 6) {
            Text(label)
            SettingsHelpTip(text: help, label: "\(label)的说明")
        }
    }
}

// MARK: - 复制

enum SettingsClipboard {
    /// 复制文本并给出轻提示（同 Web copyText + toast）
    @MainActor
    static func copy(_ text: String, feedback: Feedback, message: String) {
        UIPasteboard.general.string = text
        feedback.success(message)
    }
}

/// 复制按钮：点按复制并提示，1.8 秒内显示「已复制」（Web CopyButton / CopySurface）。
struct SettingsCopyButton: View {
    let text: String
    let title: String
    var successMessage: String = "已复制"
    @Environment(Feedback.self) private var feedback
    @State private var copied = false

    var body: some View {
        Button {
            SettingsClipboard.copy(text, feedback: feedback, message: successMessage)
            copied = true
            Task {
                try? await Task.sleep(for: .seconds(1.8))
                copied = false
            }
        } label: {
            Label(copied ? "已复制" : title, systemImage: copied ? "checkmark" : "doc.on.doc")
        }
    }
}

// MARK: - 状态点

/// 小圆点（在线 / 正常 / 降级 / 有问题），可带外发光
struct SettingsStatusDot: View {
    let color: Color
    var size: CGFloat = 8
    var glow = false

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: size, height: size)
            .shadow(color: glow ? color.opacity(0.6) : .clear, radius: glow ? 4 : 0)
    }
}

// MARK: - 胶囊页签

/// 分区内的胶囊页签（Web 外观 / 更新与维护分区顶部那排 pill），可给某个页签点一颗提示蓝点。
struct SettingsPillTabs<Tab: Hashable>: View {
    let tabs: [(id: Tab, label: String)]
    @Binding var selection: Tab
    var dotted: Set<Tab> = []
    var identifierPrefix = "settings-tab"

    var body: some View {
        HStack(spacing: 6) {
            ForEach(tabs, id: \.id) { tab in
                let active = tab.id == selection
                Button {
                    withAnimation(.snappy(duration: 0.2)) { selection = tab.id }
                } label: {
                    HStack(spacing: 5) {
                        Text(tab.label)
                        if dotted.contains(tab.id) {
                            SettingsStatusDot(color: Theme.info, size: 6)
                        }
                    }
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(active ? Theme.text : Theme.textMuted)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 7)
                    .background(active ? Color.white.opacity(0.14) : .clear, in: .capsule)
                    .contentShape(.capsule)
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(active ? .isSelected : [])
                .accessibilityIdentifier("\(identifierPrefix)-\(tab.label)")
            }
            Spacer(minLength: 0)
        }
    }
}

// MARK: - 行

/// 标题 + 说明的两行文字（Web 各分区「text-body + text-caption」的行头）
struct SettingsRowText: View {
    let title: String
    var detail: String?
    var detailColor: Color = Theme.textFaint

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).foregroundStyle(Theme.text)
            if let detail, !detail.isEmpty {
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(detailColor)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// 列表里的「加载中…」一行（分区内某一块数据还没回来时）
struct SettingsLoadingRow: View {
    var text = "加载中…"

    var body: some View {
        HStack(spacing: 10) {
            ProgressView()
            Text(text).font(.subheadline).foregroundStyle(Theme.textMuted)
        }
    }
}

// MARK: - 时间口径

enum SettingsTime {
    /// 设备分区的相对时间口径（Web lib/devices-display.ts relativeTime）：分钟粒度、宁粗勿假
    static func deviceRelative(_ raw: String?, now: Date = .now) -> String {
        guard let raw else { return "从未使用" }
        guard let date = Formatters.date(raw) else { return "未知" }
        let minutes = Int(now.timeIntervalSince(date) / 60)
        if minutes < 1 { return "刚刚活跃" }
        if minutes < 60 { return "\(minutes) 分钟前" }
        let hours = minutes / 60
        if hours < 24 { return "\(hours) 小时前" }
        return "\(hours / 24) 天前"
    }

    /// 最近 5 分钟内用过算在线（Web isLive）
    static func isLive(_ raw: String?, now: Date = .now) -> Bool {
        guard let date = Formatters.date(raw) else { return false }
        return now.timeIntervalSince(date) < 5 * 60
    }

    /// Unix 秒 → 「2026-09-25 17:03」
    static func unix(_ seconds: Int) -> String {
        Date(timeIntervalSince1970: TimeInterval(seconds))
            .formatted(.dateTime.year().month(.twoDigits).day(.twoDigits).hour().minute().locale(Locale(identifier: "zh_CN")))
    }
}

// MARK: - 界面偏好

extension API.UiPreferencesSetting {
    /// 整体覆盖式保存用的请求体：原样带回全部分组（主题、侧栏、蒙版、导航、首页行），
    /// 只改调用方关心的字段——漏带任何一组都会被后端当成「清空」覆盖掉。
    var asInput: API.UiPreferencesSettingInput {
        API.UiPreferencesSettingInput(
            theme: theme,
            themeDesktop: themeDesktop,
            themeMobile: themeMobile,
            sidebar: .init(transparency: sidebar.transparency, brightness: sidebar.brightness, depth: sidebar.depth),
            scrim: .init(blur: scrim.blur, dark: scrim.dark),
            nav: .init(order: nav.order),
            home: .init(rows: home.rows.map {
                .init(id: $0.id, sort: $0.sort, order: $0.order, name: $0.name, unwatched: $0.unwatched,
                      hidden: $0.hidden, libraryId: $0.libraryId, collectionId: $0.collectionId)
            })
        )
    }
}

// MARK: - 播放引擎偏好（本机）

/// App 专属的「播放引擎」偏好。键名、取值与播放器模块 `PlayerPreferences.engine` 完全一致
/// （`movieclaw.player.engine` = auto / system / mpv），播放器起播时读它；这里只负责展示与修改。
enum SettingsPlaybackEngine: String, CaseIterable, Identifiable {
    case auto, system, mpv

    static let storageKey = "movieclaw.player.engine"

    var id: String { rawValue }

    var label: String {
        switch self {
        case .auto: "自动"
        case .system: "系统播放器"
        case .mpv: "MPV"
        }
    }

    var hint: String {
        switch self {
        case .auto: "能直出用系统播放器，其余交给 MPV"
        case .system: "支持画中画、隔空播放、杜比视界"
        case .mpv: "本机解码 MKV/DTS/TrueHD，特效字幕原样渲染"
        }
    }
}
