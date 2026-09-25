import SwiftUI

/// 切换账号（Web components/account-switcher-dialog.tsx，设计见 docs/design/account-switching.md）。
///
/// 本机登录过的全部账号（后端 `movieclaw_accounts` Cookie 账号袋，最多 5 个）；
/// 点其他账号即切换（不用再输密码），左滑移除；底部「添加账号」与「退出全部账号」。
/// 切换后 RootView 以用户名为 id 重建整棵界面树，不会串到上一个账号的数据。
struct AccountSwitcherSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(Feedback.self) private var feedback
    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss

    @State private var accounts: Loadable<[API.AccountView]> = .loading
    @State private var busy = false
    @State private var addingAccount = false
    private static let maxAccounts = 5

    var body: some View {
        NavigationStack {
            AsyncContent(accounts, retry: load) { list in
                List {
                    Section {
                        ForEach(list, id: \.username) { account in
                            row(account)
                                .swipeActions {
                                    Button("退出", role: .destructive) { Task { await remove(account) } }
                                }
                        }
                    }
                    Section {
                        if list.count < Self.maxAccounts {
                            Button {
                                addingAccount = true
                            } label: {
                                Label("添加账号", systemImage: "person.badge.plus")
                            }
                        }
                        Button(role: .destructive) {
                            Task { await logoutAll() }
                        } label: {
                            Label("退出全部账号", systemImage: "rectangle.portrait.and.arrow.right")
                        }
                    }
                }
                .disabled(busy)
            }
            .navigationTitle("切换账号")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("关闭") { dismiss() } }
            }
            .sheet(isPresented: $addingAccount) {
                LoginView(mode: .addAccount)
            }
        }
        .task { await load() }
    }

    private func row(_ account: API.AccountView) -> some View {
        Button {
            Task { await switchTo(account) }
        } label: {
            HStack(spacing: 12) {
                AvatarBadge(session: nil, avatarUrl: account.avatarUrl, nickname: account.nickname, size: 40)
                VStack(alignment: .leading, spacing: 2) {
                    Text(account.nickname).foregroundStyle(Theme.text)
                    Text("@\(account.username) · \(account.role == "admin" ? "超级管理员" : "成员")")
                        .font(.caption)
                        .foregroundStyle(Theme.textMuted)
                }
                Spacer()
                if account.active {
                    Image(systemName: "checkmark").foregroundStyle(Theme.accentStrong)
                }
            }
        }
        .disabled(account.active)
    }

    private func load() async {
        await Loadable.load(into: $accounts) { try await api.authAccountsList() }
    }

    private func switchTo(_ account: API.AccountView) async {
        busy = true
        defer { busy = false }
        do {
            let session = try await api.authAccountsSwitch(body: .init(username: account.username))
            dismiss()
            model.update(session: session)
        } catch let error as APIError where error.status == 404 {
            feedback.error("「\(account.nickname)」的登录已过期，请重新登录")
            await load()
        } catch {
            feedback.error(error)
        }
    }

    private func remove(_ account: API.AccountView) async {
        let message = account.active
            ? "这是当前账号。退出后本机不再保留它的登录状态，会自动切到其他账号；再回来需要重新输入密码。"
            : "本机将不再保留它的登录状态，再回来需要重新输入密码。账号本身不受影响。"
        guard await feedback.confirm("退出「\(account.nickname)」？", message: message, confirmTitle: "退出", destructive: true) else { return }
        busy = true
        defer { busy = false }
        do {
            let next = try await api.authAccountsRemove(username: account.username)
            if account.active {
                dismiss()
                if let next { model.update(session: next) } else { await model.logout() }
            } else {
                await load()
            }
        } catch {
            feedback.error(error)
        }
    }

    private func logoutAll() async {
        guard await feedback.confirm("退出全部账号？", message: "本机保存的所有账号都将退出登录。", confirmTitle: "全部退出", destructive: true) else { return }
        dismiss()
        await model.logout(all: true)
    }
}
