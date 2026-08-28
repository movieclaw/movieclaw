import CryptoKit
import Darwin
import Foundation

struct FFmpegInstallation: Sendable, Equatable {
    let version: String
    let ffmpegPath: String
}

/// Jellyfin-ffmpeg 只安装到当前用户的 App Support，和媒体目录、NAS 挂载点完全隔离。
enum FFmpegInstaller {
    private static let appDirectoryName = "MovieClawTranscoder"
    private static let ffmpegDirectoryName = "ffmpeg"
    private static let versionsDirectoryName = "versions"
    private static let stagingDirectoryName = ".staging"
    private static let maximumArchiveSize: Int64 = 500 * 1024 * 1024

    static func makeArchiveURL(for release: FFmpegRelease) throws -> URL {
        let directory = try ffmpegDirectory()
        return directory.appendingPathComponent(".download-\(UUID().uuidString).tar.xz")
    }

    static func install(
        release: FFmpegRelease,
        archiveURL: URL,
        previousVersion: String?
    ) throws -> FFmpegInstallation {
        let fileManager = FileManager.default
        let archiveSize = try fileSize(at: archiveURL)
        guard archiveSize > 0, archiveSize <= maximumArchiveSize else {
            throw FFmpegReleaseError.message("Jellyfin-ffmpeg 下载包大小异常")
        }
        guard archiveSize == release.expectedSize else {
            throw FFmpegReleaseError.message("Jellyfin-ffmpeg 下载包大小校验失败")
        }
        let actualDigest = try sha256(at: archiveURL)
        guard actualDigest == release.sha256.lowercased() else {
            throw FFmpegReleaseError.message("Jellyfin-ffmpeg SHA-256 校验失败，已拒绝安装")
        }

        let root = try ffmpegDirectory()
        let versionsDirectory = root.appendingPathComponent(versionsDirectoryName, isDirectory: true)
        try fileManager.createDirectory(at: versionsDirectory, withIntermediateDirectories: true)
        let stagingRoot = root.appendingPathComponent(stagingDirectoryName, isDirectory: true)
        defer { try? fileManager.removeItem(at: stagingRoot) }

        guard let versionDirectoryName = safeVersionDirectoryName(release.version) else {
            throw FFmpegReleaseError.message("Jellyfin-ffmpeg 版本号包含不安全字符")
        }
        let finalDirectory = versionsDirectory.appendingPathComponent(versionDirectoryName, isDirectory: true)
        if let existing = try existingInstallation(
            at: finalDirectory,
            version: release.version,
            digest: release.sha256
        ) {
            pruneVersions(in: versionsDirectory, keeping: [versionDirectoryName, safeVersionDirectoryName(previousVersion)])
            return existing
        }

        let stagingDirectory = stagingRoot.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try fileManager.createDirectory(at: stagingDirectory, withIntermediateDirectories: true)
        defer { try? fileManager.removeItem(at: stagingDirectory) }

        try validateArchive(archiveURL)
        try extract(archiveURL, to: stagingDirectory)
        try rejectSymbolicLinks(in: stagingDirectory)
        let stagedFFmpeg = try locateAndValidateFFmpeg(in: stagingDirectory)
        let prefix = stagingDirectory.path.hasSuffix("/") ? stagingDirectory.path : stagingDirectory.path + "/"
        guard stagedFFmpeg.path.hasPrefix(prefix) else {
            throw FFmpegReleaseError.message("Jellyfin-ffmpeg 安装路径越过了临时目录")
        }
        let relativePath = String(stagedFFmpeg.path.dropFirst(prefix.count))
        let marker = InstallMarker(
            version: release.version,
            assetName: release.assetName,
            sha256: release.sha256,
            relativeFFmpegPath: relativePath
        )
        try writeMarker(marker, in: stagingDirectory)

        if fileManager.fileExists(atPath: finalDirectory.path) {
            try fileManager.removeItem(at: finalDirectory)
        }
        try fileManager.moveItem(at: stagingDirectory, to: finalDirectory)
        let finalFFmpegPath = finalDirectory.appendingPathComponent(relativePath).path
        guard FileManager.default.isExecutableFile(atPath: finalFFmpegPath) else {
            try? fileManager.removeItem(at: finalDirectory)
            throw FFmpegReleaseError.message("安装后的 Jellyfin-ffmpeg 不可执行")
        }
        pruneVersions(in: versionsDirectory, keeping: [versionDirectoryName, safeVersionDirectoryName(previousVersion)])
        return FFmpegInstallation(version: release.version, ffmpegPath: finalFFmpegPath)
    }

