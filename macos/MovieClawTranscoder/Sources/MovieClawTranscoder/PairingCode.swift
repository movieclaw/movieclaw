import Foundation

/// NAS 网页生成的配对码。
///
/// 存在的理由：手工在两端抄一遍地址和高熵 Token 是这个功能最容易出错的一步——
/// 抄错一个字符的表现是「连不上」，而两边都看不出哪里不对。配对码把这两个值
/// 编码成一段可粘贴的文本，用户复制一次即可。
///
/// 格式：`movieclaw-worker-v1.<base64url(JSON)>`，JSON 为 `{"url": ..., "token": ...}`。
/// 用 base64url 而不是标准 base64，是因为 `+` `/` `=` 在聊天工具和终端里换行、
/// 转义的概率更高；带版本前缀则是为了将来改结构时能给出明确的升级提示，而不是
/// 解析失败后只说「配对码无效」。
///
/// 配对码里含明文 Token，等价于凭据本身，因此网页只在用户刚生成/输入 Token 的
/// 那一刻本地拼出它，服务端不提供任何回读接口。
struct PairingCode: Equatable {
    let nasURL: String
    let token: String

    static let prefix = "movieclaw-worker-v1."

    enum ParseError: Error, CustomStringConvertible, Equatable {
        case empty
        case badPrefix
        case badEncoding
        case missingFields

        var description: String {
            switch self {
            case .empty:
                return "配对码为空"
            case .badPrefix:
                return "这段文本不是 MovieClaw 配对码，请在 NAS 网页「远程转码」页面重新复制"
            case .badEncoding:
                return "配对码已损坏，可能复制时缺了一部分，请重新复制完整的一行"
            case .missingFields:
                return "配对码缺少地址或 Token，请在 NAS 网页重新生成"
            }
        }
    }

    /// 解析配对码。会先剔除粘贴时常见的空白与换行。
    static func parse(_ raw: String) throws -> PairingCode {
        // 从聊天工具或邮件里复制常常会带上换行和空格，逐字符过滤比 trim 更稳
        let compact = raw.filter { !$0.isWhitespace }
        guard !compact.isEmpty else { throw ParseError.empty }
        guard compact.hasPrefix(prefix) else { throw ParseError.badPrefix }

        let payload = String(compact.dropFirst(prefix.count))
        guard let data = decodeBase64URL(payload) else { throw ParseError.badEncoding }
        guard
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            throw ParseError.badEncoding
        }

        let url = (object["url"] as? String)?.trimmingCharacters(in: .whitespaces) ?? ""
        let token = (object["token"] as? String) ?? ""
        guard !url.isEmpty, !token.isEmpty else { throw ParseError.missingFields }
        return PairingCode(nasURL: url, token: token)
    }

    /// base64url → Data。Foundation 只认标准 base64，所以先换回字母表再补足填充。
    private static func decodeBase64URL(_ value: String) -> Data? {
        var normalized = value
            .replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        let remainder = normalized.count % 4
        if remainder > 0 {
            normalized += String(repeating: "=", count: 4 - remainder)
        }
        return Data(base64Encoded: normalized)
    }
}
