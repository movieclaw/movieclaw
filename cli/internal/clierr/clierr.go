// Package clierr 定义 CLI 的错误模型与退出码契约（docs/design/cli.md §5.8）。
//
// 退出码是脚本与 Agent 的机器接口，一经发布不可变更含义：
//
//	0  成功
//	1  业务错误（服务端 4xx/5xx，message 中文透传）
//	2  用法错误（参数不合法）
//	3  认证失败 / 会话过期（提示 mclaw login）
//	4  无法连接服务器（网络 / 地址错误）
//	5  需要确认但未提供 --yes
//	6  长任务失败或 --wait 超时
//	7  歧义待消解（候选清单已随输出返回）
package clierr

import "fmt"

type ExitCode int

const (
	OK          ExitCode = 0
	Business    ExitCode = 1
	Usage       ExitCode = 2
	Auth        ExitCode = 3
	Network     ExitCode = 4
	NeedConfirm ExitCode = 5
	TaskFailed  ExitCode = 6
	Ambiguous   ExitCode = 7
)

// Error 是带退出码与修正提示的 CLI 错误。
//
// 「错误即帮助」：Agent 靠试错学习，Message 说清哪里错了，Hint 给出下一步该
// 怎么做（docs/design/cli.md §4.2）。
type Error struct {
	Message  string
	ExitCode ExitCode
	// Hint 是可执行的下一步，不是同义反复的抱怨。
	Hint string
	// Code 是服务端业务码（如 VALIDATION_ERROR），有则在输出里标出。
	Code string
	// Details 是服务端 details 字段，供 Agent 结构化消费。
	Details any
	wrapped error
}

func (e *Error) Error() string { return e.Message }
func (e *Error) Unwrap() error { return e.wrapped }

// New 构造一个业务错误（退出码 1）。
func New(format string, args ...any) *Error {
	return &Error{Message: fmt.Sprintf(format, args...), ExitCode: Business}
}

// Newf 按指定退出码构造错误。
func Newf(code ExitCode, format string, args ...any) *Error {
	return &Error{Message: fmt.Sprintf(format, args...), ExitCode: code}
}

// WithHint 附上修正提示，返回自身便于链式书写。
func (e *Error) WithHint(format string, args ...any) *Error {
	e.Hint = fmt.Sprintf(format, args...)
	return e
}

// WithCode 附上服务端业务码。
func (e *Error) WithCode(code string) *Error {
	e.Code = code
	return e
}

// WithDetails 附上服务端 details。
func (e *Error) WithDetails(details any) *Error {
	e.Details = details
	return e
}

// Wrap 记录底层错误，供 errors.Is/As 追溯；不进用户可见文案。
func (e *Error) Wrap(err error) *Error {
	e.wrapped = err
	return e
}

// Usagef 是用法错误的快捷构造（退出码 2）。
func Usagef(format string, args ...any) *Error { return Newf(Usage, format, args...) }

// Networkf 是网络错误的快捷构造（退出码 4）。
func Networkf(format string, args ...any) *Error { return Newf(Network, format, args...) }

// Authf 是认证错误的快捷构造（退出码 3）。
func Authf(format string, args ...any) *Error { return Newf(Auth, format, args...) }
