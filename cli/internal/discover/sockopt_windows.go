//go:build windows

package discover

import (
	"context"
	"net"
	"syscall"
)

// listen 同 unix 版：开临时端口并打开 SO_BROADCAST。
// Windows 的 setsockopt 走 syscall.Handle，故单独一份。
func listen() (*net.UDPConn, error) {
	config := net.ListenConfig{Control: func(_, _ string, c syscall.RawConn) error {
		var opErr error
		if err := c.Control(func(fd uintptr) {
			opErr = syscall.SetsockoptInt(
				syscall.Handle(fd), syscall.SOL_SOCKET, syscall.SO_BROADCAST, 1)
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
