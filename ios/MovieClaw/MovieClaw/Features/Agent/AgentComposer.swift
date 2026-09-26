import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

/// 撰写中的一条消息（输入框的全部状态，由页面持有，便于「改写重问」回填、发送成功后清空）。
///
/// 技能不混在文字里：Web 的 Lexical 编辑器把 `/skill:名字` 渲染成输入框内的原子 chip，
/// 原生输入框做不了行内原子节点，于是把技能拆成输入框上方的 chip 行——发送时按
/// `/skill:a /skill:b 正文` 的 token 形态拼回去，服务端展开逻辑完全一致。
struct AgentDraft {
    var text = ""
    var skills: [String] = []
    var images: [AgentComposerImage] = []

    /// 提交给服务端的正文（token 形态）
    var message: String {
        (skills.map(AgentSkillText.token).joined() + text).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var isEmpty: Bool { message.isEmpty && images.isEmpty }

    /// 回填一条 token 形态的原文（改写重问）
    mutating func load(_ input: String) {
        let parsed = AgentSkillText.parseTokens(input)
        skills = parsed.names
        text = parsed.text
        images = []
    }

    mutating func clear() { self = AgentDraft() }
}

/// 已上传、等待随消息发送的一张图片
struct AgentComposerImage: Identifiable {
    var attachmentId: String
    var name: String
    var preview: UIImage?
    var id: String { attachmentId }
}

/// 输入框（对应 Web `components/composer.tsx`，会话页的 flat 形态）。
///
///   ┌─ 附件托盘（有附件/技能才出现）：缩略图 chip、技能 chip、上传中、错误 ─┐
///   ├─ 多行输入区（最多 6 行，超出框内滚动）；行首或空白后敲「/」弹技能快选 ─┤
///   └─ 工具行：＋（上传图片 / 使用技能） · 模型（菜单里连带调思维链强度）  · 发送/停止 ─┘
///
/// 上传图片的三种来源同 Web 在 iPhone 上 `<input type=file accept=image/*>` 弹出的系统选择：
/// 照片图库 / 拍照 / 选取文件，三者走同一条「压缩 → 上传 → 进托盘」链路，文件名保留原名（压缩后改 .jpg）。
///
/// 能力不可用时整个控件不渲染（没有模型清单就没有模型胶囊），生成中只禁用不卸载，工具行不因运行状态改变布局。
struct AgentComposer: View {
    @Binding var draft: AgentDraft
    var placeholder: String?
    /// 生成中：提交被阻断；配合 onStop 时发送键变为停止键
    var busy = false
    /// 锁定：输入与提交全部禁用（未接入模型、改写提交中）
    var disabled = false
    var imageUpload = true
    var autoFocus = false
    var modelOptions: [API.LlmModelOptionView] = []
    var modelValue: String?
    var onModelChange: (String?) -> Void = { _ in }
    var thinkingValue: String?
    var onThinkingChange: (String?) -> Void = { _ in }
    var onSubmit: () -> Void
    var onStop: (() -> Void)?

    @Environment(\.api) private var api
    @FocusState private var focused: Bool
    @State private var uploading = 0
    @State private var uploadError: String?
    @State private var showsPlusMenu = false
    @State private var showsModelMenu = false
    @State private var pickingPhotos = false
    @State private var photoItems: [PhotosPickerItem] = []
    @State private var capturingPhoto = false
    @State private var importingFiles = false
    /// 「/」快选的技能清单：每次触发现拉（服务端改技能即生效）
    @State private var slashSkills: [API.SkillView] = []
    @State private var slashActive = false
    /// 技能快选里高亮的那一项（外接键盘上下键移动、回车选中，同 Web Lexical typeahead）
    @State private var slashHighlight = 0

    /// 每条消息的图片上限（与服务端 MAX_ATTACHMENTS_PER_MESSAGE 一致）
    static let maxImages = 4

