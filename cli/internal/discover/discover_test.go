package discover

import (
	"encoding/json"
	"net"
	"testing"
	"time"
)

// responder 起一个照 movieclaw_jellyfin/udp.py 协议应答的临时监听者。
func responder(t *testing.T, replies ...string) {
	t.Helper()
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4zero, Port: 0})
	if err != nil {
		t.Skipf("跳过：无法监听 UDP（%v）", err)
	}
	previous := port
	port = conn.LocalAddr().(*net.UDPAddr).Port
	t.Cleanup(func() { port = previous; conn.Close() })

	go func() {
		buf := make([]byte, 4096)
		for {
			n, addr, err := conn.ReadFromUDP(buf)
			if err != nil {
				return
			}
			// 暗号大小写不敏感，这里按服务端的口径只做包含判断
			if len(buf[:n]) == 0 {
				continue
			}
			for _, reply := range replies {
				_, _ = conn.WriteToUDP([]byte(reply), addr)
			}
		}
	}()
}

func reply(address, name string) string {
	raw, _ := json.Marshal(map[string]any{
		"Address": address, "Name": name, "Id": "x", "EndpointAddress": nil,
	})
	return string(raw)
}

func TestFindCollectsAndDeduplicates(t *testing.T) {
	responder(t,
		reply("http://192.168.1.10:3000", "客厅 NAS"),
		reply("http://192.168.1.10:3000", "客厅 NAS"), // 同一台重复应答只算一次
		reply("http://192.168.1.20:3000", "书房"),
	)
	found, err := Find(time.Second)
	if err != nil {
		t.Fatalf("查找失败：%v", err)
	}
	if len(found) != 2 {
		t.Fatalf("期望去重后 2 台，实际 %d：%+v", len(found), found)
	}
	if found[0].Address != "http://192.168.1.10:3000" || found[0].Name != "客厅 NAS" {
		t.Errorf("解析结果不对：%+v", found[0])
	}
}

// TestFindReturnsEmptyWhenNobodyAnswers 校验「没找到」不是错误：局域网里没有、
// 或者不在同一网段都是正常情况，调用方据空结果回落到手工给地址。
func TestFindReturnsEmptyWhenNobodyAnswers(t *testing.T) {
	previous := port
	// 随手占一个空端口号：没人在上面应答
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4zero, Port: 0})
	if err != nil {
		t.Skipf("跳过：无法分配端口（%v）", err)
	}
	port = conn.LocalAddr().(*net.UDPAddr).Port
	conn.Close()
	t.Cleanup(func() { port = previous })

	found, err := Find(300 * time.Millisecond)
	if err != nil {
		t.Fatalf("没人应答不该报错：%v", err)
	}
	if len(found) != 0 {
		t.Fatalf("不该找到任何东西：%+v", found)
	}
}

// TestFindIgnoresGarbage 校验非 JSON 或缺 Address 的应答被跳过——
// 局域网里什么都可能往这个端口发。
func TestFindIgnoresGarbage(t *testing.T) {
	responder(t, "不是 JSON", `{"Name":"缺地址"}`, reply("http://10.0.0.5:3000", "有效"))
	found, err := Find(time.Second)
	if err != nil {
		t.Fatalf("查找失败：%v", err)
	}
	if len(found) != 1 || found[0].Address != "http://10.0.0.5:3000" {
		t.Fatalf("垃圾应答没有被过滤：%+v", found)
	}
}

func TestBroadcastOfComputesSubnetAddress(t *testing.T) {
	_, ipnet, _ := net.ParseCIDR("192.168.1.42/24")
	ipnet.IP = net.ParseIP("192.168.1.42")
	if got := broadcastOf(ipnet); got.String() != "192.168.1.255" {
		t.Errorf("广播地址算错了：%v", got)
	}
}

// TestBroadcastTargetsAlwaysIncludeLimited 校验受限广播永远在列表里：
// 逐网卡枚举可能一无所获（容器、无网卡环境），那时它是唯一的出路。
func TestBroadcastTargetsAlwaysIncludeLimited(t *testing.T) {
	targets := broadcastTargets()
	if len(targets) == 0 || !targets[0].IP.Equal(net.IPv4bcast) {
		t.Fatalf("首个目标应当是 255.255.255.255：%+v", targets)
	}
}
