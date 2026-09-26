import SwiftUI

/// 界面文案里的整数一律原样显示，不加千分位（R-4：Web 用模板字符串拼数字，「10481 个文件」）。
///
/// 为什么需要：`Text("共 \(count) 个")` 的字面量会被当成 `LocalizedStringKey`，整数插值走
/// 本地化格式化，按地区自动加分组符——同一个库头，网页写「10481 个文件」、App 却成了「10,481 个文件」。
/// 这里给 `Int` 提供一个更具体的插值重载（比系统的泛型重载优先匹配），把数字先转成字符串再插入，
/// 于是全 App 的 `Text` / `Button` / `Label` 文案默认与网页同口径。
///
/// 网页刻意分组的少数地方（发现页「找到 12,345 部」、站点「已缓存种子」、字幕 token 数等）
/// 在 App 里本就显式调用 `.formatted()` 或专门的格式化函数，产出的是字符串，不受这里影响。
extension LocalizedStringKey.StringInterpolation {
    mutating func appendInterpolation(_ value: Int) {
        appendInterpolation(String(value))
    }
}
