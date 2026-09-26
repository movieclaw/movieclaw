import SwiftUI

/// 「加入合集」弹层（Web `components/add-to-collection-dialog.tsx`，设计见 docs/design/library-filtering.md F4）。
///
/// **只列名单驱动的合集**：规则驱动的合集成员是条件求值出来的，手工塞进去的片会静默消失
/// （服务端也会拒绝），一个点了会报错的选项比没有这个选项更糟，所以根本不摆出来。
/// 内置与系列合集同样是规则驱动，一并排除。
///
/// 同时给「新建一个合集」：用户多半还没有手动合集，只给一个空列表等于死路。
/// 新建时 `item_ids` 直接带上这一部，新建 + 加入是一次请求。
struct AddToCollectionSheet: View {
    let libraryId: Int
    let mediaItemId: Int
    /// 作品名，只用于文案
    let title: String

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(\.dismiss) private var dismiss

    /// nil = 正在读取；读取失败按空列表处理（同 Web）
    @State private var rows: [API.CollectionView]?
    @State private var name = ""
    @State private var busy = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    if let rows {
                        if rows.isEmpty {
                            Text("还没有手动合集。下面新建一个。")
                                .font(.subheadline)
                                .foregroundStyle(Theme.textFaint)
                                .frame(maxWidth: .infinity)
                        } else {
                            ForEach(rows, id: \.id) { row in
                                Button {
                                    Task { await addTo(row) }
                                } label: {
                                    HStack {
                                        Text(row.name).foregroundStyle(Theme.text).lineLimit(1)
                                        Spacer()
                                        Text("\(row.itemCount) 部").font(.caption).foregroundStyle(Theme.textFaint)
                                    }
                                }
                            }
                        }
                    } else {
                        HStack(spacing: 8) {
                            ProgressView()
                            Text("正在读取合集…").font(.subheadline).foregroundStyle(Theme.textFaint)
                        }
                        .frame(maxWidth: .infinity)
                    }
                } header: {
                    Text("手动合集不会自动收录新片，加进去的就是你挑的那些。")
                        .textCase(nil)
                }

                Section {
                    HStack(spacing: 10) {
                        TextField("新建合集，取个名字", text: $name)
                            .submitLabel(.done)
                            .onSubmit { Task { await createAndAdd() } }
                        Button("新建并加入") { Task { await createAndAdd() } }
                            .buttonStyle(.glass)
                            .disabled(busy || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                }
            }
            .disabled(busy)
            .scrollContentBackground(.hidden)
            .navigationTitle("加入合集")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
            }
        }
        .presentationDetents([.medium, .large])
        .interactiveDismissDisabled(busy)
        .task { await load() }
    }

    private func load() async {
        do {
            let all = try await api.collectionList(libraryId: libraryId, includeEmpty: true)
            // 名单驱动 = 不会自己长的那些
            rows = all.filter { !$0.ruleDriven }
        } catch {
            rows = []
        }
    }

    private func addTo(_ collection: API.CollectionView) async {
        busy = true
        do {
            _ = try await api.collectionItemsAdd(
                collectionId: collection.id,
                body: API.CollectionItemsPayload(mediaItemIds: [mediaItemId])
            )
            feedback.success("《\(title)》已加入「\(collection.name)」")
            dismiss()
        } catch {
            feedback.error(error)
            busy = false
        }
    }

    private func createAndAdd() async {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !busy else { return }
        busy = true
        do {
            let created = try await api.collectionCreate(
                body: API.CollectionPayload(name: trimmed, libraryId: libraryId, itemIds: [mediaItemId])
            )
            feedback.success("已新建「\(created.name)」并加入《\(title)》")
            dismiss()
        } catch {
            feedback.error(error)
            busy = false
        }
    }
}
