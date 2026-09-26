import SwiftUI

/// 媒体库表单（Web `library-form-dialog.tsx` 的 LibraryFormDialog）：`libraryId` 为 nil 走新建向导，否则编辑该库。以 .sheet 呈现。
///
/// 新建极简（向导）：只问必答题——① 选类型 → ② 名称、根目录、可见范围 → ③（影视库）收藏范围；
/// 扫描开关全按推荐值、刮削偏好覆盖是少数人的精调，都放到编辑里。最后一步「创建并开始扫描」。
///
/// 编辑分层：按用途分区折叠（基本信息 / 封面 / 扫描与监控 / 收藏范围 / 刮削设置 / 可见范围），默认全收起、
/// 同时只开一个；折叠态的摘要行先回答「这个库配成了什么样」，改哪项点哪项。封面上传即生效，不进保存的 payload。
/// 类型创建后不可改，只读展示。
struct LibraryFormSheet: View {
    var libraryId: Int?
    /// 保存成功回调，带上服务端返回的库
    var onSaved: (API.LibraryView) -> Void = { _ in }

    var body: some View {
        if let libraryId {
            ManageEditLibraryForm(libraryId: libraryId, onSaved: onSaved)
        } else {
            ManageCreateLibraryForm(onSaved: onSaved)
        }
    }
}

/// 类型卡片的文案：讲清后果，不提识别用的是哪个数据源（同 Web `KIND_CARDS`）
enum ManageKindCard {
    static func symbol(_ kind: String) -> String { LibraryKindMeta.symbol(kind) }

    static func blurb(_ kind: String) -> String {
        switch kind {
        case "movie": "自动识别影片，补齐简介、评分、演职员与海报剧照；一部片一个目录。"
        case "tv": "自动识别剧集与季集，补齐分集信息与图片；追新订阅按集补齐。"
        case "video": "不识别、不刮削、不改名的视频：放什么由你定。有 NFO 就读 NFO，否则按文件名展示，封面从视频里抓帧。"
        default: "不识别、不刮削，扫描目录里的所有图片，按拍摄时间排成相册墙；点开全屏查看。"
        }
    }

    static func traits(_ kind: String) -> String {
        switch kind {
        case "movie", "tv": "可订阅 · 可整理文件名"
        case "video": "不识别 · 可播放 · 记进度"
        default: "不识别 · 全屏查看 · 按拍摄时间"
        }
    }

    static func chosen(_ kind: String) -> String {
        switch kind {
        case "movie": "自动识别并补齐元数据与图片"
        case "tv": "自动识别季集并补齐元数据与图片"
        case "video": "不识别不刮削，按 NFO / 文件名展示"
        default: "扫描全部图片，按拍摄时间排成相册墙"
        }
    }

    static func defaultName(_ kind: String) -> String {
        switch kind {
        case "movie": "电影库"
        case "tv": "剧集库"
        case "video": "其他"
        default: "相册"
        }
    }

    static func defaults(_ kind: String) -> String {
        switch kind {
        case "movie":
            "实时监控目录变化、扫描后保留丢失记录、为未识别文件生成封面、在首页展示。第一个电影库自动成为默认库；刮削偏好建好后在「编辑库」里设。"
        case "tv":
            "实时监控目录变化、扫描后保留丢失记录、为未识别文件生成封面、在首页展示。第一个剧集库自动成为默认库；刮削偏好建好后在「编辑库」里设。"
        case "video":
            "实时监控目录变化、扫描后保留丢失记录、从视频抓帧生成封面、在首页展示。网络挂载的目录建议建好后到「编辑库 → 扫描与监控」关掉实时监控与抓帧。"
        default:
            "实时监控目录变化、扫描后保留丢失记录、生成缩略图、不在首页展示（批量导入会刷满「最近添加」）。缩略图从原图缩放而来；网络挂载的目录建议建好后到「编辑库 → 扫描与监控」关掉实时监控。"
        }
    }

    /// 新建时的类型是否带识别链（决定有没有收藏范围这一步）
    static func isScraped(_ kind: String) -> Bool { kind != "video" && kind != "photo" }
}

