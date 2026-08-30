import Darwin
import Foundation

/// 局域网里找 movieclaw（docs/design/device-auth.md §6.5）。
///
/// 用的是服务端**已经在应答**的那条通道：UDP 7359 上的 Jellyfin 发现协议
/// （`src/movieclaw_jellyfin/udp.py`）。广播一句 `who is JellyfinServer?`，
/// 服务端单播回一个 JSON，里面的 `Address` 就是 web 端口的完整地址。
/// mclaw CLI 用的是同一套（`cli/internal/discover`），两边行为保持一致。
///
/// **它只是省掉一次手打，不是必需路径**。四种情况下找不到，地址输入框
/// 因此永远保留：
///
///   1. 服务端的「Jellyfin 兼容层」开关被关掉了（就不应答了）；
///   2. 桥接网络部署下服务端自报的是容器内地址（连不上，会被剔掉）；
///   3. 跨网段、VPN、公网——广播出不去；
///   4. UDP 7359 被别的程序占了。
///
/// 另外要留意：服务端优先返回用户配的「对外访问地址」，那可能是反向代理域名。
/// 对 Worker 来说走反代明显更慢（要来回传大量视频分片），所以发现到的地址只
/// **填进输入框供用户过目**，不直接保存——最终用哪个地址是用户的决定。
///
/// 与 CLI 的一处有意差异：mclaw 拿到地址后会自己打一次 `/health` 确认对面是
/// movieclaw（局域网里的真 Jellyfin 会应答同一句问询），Worker 这边不做——
/// 紧接着的「连接并配对」按下去就是同一个检查，而且结论直接显示在窗口里。
/// 在这里再探一次只是把同一件事做两遍。
enum LANDiscovery {
    /// 一台应答了发现请求的服务器。
    struct Server: Equatable {
        /// 服务端自报的完整地址（含协议与端口）。
        let address: String
        /// 「设置 → Jellyfin 兼容」里配的服务器名，用于在多台之间区分。
        let name: String
    }

    /// 与 `movieclaw_jellyfin/udp.py` 的 DISCOVERY_PORT 一致。
    static let port: UInt16 = 7359
    /// 服务端认的暗号，大小写不敏感。
    private static let probe = "who is JellyfinServer?"

    /// 广播一次并收集 timeout 内的全部应答，按地址去重。
    ///
    /// 这是**阻塞调用**（BSD socket + 读超时），必须在后台线程执行。
    /// 找不到不是错误：局域网里没有、或者不在同一网段都是正常情况，
    /// 调用方据空数组提示用户手填即可。
    static func find(timeout: TimeInterval) -> [Server] {
        let fd = socket(AF_INET, SOCK_DGRAM, 0)
        guard fd >= 0 else { return [] }
        defer { close(fd) }

        // 内核默认拒绝往广播地址发包；不开这个选项 sendto 直接失败，
        // 那种失败会被误当成「局域网里没有 movieclaw」。
        var enable: Int32 = 1
        guard setsockopt(fd, SOL_SOCKET, SO_BROADCAST,
                         &enable, socklen_t(MemoryLayout<Int32>.size)) == 0 else {
            return []
        }
        var readTimeout = timeval(
            tv_sec: Int(timeout),
            tv_usec: Int32((timeout - floor(timeout)) * 1_000_000)
        )
        _ = setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO,
                       &readTimeout, socklen_t(MemoryLayout<timeval>.size))

        var sent = 0
        for target in broadcastTargets() {
            var addr = target
            let bytes = Array(probe.utf8)
            let result = withUnsafePointer(to: &addr) { pointer -> Int in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                    sendto(fd, bytes, bytes.count, 0, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
            if result > 0 { sent += 1 }
        }
        guard sent > 0 else { return [] }

        let deadline = Date().addingTimeInterval(timeout)
        var seen = Set<String>()
        var found: [Server] = []
        var buffer = [UInt8](repeating: 0, count: 4096)
        while Date() < deadline {
            let count = recv(fd, &buffer, buffer.count, 0)
            // 读超时（EAGAIN）或出错都表示本轮结束，这是正常出口
            guard count > 0 else { break }
            guard let server = parse(Data(buffer[0..<count])) else { continue }
            guard seen.insert(server.address).inserted else { continue }
            found.append(server)
        }
        return found
    }

    /// 解析一帧应答。非 JSON、缺 Address 的都跳过——局域网里什么都可能往这个端口发。
    static func parse(_ data: Data) -> Server? {
        guard
            let object = try? JSONSerialization.jsonObject(with: data),
            let payload = object as? [String: Any],
            let address = payload["Address"] as? String,
            !address.isEmpty
        else { return nil }
        let name = (payload["Name"] as? String) ?? ""
        return Server(address: address, name: name)
    }

    /// 要发往的广播地址。
    ///
    /// 除了受限广播 255.255.255.255，还要逐网卡取定向广播地址：多网卡机器
    /// （有线 + 无线、虚拟机网桥）上，受限广播只会从内核选的那一张网卡出去，
    /// 服务器恰好在另一张网卡那侧就找不到了。
    static func broadcastTargets() -> [sockaddr_in] {
        var targets = [makeAddress(INADDR_BROADCAST)]
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let first = head else { return targets }
        defer { freeifaddrs(head) }

        var seen = Set<UInt32>()
        var cursor: UnsafeMutablePointer<ifaddrs>? = first
        while let entry = cursor {
            defer { cursor = entry.pointee.ifa_next }
            let flags = Int32(entry.pointee.ifa_flags)
            guard flags & IFF_UP != 0, flags & IFF_BROADCAST != 0, flags & IFF_LOOPBACK == 0 else {
                continue
            }
            guard
                let raw = entry.pointee.ifa_dstaddr,
                raw.pointee.sa_family == sa_family_t(AF_INET)
            else { continue }
            let broadcast = raw.withMemoryRebound(to: sockaddr_in.self, capacity: 1) {
                $0.pointee.sin_addr.s_addr
            }
            guard broadcast != 0, seen.insert(broadcast).inserted else { continue }
            targets.append(makeAddress(broadcast, alreadyNetworkOrder: true))
        }
        return targets
    }

    private static func makeAddress(_ address: in_addr_t, alreadyNetworkOrder: Bool = false) -> sockaddr_in {
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr.s_addr = alreadyNetworkOrder ? address : address.bigEndian
        return addr
    }
}

extension LANDiscovery.Server {
    /// 界面上显示的名字；服务端没配名字时不显示空白。
    var displayName: String { name.isEmpty ? "movieclaw" : name }
}
