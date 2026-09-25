import SwiftUI

// 占位：由对应模块实现（见 docs/design/ios-app.md「模块分工」）。
struct LibraryItemDetailView: View {
    let libraryId: Int
    let itemId: Int
    var season: Int?
    var episode: Int?

    var body: some View {
        PlaceholderPage(title: "详情")
    }
}
