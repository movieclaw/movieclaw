import UIKit

/// 发现模块手写的接口（生成器跳过的 multipart 上传）。
nonisolated extension APIClient {
    /// 把一张远程剧照设为当前账号的背景（Web 详情页灯箱的「设为背景」）：
    /// 取原图 → 长边压到 2560px、JPEG 0.9（同 Web `fileToCompressedJpeg`）→ `POST /appearance/backdrops`。
    ///
    /// 在客户端压缩的原因同 Web：背景铺满屏幕不需要 4K 原图，压缩后上传快、服务器存得少。
    func uploadBackdrop(fromRemote raw: String) async throws -> API.AppearanceView {
        guard let url = image(raw) else { throw APIError.network("剧照地址无效") }
        let data: Data
        do {
            let (body, response) = try await session.data(from: url)
            guard let http = response as? HTTPURLResponse, (200 ..< 300).contains(http.statusCode) else {
                throw APIError.network("下载剧照原图失败，请检查网络后重试")
            }
            data = body
        } catch let error as APIError {
            throw error
        } catch {
            throw APIError.network("下载剧照原图失败，请检查网络后重试")
        }
        guard let jpeg = Self.compressedJPEG(data, maxEdge: 2560) else {
            throw APIError.network("图片编码失败，请重试")
        }
        return try await upload(
            "/appearance/backdrops",
            file: (name: "file", filename: "backdrop.jpg", mimeType: "image/jpeg", data: jpeg),
            as: API.AppearanceView.self
        )
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
