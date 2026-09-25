import SwiftUI

// 占位：媒体库表单（新建向导 / 编辑库，Web LibraryFormDialog）由下一批「媒体库管理」实现。
// 签名固定：libraryId 为 nil 表示新建，否则编辑该库。以 .sheet 呈现。
struct LibraryFormSheet: View {
    var libraryId: Int?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            PlaceholderPage(title: libraryId == nil ? "新建媒体库" : "编辑库")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("关闭") { dismiss() } }
                }
        }
    }
}