    private var canSubmit: Bool { !disabled && !busy && uploading == 0 && !draft.isEmpty }
    private var showStop: Bool { busy && onStop != nil }
    private var slash: (query: String, range: Range<String.Index>)? {
        disabled ? nil : AgentSkillText.slashQuery(in: draft.text)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if slashActive, let slash {
                slashMenu(query: slash.query)
            }
            tray
            TextField(placeholder ?? (busy ? "生成中，可先输入下一条…" : "随心输入，描述一个新任务…"), text: $draft.text, axis: .vertical)
                .font(.system(size: 17))
                .lineLimit(2 ... 6)
                .focused($focused)
                .disabled(disabled)
                // 外接键盘同 Web：回车发送、Shift+回车换行；技能快选展开时回车选中高亮的技能、上下键移动高亮。
                // 屏幕键盘的换行键仍是换行、发送靠发送键，符合 iOS 多行输入习惯
                .onKeyPress(.return, phases: .down) { press in
                    guard !press.modifiers.contains(.shift) else { return .ignored }
                    if slashActive, let slash {
                        let matches = slashMatches(slash.query)
                        guard !matches.isEmpty else { return .ignored }
                        pickSlash(matches[min(slashHighlight, matches.count - 1)].name)
                        return .handled
                    }
                    submit()
                    return .handled
                }
                .onKeyPress(keys: [.upArrow, .downArrow], phases: .down) { press in
                    guard slashActive, let slash else { return .ignored }
                    let count = slashMatches(slash.query).count
                    guard count > 0 else { return .ignored }
                    let step = press.key == .upArrow ? -1 : 1
                    slashHighlight = (min(slashHighlight, count - 1) + step + count) % count
                    return .handled
                }
                .padding(.horizontal, 16)
                .padding(.top, 14)
                .padding(.bottom, 4)
                .accessibilityIdentifier("agent-composer-input")
            toolRow
        }
        .background(Color.white.opacity(0.05), in: .rect(cornerRadius: 22))
        .overlay(RoundedRectangle(cornerRadius: 22).strokeBorder(Color.white.opacity(0.07)))
        .onAppear { if autoFocus, !disabled { focused = true } }
        .onChange(of: draft.text) { _, text in
            absorbTypedTokens(text)
            let active = AgentSkillText.slashQuery(in: text) != nil
            if active, !slashActive { Task { slashSkills = (try? await api.skillsList()) ?? [] } }
            slashActive = active
            // 查询变了高亮回到第一项
            slashHighlight = 0
        }
        .photosPicker(isPresented: $pickingPhotos, selection: $photoItems, maxSelectionCount: max(1, Self.maxImages - draft.images.count - uploading), matching: .images)
        .onChange(of: photoItems) { _, items in
            guard !items.isEmpty else { return }
            photoItems = []
            addPhotos(items)
        }
        .fileImporter(isPresented: $importingFiles, allowedContentTypes: AgentImageCompressor.acceptedTypes, allowsMultipleSelection: true) { result in
            switch result {
            case let .success(urls): addFiles(urls)
            case let .failure(error): uploadError = error.localizedDescription
            }
        }
        .fullScreenCover(isPresented: $capturingPhoto) {
            AgentCameraPicker { image in
                capturingPhoto = false
                if let image { addCapturedPhoto(image) }
            }
            .ignoresSafeArea()
        }
    }

    // MARK: 附件托盘

