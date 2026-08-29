// Package api 是面向单个 movieclaw 服务器的 HTTP 客户端（docs/design/cli.md §8.3）。
//
// 职责：认证注入（Bearer 设备令牌）、统一超时、ApiResponse{success,code,message,
// data} 信封拆解、错误 → 中文 clierr.Error（带退出码与 hint）映射、文件上传/下载、
// spec 版本偏斜检测（读响应头 X-Movieclaw-Spec-Hash）。业务命令层只拿到拆好信封
// 的 data。
package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/yipengfei329/movieclaw/cli/internal/clierr"
	"github.com/yipengfei329/movieclaw/cli/internal/config"
	"github.com/yipengfei329/movieclaw/cli/internal/jsonval"
	"github.com/yipengfei329/movieclaw/cli/internal/output"
)

const (
	// APIPrefix 是所有业务端点的公共前缀；调用方传相对路径。
	APIPrefix = "/api/v1"
	// SpecHashHeader 携带服务端 spec 指纹，用于版本偏斜检测。
	SpecHashHeader = "X-Movieclaw-Spec-Hash"
)

// LastSeen 记录最近一次请求观察到的服务端 spec 指纹，供命令执行完后统一处理
// 偏斜刷新（避免每个命令自行处理）。
var (
	LastSeenSpecHash string
	LastSeenServer   string
)

// Client 是一个服务器的同步 HTTP 客户端。
type Client struct {
	Server string
	Debug  bool
	// LastMessage 是最近一次成功响应的服务端 message，命令层透传到 stderr。
	LastMessage string

	http  *http.Client
	token string
}

// New 构造客户端。凭证只有 Bearer 一条通道（docs/design/device-auth.md §6.2）：
// 环境变量优先于落盘令牌——产品内 Agent 工作区注入的短时效令牌与 CI 里注入的
// 令牌都走这条，且完全不落盘。
func New(server string, timeout time.Duration, debug bool) (*Client, error) {
	// 地址漏写 http:// 是最常见的输入错误。在这里一次性挡掉，比让它漏成
	// 传输层的英文解析错误强得多——「错误即帮助」，用户要的是能照做的下一步。
	if !strings.HasPrefix(server, "http://") && !strings.HasPrefix(server, "https://") {
		return nil, clierr.Networkf("服务器地址缺少 http:// 或 https:// 前缀：%s", server).
			WithHint("改成 http://%s 再试", strings.TrimPrefix(strings.TrimPrefix(server, "//"), "/"))
	}
	token, err := TokenFor(server)
	if err != nil {
		return nil, err
	}
	return &Client{
		Server: server,
		Debug:  debug,
		token:  token,
		http:   &http.Client{Timeout: timeout},
	}, nil
}

// TokenFor 解析该服务器要用的令牌：环境变量 > 本地凭证。
func TokenFor(server string) (string, error) {
	if token := os.Getenv(config.EnvToken); token != "" {
		return token, nil
	}
	return config.LoadToken(server)
}

// Request 发起请求并拆信封，返回 data 字段。
func (c *Client) Request(method, path string, params url.Values, body any) (any, error) {
	data, _, err := c.RequestRaw(method, path, params, body)
	return data, err
}

// RequestRaw 同 Request，但额外返回 HTTP 状态码——设备配对的轮询需要按状态码
// 区分「等待批准 / 退避 / 已批准」三种结论。
func (c *Client) RequestRaw(method, path string, params url.Values, body any) (any, int, error) {
	var reader io.Reader
	contentType := ""
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return nil, 0, clierr.New("请求体序列化失败：%v", err)
		}
		reader = bytes.NewReader(encoded)
		contentType = "application/json"
	}
	resp, err := c.send(method, path, params, reader, contentType)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	data, err := c.parse(resp)
	return data, resp.StatusCode, err
}

