import SwiftUI
import UIKit

extension PhotoWallView {
    /// 图片库的全屏灯箱（对应 Web `components/photo-lightbox.tsx`）。
    ///
    /// 舞台交互（缩放、翻页、缩略条、收放控件）全在 `LibraryZoomableImage.Lightbox` 里，
    /// 与图廊、章节图的灯箱共用；这里只负责图片库特有的三件事：
    /// - **三级地址**：墙上的缩略图 → 长边 2048 的屏幕适配图（`?size=screen`，服务端按原图
    ///   惰性派生并缓存）→ 只有放大后才拉几 MB 的原图；
    /// - **拍摄信息**面板：文件名、拍摄日期、尺寸、大小、格式、路径，按需从条目详情接口
    ///   （`GET /libraries/{lib}/items/{id}`）拉，同一张只拉一次；
    /// - **下载原图**：Web 在 iOS 桌面应用里先下载、再弹系统分享面板，失败在灯箱顶部提示原因；
    ///   原生同样先下（顶部「正在准备下载…」），成功后弹系统分享面板——用户「存储图像」到相册或存到文件，
    ///   不需要申请相册写入权限；失败写明原因（同 Web downloadOriginal）。
    struct Lightbox: View {
        let libraryId: Int
        let feed: Feed
        let fetch: Feed.Fetch
        @State var index: Int

        @Environment(\.api) private var api
        @State private var infoOpen = false
        @State private var details: [Int: API.LibraryItemDetailView] = [:]
        /// 下载原图的进度 / 失败提示（画面顶部）
        @State private var downloadNote: String?
        /// 下好的原图：弹系统分享面板
        @State private var shareFile: SharedOriginal?

        var body: some View {
            LibraryZoomableImage.Lightbox(
                slides: slides,
                index: $index,
                hasMore: feed.hasMore,
                onReachEnd: { Task { await feed.loadMore(fetch: fetch) } },
                note: downloadNote
            ) {
                if let item = current {
                    if let fileId = item.primaryFileId {
                        LibraryZoomableImage.ActionButton(systemImage: "square.and.arrow.down", label: "下载原图") {
                            Task { await download(fileId: fileId, item: item) }
                        }
                        .disabled(downloadNote == Self.preparing)
                    }
                    LibraryZoomableImage.ActionButton(
                        systemImage: infoOpen ? "info.circle.fill" : "info.circle",
                        label: "拍摄信息",
                        tint: infoOpen ? .white : .white.opacity(0.85)
                    ) {
                        infoOpen.toggle()
                    }
                }
            } overlay: {
                if infoOpen, let item = current {
                    infoPanel(item)
                }
            }
            .task(id: InfoRequest(open: infoOpen, itemId: current?.mediaItemId)) { await loadDetail() }
            .sheet(item: $shareFile) { file in
                OriginalShareSheet(url: file.url)
                    .presentationDetents([.medium, .large])
                    .ignoresSafeArea()
            }
        }

        private static let preparing = "正在准备下载…"

        /// 先把原图下到临时目录，成功再弹分享面板；失败在灯箱顶部写明原因，4 秒后收起
        private func download(fileId: Int, item: API.LibraryItemView) async {
            downloadNote = Self.preparing
            do {
                let url = try await OriginalPhoto.fetch(
                    url: api.url("/libraries/files/\(fileId)/original", query: [URLQueryItem(name: "download", value: "1")]),
                    fallbackName: fileName(of: item),
                    session: api.session
                )
                downloadNote = nil
                shareFile = SharedOriginal(url: url)
            } catch is CancellationError {
                downloadNote = nil
            } catch {
                let reason = error.localizedDescription
                downloadNote = "下载原图失败：\(reason.isEmpty ? "请检查网络后重试" : reason)"
                try? await Task.sleep(for: .seconds(4))
                if downloadNote?.hasPrefix("下载原图失败") == true { downloadNote = nil }
            }
        }

        private var current: API.LibraryItemView? {
            feed.items.indices.contains(index) ? feed.items[index] : nil
        }

