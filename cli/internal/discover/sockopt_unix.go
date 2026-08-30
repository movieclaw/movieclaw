//go:build !windows

package discover

import (
	"context"
	"net"
	"syscall"
)

// listen 开一个临时端口的 UDP 套接字，并打开 SO_BROADCAST。
//
// 这个选项是必需的：内核默认拒绝往广播地址发包，不设它 WriteToUDP 会直接
// 报 permission denied，而不是静默无应答——那种失败会被误当成「局域网里
// 没有 movieclaw」。
func listen() (*net.UDPConn, error) {
	config := net.ListenConfig{Control: func(_, _ string, c syscall.RawConn) error {
		var opErr error
		if err := c.Control(func(fd uintptr) {
			opErr = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_BROADCAST, 1)
		}); err != nil {
			return err
		}
		return opErr
	}}
	conn, err := config.ListenPacket(context.Background(), "udp4", ":0")
	if err != nil {
		return nil, err
	}
	return conn.(*net.UDPConn), nil
}
