import SwiftUI

/// 尚未实现的页面占位（开发期使用，交付前应全部被替换）
struct PlaceholderPage: View {
    var title: String?

    var body: some View {
        EmptyState(systemImage: "hammer", title: "开发中", message: title.map { "「\($0)」页面正在开发" })
            .navigationTitle(title ?? "")
            .appBackground()
    }
}
