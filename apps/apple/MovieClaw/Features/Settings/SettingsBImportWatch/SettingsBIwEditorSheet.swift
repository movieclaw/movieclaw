import SwiftUI

/// 自动入库规则的导入目标三态（同 Web）：
/// - 指定库：固定导入所选库（落其主根）；
/// - 自动路由：识别出作品后按各库「收藏范围」分流，kind 为 movie / tv / video（video = 落入其他库）；
/// - 自定义目录：识别改名后落所选目录、不进任何库（整理结果需外部流转再进库的场景）。
enum SettingsBIwTarget: Equatable {
    case library(Int)
    case auto(kind: String)
    case path(String, kind: String)
}

/// 新建 / 编辑自动入库规则（对应 Web `RuleFormDialog`）。
///
/// 字段：源目录（服务端目录选择器 + 下载器目录快捷候选）、搬运策略（硬链接 / 复制）、
/// 导入目标（库 / 自动路由 / 自定义目录）、存量内容（整理 / 跳过）。每组下方的说明文字随选择切换，
/// 文案逐字照搬 Web。硬链接同盘检测、目录重叠等校验由后端在保存时做，失败原因原样提示。
struct SettingsBIwEditorSheet: View {
    let rule: API.ImportWatchView?
    let libraries: [API.LibraryView]
    let downloaderDirs: [SettingsBIwDirOption]
    let onSaved: () -> Void

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(\.dismiss) private var dismiss

    @State private var sourcePath: String
    @State private var strategy: String
    @State private var processExisting: Bool
    @State private var target: SettingsBIwTarget?
    @State private var busy = false
    @State private var error: String?

    init(rule: API.ImportWatchView?, libraries: [API.LibraryView], downloaderDirs: [SettingsBIwDirOption],
         initialTarget: SettingsBIwTarget? = nil, onSaved: @escaping () -> Void) {
        self.rule = rule
        self.libraries = libraries
        self.downloaderDirs = downloaderDirs
        self.onSaved = onSaved
        _sourcePath = State(initialValue: rule?.sourcePath ?? "")
        _strategy = State(initialValue: rule?.strategy ?? "hardlink")
        _processExisting = State(initialValue: rule?.processExisting ?? true)
        let initial: SettingsBIwTarget?
        if let libraryId = rule?.libraryId {
            initial = .library(libraryId)
        } else if let path = rule?.targetPath, !path.isEmpty {
            initial = .path(path, kind: rule?.kind ?? "movie")
        } else if let kind = rule?.kind {
            initial = .auto(kind: kind)
        } else if rule == nil, let initialTarget {
            initial = initialTarget
        } else {
            initial = libraries.first.map { .library($0.id) }
        }
        _target = State(initialValue: initial)
    }

    private var customPath: String? {
        if case let .path(path, _) = target { return path }
        return nil
    }

    private var canSubmit: Bool {
        guard !busy, !sourcePath.isEmpty, let target else { return false }
        if case let .path(path, _) = target { return !path.isEmpty }
        return true
    }

