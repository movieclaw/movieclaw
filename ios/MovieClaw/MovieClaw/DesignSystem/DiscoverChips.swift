import SwiftUI

/// 流式换行布局：筛选 chips、类型标签、徽标组按行排满自动折行（对应 Web `flex-wrap`）。
struct DiscoverFlowLayout: Layout {
    var spacing: CGFloat = 8
    var lineSpacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = arrange(width: proposal.width ?? .infinity, subviews: subviews)
        let height = rows.reduce(0) { $0 + $1.height } + CGFloat(max(rows.count - 1, 0)) * lineSpacing
        let width = rows.map(\.width).max() ?? 0
        return CGSize(width: proposal.width ?? width, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let rows = arrange(width: bounds.width, subviews: subviews)
        var y = bounds.minY
        for row in rows {
            var x = bounds.minX
            for index in row.indices {
                let size = subviews[index].sizeThatFits(.unspecified)
                subviews[index].place(at: CGPoint(x: x, y: y + (row.height - size.height) / 2), proposal: ProposedViewSize(size))
                x += size.width + spacing
            }
            y += row.height + lineSpacing
        }
    }

    private struct Row {
        var indices: [Int] = []
        var width: CGFloat = 0
        var height: CGFloat = 0
    }

    private func arrange(width: CGFloat, subviews: Subviews) -> [Row] {
        var rows: [Row] = []
        var current = Row()
        for index in subviews.indices {
            let size = subviews[index].sizeThatFits(.unspecified)
            let needed = current.indices.isEmpty ? size.width : current.width + spacing + size.width
            if needed > width, !current.indices.isEmpty {
                rows.append(current)
                current = Row()
            }
            current.width = current.indices.isEmpty ? size.width : current.width + spacing + size.width
            current.height = max(current.height, size.height)
            current.indices.append(index)
        }
        if !current.indices.isEmpty { rows.append(current) }
        return rows
    }
}

/// 可切换的筛选胶囊（选中白底高亮），可带命中计数
struct DiscoverChip: View {
    let label: String
    var count: Int?
    var active: Bool
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                Text(label)
                if let count {
                    Text("\(count)").monospacedDigit().opacity(0.6)
                }
            }
            .font(.subheadline.weight(active ? .semibold : .regular))
            .foregroundStyle(active ? Theme.text : Theme.textMuted)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(active ? Color.white.opacity(0.16) : Color.white.opacity(0.04), in: .capsule)
            .overlay(Capsule().strokeBorder(active ? Color.white.opacity(0.22) : Color.white.opacity(0.08)))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(active ? .isSelected : [])
    }
}

/// 小号只读徽标（促销 / 属性 / 站点名）
struct DiscoverTag: View {
    let text: String
    var foreground: Color = Theme.textMuted
    var background: Color = Color.white.opacity(0.06)
    var weight: Font.Weight = .medium

    var body: some View {
        Text(text)
            .font(.system(size: 11, weight: weight))
            .monospacedDigit()
            .foregroundStyle(foreground)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(background, in: .rect(cornerRadius: 6))
    }
}

extension View {
    /// 强调按钮：液态玻璃强调底（App tint 是冷银浅色）+ 深色文字，避免白底白字
    func discoverProminentButton() -> some View {
        buttonStyle(.glassProminent).foregroundStyle(Color.black.opacity(0.85))
    }
}
