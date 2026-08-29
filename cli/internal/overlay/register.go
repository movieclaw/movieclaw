package overlay

import "github.com/spf13/cobra"

// Register 挂载全部精选命令。生成层随后挂载，同名让位于这里注册的命令。
func Register(root *cobra.Command) {
	root.AddCommand(NewLoginCommand())
	root.AddCommand(NewLogoutCommand())
	root.AddCommand(NewStatusCommand())
}
