import Foundation

/// 一条已结束任务的记录，状态面板的「今天」统计与「最近」列表都从这里来。
struct JobRecord: Codable, Equatable, Sendable {
    enum Outcome: String, Codable, Sendable {
        /// ffmpeg 正常转完。
        case finished
        /// NAS 叫停（播放器关了、换了片）或连接断开。远程转码是跟着播放走的，
        /// 大多数任务都以这种方式收尾，**不算失败**。
        case stopped
        case failed
    }

    let id: String
    var name: String?
    var outcome: Outcome
    var error: String?
    var endedAt: Date
    /// 累计转出的片长（毫秒）：转出了多长的片子，不是花了多久。
    var mediaMS: Int64
    /// 累计耗时（秒）。
    var elapsed: TimeInterval
    /// 结束时观众看到的片内位置（毫秒）。NAS 推过播放位置才有。
    var watchedMS: Int64? = nil
}

/// 最近任务的本地记录（UserDefaults，只存片名与统计数字，不含任何地址或令牌）。
///
/// ## 同一个 job id 合并成一条
///
/// 播放中拖动进度条，NAS 会先强停旧一轮、再用**同一个 job id** 起新一轮
/// （seek 重启）。按轮记录的话，看一集拖三次就是四条，「今天转了几个」也跟着
/// 虚高。所以同一个 id 在短时间内再次结束时，合并进已有那条：片长和耗时累加，
/// 结果与时间取最新一轮。
///
/// ## 线程
///
/// WorkerClient（actor）写，界面（主线程）读，用一把锁串起来；数据量很小
/// （最多几十条），每次写都整体落盘。
final class JobHistory: @unchecked Sendable {
    /// 最多保留的条数与天数。「最近」列表只看前几条，「今天」只看当天。
    private static let maxRecords = 50
    private static let maxAge: TimeInterval = 7 * 24 * 3_600
    /// 同一 job id 多久之内再次结束算同一个任务（seek 重启的间隔远小于它）。
    private static let mergeWindow: TimeInterval = 30 * 60
    /// v2：早先的记录把 ffmpeg 的微秒当毫秒存，片长大了 1000 倍，换个键丢掉重来。
    private static let key = "movieclaw.jobHistory.v2"
    private static let legacyKeys = ["movieclaw.jobHistory"]

    private let defaults: UserDefaults
    private let lock = NSLock()
    private var records: [JobRecord]

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        for key in Self.legacyKeys {
            defaults.removeObject(forKey: key)
        }
        if let data = defaults.data(forKey: Self.key),
           let decoded = try? JSONDecoder().decode([JobRecord].self, from: data) {
            records = decoded
        } else {
            records = []
        }
    }

    func record(_ record: JobRecord) {
        lock.lock()
        defer { lock.unlock() }
        if let index = records.firstIndex(where: {
            $0.id == record.id && record.endedAt.timeIntervalSince($0.endedAt) < Self.mergeWindow
        }) {
            var merged = records.remove(at: index)
            merged.name = record.name ?? merged.name
            merged.outcome = record.outcome
            merged.error = record.error
            merged.endedAt = record.endedAt
            merged.mediaMS += record.mediaMS
            merged.elapsed += record.elapsed
            merged.watchedMS = record.watchedMS ?? merged.watchedMS
            records.insert(merged, at: 0)
        } else {
            records.insert(record, at: 0)
        }
        let cutoff = record.endedAt.addingTimeInterval(-Self.maxAge)
        records = Array(records.filter { $0.endedAt >= cutoff }.prefix(Self.maxRecords))
        if let data = try? JSONEncoder().encode(records) {
            defaults.set(data, forKey: Self.key)
        }
    }

    /// 最近结束的几条，新的在前。
    func recent(limit: Int) -> [JobRecord] {
        lock.lock()
        defer { lock.unlock() }
        return Array(records.prefix(limit))
    }

    /// 当天（本地时区）的记录。
    func today(now: Date = Date(), calendar: Calendar = .current) -> [JobRecord] {
        lock.lock()
        defer { lock.unlock() }
        return records.filter { calendar.isDate($0.endedAt, inSameDayAs: now) }
    }
}