    var body: some View {
        NavigationStack {
            Form {
                if let error {
                    Section {
                        SettingsBNotice(text: error, tone: .danger)
                            .accessibilityIdentifier("import-watch-form-error")
                    }
                }
                sourceSection
                strategySection
                targetSection
                existingSection
            }
            .scrollContentBackground(.hidden)
            .scrollDismissesKeyboard(.interactively)
            .navigationTitle(rule == nil ? "添加自动入库规则" : "编辑自动入库规则")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消", systemImage: "xmark") { dismiss() }
                        .accessibilityIdentifier("sheet-close")
                }
            }
            .safeAreaBar(edge: .bottom) {
                SubsPrimaryButton(title: busy ? "保存中…" : "保存", busy: busy, enabled: canSubmit,
                                  identifier: "import-watch-form-save") {
                    Task { await submit() }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 12)
            }
        }
        .presentationBackground(.regularMaterial)
    }

    // MARK: - 源目录

    private var sourceSection: some View {
        Section {
            SettingsBPathField(
                label: "源目录（监听这里的下载）",
                path: $sourcePath,
                placeholder: "浏览服务器目录并选择…",
                identifier: "import-watch-form-source"
            )
            // 下载器目录快捷候选：源目录大概率就是下载器目录，点选直填；
            // 想用其子目录（如 watch/）点选后再「浏览」，选择器会从该目录起步
            if !downloaderDirs.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("从下载器目录快速选择（选后可再浏览细化到子目录）：")
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                    SettingsBFlow {
                        ForEach(downloaderDirs, id: \.path) { option in
                            SettingsBSelectChip(title: option.path, selected: sourcePath == option.path,
                                                identifier: "import-watch-form-dir-chip") {
                                sourcePath = option.path
                            }
                            .accessibilityHint("来自下载器「\(option.downloaderName)」")
                        }
                    }
                }
                .padding(.vertical, 2)
            }
        } footer: {
            Text("不能与任何媒体库的根路径重叠（库根下的内容由库自己扫描管理）。订阅和手动下载会把种子投到这个目录（movieclaw 视角）；若下载器在另一个容器/主机上、看到的路径不同，请先到「设置 → 下载器」配置路径映射，否则会下载到错误位置。")
        }
    }

    // MARK: - 搬运策略

    private var strategySection: some View {
        Section {
            SettingsBFlow {
                SettingsBSelectChip(title: "硬链接", selected: strategy == "hardlink", identifier: "import-watch-form-hardlink") {
                    strategy = "hardlink"
                }
                SettingsBSelectChip(title: "复制", selected: strategy == "copy", identifier: "import-watch-form-copy") {
                    strategy = "copy"
                }
            }
            .padding(.vertical, 2)
        } header: {
            Text("搬运策略")
        } footer: {
            Text(strategy == "hardlink"
                 ? "保存时会检测源目录与整理落点（库主根或自定义目录）是否同一文件系统，跨盘会提示改用复制。"
                 : "复制适合源目录与落点不在同一块盘的部署。")
        }
    }

    // MARK: - 导入目标

    private var targetSection: some View {
        Section {
            SettingsBFlow {
                ForEach(libraries, id: \.id) { lib in
                    SettingsBSelectChip(title: lib.name, selected: target == .library(lib.id),
                                        identifier: "import-watch-form-library-\(lib.id)") {
                        target = .library(lib.id)
                    }
                }
                ForEach([("movie", "自动路由（电影）"), ("tv", "自动路由（剧集）"), ("video", "落入其他库")], id: \.0) { kind, label in
                    SettingsBSelectChip(title: label, selected: target == .auto(kind: kind),
                                        identifier: "import-watch-form-auto-\(kind)") {
                        target = .auto(kind: kind)
                    }
                }
                SettingsBSelectChip(title: "自定义目录…", selected: customPath != nil, identifier: "import-watch-form-custom") {
                    if customPath == nil { target = .path("", kind: "movie") }
                }
            }
            .padding(.vertical, 2)
            if libraries.isEmpty {
                Text("还没有媒体库，请先在「媒体库」页创建。")
                    .font(.footnote)
                    .foregroundStyle(Theme.textFaint)
            }
            if case let .path(path, kind) = target {
                SettingsBPathField(
                    label: "自定义目录",
                    path: Binding(get: { path }, set: { target = .path($0, kind: kind) }),
                    placeholder: "选择整理结果的存放目录…",
                    identifier: "import-watch-form-target-path"
                )
                SettingsBFlow {
                    ForEach([("movie", "电影"), ("tv", "剧集"), ("video", "其他")], id: \.0) { k, label in
                        SettingsBSelectChip(title: label, selected: kind == k, identifier: "import-watch-form-path-kind-\(k)") {
                            target = .path(path, kind: k)
                        }
                    }
                }
                .padding(.vertical, 2)
            }
        } header: {
            Text("导入目标")
        } footer: {
            Text(targetHint)
        }
    }

    private var targetHint: String {
        switch target {
        case .auto:
            "自动路由：识别出作品后按各媒体库的「收藏范围」分流（如动画进动漫库），未命中进该类型的默认库；订阅投递的内容始终进订阅指定的库。每个类型至多一条自动路由规则。"
        case let .path(_, kind) where kind == "video":
            "自定义目录（其他）：不识别不改名，条目原样搬进该目录，不进入任何媒体库。文件后续出现在某个「其他」库的根目录时会被自动扫描入账。目录不得与库根或监听源重叠，每个类型至多一条。"
        case .path:
            "自定义目录：下载整理（识别改名）后的文件放入该目录，不进入任何媒体库。适合整理结果还需外部流转（如上传网盘、转存、人工确认）再进入媒体库的场景——文件后续出现在某个库的根目录时会被自动扫描入账。目录不得与库根或监听源重叠，每个类型至多一条。"
        default:
            "指定库：这个目录里的内容固定导入所选库（落其主根）。"
        }
    }

    // MARK: - 存量内容

    private var existingSection: some View {
        Section {
            SettingsBFlow {
                SettingsBSelectChip(title: "整理存量", selected: processExisting, identifier: "import-watch-form-existing-process") {
                    processExisting = true
                }
                SettingsBSelectChip(title: "跳过存量", selected: !processExisting, identifier: "import-watch-form-existing-skip") {
                    processExisting = false
                }
            }
            .padding(.vertical, 2)
        } header: {
            Text("存量内容")
        } footer: {
            Text(processExisting
                 ? "目录里已有的内容也会被识别并整理入库；认不出的进入「待处理」清单，可逐个认领或忽略。"
                 : "规则生效时目录里已有的条目会被标记为「已忽略」（不整理、不报错），之后只处理新增的下载；忽略的条目随时可在清单中恢复处理。适合目录里有其他工具管理的存量内容的场景。")
        }
    }

    // MARK: - 提交

    private func submit() async {
        guard canSubmit, let target else { return }
        busy = true
        error = nil
        defer { busy = false }
        let payload: API.ImportWatchPayload
        switch target {
        case let .library(id):
            payload = .init(sourcePath: sourcePath, strategy: strategy, libraryId: id, targetPath: nil, kind: nil, processExisting: processExisting)
        case let .auto(kind):
            payload = .init(sourcePath: sourcePath, strategy: strategy, libraryId: nil, targetPath: nil, kind: kind, processExisting: processExisting)
        case let .path(path, kind):
            payload = .init(sourcePath: sourcePath, strategy: strategy, libraryId: nil, targetPath: path, kind: kind, processExisting: processExisting)
        }
        do {
            if let rule {
                _ = try await api.watchUpdate(ruleId: rule.id, body: payload)
            } else {
                _ = try await api.watchCreate(body: payload)
            }
            onSaved()
            dismiss()
        } catch is CancellationError {
        } catch {
            // 同 Web：错误显示在弹层顶部；表单可能已滚动，再弹一次 Toast 确保看得到
            self.error = error.localizedDescription
            feedback.error(error)
        }
    }
}
