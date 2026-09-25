import Foundation

/// 订阅模块手写的接口补充。
///
/// 生成的 `SubscriptionUpdatePayload` 用合成的 Codable 编码，可选字段为 nil 时直接省略——
/// 但「调整订阅」要能显式把 `library_id` 置成 null（清除指定库、改回按默认库路由），
/// 后端区分「未传」与「传 null」。这里手写一个只在需要时编码 null 的载荷。
nonisolated struct SubscriptionAdjustPayload: Encodable, Sendable {
    var selectedSeasons: [Int]?
    /// 外层 nil = 不改库；内层 nil = 显式清除（按默认库路由）
    var libraryId: Int??

    enum CodingKeys: String, CodingKey {
        case selectedSeasons = "selected_seasons"
        case libraryId = "library_id"
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encodeIfPresent(selectedSeasons, forKey: .selectedSeasons)
        if let libraryId {
            if let value = libraryId {
                try container.encode(value, forKey: .libraryId)
            } else {
                try container.encodeNil(forKey: .libraryId)
            }
        }
    }
}

nonisolated extension APIClient {
    /// `PATCH /subscriptions/{id}`：调整季选择与入库库（支持显式清除指定库）
    func subscriptionsAdjust(subscriptionId: Int, body: SubscriptionAdjustPayload) async throws -> API.SubscriptionDetailView {
        try await send("PATCH", "/subscriptions/\(subscriptionId)", body: body)
    }
}
