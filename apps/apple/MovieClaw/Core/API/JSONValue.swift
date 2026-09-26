import Foundation

/// 生成模型的命名空间：`API.SessionView`、`API.LibraryView`……
/// 放在命名空间里是为了避免与 SwiftUI / Foundation 的同名类型（Image、Section、Group…）冲突。
nonisolated enum API {}

nonisolated extension API {
    /// 任意 JSON 值。生成器遇到无法静态表达的类型（多类型联合、任意对象）时落到这里，
    /// 使用方按需用 `stringValue` / `intValue` / 下标取值。
    enum JSONValue: Codable, Hashable, Sendable {
        case null
        case bool(Bool)
        case int(Int)
        case double(Double)
        case string(String)
        case array([JSONValue])
        case object([String: JSONValue])

        init(from decoder: Decoder) throws {
            let container = try decoder.singleValueContainer()
            if container.decodeNil() {
                self = .null
            } else if let value = try? container.decode(Bool.self) {
                self = .bool(value)
            } else if let value = try? container.decode(Int.self) {
                self = .int(value)
            } else if let value = try? container.decode(Double.self) {
                self = .double(value)
            } else if let value = try? container.decode(String.self) {
                self = .string(value)
            } else if let value = try? container.decode([JSONValue].self) {
                self = .array(value)
            } else {
                self = .object(try container.decode([String: JSONValue].self))
            }
        }

        func encode(to encoder: Encoder) throws {
            var container = encoder.singleValueContainer()
            switch self {
            case .null: try container.encodeNil()
            case let .bool(value): try container.encode(value)
            case let .int(value): try container.encode(value)
            case let .double(value): try container.encode(value)
            case let .string(value): try container.encode(value)
            case let .array(value): try container.encode(value)
            case let .object(value): try container.encode(value)
            }
        }

        var stringValue: String? {
            switch self {
            case let .string(value): value
            case let .int(value): String(value)
            case let .double(value): String(value)
            case let .bool(value): String(value)
            default: nil
            }
        }

        var intValue: Int? {
            switch self {
            case let .int(value): value
            case let .double(value): Int(value)
            case let .string(value): Int(value)
            default: nil
            }
        }

        var doubleValue: Double? {
            switch self {
            case let .int(value): Double(value)
            case let .double(value): value
            case let .string(value): Double(value)
            default: nil
            }
        }

        var boolValue: Bool? {
            if case let .bool(value) = self { return value }
            return nil
        }

        var arrayValue: [JSONValue]? {
            if case let .array(value) = self { return value }
            return nil
        }

        var objectValue: [String: JSONValue]? {
            if case let .object(value) = self { return value }
            return nil
        }

        var isNull: Bool { self == .null }

        subscript(key: String) -> JSONValue? { objectValue?[key] }
        subscript(index: Int) -> JSONValue? {
            guard let array = arrayValue, array.indices.contains(index) else { return nil }
            return array[index]
        }

        /// 把 JSONValue 重新解码成具体类型（用于联合类型按判别字段分派后再解码）
        func decode<T: Decodable>(as type: T.Type = T.self) throws -> T {
            let data = try JSONEncoder().encode(self)
            return try JSONDecoder().decode(T.self, from: data)
        }
    }
}