    @ViewBuilder
    private var tray: some View {
        if !draft.skills.isEmpty || !draft.images.isEmpty || uploading > 0 || uploadError != nil {
            AgentFlowLayout(spacing: 8) {
                ForEach(draft.skills, id: \.self) { name in
                    HStack(spacing: 4) {
                        Text("⚡ \(name)").font(.system(size: 13)).foregroundStyle(Theme.text)
                        Button {
                            draft.skills.removeAll { $0 == name }
                        } label: {
                            Image(systemName: "xmark").font(.system(size: 9, weight: .bold)).frame(width: 18, height: 18)
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(Theme.textFaint)
                        .accessibilityLabel("移除技能 \(name)")
                    }
                    .padding(.leading, 8).padding(.trailing, 2).padding(.vertical, 4)
                    .background(Theme.accent.opacity(0.14), in: .rect(cornerRadius: 8))
                }
                ForEach(draft.images) { image in
                    HStack(spacing: 8) {
                        Group {
                            if let preview = image.preview {
                                Image(uiImage: preview).resizable().scaledToFill()
                            } else {
                                Color.white.opacity(0.08)
                            }
                        }
                        .frame(width: 28, height: 28)
                        .clipShape(.rect(cornerRadius: 6))
                        Text(image.name).font(.system(size: 13)).foregroundStyle(Theme.textMuted).lineLimit(1).frame(maxWidth: 110, alignment: .leading)
                        Button {
                            draft.images.removeAll { $0.id == image.id }
                        } label: {
                            Image(systemName: "xmark").font(.system(size: 9, weight: .bold)).frame(width: 20, height: 20)
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(Theme.textFaint)
                        .accessibilityLabel("移除图片 \(image.name)")
                    }
                    .padding(4)
                    .background(Color.white.opacity(0.05), in: .rect(cornerRadius: 8))
                    .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.white.opacity(0.06)))
                }
                if uploading > 0 {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.mini)
                        Text("上传中…")
                    }
                    .font(.system(size: 13))
                    .foregroundStyle(Theme.textFaint)
                    .frame(height: 36)
                    .padding(.horizontal, 10)
                    .background(Color.white.opacity(0.05), in: .rect(cornerRadius: 8))
                }
                if let uploadError {
                    Text(uploadError).font(.system(size: 13)).foregroundStyle(Theme.danger).frame(height: 36)
                }
            }
            .padding(.horizontal, 14)
            .padding(.top, 12)
        }
    }

    // MARK: 工具行

    private var toolRow: some View {
        HStack(spacing: 4) {
            Button {
                showsPlusMenu = true
            } label: {
                Image(systemName: "plus").font(.system(size: 20)).frame(width: 44, height: 44).contentShape(.rect)
            }
            .buttonStyle(.plain)
            .foregroundStyle(Theme.textMuted)
            .disabled(disabled)
            .accessibilityLabel("添加图片或技能")
            .accessibilityIdentifier("agent-composer-plus")
            .popover(isPresented: $showsPlusMenu, arrowEdge: .bottom) {
                AgentPlusMenu(imageUpload: imageUpload) { source in
                    showsPlusMenu = false
                    switch source {
                    case .library: pickingPhotos = true
                    case .camera: capturingPhoto = true
                    case .files: importingFiles = true
                    }
                } onPickSkill: { name in
                    showsPlusMenu = false
                    addSkill(name)
                }
                .presentationCompactAdaptation(.popover)
            }

            if !modelOptions.isEmpty {
                let label = AgentCatalog.resolve(modelOptions, ref: modelValue)?.label ?? "模型"
                Button {
                    showsModelMenu = true
                } label: {
                    HStack(spacing: 4) {
                        Text(label).lineLimit(1)
                        Image(systemName: "chevron.down").font(.system(size: 10, weight: .semibold))
                    }
                    .font(.system(size: 13))
                    .padding(.horizontal, 10)
                    .frame(height: 44)
                    .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .foregroundStyle(Theme.textMuted)
                .disabled(disabled)
                .accessibilityLabel("模型与思维链：\(label)")
                .accessibilityIdentifier("agent-model-menu")
                .popover(isPresented: $showsModelMenu, arrowEdge: .bottom) {
                    AgentModelMenu(
                        options: modelOptions,
                        value: modelValue,
                        thinkingValue: thinkingValue,
                        onChange: { ref in
                            onModelChange(ref)
                            showsModelMenu = false
                        },
                        onThinkingChange: onThinkingChange
                    )
                    .presentationCompactAdaptation(.popover)
                }
            }

            Spacer(minLength: 8)

            Button {
                if showStop { onStop?() } else { submit() }
            } label: {
                Group {
                    if showStop {
                        RoundedRectangle(cornerRadius: 3).frame(width: 11, height: 11)
                    } else {
                        Image(systemName: "arrow.right").font(.system(size: 19, weight: .semibold))
                    }
                }
                .frame(width: 44, height: 44)
                .background(Color.white.opacity(0.1), in: .rect(cornerRadius: 12))
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .foregroundStyle(Theme.text)
            .opacity(!showStop && !canSubmit ? 0.4 : 1)
            .disabled(!showStop && !canSubmit)
            .accessibilityLabel(showStop ? "停止生成" : "发送")
            .accessibilityIdentifier(showStop ? "agent-stop" : "agent-send")
        }
        .padding(.horizontal, 8)
        .padding(.bottom, 8)
    }

    // MARK: 「/」技能快选

    /// 快选候选（最多 8 项）
    private func slashMatches(_ query: String) -> [API.SkillView] {
        Array(slashSkills.filter {
            query.isEmpty || $0.name.lowercased().contains(query) || $0.description.lowercased().contains(query)
        }.prefix(8))
    }

    private func slashMenu(query: String) -> some View {
        let matches = slashMatches(query)
        return Group {
            if !matches.isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    Text("使用技能").font(.system(size: 13)).foregroundStyle(Theme.textFaint).padding(.horizontal, 10).padding(.top, 6).padding(.bottom, 2)
                    ScrollView {
                        VStack(alignment: .leading, spacing: 0) {
                            ForEach(Array(matches.enumerated()), id: \.element.name) { index, skill in
                                Button {
                                    pickSlash(skill.name)
                                } label: {
                                    AgentSkillRow(skill: skill)
                                        .background(
                                            index == min(slashHighlight, matches.count - 1) ? Color.white.opacity(0.08) : .clear,
                                            in: .rect(cornerRadius: 10)
                                        )
                                }
                                .buttonStyle(.plain)
                            }
                        }
                    }
                    .frame(maxHeight: 220)
                    .fixedSize(horizontal: false, vertical: true)
                }
                .padding(6)
                .background(Theme.surfaceRaised, in: .rect(cornerRadius: 16))
                .padding(8)
                .accessibilityIdentifier("agent-slash-menu")
            }
        }
    }

    private func pickSlash(_ name: String) {
        if let slash { draft.text.replaceSubrange(slash.range, with: "") }
        addSkill(name)
    }

    private func addSkill(_ name: String) {
        if !draft.skills.contains(where: { $0.lowercased() == name.lowercased() }) { draft.skills.append(name) }
        focused = true
    }

    /// 手敲完整的 `/skill:名字 `（带尾随空格）也收成 chip，与 Lexical 的 token 转换行为一致
    private func absorbTypedTokens(_ text: String) {
        guard text.contains("/skill:"), text.range(of: #"(^|\s)/skill:[A-Za-z0-9._-]+\s"#, options: .regularExpression) != nil else { return }
        var remaining = text
        var names: [String] = []
        while let range = remaining.range(of: #"(^|(?<=\s))/skill:[A-Za-z0-9._-]+[ \t]"#, options: .regularExpression) {
            let token = remaining[range]
            names.append(String(token.dropFirst("/skill:".count).dropLast()))
            remaining.removeSubrange(range)
        }
        guard !names.isEmpty else { return }
        for name in names { addSkill(name) }
        draft.text = remaining
    }

    // MARK: 发送 / 图片

    private func submit() {
        guard canSubmit else { return }
        uploadError = nil
        onSubmit()
    }

    /// 照片图库：优先按文件取（带原文件名），取不到再退回裸数据
    private func addPhotos(_ items: [PhotosPickerItem]) {
        addImages(items.map { item in
            {
                if let file = try? await item.loadTransferable(type: AgentPickedImageFile.self) {
                    return (file.data, file.filename, file.contentType ?? item.supportedContentTypes.first)
                }
                guard let data = try await item.loadTransferable(type: Data.self) else {
                    throw APIError.network("读取图片失败，请换一张再试")
                }
                return (data, nil, item.supportedContentTypes.first)
            }
        })
    }

    /// 「文件」App 选的图片（沙盒外的文件要先申请访问权）
    private func addFiles(_ urls: [URL]) {
        addImages(urls.map { url in
            {
                let scoped = url.startAccessingSecurityScopedResource()
                defer { if scoped { url.stopAccessingSecurityScopedResource() } }
                guard let data = try? Data(contentsOf: url) else {
                    throw APIError.network("读取图片失败，请换一张再试")
                }
                return (data, url.lastPathComponent, UTType(filenameExtension: url.pathExtension))
            }
        })
    }

    /// 相机拍的照片：没有文件名，统一编成 JPEG 后走同一条压缩链
    private func addCapturedPhoto(_ image: UIImage) {
        addImages([{
            guard let data = image.jpegData(compressionQuality: 0.95) else {
                throw APIError.network("读取照片失败，请重拍一张")
            }
            return (data, nil, .jpeg)
        }])
    }

    /// 三种来源共用：超出张数上限先报错，其余逐张读取 → 压缩 → 上传 → 进托盘
    private func addImages(_ loaders: [() async throws -> (Data, String?, UTType?)]) {
        uploadError = nil
        let room = max(0, Self.maxImages - draft.images.count - uploading)
        if loaders.count > room { uploadError = "一条消息最多发送 \(Self.maxImages) 张图片" }
        for load in loaders.prefix(room) {
            uploading += 1
            Task {
                defer { uploading -= 1 }
                do {
                    let (data, filename, contentType) = try await load()
                    let prepared = try AgentImageCompressor.prepare(data, contentType: contentType, filename: filename)
                    let uploaded = try await api.agentUploadAttachment(data: prepared.data, filename: prepared.filename, mimeType: prepared.mimeType)
                    let preview = UIImage(data: prepared.data)
                    if let preview { AgentImagePreviews.images[uploaded.attachmentId] = preview }
                    draft.images.append(AgentComposerImage(attachmentId: uploaded.attachmentId, name: uploaded.name, preview: preview))
                } catch {
                    uploadError = error.localizedDescription
                }
            }
        }
    }
}