    private static func ffmpegDirectory() throws -> URL {
        let fileManager = FileManager.default
        let applicationSupport = try fileManager.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let directory = applicationSupport
            .appendingPathComponent(appDirectoryName, isDirectory: true)
            .appendingPathComponent(ffmpegDirectoryName, isDirectory: true)
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        try? fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        return directory
    }

    private static func safeVersionDirectoryName(_ version: String?) -> String? {
        guard let version, !version.isEmpty,
              version.range(of: "^[A-Za-z0-9._-]{1,80}$", options: .regularExpression) != nil
        else { return nil }
        return version
    }

    private static func fileSize(at url: URL) throws -> Int64 {
        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        guard let size = attributes[.size] as? NSNumber else {
            throw FFmpegReleaseError.message("无法读取 Jellyfin-ffmpeg 下载包大小")
        }
        return size.int64Value
    }

    private static func sha256(at url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let chunk = try handle.read(upToCount: 1_048_576), !chunk.isEmpty {
            hasher.update(data: chunk)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    private static func validateArchive(_ archiveURL: URL) throws {
        let output = try runTar(arguments: ["-t", "-J", "-f", archiveURL.path])
        let entries = output.split(separator: "\n", omittingEmptySubsequences: true).map(String.init)
        guard !entries.isEmpty else {
            throw FFmpegReleaseError.message("Jellyfin-ffmpeg 压缩包为空")
        }
        for entry in entries {
            let components = entry.split(separator: "/", omittingEmptySubsequences: true)
            guard !entry.hasPrefix("/"),
                  !entry.contains("\0"),
                  !components.contains("..")
            else {
                throw FFmpegReleaseError.message("Jellyfin-ffmpeg 压缩包包含不安全路径")
            }
        }
    }

    private static func extract(_ archiveURL: URL, to directory: URL) throws {
        _ = try runTar(arguments: ["-x", "-J", "-f", archiveURL.path, "-C", directory.path])
    }

    private static func runTar(arguments: [String]) throws -> String {
        let tarPath = "/usr/bin/tar"
        guard FileManager.default.isExecutableFile(atPath: tarPath) else {
            throw FFmpegReleaseError.message("系统缺少 /usr/bin/tar，无法安装 Jellyfin-ffmpeg")
        }
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: tarPath)
        process.arguments = arguments
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        process.waitUntilExit()
        let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        guard process.terminationStatus == 0 else {
            throw FFmpegReleaseError.message("解压 Jellyfin-ffmpeg 失败：\(output.suffix(600))")
        }
        return output
    }

    private static func rejectSymbolicLinks(in directory: URL) throws {
        guard let enumerator = FileManager.default.enumerator(
            at: directory,
            includingPropertiesForKeys: nil,
            options: []
        ) else { return }
        for case let url as URL in enumerator {
            var info = stat()
            guard lstat(url.path, &info) == 0 else {
                throw FFmpegReleaseError.message("无法检查 Jellyfin-ffmpeg 安装文件")
            }
            if (info.st_mode & S_IFMT) == S_IFLNK {
                throw FFmpegReleaseError.message("Jellyfin-ffmpeg 压缩包包含不允许的符号链接")
            }
        }
    }

    private static func locateAndValidateFFmpeg(in directory: URL) throws -> URL {
        let preferredNames = ["jellyfin-ffmpeg", "ffmpeg7", "ffmpeg"]
        var candidates: [URL] = []
        guard let enumerator = FileManager.default.enumerator(
            at: directory,
            includingPropertiesForKeys: nil,
            options: []
        ) else {
            throw FFmpegReleaseError.message("无法读取 Jellyfin-ffmpeg 安装目录")
        }
        for case let url as URL in enumerator {
            let name = url.lastPathComponent.lowercased()
            guard preferredNames.contains(name),
                  let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
                  let type = attributes[.type] as? FileAttributeType,
                  type == .typeRegular
            else { continue }
            candidates.append(url)
        }
        candidates.sort { left, right in
            let leftName = left.lastPathComponent.lowercased()
            let rightName = right.lastPathComponent.lowercased()
            let leftRank = preferredNames.firstIndex(of: leftName) ?? preferredNames.count
            let rightRank = preferredNames.firstIndex(of: rightName) ?? preferredNames.count
            return leftRank == rightRank ? left.path.count < right.path.count : leftRank < rightRank
        }
        for candidate in candidates {
            try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: candidate.path)
            if (try? CapabilityProbe.run(ffmpegPath: candidate.path)) != nil {
                return candidate
            }
        }
        throw FFmpegReleaseError.message("安装后的 Jellyfin-ffmpeg 不包含可用的 h264_videotoolbox 能力")
    }

