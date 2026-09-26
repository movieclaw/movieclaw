import UIKit

/// 发现模块手写的接口（生成器跳过的 multipart 上传、不可达提示）。
nonisolated extension APIClient {
    /// 上游（TMDB / 豆瓣）不可达时，取后端给的下一步提示（错误体 `details[0].hint`，
    /// 如「到「设置 → 网络」为 TMDB 配置代理或镜像地址…」），对应 Web `toErrorInfo`。
    ///
    /// 为什么要补发一次：通用错误类型 `APIError` 只带 message/code，错误体的 details 在
    /// Core/Networking 里就丢了（不归发现模块改）。这里只在已判定 UPSTREAM_UNREACHABLE 的
    /// 失败路径上，对同一地址再 GET 一次、只解析错误体；后端对不可达上游有熔断，补发通常立刻返回。
    /// 拿不到（这次又成功了、或响应不是错误体）就返回 nil，错误态照常只显示原因。
    func discoverUnreachableHint(path: String, query: [URLQueryItem] = []) async -> String? {
        struct ErrorBody: Decodable {
            struct Detail: Decodable { let hint: String? }
            let details: [Detail]?
        }
        var request = URLRequest(url: url(path, query: query))
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        guard let (data, response) = try? await session.data(for: request),
              let http = response as? HTTPURLResponse, !(200 ..< 300).contains(http.statusCode)
        else { return nil }
        let hint = (try? Self.decoder.decode(ErrorBody.self, from: data))?.details?.first?.hint
        return hint?.isEmpty == false ? hint : nil
    }

    /// 长边限制到 maxEdge 并编码为 JPEG（质量 0.9）；小图不放大
    static func compressedJPEG(_ data: Data, maxEdge: CGFloat) -> Data? {
        guard let image = UIImage(data: data) else { return nil }
        let size = image.size
        let scale = min(1, maxEdge / max(size.width, size.height))
        let target = CGSize(width: (size.width * scale).rounded(), height: (size.height * scale).rounded())
        let format = UIGraphicsImageRendererFormat.default()
        format.scale = 1
        format.opaque = true
        let rendered = UIGraphicsImageRenderer(size: target, format: format).image { _ in
            image.draw(in: CGRect(origin: .zero, size: target))
        }
        return rendered.jpegData(compressionQuality: 0.9)
    }
}
