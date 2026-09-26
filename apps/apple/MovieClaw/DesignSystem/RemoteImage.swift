import Nuke
import NukeUI
import SwiftUI

/// 图片尺寸预设（同 Web `lib/image-proxy.ts` 的 ImageVariant）：后端按预设生成派生图并缓存，
/// 列表里用小图省流量，全屏场景不带预设取原图。
nonisolated enum ImageVariant: String {
    case landscapeCard = "landscape-card"
    case posterCard = "poster-card"
    case photoTile = "photo-tile"
    case galleryTile = "gallery-tile"
    case photoScreen = "photo-screen"

    /// 海报墙按主图比例挑预设：横图取横卡，竖图取海报卡
    static func card(aspect: Double?) -> ImageVariant {
        (aspect ?? 0.66) >= 1 ? .landscapeCard : .posterCard
    }
}

nonisolated extension ServerAddress {
    /// 后端给出的图片地址 → 可请求的 URL（同 Web `imageUrl()`）：
    /// - http(s) 远程图（TMDB、豆瓣、PT 站截图）一律走后端缓存代理 `/images/proxy`；
    /// - 相对路径（`/images/assets/...`、`/libraries/...`）补上 `/api/v1` 直连；
    /// - Windows 刮削器写入的反斜杠统一换成 `/`。
    func imageURL(_ raw: String?, variant: ImageVariant? = nil) -> URL? {
        guard let raw, !raw.isEmpty else { return nil }
        var components: URLComponents
        if raw.hasPrefix("http://") || raw.hasPrefix("https://") {
            guard var c = URLComponents(url: apiBase.appending(path: "images/proxy"), resolvingAgainstBaseURL: false) else { return nil }
            c.queryItems = [URLQueryItem(name: "url", value: raw)]
            if let variant { c.queryItems?.append(URLQueryItem(name: "variant", value: variant.rawValue)) }
            components = c
        } else {
            var path = raw.replacingOccurrences(of: "\\", with: "/")
            if path.hasPrefix("/api/v1/") { path = String(path.dropFirst("/api/v1".count)) }
            guard let url = URL(string: apiBase.absoluteString + (path.hasPrefix("/") ? path : "/" + path)),
                  var c = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return nil }
            // 只有刮削资产路由支持派生预设
            if let variant, path.hasPrefix("/images/assets/") {
                c.queryItems = (c.queryItems ?? []) + [URLQueryItem(name: "variant", value: variant.rawValue)]
            }
            components = c
        }
        return components.url
    }

    /// TMDB 图升级到 original 档（发现页 Hero、详情页沉浸背景用；非 TMDB 图原样）
    func originalTMDBImageURL(_ raw: String?) -> URL? {
        guard let raw else { return nil }
        let upgraded = raw.replacingOccurrences(of: #"/t/p/w\d+/"#, with: "/t/p/original/", options: .regularExpression)
        return imageURL(upgraded)
    }
}

/// 统一的远程图片视图：带占位底色、渐显、失败兜底图标。
/// 会话 Cookie 由共享 Cookie 存储自动携带（后端图片接口需要登录）。
struct RemoteImage: View {
    let url: URL?
    var contentMode: ContentMode = .fill
    /// 失败或无图时的兜底图标
    var placeholderSymbol: String = "film"
    /// 失败或无图时改显示这句文字（如「暂无封面」）；nil = 显示图标
    var placeholderText: String? = nil

    var body: some View {
        LazyImage(url: url, transaction: Transaction(animation: .easeOut(duration: 0.2))) { state in
            if let image = state.image {
                image.resizable().aspectRatio(contentMode: contentMode)
            } else if state.error != nil || url == nil {
                ZStack {
                    Theme.surfaceRaised
                    if let placeholderText {
                        Text(placeholderText)
                            .font(.caption)
                            .foregroundStyle(Theme.textFaint)
                    } else {
                        Image(systemName: placeholderSymbol)
                            .font(.title2)
                            .foregroundStyle(Theme.textFaint)
                    }
                }
            } else {
                Theme.surfaceRaised
            }
        }
    }
}

/// App 启动时配置 Nuke：300MB 磁盘缓存 + 与 APIClient 同一套 Cookie
enum ImagePipelineSetup {
    static func configure() {
        var configuration = ImagePipeline.Configuration.withDataCache(name: "io.movieclaw.images", sizeLimit: 300 * 1024 * 1024)
        let urlConfig = DataLoader.defaultConfiguration
        urlConfig.httpCookieStorage = .shared
        urlConfig.httpShouldSetCookies = true
        configuration.dataLoader = DataLoader(configuration: urlConfig)
        ImagePipeline.shared = ImagePipeline(configuration: configuration)
    }
}
