import Foundation

// 生成模型的协议补充集中放在这里（Generated/ 勿手改）。
// 各模块需要给 API.* 模型加 Identifiable 等一致性时，先 grep 本文件，没有再加到这里，
// 不要在模块目录里各加一份——同一类型重复声明一致性会编译失败。

extension API.LibraryFileView: Identifiable {}

extension API.LibraryItemView: Identifiable {
    var id: Int { mediaItemId }
}

extension API.FavoriteItemView: Identifiable {
    var id: Int { mediaItemId }
}

// 活动模块：删除确认弹层按种子任务弹出、列表按 Job id 区分
extension API.DownloadTaskView: Identifiable {}

extension API.JobView: Identifiable {}