// Upload 以 multipart 上传一个文件。
func (c *Client) Upload(method, path string, params url.Values, filePath string) (any, error) {
	content, err := os.ReadFile(filePath)
	if err != nil {
		return nil, clierr.Usagef("无法读取文件：%s（%v）", filePath, err)
	}
	var buf bytes.Buffer
	writer := multipart.NewWriter(&buf)
	part, err := writer.CreateFormFile("file", filepath.Base(filePath))
	if err != nil {
		return nil, clierr.New("构造上传请求失败：%v", err)
	}
	if _, err := part.Write(content); err != nil {
		return nil, clierr.New("构造上传请求失败：%v", err)
	}
	if err := writer.Close(); err != nil {
		return nil, clierr.New("构造上传请求失败：%v", err)
	}
	resp, err := c.send(method, path, params, &buf, writer.FormDataContentType())
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return c.parse(resp)
}

// Download 把文件直出端点的响应写到本地文件。
func (c *Client) Download(path string, params url.Values, target string) error {
	resp, err := c.send(http.MethodGet, path, params, nil, "")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		_, err := c.parse(resp) // 统一走错误映射
		if err != nil {
			return err
		}
		return clierr.New("下载失败（HTTP %d）", resp.StatusCode)
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return clierr.Usagef("无法创建目录：%v", err)
	}
	file, err := os.Create(target)
	if err != nil {
		return clierr.Usagef("无法写入文件：%s（%v）", target, err)
	}
	defer file.Close()
	if _, err := io.Copy(file, resp.Body); err != nil {
		return clierr.Usagef("写入文件失败：%s（%v）", target, err)
	}
	return nil
}

// StreamIdleTimeout 是事件流「多久没数据就判定为断了」的阈值。
//
// 服务端有心跳和事件节奏，正常的流不会静默这么久；没有这个阈值，半开连接
// （NAT 超时、反代重启、服务假死）会让命令永久挂着，既不出结果也不报错。
const StreamIdleTimeout = 120 * time.Second

// Stream 打开一个 SSE 端点，返回响应体供调用方分帧消费。
func (c *Client) Stream(path string, params url.Values, lastEventID string) (io.ReadCloser, error) {
	// 事件流是长连接，不受全局超时约束；改用「读空闲」超时看住它
	ctx, cancel := context.WithCancel(context.Background())
	req, err := c.newRequest(http.MethodGet, path, params, nil, "")
	if err != nil {
		cancel()
		return nil, err
	}
	req = req.WithContext(ctx)
	req.Header.Set("Accept", "text/event-stream")
	if lastEventID != "" {
		req.Header.Set("Last-Event-ID", lastEventID)
	}
	streamer := &http.Client{Timeout: 0, Transport: c.http.Transport}
	resp, err := streamer.Do(req)
	if err != nil {
		cancel()
		return nil, c.transportError(err, req.URL.String())
	}
	if resp.StatusCode >= 400 {
		defer resp.Body.Close()
		cancel()
		if _, parseErr := c.parse(resp); parseErr != nil {
			return nil, parseErr
		}
		return nil, clierr.New("事件流打开失败（HTTP %d）", resp.StatusCode)
	}
	c.recordSpecHash(resp)
	return newIdleGuard(resp.Body, cancel, StreamIdleTimeout), nil
}

// idleGuard 在读空闲超过 timeout 时取消请求，让阻塞中的 Read 返回错误。
//
// 定时器由每次成功的 Read 续期：有数据流动就一直读下去，真的静默了才断。
type idleGuard struct {
	body    io.ReadCloser
	cancel  context.CancelFunc
	timer   *time.Timer
	timeout time.Duration
}

func newIdleGuard(body io.ReadCloser, cancel context.CancelFunc, timeout time.Duration) io.ReadCloser {
	guard := &idleGuard{body: body, cancel: cancel}
	guard.timer = time.AfterFunc(timeout, cancel)
	guard.timeout = timeout
	return guard
}

func (g *idleGuard) Read(p []byte) (int, error) {
	n, err := g.body.Read(p)
	if n > 0 {
		g.timer.Reset(g.timeout)
	}
	return n, err
}

func (g *idleGuard) Close() error {
	g.timer.Stop()
	g.cancel()
	return g.body.Close()
}

