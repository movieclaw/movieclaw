import Foundation

enum BuildInfo {
    static let version = (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "0.1.0-dev"
    static let protocolVersion = 1
}
