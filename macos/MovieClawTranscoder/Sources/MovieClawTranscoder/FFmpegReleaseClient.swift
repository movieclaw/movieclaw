import Foundation

struct FFmpegRelease: Sendable, Equatable {
    let version: String
    let assetName: String
    let downloadURL: URL
    let expectedSize: Int64
    let sha256: String
}

enum FFmpegReleaseError: Error, LocalizedError, Sendable {
    case message(String)

    var errorDescription: String? {
        switch self {
        case let .message(text): return text
        }
    }
}

/// 只接受 Jellyfin 官方 GitHub Release，避免把任意网络地址当作可执行文件来源。
struct FFmpegReleaseClient: Sendable {
    private static let latestURL = URL(string: "https://api.github.com/repos/jellyfin/jellyfin-ffmpeg/releases/latest")!
    private static let allowedHosts: Set<String> = [
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    ]
    private static let maxAssetSize: Int64 = 500 * 1024 * 1024

    func fetchLatest() async throws -> FFmpegRelease {
        var request = URLRequest(url: Self.latestURL)
        request.httpMethod = "GET"
        request.timeoutInterval = 30
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("2022-11-28", forHTTPHeaderField: "X-GitHub-Api-Version")
        request.setValue("MovieClawTranscoder/\(BuildInfo.version)", forHTTPHeaderField: "User-Agent")

        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 60
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }

        let (data, response) = try await session.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse,
              (200..<300).contains(httpResponse.statusCode),
              Self.isAllowedHost(httpResponse.url?.host, expected: "api.github.com")
        else {
            let status = (response as? HTTPURLResponse)?.statusCode ?? -1
            throw FFmpegReleaseError.message("无法读取 Jellyfin 官方版本信息（HTTP \(status)）")
        }

        let release: GitHubReleaseResponse
        do {
            release = try JSONDecoder().decode(GitHubReleaseResponse.self, from: data)
        } catch {
            throw FFmpegReleaseError.message("Jellyfin 官方版本信息格式无法识别")
        }
        guard !release.draft, !release.prerelease, !release.tagName.isEmpty else {
            throw FFmpegReleaseError.message("Jellyfin 官方最新版本不可用")
        }

        let candidates = release.assets.filter { asset in
            let name = asset.name.lowercased()
            return name.hasPrefix("jellyfin-ffmpeg_")
                && name.contains("_portable_macarm64-gpl.")
                && name.hasSuffix(".tar.xz")
        }
        guard candidates.count == 1, let asset = candidates.first else {
            throw FFmpegReleaseError.message("官方版本中没有唯一的 macOS arm64 Jellyfin-ffmpeg 资产")
        }
        guard asset.size > 0, asset.size <= Self.maxAssetSize else {
            throw FFmpegReleaseError.message("官方 Jellyfin-ffmpeg 下载包大小不在安全范围内")
        }
        guard let digest = normalizedDigest(asset.digest) else {
            throw FFmpegReleaseError.message("官方 Jellyfin-ffmpeg 缺少可验证的 SHA-256 摘要，已拒绝安装")
        }
        guard let downloadURL = URL(string: asset.browserDownloadURL),
              validateDownloadURL(downloadURL, assetName: asset.name)
        else {
            throw FFmpegReleaseError.message("官方 Jellyfin-ffmpeg 下载地址无效")
        }

