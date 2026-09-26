import SwiftUI

/// 扩大按钮的点击区而不改布局与外观。
///
/// 背景：`.buttonStyle(.plain)` 的按钮只有**画出来的像素**（加上显式声明的 `.contentShape`）响应点击，
/// 透明的内边距、`.clear` 背景、`frame` 撑出来的空白都点不中。图标键、细文字链接在真机上因此时灵时不灵
/// （播放器「⋯」只剩三个小圆点可点，真机反馈）。苹果建议可点区域不小于 44×44pt。
///
/// 做法：先向外扩一圈 → 声明矩形命中形状 → 再收回同样的距离。布局尺寸与视觉完全不变，
/// 只有命中区变大（SwiftUI 的命中测试不受父视图边界裁剪）。挨得很近的一排小键只扩纵向，
/// 免得相邻两个键的命中区互相压住。
extension View {
    func expandedHitArea(horizontal: CGFloat = 0, vertical: CGFloat = 0) -> some View {
        padding(.horizontal, horizontal)
            .padding(.vertical, vertical)
            .contentShape(.rect)
            .padding(.horizontal, -horizontal)
            .padding(.vertical, -vertical)
    }
}
