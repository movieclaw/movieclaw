import SwiftUI

/// 首次启动：输入服务器页面地址并测试连通性。
struct ServerConnectView: View {
    @Environment(AppModel.self) private var model
    @State private var input = ""
    @State private var connecting = false
    @State private var error: String?
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("http://192.168.1.10:3000", text: $input)
                        .keyboardType(.URL)
                        .textContentType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .focused($focused)
                        .submitLabel(.go)
                        .onSubmit(connect)
                        .accessibilityIdentifier("server-address")
                } header: {
                    Text("服务器地址")
                } footer: {
                    Text(verbatim: "填写在浏览器里打开 MovieClaw 时地址栏中的地址，例如 http://192.168.1.10:3000 或 https://movie.example.com")
                }

                if let error {
                    Section {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(.red)
                            .accessibilityIdentifier("connect-error")
                    }
                }

                Section {
                    Button(action: connect) {
                        HStack {
                            Spacer()
                            if connecting {
                                ProgressView()
                                Text("正在测试连接…")
                            } else {
                                Text("连接")
                            }
                            Spacer()
                        }
                    }
                    .disabled(connecting || input.trimmingCharacters(in: .whitespaces).isEmpty)
                    .accessibilityIdentifier("connect-button")
                }
            }
            .navigationTitle("连接 MovieClaw")
            .onAppear {
                if input.isEmpty, let server = model.server {
                    input = server.displayString
                }
                focused = input.isEmpty
            }
        }
    }

    private func connect() {
        error = nil
        let address: ServerAddress
        do {
            address = try ServerAddress(parsing: input)
        } catch {
            self.error = error.localizedDescription
            return
        }
        connecting = true
        Task {
            defer { connecting = false }
            do {
                try await model.connect(to: address)
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
