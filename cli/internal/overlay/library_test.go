package overlay

import (
	"strings"
	"testing"
)

// 选择集是 Agent 最常踩的地方：三种来源要能混用、要去重、要保序，
// 而且 --items-from 必须同时吃 JSON 与纯文本，否则每次都得先装 jq。

func TestCollectItemIDsMergesSourcesAndDeduplicates(t *testing.T) {
	ids, err := collectItemIDs(
		[]string{"3", "42"},             // 位置参数
		[]string{"43,44", "42"},         // --item（逗号分隔 + 与位置参数重复）
		"-",                             // --items-from 读标准输入
		strings.NewReader("45\n\n43\n"), // 纯文本，一行一个，含空行与重复
	)
	if err != nil {
		t.Fatalf("解析选择集失败：%v", err)
	}
	want := []int{42, 43, 44, 45}
	if len(ids) != len(want) {
		t.Fatalf("期望 %v，实际 %v", want, ids)
	}
	for i, value := range want {
		if ids[i] != value {
			t.Fatalf("顺序应保持首次出现的次序，期望 %v，实际 %v", want, ids)
		}
	}
}

func TestCollectItemIDsRejectsNonInteger(t *testing.T) {
	if _, err := collectItemIDs([]string{"3"}, []string{"abc"}, "", nil); err == nil {
		t.Fatal("非整数的条目 id 必须报用法错误，而不是静默丢掉")
	}
}

func TestReadItemIDsAcceptsListOutputAndJobResult(t *testing.T) {
	// mclaw library items list 的输出：裸对象数组，取 id
	listOutput := `[{"id": 7, "title": "甲"}, {"id": 9, "title": "乙"}]`
	ids, err := readItemIDs("-", strings.NewReader(listOutput))
	if err != nil {
		t.Fatalf("应当能直接吃 items list 的 JSON：%v", err)
	}
	if strings.Join(ids, ",") != "7,9" {
		t.Fatalf("期望 7,9，实际 %v", ids)
	}

	// mclaw jobs show 的结论：失败明细埋在信封里，也要能直接喂回来重跑
	jobOutput := `{"id": "job_1", "result": {"moved": 2,
		"failures": [{"media_item_id": 11, "reason": "源目录已不在原位"}]}}`
	ids, err = readItemIDs("-", strings.NewReader(jobOutput))
	if err != nil {
		t.Fatalf("应当能从作业结论里取出失败条目：%v", err)
	}
	if len(ids) != 1 || ids[0] != "11" {
		t.Fatalf("期望只取到失败条目 11，实际 %v", ids)
	}
}

func TestReadItemIDsExplainsEmptyJSON(t *testing.T) {
	_, err := readItemIDs("-", strings.NewReader(`[{"title": "没有 id"}]`))
	if err == nil {
		t.Fatal("JSON 里没有 id 时必须报错并指路，不能当成空选择集继续")
	}
	if !strings.Contains(err.Error(), "条目 id") {
		t.Fatalf("错误信息要说清缺什么，实际：%v", err)
	}
}

func TestHumanBytesUsesBinaryUnits(t *testing.T) {
	for _, tc := range []struct {
		value int
		want  string
	}{
		{512, "512 B"},
		{1024, "1.0 KiB"},
		{30 * 1024 * 1024 * 1024 * 1024, "30.0 TiB"},
	} {
		if got := humanBytes(tc.value); got != tc.want {
			t.Errorf("humanBytes(%d) = %q，期望 %q", tc.value, got, tc.want)
		}
	}
}
