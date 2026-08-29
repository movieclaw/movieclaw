package overlay

import (
	"errors"

	"github.com/yipengfei329/movieclaw/cli/internal/clierr"
)

// asCliError 是 errors.As 的窄化包装，避免各处重复写类型断言样板。
func asCliError(err error, target **clierr.Error) bool {
	return errors.As(err, target)
}
