import Darwin
import Foundation

/// 转码的 CPU 与内存占用采样器，给状态面板里那两张小图表用（iStat Menus 那种）。
///
/// 「转码」= ffmpeg 进程，**不含 App 自己**：没在转的时候就该是 0。ffmpeg 是转码内核
/// 进程（``CoreRunner``）起的，也就是本 App 的孙进程。App 自身 = 界面进程 + 转码内核进程
/// （界面、采样、心跳、上传代理里暂存的分片），单独记，只在悬停提示里说一句。
/// 另外采整机的 CPU 与已用内存作对照：用户一眼看出 Mac 忙是转码忙，还是别的程序在忙。
///
/// 口径：
/// - **CPU 按整台 Mac 算百分比**（8 核合计 100%），不是「活动监视器」那种每核 100%——
///   对不懂的人，「占整机 18%」比「145%」好懂得多。用各进程累计 CPU 时间的差值 ÷ 经过的
///   时间 ÷ 核数。Apple 芯片的 `ri_user_time` 等是 mach 时间单位，要按 timebase 换算成纳秒。
/// - **内存用 phys_footprint**，与「活动监视器」的「内存」一栏同口径（RSS 会把共享库算进来，偏大）。
/// - 整机 CPU 取 `host_statistics` 的 tick 差值；整机已用内存 = App 内存 + 联动 + 压缩，
///   与「活动监视器」的「已使用内存」同口径。
/// - VideoToolbox 硬件编码在 Apple 芯片的媒体引擎上跑，**不计入 CPU**——CPU 数字主要是
///   解码、缩放、色调映射这些。
///
/// 每 2 秒采一次，留最近 2 分钟；几次系统调用，开销可以忽略，所以面板收着时也一直采，
/// 一打开就有曲线。
@MainActor
final class ResourceMonitor {
    struct Sample: Equatable {
        /// 转码（ffmpeg）占整机 CPU 的比例（0…1）。
        var transcoderCPU: Double
        /// 整机 CPU 使用率（0…1）。
        var systemCPU: Double
        /// 转码（ffmpeg）占用的内存（字节）。
        var transcoderMemory: UInt64
        /// 整机已用内存（字节）。
        var systemMemory: UInt64
        /// App 自身占整机 CPU 的比例、占用的内存。
        var appCPU: Double = 0
        var appMemory: UInt64 = 0
    }

    static let interval: TimeInterval = 2
    static let capacity = 60

    /// 采到新数据时回调（面板开着就重画）。
    var onSample: (() -> Void)?
    private(set) var samples: [Sample] = []
    let physicalMemory = ProcessInfo.processInfo.physicalMemory
    let cores = max(1, ProcessInfo.processInfo.activeProcessorCount)

    private var timer: Timer?
    /// 上一次各进程的累计 CPU 时间（纳秒）。新出现的子进程没有记录，按「刚启动」
    /// 把它的全部 CPU 时间算进这一格——ffmpeg 都是采样间隔内新起的。
    private var lastProcessCPU: [pid_t: UInt64] = [:]
    private var lastSystemTicks: (busy: UInt64, total: UInt64)?
    private var lastWall: UInt64 = 0
    private let timebase: mach_timebase_info_data_t = {
        var info = mach_timebase_info_data_t()
        mach_timebase_info(&info)
        return info
    }()

    func start() {
        guard timer == nil else { return }
        takeBaseline()
        let timer = Timer(timeInterval: Self.interval, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.sample() }
        }
        // common 模式：「更多」菜单弹着的时候也照常采样
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    private func takeBaseline() {
        let processes = trackedProcesses()
        lastProcessCPU = Dictionary(uniqueKeysWithValues: (processes.app + processes.transcoders).compactMap { pid in
            usage(of: pid).map { (pid, $0.cpuNanos) }
        })
        lastSystemTicks = Self.systemTicks()
        lastWall = DispatchTime.now().uptimeNanoseconds
    }