        private var slides: [LibraryZoomableImage.Slide] {
            feed.items.enumerated().map { i, item in
                let original = item.primaryFileId.map { "/libraries/files/\($0)/original" }
                return LibraryZoomableImage.Slide(
                    id: i,
                    title: item.title,
                    thumbURL: api.image(item.posterUrl),
                    screenURL: original.map { api.url($0, query: [URLQueryItem(name: "size", value: "screen")]) },
                    fullURL: original.map { api.url($0) },
                    aspect: item.primaryAspect
                )
            }
        }

        /// 信息面板打开时按需拉条目详情（文件路径、大小、格式都在文件行上）；拉过的不再拉
        private func loadDetail() async {
            guard infoOpen, let item = current, details[item.mediaItemId] == nil else { return }
            if let detail = try? await api.libraryItemsGet(libraryId: libraryId, mediaItemId: item.mediaItemId) {
                details[item.mediaItemId] = detail
            }
        }

        private func primaryFile(of item: API.LibraryItemView) -> API.LibraryFileView? {
            guard let detail = details[item.mediaItemId] else { return nil }
            return detail.files.first { $0.id == item.primaryFileId } ?? detail.files.first
        }

        private func fileName(of item: API.LibraryItemView) -> String {
            primaryFile(of: item)?.fileName ?? item.title
        }

        /// 拍摄信息：照播放器诊断面板的做法，一块压在画面上的半透明黑，手机上横向铺满、限高可滚
        private func infoPanel(_ item: API.LibraryItemView) -> some View {
            let file = primaryFile(of: item)
            let rows: [(String, String)] = [
                ("文件名", file?.fileName ?? item.title),
                ("拍摄日期", item.releaseDate ?? "—"),
                ("尺寸", file?.resolution ?? item.resolutions.first ?? "—"),
                ("大小", Formatters.bytes(file?.sizeBytes ?? item.totalSizeBytes)),
                ("格式", file?.container.map { $0.uppercased() } ?? "—"),
                ("路径", file?.filePath ?? "—"),
            ]
            return VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("拍摄信息")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.white.opacity(0.9))
                    Spacer()
                    Button { infoOpen = false } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(.white.opacity(0.5))
                            .frame(width: 28, height: 28)
                            .contentShape(.circle)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("关闭拍摄信息")
                }
                ScrollView {
                    Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 10, verticalSpacing: 4) {
                        ForEach(rows, id: \.0) { label, value in
                            GridRow {
                                Text(label)
                                    .foregroundStyle(.white.opacity(0.5))
                                    .frame(width: 56, alignment: .leading)
                                Text(value)
                                    .foregroundStyle(.white.opacity(0.9))
                                    .monospacedDigit()
                                    .textSelection(.enabled)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }
                    }
                    .font(.system(size: 12))
                    if details[item.mediaItemId] == nil {
                        Text("正在读取文件信息…")
                            .font(.system(size: 12))
                            .foregroundStyle(.white.opacity(0.5))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.top, 6)
                    }
                }
                .frame(maxHeight: 260)
                .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(.black.opacity(0.7), in: .rect(cornerRadius: 14))
            .padding(.horizontal, 12)
        }
    }
}

private struct InfoRequest: Equatable {
    var open: Bool
    var itemId: Int?
}

/// 原图下载：用 App 的会话（带登录 Cookie）取 `?download=1`，文件名取响应头里的原文件名，
/// 落到临时目录交给分享面板。
private enum OriginalPhoto {
    static func fetch(url: URL, fallbackName: String, session: URLSession) async throws -> URL {
        let (data, response) = try await session.data(from: url)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        guard (200 ..< 300).contains(http.statusCode) else {
            throw NSError(domain: "MovieClaw", code: http.statusCode, userInfo: [NSLocalizedDescriptionKey: "服务器返回 HTTP \(http.statusCode)"])
        }
        let name = response.suggestedFilename ?? fallbackName
        let folder = FileManager.default.temporaryDirectory.appending(path: "movieclaw-originals", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let file = folder.appending(path: name)
        try? FileManager.default.removeItem(at: file)
        try data.write(to: file)
        return file
    }
}

private struct SharedOriginal: Identifiable {
    let id = UUID()
    let url: URL
}

/// 系统分享面板（存储图像 / 存到文件 / 隔空投送）
private struct OriginalShareSheet: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: [url], applicationActivities: nil)
    }

    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}
