import SwiftUI

/// 登录 / 首次初始化（创建超级管理员）页。对应 Web 端 /login 与 /setup。
struct LoginView: View {
    enum Mode {
        /// 服务器已初始化：用自己的账号密码登录
        case login
        /// 服务器全新：创建超级管理员（全生命周期仅一次）
        case setup
        /// 已登录状态下添加另一个账号（Web /login?add=1），成功后切换过去
        case addAccount
    }

    let mode: Mode

    /// UI 自动化测试时关掉系统密码自动填充：「存储密码？」弹层会挡住后续操作
    private static let autofill = !ProcessInfo.processInfo.arguments.contains("--ui-testing")

    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var username = ""
    @State private var password = ""
    @State private var confirm = ""
    /// 「30 天内记住我」默认不勾（同 Web 登录页；不勾时会话 7 天有效）
    @State private var remember = false
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                if mode != .addAccount, let launchError = model.launchError {
                    Section {
                        Label(launchError, systemImage: "wifi.exclamationmark")
                            .foregroundStyle(.orange)
                    }
                }

                Section {
                    TextField(mode == .setup ? "管理员用户名" : "用户名", text: $username)
                        .textContentType(Self.autofill ? .username : nil)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .accessibilityIdentifier("login-username")
                    SecureField(mode == .setup ? "密码（至少 8 位）" : "密码", text: $password)
                        .textContentType(Self.autofill ? (mode == .setup ? .newPassword : .password) : nil)
                        .accessibilityIdentifier("login-password")
                    if mode == .setup {
                        SecureField("确认密码", text: $confirm)
                            .textContentType(Self.autofill ? .newPassword : nil)
                    }
                } header: {
                    Text(headerText)
                }

                if mode != .setup {
                    Toggle("30 天内记住我", isOn: $remember)
                }

                if let error {
                    Section {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(.red)
                            .accessibilityIdentifier("login-error")
                    }
                }

                Section {
                    Button(action: submit) {
                        HStack {
                            Spacer()
                            if busy { ProgressView() }
                            Text(buttonTitle)
                            Spacer()
                        }
                    }
                    .disabled(busy || username.trimmingCharacters(in: .whitespaces).isEmpty || password.isEmpty || (mode == .setup && confirm.isEmpty))
                    .accessibilityIdentifier("login-submit")
                }

                if mode == .addAccount {
                    Section {
                        Button("取消，回到当前账号") { dismiss() }
                    }
                } else {
                    Section {
                        Button("更换服务器") { model.changeServer() }
                    } footer: {
                        if let server = model.server {
                            Text("当前服务器：\(server.displayString)")
                        }
                    }
                }
            }
            .navigationTitle(mode == .setup ? "初始化" : mode == .addAccount ? "添加账号" : "登录")
            .appBackground()
        }
    }

    /// 表单上方的说明（Web AuthScreen 的副标题，逐字对齐）
    private var headerText: String {
        switch mode {
        case .setup: "欢迎使用。请设置超级管理员账号——它是本站唯一的管理身份，此流程仅在首次部署时出现。"
        case .login: "使用你的 MovieClaw 账号进入。"
        case .addAccount: "登录另一个账号；之后可在「切换账号」里一键切换，不用再输密码。"
        }
    }

    private var buttonTitle: String {
        switch mode {
        case .setup: busy ? "创建中…" : "创建账号并进入"
        case .login: busy ? "登录中…" : "登录"
        case .addAccount: busy ? "登录中…" : "添加并切换"
        }
    }

    private func submit() {
        guard !busy else { return }
        let name = username.trimmingCharacters(in: .whitespaces)
        if mode == .setup {
            // 与 Web /setup 相同的前端校验，后端仍会再校验一次
            if name.count < 3 { error = "用户名至少 3 个字符"; return }
            if password.count < 8 { error = "密码至少 8 位，建议混用字母与数字"; return }
            if password != confirm { error = "两次输入的密码不一致"; return }
        }
        error = nil
        busy = true
        Task {
            defer { busy = false }
            do {
                switch mode {
                case .login, .addAccount: try await model.login(username: name, password: password, remember: remember)
                case .setup: try await model.createAdmin(username: name, password: password)
                }
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