/// 上传前压缩（对应 Web `lib/agent-attachments.ts`）：最长边 2048、JPEG 0.85。
/// 视觉模型的有效分辨率就在这个量级，手机原图从几 MB 降到几百 KB；GIF 不压缩（会丢动画帧）。
/// 服务端只收 jpeg/png/gif/webp：HEIC 等其它格式一律转成 JPEG。
///
/// 文件名同 Web：原样上传保留原名；压缩或转码成 JPEG 时换成 `.jpg` 扩展名；没有原名（相机）兜底「图片」。
enum AgentImageCompressor {
    static let maxEdge: CGFloat = 2048
    /// 「选取文件」可选的格式（同 Web `accept`）
    static let acceptedTypes: [UTType] = [.jpeg, .png, .gif, .webP]

    struct Prepared {
        var data: Data
        var filename: String
        var mimeType: String
    }

    static func prepare(_ data: Data, contentType: UTType?, filename: String? = nil) throws -> Prepared {
        let original = filename.flatMap { $0.isEmpty ? nil : $0 }
        /// 原样上传：有原名用原名，没有按类型兜底
        func keep(_ ext: String, _ mime: String) -> Prepared {
            Prepared(data: data, filename: original ?? "图片.\(ext)", mimeType: mime)
        }
        if contentType?.conforms(to: .gif) == true {
            return keep("gif", "image/gif")
        }
        guard let image = UIImage(data: data) else {
            throw APIError.network("不支持的图片格式，请选择 JPG / PNG / WebP / GIF 图片")
        }
        let longest = max(image.size.width * image.scale, image.size.height * image.scale)
        if longest <= maxEdge {
            if contentType?.conforms(to: .jpeg) == true { return keep("jpg", "image/jpeg") }
            if contentType?.conforms(to: .png) == true { return keep("png", "image/png") }
            if contentType?.conforms(to: .webP) == true { return keep("webp", "image/webp") }
        }
        let scale = min(1, maxEdge / longest)
        let size = CGSize(width: (image.size.width * image.scale * scale).rounded(), height: (image.size.height * image.scale * scale).rounded())
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let resized = UIGraphicsImageRenderer(size: size, format: format).image { _ in
            image.draw(in: CGRect(origin: .zero, size: size))
        }
        guard let jpeg = resized.jpegData(compressionQuality: 0.85) else {
            throw APIError.network("图片压缩失败，请换一张再试")
        }
        return Prepared(data: jpeg, filename: jpegName(original), mimeType: "image/jpeg")
    }