        return FFmpegRelease(
            version: release.tagName,
            assetName: asset.name,
            downloadURL: downloadURL,
            expectedSize: asset.size,
            sha256: digest
        )
    }

    static func makeDownloadRequest(for release: FFmpegRelease) -> URLRequest {
        var request = URLRequest(url: release.downloadURL)
        request.httpMethod = "GET"
        request.timeoutInterval = 30
        request.setValue("application/octet-stream", forHTTPHeaderField: "Accept")
        request.setValue("MovieClawTranscoder/\(BuildInfo.version)", forHTTPHeaderField: "User-Agent")
        return request
    }

    static func isAllowedHost(_ host: String?, expected: String? = nil) -> Bool {
        guard let host = host?.lowercased(), allowedHosts.contains(host) else { return false }
        if let expected { return host == expected }
        return true
    }

    private func validateDownloadURL(_ url: URL, assetName: String) -> Bool {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.scheme == "https",
              components.user == nil,
              components.password == nil,
              components.fragment == nil,
              components.host?.lowercased() == "github.com",
              url.path.contains("/releases/download/"),
              url.lastPathComponent == assetName
        else {
            return false
        }
        return true
    }

    private func normalizedDigest(_ value: String?) -> String? {
        guard let value else { return nil }
        let prefix = "sha256:"
        guard value.lowercased().hasPrefix(prefix) else { return nil }
        let digest = String(value.dropFirst(prefix.count)).lowercased()
        guard digest.count == 64,
              digest.allSatisfy({ $0.isHexDigit })
        else { return nil }
        return digest
    }

    private struct GitHubReleaseResponse: Decodable {
        let tagName: String
        let draft: Bool
        let prerelease: Bool
        let assets: [GitHubAsset]

        enum CodingKeys: String, CodingKey {
            case tagName = "tag_name"
            case draft
            case prerelease
            case assets
        }
    }

    private struct GitHubAsset: Decodable {
        let name: String
        let size: Int64
        let digest: String?
        let browserDownloadURL: String

        enum CodingKeys: String, CodingKey {
            case name
            case size
            case digest
            case browserDownloadURL = "browser_download_url"
        }
    }
}

/// URLSession 下载代理只允许官方域名之间的 HTTPS 跳转，并把临时文件移到
/// App Support 后再返回；URLSession 提供的临时路径不会在回调结束后继续保留。
final class FFmpegDownloadDelegate: NSObject, URLSessionDownloadDelegate, URLSessionTaskDelegate, @unchecked Sendable {
    private let destination: URL
    private let onProgress: @Sendable (Int64, Int64) -> Void
    private let onCompletion: @Sendable (Result<URL, Error>) -> Void
    private let lock = NSLock()
    private var completed = false

    weak var session: URLSession?

    init(
        destination: URL,
        onProgress: @escaping @Sendable (Int64, Int64) -> Void,
        onCompletion: @escaping @Sendable (Result<URL, Error>) -> Void
    ) {
        self.destination = destination
        self.onProgress = onProgress
        self.onCompletion = onCompletion
    }

    func urlSession(
        _ session: URLSession,
        downloadTask: URLSessionDownloadTask,
        didWriteData bytesWritten: Int64,
        totalBytesWritten: Int64,
        totalBytesExpectedToWrite: Int64
    ) {
        onProgress(totalBytesWritten, totalBytesExpectedToWrite)
    }

    func urlSession(
        _ session: URLSession,
        downloadTask: URLSessionDownloadTask,
        didFinishDownloadingTo location: URL
    ) {
        do {
            let fileManager = FileManager.default
            try fileManager.createDirectory(
                at: destination.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try? fileManager.removeItem(at: destination)
            try fileManager.moveItem(at: location, to: destination)
            finish(.success(destination))
        } catch {
            finish(.failure(FFmpegReleaseError.message("保存 Jellyfin-ffmpeg 下载包失败：\(error.localizedDescription)")))
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error {
            finish(.failure(error))
        }
        session.finishTasksAndInvalidate()
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        guard let url = request.url,
              url.scheme == "https",
              FFmpegReleaseClient.isAllowedHost(url.host)
        else {
            completionHandler(nil)
            finish(.failure(FFmpegReleaseError.message("下载地址跳转到了不受信任的主机")))
            return
        }
        completionHandler(request)
    }

    private func finish(_ result: Result<URL, Error>) {
        lock.lock()
        guard !completed else {
            lock.unlock()
            return
        }
        completed = true
        lock.unlock()
        onCompletion(result)
    }
}