func (c *Client) newRequest(
	method, path string, params url.Values, body io.Reader, contentType string,
) (*http.Request, error) {
	target := c.Server + APIPrefix + path
	if len(params) > 0 {
		target += "?" + params.Encode()
	}
	req, err := http.NewRequest(method, target, body)
	if err != nil {
		return nil, clierr.Networkf("与服务器通信失败：%v", err).
			WithHint("检查服务器地址格式（须含 http:// 前缀，当前为 %s）与网络状况", c.Server)
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("X-MovieClaw-Client", "cli")
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	return req, nil
}

func (c *Client) send(
	method, path string, params url.Values, body io.Reader, contentType string,
) (*http.Response, error) {
	req, err := c.newRequest(method, path, params, body, contentType)
	if err != nil {
		return nil, err
	}
	if c.Debug {
		output.Info("[debug] %s %s", method, req.URL.String())
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, c.transportError(err, req.URL.String())
	}
	if c.Debug {
		output.Info("[debug] -> %d", resp.StatusCode)
	}
	c.recordSpecHash(resp)
	return resp, nil
}

func (c *Client) recordSpecHash(resp *http.Response) {
	if hash := resp.Header.Get(SpecHashHeader); hash != "" {
		LastSeenSpecHash = hash
		LastSeenServer = c.Server
	}
}

// transportError 把传输层故障归一成带修正指引的中文错误，绝不裸抛。
func (c *Client) transportError(err error, target string) error {
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return clierr.Networkf("请求超时（%s）", target).
			WithHint("可用 --timeout 调大超时时间")
	}
	var urlErr *url.Error
	if errors.As(err, &urlErr) {
		var opErr *net.OpError
		if errors.As(urlErr.Err, &opErr) {
			return clierr.Networkf("无法连接 movieclaw 服务器：%s", c.Server).
				WithHint("确认服务已启动、地址正确（含端口）；" +
					"地址来源优先级：--server > MOVIECLAW_SERVER > 当前上下文")
		}
	}
	return clierr.Networkf("与服务器通信失败：%v", err).
		WithHint("检查服务器地址格式（须含 http:// 前缀，当前为 %s）与网络状况", c.Server)
}

// parse 拆信封并把错误映射成 clierr.Error。
func (c *Client) parse(resp *http.Response) (any, error) {
	c.LastMessage = ""
	raw, readErr := io.ReadAll(resp.Body)

	if resp.StatusCode == http.StatusUnauthorized {
		// 优先透传服务端的具体原因（如「令牌无效或已吊销」），拿不到才用通用话术
		message := messageFrom(raw)
		if message == "" {
			message = "未登录或会话已过期"
		}
		return nil, clierr.Authf("%s", message).
			WithHint("请先执行 mclaw login（或检查 MOVIECLAW_TOKEN 是否有效）")
	}
	if resp.StatusCode == http.StatusNoContent {
		return nil, nil
	}
	if readErr != nil {
		return nil, clierr.Networkf("读取响应失败：%v", readErr)
	}

	// 保序解析：服务端字段顺序是输出契约的一部分（jsonval.Map 的注释说明了原因）
	payload, err := jsonval.Decode(raw)
	if err != nil {
		return nil, clierr.New("服务器返回了无法解析的响应（HTTP %d）", resp.StatusCode).
			WithHint("确认 --server 指向的是 movieclaw 服务而非其他程序")
	}

	// 统一信封：success 字段存在即为 ApiResponse / ErrorResponse
	if envelope := jsonval.Object(payload); envelope.Len() > 0 {
		if success := envelope.Get("success"); success != nil {
			if ok, _ := success.(bool); ok {
				if message := jsonval.Str(envelope.Get("message")); message != "" {
					c.LastMessage = message
				}
				return envelope.Get("data"), nil
			}
			message := jsonval.Str(envelope.Get("message"))
			if message == "" {
				message = fmt.Sprintf("请求失败（HTTP %d）", resp.StatusCode)
			}
			return nil, clierr.New("%s", message).
				WithCode(jsonval.Str(envelope.Get("code"))).
				WithDetails(envelope.Get("details"))
		}
	}

	// 非信封 JSON（如 /health）原样返回
	if resp.StatusCode >= 400 {
		return nil, clierr.New("请求失败（HTTP %d）", resp.StatusCode).WithDetails(payload)
	}
	return payload, nil
}

func messageFrom(raw []byte) string {
	payload, err := jsonval.Decode(raw)
	if err != nil {
		return ""
	}
	return jsonval.Str(jsonval.Object(payload).Get("message"))
}
