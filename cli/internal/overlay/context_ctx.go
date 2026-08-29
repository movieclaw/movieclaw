package overlay

import (
	"context"

	"github.com/spf13/cobra"
)

func contextWith(cmd *cobra.Command, s *Settings) context.Context {
	parent := cmd.Context()
	if parent == nil {
		parent = context.Background()
	}
	return context.WithValue(parent, settingsKey{}, s)
}
