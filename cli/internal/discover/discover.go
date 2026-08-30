// Package discover 是局域网里找 movieclaw 的最后一招。
//
// 用的是服务端已经在应答的那条通道：UDP 7359 上的 Jellyfin 发现协议
// （`src/movieclaw_jellyfin/udp.py`）。广播一句 `who is JellyfinServer?`，
// 服务端单播回一个 JSON，里面的 Address 就是 web 端口的完整地址——正好是
// mclaw 要的那个。复用它意味着服务端一行不用改，也不占新端口。
//
// **它只是兜底，不是主路**。四种情况下发现不到，`--server` 必须一直留着：
//
//  1. 「Jellyfin 兼容层」开关被关掉了（服务端就不应答）；
//  2. Docker 桥接部署下探测出的可能是容器内网段地址（服务端自己会告警）；
//  3. 跨网段、VPN、公网——广播出不去；
//  4. UDP 7359 被别的程序占了。
package discover

import (
	"encoding/json"
	"errors"
	"net"
	"time"
)

// probe 是服务端认的暗号，大小写不敏感。
const probe = "who is JellyfinServer?"

// port 与 movieclaw_jellyfin/udp.py 的 DISCOVERY_PORT 一致。
// 是变量而非常量，只为让测试能在临时端口上起应答者（7359 常被真服务占着）。
var port = 7359

// Server 是一台应答了发现请求的服务器。
type Server struct {
	// Address 是服务端自报的完整地址（含协议与端口），可直接拿来发请求。
	Address string
	// Name 是「设置 → Jellyfin 兼容」里配的服务器名，用于在多台之间区分。
	Name string
}

// response 是服务端应答的 JSON（字段名对齐 Jellyfin，故为大写开头）。
type response struct {
	Address string `json:"Address"`
	Name    string `json:"Name"`
}

// Find 广播一次并收集 timeout 内的全部应答，按 Address 去重。
//
// 找不到不是错误：局域网里没有、或者根本不在同一网段，都是正常情况，
// 调用方据空结果回落到「请给地址」即可。返回 error 只表示「这次探测本身
// 没能发出去」（没有可用网卡、权限不足）。
func Find(timeout time.Duration) ([]Server, error) {
	conn, err := listen()
	if err != nil {
		return nil, err
	}
	defer conn.Close()

	targets := broadcastTargets()
	if len(targets) == 0 {
		return nil, errors.New("没有找到可用于广播的网络接口")
	}
	var sent int
	for _, target := range targets {
		if _, err := conn.WriteToUDP([]byte(probe), target); err == nil {
			sent++
		}
	}
	if sent == 0 {
		return nil, errors.New("广播发送失败（可能被防火墙拦截或缺少权限）")
	}

	if err := conn.SetReadDeadline(time.Now().Add(timeout)); err != nil {
		return nil, err
	}
	seen := map[string]bool{}
	var found []Server
	buf := make([]byte, 4096)
	for {
		n, _, err := conn.ReadFromUDP(buf)
		if err != nil {
			// 读超时即本轮结束——这是正常出口，不是失败
			return found, nil
		}
		var reply response
		if json.Unmarshal(buf[:n], &reply) != nil || reply.Address == "" {
			continue
		}
		if seen[reply.Address] {
			continue
		}
		seen[reply.Address] = true
		found = append(found, Server{Address: reply.Address, Name: reply.Name})
	}
}

// broadcastTargets 收集要发往的广播地址。
//
// 除了受限广播 255.255.255.255，还要逐网卡算出定向广播地址：多网卡机器
// （有线 + 无线、Docker 网桥）上，受限广播只会从内核选的那一张网卡出去，
// 服务器恰好在另一张网卡那侧就找不到了。
func broadcastTargets() []*net.UDPAddr {
	targets := []*net.UDPAddr{{IP: net.IPv4bcast, Port: port}}
	interfaces, err := net.Interfaces()
	if err != nil {
		return targets
	}
	for _, iface := range interfaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagBroadcast == 0 {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, addr := range addrs {
			ipnet, ok := addr.(*net.IPNet)
			if !ok || ipnet.IP.To4() == nil {
				continue
			}
			if bcast := broadcastOf(ipnet); bcast != nil {
				targets = append(targets, &net.UDPAddr{IP: bcast, Port: port})
			}
		}
	}
	return targets
}

// broadcastOf 由地址与掩码算出该网段的定向广播地址。
func broadcastOf(ipnet *net.IPNet) net.IP {
	ip := ipnet.IP.To4()
	mask := net.IP(ipnet.Mask).To4()
	if ip == nil || mask == nil {
		return nil
	}
	bcast := make(net.IP, net.IPv4len)
	for i := range bcast {
		bcast[i] = ip[i] | ^mask[i]
	}
	return bcast
}