    private static func writeMarker(_ marker: InstallMarker, in directory: URL) throws {
        let data = try JSONEncoder().encode(marker)
        let markerURL = directory.appendingPathComponent(".movieclaw-install.json")
        try data.write(to: markerURL, options: [.atomic])
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: markerURL.path)
    }

    private static func existingInstallation(
        at directory: URL,
        version: String,
        digest: String
    ) throws -> FFmpegInstallation? {
        let markerURL = directory.appendingPathComponent(".movieclaw-install.json")
        guard let data = try? Data(contentsOf: markerURL),
              let marker = try? JSONDecoder().decode(InstallMarker.self, from: data),
              marker.version == version,
              marker.sha256 == digest,
              !marker.relativeFFmpegPath.isEmpty
        else { return nil }
        guard let pathURL = safeRelativeURL(marker.relativeFFmpegPath, under: directory),
              !isSymbolicLink(at: pathURL)
        else { return nil }
        let path = pathURL.path
        guard FileManager.default.isExecutableFile(atPath: path),
              (try? CapabilityProbe.run(ffmpegPath: path)) != nil
        else { return nil }
        return FFmpegInstallation(version: version, ffmpegPath: path)
    }

    private static func safeRelativeURL(_ relativePath: String, under directory: URL) -> URL? {
        let components = relativePath.split(separator: "/", omittingEmptySubsequences: true)
        guard !relativePath.hasPrefix("/"),
              !relativePath.contains("\0"),
              !components.contains(".."),
              !components.isEmpty
        else { return nil }
        let directoryPath = directory.standardizedFileURL.path.hasSuffix("/")
            ? directory.standardizedFileURL.path
            : directory.standardizedFileURL.path + "/"
        let candidate = directory.appendingPathComponent(relativePath).standardizedFileURL
        guard candidate.path.hasPrefix(directoryPath) else { return nil }
        return candidate
    }

    private static func isSymbolicLink(at url: URL) -> Bool {
        var info = stat()
        guard lstat(url.path, &info) == 0 else { return false }
        return (info.st_mode & S_IFMT) == S_IFLNK
    }

    private static func pruneVersions(in directory: URL, keeping names: [String?]) {
        let keep = Set(names.compactMap { $0 })
        guard let children = try? FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsHiddenFiles]
        ) else { return }
        for child in children where !keep.contains(child.lastPathComponent) {
            guard (try? child.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true else { continue }
            try? FileManager.default.removeItem(at: child)
        }
    }

    private struct InstallMarker: Codable {
        let version: String
        let assetName: String
        let sha256: String
        let relativeFFmpegPath: String
    }
}