    /// 压缩/转码后的文件名：原名去扩展名换成 .jpg（同 Web `name.replace(/\.[^.]+$/, "") + ".jpg"`）
    static func jpegName(_ original: String?) -> String {
        guard let original else { return "图片.jpg" }
        let base = (original as NSString).deletingPathExtension
        return base.isEmpty ? "图片.jpg" : base + ".jpg"
    }
}

/// 照片图库里选中的图片按「文件」取：能拿到原文件名（如 IMG_1234.HEIC），托盘与气泡显示原名
struct AgentPickedImageFile: Transferable {
    var data: Data
    var filename: String
    var contentType: UTType?

    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(importedContentType: .image) { received in
            let url = received.file
            return AgentPickedImageFile(
                data: try Data(contentsOf: url),
                filename: url.lastPathComponent,
                contentType: UTType(filenameExtension: url.pathExtension)
            )
        }
    }
}

/// 相机拍照（UIImagePickerController 的 SwiftUI 包装）；取消返回 nil
struct AgentCameraPicker: UIViewControllerRepresentable {
    let onFinish: (UIImage?) -> Void

    /// 设备有相机且 App 声明了相机用途（Info.plist 的 NSCameraUsageDescription）才提供「拍照」——
    /// 缺用途说明时调起相机会被系统直接终止进程
    static var isAvailable: Bool {
        UIImagePickerController.isSourceTypeAvailable(.camera)
            && Bundle.main.object(forInfoDictionaryKey: "NSCameraUsageDescription") != nil
    }

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let picker = UIImagePickerController()
        picker.sourceType = .camera
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ controller: UIImagePickerController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(onFinish: onFinish) }

    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let onFinish: (UIImage?) -> Void
        init(onFinish: @escaping (UIImage?) -> Void) { self.onFinish = onFinish }

        func imagePickerController(_ picker: UIImagePickerController, didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            onFinish(info[.originalImage] as? UIImage)
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
            onFinish(nil)
        }
    }
}