    private func sample() {
        let wall = DispatchTime.now().uptimeNanoseconds
        let elapsed = Double(wall - lastWall)
        guard elapsed > 0 else { return }

        var cpuNanos: UInt64 = 0, memory: UInt64 = 0
        var appNanos: UInt64 = 0, appMemory: UInt64 = 0
        var current: [pid_t: UInt64] = [:]
        let processes = trackedProcesses()
        for pid in processes.app + processes.transcoders {
            guard let usage = usage(of: pid) else { continue }
            current[pid] = usage.cpuNanos
            let previous = lastProcessCPU[pid] ?? 0
            let delta = usage.cpuNanos >= previous ? usage.cpuNanos - previous : 0
            if processes.app.contains(pid) {
                appNanos += delta
                appMemory += usage.footprint
            } else {
                cpuNanos += delta
                memory += usage.footprint
            }
        }

        var systemCPU = 0.0
        let ticks = Self.systemTicks()
        if let last = lastSystemTicks, ticks.total > last.total {
            systemCPU = Double(ticks.busy - last.busy) / Double(ticks.total - last.total)
        }

        lastProcessCPU = current
        lastSystemTicks = ticks
        lastWall = wall
        samples.append(Sample(
            transcoderCPU: min(1, Double(cpuNanos) / elapsed / Double(cores)),
            systemCPU: min(1, max(0, systemCPU)),
            transcoderMemory: memory,
            systemMemory: Self.systemMemoryUsed(),
            appCPU: min(1, Double(appNanos) / elapsed / Double(cores)),
            appMemory: appMemory
        ))
        if samples.count > Self.capacity {
            samples.removeFirst(samples.count - Self.capacity)
        }
        onSample?()
    }

    // MARK: - 系统调用

    /// App 自身（本进程 + 转码内核）与转码（ffmpeg）各是哪些进程。
    ///
    /// 本进程的子进程里，可执行文件和自己相同的是转码内核，其余（界面这边的 ffmpeg 能力探测）
    /// 算转码；转码内核的子进程全是 ffmpeg。
    private func trackedProcesses() -> (app: [pid_t], transcoders: [pid_t]) {
        let me = getpid()
        var app = [me]
        var transcoders: [pid_t] = []
        let myPath = Self.executablePath(of: me)
        for child in Self.children(of: me) {
            if myPath != nil, Self.executablePath(of: child) == myPath {
                app.append(child)
                transcoders += Self.children(of: child)
            } else {
                transcoders.append(child)
            }
        }
        return (app, transcoders)
    }

    private static func children(of pid: pid_t) -> [pid_t] {
        var buffer = [pid_t](repeating: 0, count: 64)
        // 返回的是子进程个数（本机实测），不是字节数
        let count = buffer.withUnsafeMutableBytes {
            proc_listchildpids(pid, $0.baseAddress, Int32($0.count))
        }
        return Array(buffer.prefix(Int(max(0, min(count, 64)))))
    }

    private static func executablePath(of pid: pid_t) -> String? {
        var path = [CChar](repeating: 0, count: Int(MAXPATHLEN) * 4)
        guard proc_pidpath(pid, &path, UInt32(path.count)) > 0 else { return nil }
        return String(cString: path)
    }

    private func usage(of pid: pid_t) -> (cpuNanos: UInt64, footprint: UInt64)? {
        var info = rusage_info_v4()
        let result = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) {
                proc_pid_rusage(pid, RUSAGE_INFO_V4, $0)
            }
        }
        guard result == 0 else { return nil }
        let ticks = info.ri_user_time + info.ri_system_time
        return (ticks * UInt64(timebase.numer) / UInt64(max(1, timebase.denom)), info.ri_phys_footprint)
    }

    private static func systemTicks() -> (busy: UInt64, total: UInt64) {
        var info = host_cpu_load_info()
        var count = mach_msg_type_number_t(
            MemoryLayout<host_cpu_load_info>.size / MemoryLayout<integer_t>.size
        )
        let result = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                host_statistics(mach_host_self(), HOST_CPU_LOAD_INFO, $0, &count)
            }
        }
        guard result == KERN_SUCCESS else { return (0, 0) }
        let user = UInt64(info.cpu_ticks.0), system = UInt64(info.cpu_ticks.1)
        let idle = UInt64(info.cpu_ticks.2), nice = UInt64(info.cpu_ticks.3)
        return (user + system + nice, user + system + idle + nice)
    }

    private static func systemMemoryUsed() -> UInt64 {
        var stats = vm_statistics64()
        var count = mach_msg_type_number_t(
            MemoryLayout<vm_statistics64>.size / MemoryLayout<integer_t>.size
        )
        let result = withUnsafeMutablePointer(to: &stats) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                host_statistics64(mach_host_self(), HOST_VM_INFO64, $0, &count)
            }
        }
        guard result == KERN_SUCCESS else { return 0 }
        let page = UInt64(vm_kernel_page_size)
        let appMemory = UInt64(stats.internal_page_count) - min(UInt64(stats.internal_page_count), UInt64(stats.purgeable_count))
        return (appMemory + UInt64(stats.wire_count) + UInt64(stats.compressor_page_count)) * page
    }
}
