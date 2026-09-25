import SwiftUI

private struct APIClientKey: EnvironmentKey {
    /// 未登录前的占位（不会真的被调用）：避免页面里到处处理可选值
    static let defaultValue = APIClient(server: ServerAddress(origin: URL(string: "http://localhost")!))
}

extension EnvironmentValues {
    /// 当前服务器的接口客户端；由 MainTabView 注入。页面里：
    /// `@Environment(\.api) private var api` → `try await api.librariesList()`
    var api: APIClient {
        get { self[APIClientKey.self] }
        set { self[APIClientKey.self] = newValue }
    }
}

extension APIClient {
    /// 图片地址解析的便捷入口：`api.image(item.posterUrl, .posterCard)`
    nonisolated func image(_ raw: String?, _ variant: ImageVariant? = nil) -> URL? {
        server.imageURL(raw, variant: variant)
    }
}
