import SwiftUI

/// 「筛完存为合集」（对应网页 `components/save-as-collection-dialog.tsx`，设计见
/// docs/design/library-filtering.md 4.2）。
///
/// 合集不是一个新概念，是**存好的筛选**——所以这里只做两件事：起个名字，以及问清楚
/// 一件用户真正会在意的事：
///
///   自动收录  ——「以后符合这组条件的片都算」（规则驱动）
///   固定这批  ——「就现在这些，别再变了」（名单驱动）
///
/// 两者的差别在几个月后才显形（多了/少了片），事后无从追查，所以必须在创建这一刻问，
/// 而且用后果的语言问，不写成「smart / manual」让用户自己猜。
/// 「固定这批」由服务端定格（payload.snapshot）：客户端只表达意图，不必把上千个 id 拉下来再传回去。
///
/// 用法：宿主 `.sheet { SaveAsCollectionSheet(libraryId:, filter:, onSaved:) }`；
/// 新建之后宿主应立刻刷新合集 chip 行——用户刚存的合集要马上看得见。
struct SaveAsCollectionSheet: View {
    let libraryId: Int
    /// 要存下来的那组条件（即此刻墙上生效的筛选）
    let filter: LibraryFilter
    var onSaved: (API.CollectionView) -> Void = { _ in }

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(\.dismiss) private var dismiss

    @State private var facets: API.LibraryFacetsView?
    @State private var name = ""
    /// 用户动过输入框没有：没动过就跟着建议名走，动过就不再覆盖他的输入
    @State private var touched = false
    @State private var snapshot = false
    @State private var privateOnly = false
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField(
                        "名字",
                        text: Binding(get: { name }, set: { touched = true; name = $0 }),
                        prompt: Text(suggested.isEmpty ? "合集名" : suggested)
                    )
                    .submitLabel(.done)
                } header: {
                    Text("名字")
                } footer: {
                    Text(total.map { "当前条件命中 \($0) 部" } ?? "正在数当前条件命中多少部…")
                }

                Section {
                    modeOption(
                        selected: !snapshot,
                        title: "自动收录",
                        detail: "以后新入库的片，只要符合这组条件就会自己进来。"
                    ) { snapshot = false }
                    modeOption(
                        selected: snapshot,
                        title: total.map { "固定现在这 \($0) 部" } ?? "固定现在这批",
                        detail: "就留下此刻这些，之后不再变化。"
                    ) { snapshot = true }
                }

                Section {
                    Toggle("只有我可见", isOn: $privateOnly)
                }
            }
            .scrollContentBackground(.hidden)
            .navigationTitle("存为合集")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "保存中…" : "存为合集") {
                        Task { await submit() }
                    }
                    .disabled(saving)
                }
            }
        }
        .presentationDetents([.medium, .large])
        // 命中数与取值的中文名都来自 facet：与筛选条上是同一份口径，
        // 这里写「命中 42 部」而墙上是 39 部这种事不可能发生
        .task(id: filter) {
            facets = try? await api.libraryFacetsFiltered(libraryId: libraryId, filter: filter, allTiers: true)
        }
        .onChange(of: suggested, initial: true) { _, next in
            if !touched { name = next }
        }
    }

    private var total: Int? { facets?.total }

    /// 建议名：把条件本身念出来（「动画 · 日本」），比「新建合集 3」有用得多。
    ///
    /// **认不出的取值直接跳过**，绝不把裸值填进输入框：facet 还没到时它是「878」「JP」，
    /// 用户手一快就会存下一个叫「878」的合集。跳过的结果是建议名先空着，facet 一到自己补上。
    private var suggested: String {
        func label(_ pool: [API.FacetValueView]?, _ value: String) -> String? {
            pool?.first { $0.value == value }?.label
        }
        var parts: [String?] = []
        parts += filter.genres.map { label(facets?.genres, String($0)) }
        parts += filter.countries.map { label(facets?.countries, $0) }
        parts += filter.decades.map { label(facets?.decades, $0) }
        if let watch = filter.watch { parts.append(label(facets?.watch, watch)) }
        if let rating = filter.ratingGte { parts.append("\(LibraryFilter.ratingString(rating)) 分以上") }
        // 画质的取值本身就是人话（"2160p"），不必等 facet
        parts += filter.resolutions.map { Optional($0) }
        return parts.compactMap { $0 }.prefix(3).joined(separator: " · ")
    }

    /// 两种形态各占一整行：标题说做法、副行说后果——差别在几个月后才显形，得说清楚。
    private func modeOption(selected: Bool, title: String, detail: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.body.weight(.medium)).foregroundStyle(Theme.text)
                    Text(detail).font(.subheadline).foregroundStyle(Theme.textFaint)
                }
                Spacer(minLength: 0)
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(selected ? Theme.accentStrong : Theme.textFaint)
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private func submit() async {
        let finalName = name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? suggested
            : name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !finalName.isEmpty else {
            feedback.error("给这个合集起个名字")
            return
        }
        saving = true
        defer { saving = false }
        do {
            let created = try await api.collectionCreate(body: API.CollectionPayload(
                name: finalName,
                libraryId: libraryId,
                rules: filter.rules,
                visibility: privateOnly ? "private" : "household",
                snapshot: snapshot
            ))
            feedback.success("已存为合集「\(created.name)」")
            onSaved(created)
            dismiss()
        } catch {
            feedback.error(error)
        }
    }
}
