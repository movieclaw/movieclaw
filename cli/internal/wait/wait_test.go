package wait

import (
	"testing"

	"github.com/movieclaw/movieclaw/cli/internal/clierr"
	"github.com/movieclaw/movieclaw/cli/internal/jsonval"
)

// decode 走 api 层同一个解码器（保序 *jsonval.Map），否则 jsonval.Object
// 拿到标准库的 map[string]any 会一律返回 nil，测试就测不到真实路径。
func decode(t *testing.T, raw string) any {
	t.Helper()
	value, err := jsonval.Decode([]byte(raw))
	if err != nil {
		t.Fatalf("测试用例 JSON 有误：%v", err)
	}
	return value
}

func TestPartialSuccessTreatsUnfinishedItemsAsBusinessError(t *testing.T) {
	cases := []struct {
		name    string
		result  string
		wantErr bool
	}{
		{"全部完成", `{"moved": 593, "failures": [], "errors": []}`, false},
		{"没有 result", "null", false},
		{"结构化失败明细", `{"failures": [{"media_item_id": 7, "title": "风筝", "reason": "源目录已不在原位"}]}`, true},
		{"既有任务的中文原因列表", `{"errors": ["附属文件搬运失败：权限不足"]}`, true},
		// 跳过是预检里用户已经确认过的策略性结果，不该影响退出码
		{"只有跳过", `{"moved": 586, "skips": [{"media_item_id": 9, "reason": "目录撞名"}]}`, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := partialSuccess("job_1", decode(t, tc.result))
			if !tc.wantErr {
				if err != nil {
					t.Fatalf("期望退出码 0，却得到错误：%v", err)
				}
				return
			}
			if err == nil {
				t.Fatal("有未完成项时必须以非零退出码结束，否则 Agent 会以为全做完了")
			}
			cliErr, ok := err.(*clierr.Error)
			if !ok {
				t.Fatalf("错误类型应为 *clierr.Error，实际 %T", err)
			}
			if cliErr.ExitCode != clierr.Business {
				t.Fatalf("退出码应为业务错误 %d，实际 %d", clierr.Business, cliErr.ExitCode)
			}
			if cliErr.Hint == "" {
				t.Fatal("必须给出可执行的下一步，否则用户不知道怎么收拾残局")
			}
		})
	}
}

func TestFirstReasonsCapsOutputAndFlattensBothShapes(t *testing.T) {
	structured := []any{}
	for i := 0; i < 10; i++ {
		structured = append(structured, jsonval.NewMap("title", "片子", "reason", "搬不动"))
	}
	got := firstReasons(structured, []any{"另一条"})
	if len(got) != 5 {
		t.Fatalf("最多只展示 5 条，实际 %d 条", len(got))
	}
	if got[0] != "片子：搬不动" {
		t.Fatalf("结构化条目应拼成「标题：原因」，实际 %q", got[0])
	}
	if plain := firstReasons([]any{"只有一条"}); len(plain) != 1 || plain[0] != "只有一条" {
		t.Fatalf("纯字符串原因应原样透传，实际 %v", plain)
	}
}
