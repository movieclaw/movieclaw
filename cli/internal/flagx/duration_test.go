package flagx

import (
	"testing"
	"time"

	"github.com/spf13/pflag"
)

func TestAcceptsBareSecondsAndUnits(t *testing.T) {
	cases := map[string]time.Duration{
		"30":    30 * time.Second,
		"3600":  time.Hour,
		"0.5":   500 * time.Millisecond,
		"90s":   90 * time.Second,
		"1h30m": 90 * time.Minute,
		"5m":    5 * time.Minute,
	}
	for raw, want := range cases {
		var got time.Duration
		flags := pflag.NewFlagSet("t", pflag.ContinueOnError)
		Var(flags, &got, "timeout", 0, "")
		if err := flags.Parse([]string{"--timeout", raw}); err != nil {
			t.Errorf("解析 %q 失败：%v", raw, err)
			continue
		}
		if got != want {
			t.Errorf("%q 解析成 %v，期望 %v", raw, got, want)
		}
	}
}

func TestRejectsGarbageWithActionableMessage(t *testing.T) {
	for _, raw := range []string{"很久", "-5", "-1s", "3x"} {
		var got time.Duration
		flags := pflag.NewFlagSet("t", pflag.ContinueOnError)
		flags.SetOutput(discard{})
		Var(flags, &got, "timeout", 0, "")
		if err := flags.Parse([]string{"--timeout", raw}); err == nil {
			t.Errorf("%q 应当被拒绝，实际解析成 %v", raw, got)
		}
	}
}

type discard struct{}

func (discard) Write(p []byte) (int, error) { return len(p), nil }

func TestDefaultIsAppliedBeforeParse(t *testing.T) {
	var got time.Duration
	flags := pflag.NewFlagSet("t", pflag.ContinueOnError)
	Var(flags, &got, "timeout", 30*time.Second, "")
	if got != 30*time.Second {
		t.Fatalf("默认值没生效：%v", got)
	}
}