/// 技能菜单项：⚡ 名字 + 一行描述
struct AgentSkillRow: View {
    let skill: API.SkillView

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("⚡ \(skill.name)").font(.system(size: 15)).foregroundStyle(Theme.text)
            Text(skill.description).font(.system(size: 13)).foregroundStyle(Theme.textMuted).lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 10).padding(.vertical, 7)
        .contentShape(.rect)
    }
}

/// 加号菜单：上传图片（照片图库 / 拍照 / 选取文件）+ 使用技能（技能列表每次展开现拉，与服务端「改技能即生效」一致）
struct AgentPlusMenu: View {
    enum ImageSource { case library, camera, files }

    var imageUpload: Bool
    var onPickImage: (ImageSource) -> Void
    var onPickSkill: (String) -> Void

    @Environment(\.api) private var api
    /// nil = 加载中
    @State private var skills: [API.SkillView]?
    @State private var failed = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                if imageUpload {
                    Text("上传图片").font(.system(size: 13)).foregroundStyle(Theme.textFaint).padding(.horizontal, 10).padding(.top, 4).padding(.bottom, 2)
                    imageSourceRow("照片图库", systemImage: "photo.on.rectangle", source: .library, id: "agent-pick-image")
                    if AgentCameraPicker.isAvailable {
                        imageSourceRow("拍照", systemImage: "camera", source: .camera, id: "agent-pick-camera")
                    }
                    imageSourceRow("选取文件", systemImage: "folder", source: .files, id: "agent-pick-file")
                    Divider().padding(.horizontal, 10).padding(.vertical, 4)
                }
                Text("使用技能").font(.system(size: 13)).foregroundStyle(Theme.textFaint).padding(.horizontal, 10).padding(.top, 4).padding(.bottom, 2)
                if let skills {
                    if skills.isEmpty {
                        Text(failed ? "技能列表加载失败" : "暂无可用技能")
                            .font(.system(size: 13)).foregroundStyle(Theme.textFaint).padding(.horizontal, 10).padding(.vertical, 6)
                    }
                    ForEach(skills, id: \.name) { skill in
                        Button { onPickSkill(skill.name) } label: { AgentSkillRow(skill: skill) }
                            .buttonStyle(.plain)
                    }
                } else {
                    Text("加载中…").font(.system(size: 13)).foregroundStyle(Theme.textFaint).padding(.horizontal, 10).padding(.vertical, 6)
                }
            }
            .padding(6)
        }
        .frame(width: 290)
        .frame(maxHeight: 340)
        .fixedSize(horizontal: false, vertical: true)
        .task {
            do {
                skills = try await api.skillsList()
            } catch {
                skills = []
                failed = true
            }
        }
        .accessibilityIdentifier("agent-plus-menu")
    }

    private func imageSourceRow(_ title: String, systemImage: String, source: ImageSource, id: String) -> some View {
        Button { onPickImage(source) } label: {
            Label(title, systemImage: systemImage)
                .font(.system(size: 15))
                .foregroundStyle(Theme.text)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 10).padding(.vertical, 10)
                .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier(id)
    }
}

