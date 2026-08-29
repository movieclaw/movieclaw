package overlay

import "github.com/spf13/cobra"

// Register 挂载全部精选命令。生成层随后挂载，同名让位于这里注册的命令；
// 这里预建的组（search / library / session / logs / jobs）则会被生成命令并入，
// 让 `mclaw search torrents` 与 `mclaw search history list` 出现在同一个组下。
func Register(root *cobra.Command) {
	root.AddCommand(NewLoginCommand())
	root.AddCommand(NewLogoutCommand())
	root.AddCommand(NewStatusCommand())
	root.AddCommand(NewDownloadCommand())
	root.AddCommand(NewSearchGroup())
	root.AddCommand(NewLibraryGroup())
	root.AddCommand(NewSessionGroup())
	root.AddCommand(NewLogsGroup())
	root.AddCommand(NewJobsGroup())
}