// MARK: - 新建向导

struct ManageCreateLibraryForm: View {
    let onSaved: (API.LibraryView) -> Void

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss

    @State private var step = 1
    @State private var kind: String?
    @State private var name = ""
    @State private var roots: [String] = []
    @State private var regions: [String] = []
    @State private var genres: [Int] = []
    @State private var accessMode = "everyone"
    @State private var adminVisible = true
    @State private var memberIds: [Int] = []
    @State private var busy = false
    @State private var error: String?
    @State private var options: ManageRoutingOptions?

    private var hasScope: Bool { kind.map(ManageKindCard.isScraped) ?? false }
    private var totalSteps: Int { hasScope ? 3 : 2 }
    private var ready: Bool { !name.trimmingCharacters(in: .whitespaces).isEmpty && !roots.isEmpty }
    private var scope: (declared: Bool, text: String)? {
        kind.map { ManageMatchRules.summary(kind: $0, regions: regions, genres: genres, options: options) }
    }

    var body: some View {
        NavigationStack {
            Form {
                stepHeader
                switch step {
                case 1: kindStep
                case 2: basicsStep
                default: scopeStep
                }
                if let error {
                    Section {
                        Text(error).font(.subheadline).foregroundStyle(Theme.danger)
                    }
                }
                Section {
                    // 主按钮随步骤变化：第 2 步对影视库是「下一步」，最后一步保存即扫描
                    Button {
                        primaryAction()
                    } label: {
                        Text(primaryLabel)
                            .font(.body.weight(.semibold))
                            .frame(maxWidth: .infinity)
                    }
                    .discoverProminentButton()
                    .disabled(primaryDisabled)
                    .listRowBackground(Color.clear)
                    .listRowInsets(EdgeInsets())
                    .accessibilityIdentifier("form-primary")
                } footer: {
                    Text("第 \(step) 步，共 \(totalSteps) 步" + (step == totalSteps ? " · 保存即开始扫描存量文件" : ""))
                }
            }
            .scrollContentBackground(.hidden)
            .scrollDismissesKeyboard(.interactively)
            .navigationTitle("添加媒体库")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    if step == 1 {
                        Button("取消") { dismiss() }.accessibilityIdentifier("form-cancel")
                    } else {
                        Button("上一步") { step = step == 3 ? 2 : 1 }.accessibilityIdentifier("form-back")
                    }
                }
            }
        }
        .presentationBackground(.regularMaterial)
        .task { options = await ManageRoutingOptions.load(api) }
    }

    /// 步骤条：影视库三步，其他 / 图片库两步
    private var stepHeader: some View {
        Section {
            HStack(spacing: 18) {
                ForEach([(1, "1 · 选类型"), (2, "2 · 名称与目录"), (3, "3 · 收藏范围")].filter { $0.0 != 3 || hasScope }, id: \.0) { n, label in
                    VStack(spacing: 6) {
                        Text(label)
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(step == n ? .white : Theme.textFaint)
                        Capsule().fill(step == n ? Theme.accent : .clear).frame(height: 2)
                    }
                    .fixedSize()
                }
            }
            .listRowBackground(Color.clear)
        }
    }

    // MARK: 步骤

    @ViewBuilder
    private var kindStep: some View {
        Section {
            ForEach(ManageKind.all, id: \.self) { k in
                Button {
                    choose(k)
                } label: {
                    VStack(alignment: .leading, spacing: 8) {
                        HStack(spacing: 8) {
                            Image(systemName: ManageKindCard.symbol(k)).foregroundStyle(Theme.accent)
                            Text(ManageKind.label(k)).font(.body.weight(.semibold)).foregroundStyle(.white)
                            Spacer()
                            if kind == k { Image(systemName: "checkmark").foregroundStyle(Theme.accent) }
                        }
                        Text(ManageKindCard.blurb(k)).font(.caption).foregroundStyle(Theme.textMuted)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(ManageKindCard.traits(k)).font(.caption2).foregroundStyle(Theme.textFaint)
                    }
                    .padding(.vertical, 4)
                    .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("form-kind-\(k)")
            }
        } footer: {
            Text("类型决定这个库怎么扫、怎么摆、能不能订阅，创建后不可更改。")
        }
    }

    @ViewBuilder
    private var basicsStep: some View {
        if let kind {
            Section {
                HStack(spacing: 10) {
                    Image(systemName: ManageKindCard.symbol(kind)).foregroundStyle(Theme.accent)
                    Text(ManageKind.label(kind)).font(.body.weight(.semibold))
                    Text(ManageKindCard.chosen(kind)).font(.caption).foregroundStyle(Theme.textMuted).lineLimit(1)
                    Spacer(minLength: 4)
                    Button("换类型") { step = 1 }.font(.caption).buttonStyle(.borderless)
                }
            }
            Section("名称") {
                TextField("如：电影库 / 动漫库", text: $name)
                    .autocorrectionDisabled()
                    .submitLabel(.done)
                    // 回车执行主按钮（同 Web；输入法选词的回车由系统消化，不会走到 onSubmit）
                    .onSubmit { if !primaryDisabled { primaryAction() } }
                    .accessibilityIdentifier("form-name")
            }
            Section {
                ManageRootsEditor(roots: $roots)
            } header: {
                Text("根目录")
            } footer: {
                Text("第一个为主根，新内容落在这里")
            }
            Section("可见范围") {
                ManageAccessEditor(mode: $accessMode, adminVisible: $adminVisible, memberIds: $memberIds)
            }
            Section {
                Label {
                    Text("\(Text("已按推荐值设好：").foregroundStyle(Theme.text).fontWeight(.medium))\(ManageKindCard.defaults(kind))")
                        .font(.caption)
                        .foregroundStyle(Theme.textMuted)
                } icon: {
                    Image(systemName: "info.circle").foregroundStyle(Theme.info)
                }
            }
        }
    }

    @ViewBuilder
    private var scopeStep: some View {
        if let kind {
            Section {
                Text("声明「本库收什么」，订阅与自动入库就会按作品的区域和类型自动选进这个库。\(Text("全部留空也可以").foregroundStyle(Theme.text).fontWeight(.medium))：作为默认库承接所有未命中的作品；订阅时永远可以手动改库。")
                    .font(.footnote)
                    .foregroundStyle(Theme.textMuted)
                ManageScopeEditor(kind: kind, regions: $regions, genres: $genres, options: options)
            } footer: {
                if let scope {
                    Text(scope.declared ? "当前：收 \(scope.text)；其他作品去该类型的默认库。" : "当前：未声明，本库将承接该类型全部未命中的作品。")
                }
            }
        }
    }

    // MARK: 动作

    /// 名称按类型预填；用户已经改过的名字不动
    private func choose(_ next: String) {
        let trimmed = name.trimmingCharacters(in: .whitespaces)
        let untouched = trimmed.isEmpty || (kind.map { name == ManageKindCard.defaultName($0) } ?? false)
        kind = next
        if untouched { name = ManageKindCard.defaultName(next) }
        if !ManageKindCard.isScraped(next) {
            regions = []
            genres = []
        }
        step = 2
    }

    private var primaryLabel: String {
        if step == 1 { return "创建并开始扫描" }
        if step == 2, hasScope { return "下一步：收藏范围" }
        if busy { return "创建中…" }
        if step == 3, let scope, !scope.declared { return "跳过，创建并开始扫描" }
        return "创建并开始扫描"
    }

    private var primaryDisabled: Bool {
        if step == 1 { return true }
        if step == 2, hasScope { return !ready }
        return !ready || busy
    }

    private func primaryAction() {
        if step == 2, hasScope {
            step = 3
        } else {
            Task { await submit() }
        }
    }

    private func submit() async {
        guard let kind, ready, !busy else { return }
        busy = true
        error = nil
        defer { busy = false }
        // 开关全按推荐值：监控开、自动清理关、封面开、章节开、首页展示（图片库默认不上首页）；建好后在编辑里调
        let payload = API.LibraryPayload(
            name: name.trimmingCharacters(in: .whitespaces),
            kind: kind,
            generateThumbnails: true,
            extractChapterImages: true,
            excludeFromHome: kind == "photo",
            accessMode: accessMode,
            adminVisible: adminVisible,
            memberIds: accessMode == "selected" ? memberIds : [],
            rootPaths: roots,
            matchRules: hasScope ? ManageMatchRules.build(
                genres: ManageMatchRules.validGenres(kind: kind, genres: genres, options: options), regions: regions
            ) : [],
            autoClearMissing: false,
            scrapeOverrides: [:],
            realtimeWatch: true
        )
        do {
            let saved = try await api.libraryCreate(body: payload)
            onSaved(saved)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: - 编辑

struct ManageEditLibraryForm: View {
    let libraryId: Int
    let onSaved: (API.LibraryView) -> Void

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss

    @State private var state: Loadable<API.LibraryView> = .loading

    var body: some View {
        NavigationStack {
            AsyncContent(state, retry: load) { library in
                ManageEditLibraryBody(library: library, onSaved: onSaved)
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if state.value == nil {
                    ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                }
            }
        }
        .presentationBackground(.regularMaterial)
        .task { await load() }
    }

    private func load() async {
        await Loadable.load(into: $state) { try await api.libraryGet(libraryId: libraryId) }
    }
}

/// 编辑表单本体：草稿以库的当前配置起步
private struct ManageEditLibraryBody: View {
    let library: API.LibraryView
    let onSaved: (API.LibraryView) -> Void

    enum SectionId: String { case basic, cover, scan, scope, scrape, access }

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss

    @State private var name: String
    @State private var roots: [String]
    @State private var realtimeWatch: Bool
    @State private var autoClearMissing: Bool
    @State private var generateThumbnails: Bool
    @State private var extractChapterImages: Bool
    @State private var excludeFromHome: Bool
    @State private var autoSeriesCollections: Bool
    @State private var accessMode: String
    @State private var adminVisible: Bool
    @State private var memberIds: [Int]
    @State private var regions: [String]
    @State private var genres: [Int]
    @State private var scrapeOverrides: [String: API.JSONValue]
    @State private var customCover: Bool
    /// 展开的分区（同时只开一个）；默认全收起——摘要行已经把现状说清
    @State private var open: SectionId?
    @State private var busy = false
    @State private var error: String?
    @State private var options: ManageRoutingOptions?

    init(library: API.LibraryView, onSaved: @escaping (API.LibraryView) -> Void) {
        self.library = library
        self.onSaved = onSaved
        let parsed = ManageMatchRules.parse(library.matchRules)
        _name = State(initialValue: library.name)
        _roots = State(initialValue: library.rootPaths)
        _realtimeWatch = State(initialValue: library.realtimeWatch)
        _autoClearMissing = State(initialValue: library.autoClearMissing)
        _generateThumbnails = State(initialValue: library.generateThumbnails)
        _extractChapterImages = State(initialValue: library.extractChapterImages)
        _excludeFromHome = State(initialValue: library.excludeFromHome)
        _autoSeriesCollections = State(initialValue: library.autoSeriesCollections)
        _accessMode = State(initialValue: library.accessMode)
        _adminVisible = State(initialValue: library.adminVisible)
        _memberIds = State(initialValue: library.memberIds)
        _regions = State(initialValue: parsed.regions)
        _genres = State(initialValue: parsed.genres)
        _scrapeOverrides = State(initialValue: library.scrapeOverrides)
        _customCover = State(initialValue: library.customCover)
    }

    private var scraped: Bool { library.capabilities.scraped }
    /// 图片库：条目只看不播，缩略图从原图缩放而来（不是抓帧），开关文案随之换
    private var playable: Bool { library.capabilities.playable }
    private var ready: Bool { !name.trimmingCharacters(in: .whitespaces).isEmpty && !roots.isEmpty }
    private var missingFields: [String] {
        [name.trimmingCharacters(in: .whitespaces).isEmpty ? "名称" : nil, roots.isEmpty ? "根目录" : nil].compactMap { $0 }
    }

    var body: some View {
        Form {
            Section {
                HStack(spacing: 6) {
                    Image(systemName: ManageKindCard.symbol(library.kind)).foregroundStyle(Theme.accent)
                    Text(ManageKind.label(library.kind))
                }
                .font(.caption)
                .foregroundStyle(Theme.textMuted)
                .padding(.horizontal, 10).padding(.vertical, 4)
                .background(Color.white.opacity(0.04), in: .capsule)
                .overlay(Capsule().strokeBorder(Theme.line))
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets(top: 0, leading: 4, bottom: 0, trailing: 0))
            }

            section(.basic, title: "基本信息") {
                HStack(spacing: 8) {
                    Text(name.trimmingCharacters(in: .whitespaces).isEmpty ? "未命名" : name).foregroundStyle(Theme.text)
                    Text(roots.first ?? "未设根目录").font(.caption.monospaced()).lineLimit(1).truncationMode(.head)
                    if roots.count > 1 { Text("等 \(roots.count) 个目录").fixedSize() }
                }
            } content: {
                VStack(alignment: .leading, spacing: 6) {
                    Text("名称").font(.subheadline.weight(.medium)).foregroundStyle(Theme.textMuted)
                    TextField("名称", text: $name).autocorrectionDisabled().accessibilityIdentifier("form-name")
                }
                Text("根目录（第一个为主根，新内容落在这里）").font(.subheadline.weight(.medium)).foregroundStyle(Theme.textMuted)
                ManageRootsEditor(roots: $roots)
            }

            section(.cover, title: "封面") {
                Text(customCover ? "自定义封面" : "自动拼贴（库内最近入库的作品）")
            } content: {
                ManageCoverEditor(libraryId: library.id, custom: $customCover)
            }

            section(.scan, title: "扫描与监控") {
                ManageFlow(spacing: 10, lineSpacing: 4) {
                    dot(realtimeWatch, "实时监控")
                    dot(autoClearMissing, "自动清理丢失")
                    dot(generateThumbnails, scraped ? "抓帧补图" : playable ? "抓帧封面" : "缩略图")
                    if playable { dot(extractChapterImages, "章节") }
                    dot(!excludeFromHome, "首页展示")
                    if library.kind != "photo" { dot(autoSeriesCollections, "系列合集") }
                }
            } content: {
                scanSwitches
            }

            if scraped {
                section(.scope, title: "收藏范围") {
                    let scope = ManageMatchRules.summary(kind: library.kind, regions: regions, genres: genres, options: options)
                    Text(scope.declared ? scope.text : "未声明（承接该类型未命中的作品）")
                        .foregroundStyle(scope.declared ? Theme.text : Theme.textFaint)
                } content: {
                    Text("声明「本库收什么」，订阅与自动入库按作品特征自动选进本库；全部留空 = 不声明。")
                        .font(.caption).foregroundStyle(Theme.textFaint)
                    ManageScopeEditor(kind: library.kind, regions: $regions, genres: $genres, options: options)
                }

                section(.scrape, title: "刮削设置") {
                    if let text = scrapeSummary {
                        Text(text).foregroundStyle(Theme.text)
                    } else {
                        Text("跟随全局设置").foregroundStyle(Theme.textFaint)
                    }
                } content: {
                    ManageScrapeOverrides(overrides: $scrapeOverrides)
                }
            }

            // 可见范围放最后：它决定的是「谁能看」，与库怎么扫、收什么无关
            section(.access, title: "可见范围") {
                Text(ManageAccessEditor.summary(mode: accessMode, adminVisible: adminVisible, memberCount: memberIds.count))
                    .foregroundStyle(accessMode == "everyone" ? Theme.text : Theme.warning)
            } content: {
                ManageAccessEditor(mode: $accessMode, adminVisible: $adminVisible, memberIds: $memberIds)
            }

            if let error {
                Section {
                    Text(error).font(.subheadline).foregroundStyle(Theme.danger)
                }
            }
            Section {
            } footer: {
                Text(missingFields.isEmpty ? "类型创建后不可更改" : "还需填写：\(missingFields.joined(separator: "、"))")
            }
        }
        .scrollContentBackground(.hidden)
        .scrollDismissesKeyboard(.interactively)
        .navigationTitle("编辑「\(library.name)」")
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("取消") { dismiss() }.accessibilityIdentifier("form-cancel")
            }
            ToolbarItem(placement: .confirmationAction) {
                Button(busy ? "保存中…" : "保存") { Task { await submit() } }
                    .disabled(!ready || busy)
                    .accessibilityIdentifier("form-save")
            }
        }
        .task { options = await ManageRoutingOptions.load(api) }
    }

    // MARK: 分区

    /// 一个折叠分区：头行是「标题 + 摘要 + 箭头」，展开后内容行接在同一个 Section 里
    @ViewBuilder
    private func section<Summary: View, Content: View>(
        _ id: SectionId, title: String,
        @ViewBuilder summary: () -> Summary,
        @ViewBuilder content: () -> Content
    ) -> some View {
        let expanded = open == id
        Section {
            Button {
                withAnimation(.snappy) { open = expanded ? nil : id }
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text(title).font(.body.weight(.semibold)).foregroundStyle(.white)
                        .frame(width: 76, alignment: .leading)
                    summary()
                        .font(.caption)
                        .foregroundStyle(Theme.textMuted)
                        .lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Image(systemName: "chevron.down")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.textFaint)
                        .rotationEffect(.degrees(expanded ? 180 : 0))
                }
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("form-section-\(id.rawValue)")
            .accessibilityAddTraits(expanded ? .isSelected : [])
            if expanded {
                content()
            }
        }
    }

    private func dot(_ on: Bool, _ text: String) -> some View {
        HStack(spacing: 5) {
            Circle().fill(on ? Theme.success : Color.white.opacity(0.25)).frame(width: 6, height: 6)
            Text(text)
        }
    }

    @ViewBuilder
    private var scanSwitches: some View {
        ManageSwitchRow(
            title: "实时监控目录变化",
            detail: library.networkMount
                ? "检测到根路径在网络挂载（NFS/SMB）上：这里收不到远端的变化通知，开着也不会建监听。新文件由定期对账发现（可在「设置 → 应用 → 定时任务」调周期，增量对账通常只需几秒）。"
                : "新文件落盘后自动增量扫描入库。SMB/NFS 等网络挂载收不到远端变化通知、建立监听还很慢，建议关闭；关闭后由定期对账和手动扫描发现新文件，不实时但不会缺失。",
            isOn: $realtimeWatch, identifier: "form-switch-watch"
        )
        ManageSwitchRow(
            title: "扫描后自动清理丢失记录",
            detail: "自己在磁盘上删了片子后，扫描结束即把这些记录清出台账。只删记录、不动磁盘，但记录删了不可恢复——「缺失」清单里的「重新下载」也会随之消失。关闭时记录保留，文件回归自动恢复；目录读不动的那一轮不会清理。",
            isOn: $autoClearMissing, identifier: "form-switch-clear-missing"
        )
        ManageSwitchRow(
            title: scraped ? "缺图时从视频抓帧补图" : playable ? "从视频抓帧生成封面" : "生成缩略图",
            detail: scraped
                ? "未识别文件的封面、以及 TMDB 没有剧照的分集，从视频本身抓一帧顶上；TMDB 有图时始终用 TMDB 的。网络挂载库抓帧需要读取每个文件，介意流量可关闭，关闭后显示占位图。"
                : playable
                ? "没有在线海报的内容从视频本身抓一帧当封面：优先用同名图片或内嵌封面，没有再抓帧。网络挂载库抓帧需要读取每个文件，介意流量可关闭，关闭后显示占位图。"
                : "把原图缩到长边 720 像素当相册墙上的缩略图，网格只加载缩略图。关闭后墙会直接加载原图，大照片会很慢；只有网络挂载的大库介意流量时才建议关。",
            isOn: $generateThumbnails, identifier: "form-switch-thumbnails"
        )
        // 章节是视频的事：图片库（不可播）没有这一项
        if playable {
            ManageSwitchRow(
                title: "生成章节",
                detail: "每个视频按章节（有内嵌章节用内嵌，没有按时长切成 3～12 段）各抓一张画面：条目页出「章节」横排、点一张从那里开始播，Infuse 等播放器也能按章节跳转。扫描后在后台低优先级生成，每个文件要定位读取若干次，网络挂载的大库介意读取量可关闭；关闭后已生成的图保留。",
                isOn: $extractChapterImages, identifier: "form-switch-chapters"
            )
        }
        ManageSwitchRow(
            title: "在首页展示",
            detail: "关闭后首页「最近添加」与 Jellyfin 客户端的「最新媒体」都跳过这个库；库卡片仍在，进库内看照常。",
            isOn: Binding(get: { !excludeFromHome }, set: { excludeFromHome = !$0 }), identifier: "form-switch-home"
        )
        // 系列是「作品的属性」：影视库来自 TMDB，其他库来自视频旁 NFO 的 <set>；照片没有系列
        if library.kind != "photo" {
            ManageSwitchRow(
                title: "按作品系列自动生成合集",
                detail: scraped
                    ? "《哈利·波特》《碟中谍》这类系列会在合集页自动成集，按上映顺序排列，还能看出你缺哪几部、一键去补。这只是展示偏好：关掉之后系列信息照常入库、NFO 里照常写，Kodi/Emby 那边不受影响，只是我们的合集页不再自动多出几十个系列；重新打开会把已有的补齐，不会重新刮削。"
                    : "视频旁的 NFO 里写了系列（<set>）的，会在合集页按系列自动成集。这只是展示偏好：关掉之后系列信息照常读取，只是合集页不再自动生成；重新打开会把已有的补齐。改了 NFO 之后，在库的 ⋯ 菜单里点「重新读取 NFO 与封面」生效。",
                isOn: $autoSeriesCollections, identifier: "form-switch-series"
            )
        }
    }

    /// 刮削覆盖的摘要：「已覆盖 3 项 · 元数据 · 命名与整理」；键按前缀归到设置页的四个分区
    private var scrapeSummary: String? {
        let keys = scrapeOverrides.keys.sorted()
        guard !keys.isEmpty else { return nil }
        var groups: [String] = []
        for key in keys {
            let group = key.hasPrefix("naming_") ? "命名与整理"
                : key.hasPrefix("mirror_") ? "目录写入"
                : (key.hasPrefix("poster_") || key.hasPrefix("backdrop_") || key.hasPrefix("still_")) ? "图片" : "元数据"
            if !groups.contains(group) { groups.append(group) }
        }
        return "已覆盖 \(keys.count) 项 · \(groups.joined(separator: " · "))"
    }

    private func submit() async {
        guard ready, !busy else { return }
        busy = true
        error = nil
        defer { busy = false }
        let payload = API.LibraryPayload(
            name: name.trimmingCharacters(in: .whitespaces),
            kind: library.kind,
            generateThumbnails: generateThumbnails,
            extractChapterImages: extractChapterImages,
            excludeFromHome: excludeFromHome,
            autoSeriesCollections: autoSeriesCollections,
            accessMode: accessMode,
            adminVisible: adminVisible,
            memberIds: memberIds,
            rootPaths: roots,
            matchRules: scraped ? ManageMatchRules.build(
                genres: ManageMatchRules.validGenres(kind: library.kind, genres: genres, options: options), regions: regions
            ) : [],
            autoClearMissing: autoClearMissing,
            scrapeOverrides: scraped ? scrapeOverrides : [:],
            realtimeWatch: realtimeWatch
        )
        do {
            let saved = try await api.libraryUpdate(libraryId: library.id, body: payload)
            onSaved(saved)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