/// 模型菜单：一个胶囊管两件事（选模型 + 调思维链强度）。
/// 选模型即收起（终态操作），调强度不收起（滑杆是拖着调的）；换模型时由页面把档位清回默认。
struct AgentModelMenu: View {
    let options: [API.LlmModelOptionView]
    let value: String?
    let thinkingValue: String?
    let onChange: (String?) -> Void
    let onThinkingChange: (String?) -> Void

    var body: some View {
        let current = AgentCatalog.resolve(options, ref: value)
        let stops = AgentThinking.stops(current?.thinkingLevels ?? [])
        VStack(alignment: .leading, spacing: 0) {
            Text("模型").font(.system(size: 13)).foregroundStyle(Theme.textFaint).padding(.horizontal, 10).padding(.top, 4).padding(.bottom, 2)
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(spacing: 0) {
                        ForEach(options, id: \.ref) { option in
                            let selected = option.ref == current?.ref
                            Button {
                                // 选回全局默认项即「默认」（nil）：续聊沿用、设置页改默认后自动跟随
                                onChange(option.isDefault ? nil : option.ref)
                            } label: {
                                HStack {
                                    Text(option.label).lineLimit(1)
                                    Spacer()
                                    if selected { Image(systemName: "checkmark").font(.system(size: 13, weight: .semibold)) }
                                }
                                .font(.system(size: 15))
                                .foregroundStyle(selected ? Theme.text : Theme.textMuted)
                                .padding(.horizontal, 10).padding(.vertical, 8)
                                .contentShape(.rect)
                            }
                            .buttonStyle(.plain)
                            .id(option.ref)
                        }
                    }
                }
                .frame(maxHeight: 224)
                .fixedSize(horizontal: false, vertical: true)
                .onAppear { if let ref = current?.ref { proxy.scrollTo(ref, anchor: .center) } }
            }
            Divider().padding(.horizontal, 10).padding(.vertical, 6)
            switch AgentThinking.shape(stops) {
            case .slider:
                AgentThinkingSlider(stops: stops, value: thinkingValue, onChange: onThinkingChange)
            case .toggle:
                AgentThinkingToggle(stops: stops, value: thinkingValue, onChange: onThinkingChange)
            case .hidden:
                // 不可控的模型：下半空着会让人以为控件丢了，留一行灰字交代
                Text("该模型不支持调节思考强度").font(.system(size: 13)).foregroundStyle(Theme.textFaint).padding(.horizontal, 10).padding(.bottom, 6)
            }
        }
        .padding(6)
        .frame(width: 304)
        .accessibilityIdentifier("agent-model-panel")
    }
}

/// 思维链强度 · 滑杆形态：标题行「思维链强度 高」+「恢复默认 / 由模型自行决定」，轴标签「更快 … 更聪明」，
/// 刻度 = 该模型声明的档位。整条轨道可点可拖，吸附到最近刻度；默认态无滑块。
struct AgentThinkingSlider: View {
    let stops: [String]
    let value: String?
    let onChange: (String?) -> Void

    private let inset: CGFloat = 16

    var body: some View {
        let index = value.flatMap { stops.firstIndex(of: $0) } ?? -1
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text("思维链强度").font(.system(size: 15)).foregroundStyle(Theme.textMuted)
                Text(AgentThinking.label(value)).font(.system(size: 16, weight: .medium)).foregroundStyle(Theme.text)
                Spacer()
                if value != nil {
                    Button("恢复默认") { onChange(nil) }
                        .font(.system(size: 13))
                        .foregroundStyle(Theme.textFaint)
                        .buttonStyle(.plain)
                } else {
                    Text("由模型自行决定").font(.system(size: 13)).foregroundStyle(Theme.textFaint)
                }
            }
            HStack {
                Text("更快")
                Spacer()
                Text("更聪明")
            }
            .font(.system(size: 13))
            .foregroundStyle(Theme.textFaint)
            GeometryReader { proxy in
                let usable = proxy.size.width - inset * 2
                let x = { (i: Int) -> CGFloat in inset + (stops.count > 1 ? usable * CGFloat(i) / CGFloat(stops.count - 1) : usable / 2) }
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.white.opacity(0.06))
                    if index >= 0 {
                        Capsule().fill(Theme.accent.opacity(0.55)).frame(width: x(index))
                    }
                    ForEach(stops.indices, id: \.self) { i in
                        Circle().fill(i <= index ? Color.black.opacity(0.4) : Color.white.opacity(0.25))
                            .frame(width: 6, height: 6)
                            .position(x: x(i), y: proxy.size.height / 2)
                    }
                    if index >= 0 {
                        Circle().fill(.white).frame(width: 28, height: 28)
                            .shadow(color: .black.opacity(0.4), radius: 3, y: 1)
                            .position(x: x(index), y: proxy.size.height / 2)
                    }
                }
                .contentShape(.rect)
                .gesture(DragGesture(minimumDistance: 0).onChanged { drag in
                    guard !stops.isEmpty else { return }
                    let ratio = min(1, max(0, (drag.location.x - inset) / max(usable, 1)))
                    let next = stops[Int((ratio * CGFloat(stops.count - 1)).rounded())]
                    if next != value { onChange(next) }
                })
            }
            .frame(height: 32)
            .accessibilityElement()
            .accessibilityLabel("思维链强度")
            .accessibilityValue(AgentThinking.label(value))
            .accessibilityAdjustableAction { direction in
                let next: Int
                switch direction {
                case .increment: next = min(index + 1, stops.count - 1)
                case .decrement: next = max(index - 1, 0)
                @unknown default: return
                }
                onChange(stops[next])
            }
        }
        .padding(.horizontal, 10)
        .padding(.bottom, 8)
    }
}

/// 思维链强度 · 分段形态（只有一档的模型）：两格「开启（模型默认）/ 关闭」，下方只显示当前格的说明
struct AgentThinkingToggle: View {
    let stops: [String]
    let value: String?
    let onChange: (String?) -> Void

    var body: some View {
        let items = AgentThinking.listItems(stops)
        let current = items.first(where: { $0.level == value }) ?? items[0]
        VStack(alignment: .leading, spacing: 6) {
            Text("思维链").font(.system(size: 15)).foregroundStyle(Theme.textMuted)
            HStack(spacing: 4) {
                ForEach(items) { item in
                    let selected = item.level == value
                    Button { onChange(item.level) } label: {
                        Text(item.label)
                            .font(.system(size: 14))
                            .lineLimit(1)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 7)
                            .foregroundStyle(selected ? Theme.text : Theme.textMuted)
                            .background(selected ? Color.white.opacity(0.14) : .clear, in: .rect(cornerRadius: 9))
                            .contentShape(.rect)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(3)
            .background(Color.white.opacity(0.05), in: .rect(cornerRadius: 12))
            Text(current.description).font(.system(size: 13)).foregroundStyle(Theme.textFaint)
        }
        .padding(.horizontal, 10)
        .padding(.bottom, 8)
    }
}
