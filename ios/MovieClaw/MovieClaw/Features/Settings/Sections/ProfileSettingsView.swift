import PhotosUI
import SwiftUI

/// 设置 → 个人信息（Web settings-view.tsx 的 ProfileSection）。
///
/// 四块内容与 Web 一一对应：
/// - 账号总览卡：大头像（点按从相册选图 → 压到 512px JPEG → `POST /auth/avatar`）、昵称、身份徽章、用户名；
/// - 账号信息：昵称原地编辑（≤32 字，`PUT /auth/profile`）、用户名只读；
/// - 安全：修改密码（当前 / 新（≥8 位）/ 确认，`PUT /auth/password`，其它设备随即下线）；
/// - 观看历史：清空自己的全部观看记录（二次确认，`DELETE /playback/history?scope=all`）。
///
/// 头像与昵称改完立即写回全局会话（`AppModel.update(session:)`），「更多」面板与头像按钮同步换新。
struct ProfileSettingsView: View {
    @Environment(\.api) private var api
    @Environment(AppModel.self) private var model
    @Environment(Feedback.self) private var feedback

    // 头像
    @State private var avatarItem: PhotosPickerItem?
    @State private var pickingAvatar = false
    @State private var avatarBusy = false
    @State private var avatarError: String?
    // 昵称
    @State private var editingNickname = false
    @State private var nicknameDraft = ""
    @State private var nicknameBusy = false
    @State private var nicknameError: String?
    // 密码
    @State private var oldPassword = ""
    @State private var newPassword = ""
    @State private var confirmPassword = ""
    @State private var passwordBusy = false
    @State private var passwordError: String?
    @State private var passwordDone = false
    // 观看记录
    @State private var clearing = false

    var body: some View {
        if let session = model.session {
            List {
                Section { overviewCard(session) }
                accountSection(session)
                securitySection
                historySection
            }
            .scrollDismissesKeyboard(.interactively)
            .appBackground()
            .photosPicker(isPresented: $pickingAvatar, selection: $avatarItem, matching: .images)
            .onChange(of: avatarItem) { _, item in
                guard let item else { return }
                avatarItem = nil
                Task { await uploadAvatar(item) }
            }
        } else {
            ProgressView()
        }
    }

    // MARK: 账号总览

    private func overviewCard(_ session: API.SessionView) -> some View {
        HStack(spacing: 18) {
            Button { pickingAvatar = true } label: {
                ZStack {
                    AvatarBadge(session: session, size: 72)
                    if avatarBusy {
                        Circle().fill(.black.opacity(0.55))
                        Text("上传中…").font(.caption.weight(.semibold)).foregroundStyle(.white)
                    }
                }
                .frame(width: 72, height: 72)
                .overlay(alignment: .bottomTrailing) {
                    if !avatarBusy {
                        Image(systemName: "camera.fill")
                            .font(.caption2)
                            .foregroundStyle(.black.opacity(0.8))
                            .padding(5)
                            .background(Theme.accentStrong, in: .circle)
                    }
                }
            }
            .buttonStyle(.plain)
            .disabled(avatarBusy)
            .accessibilityLabel("上传头像")
            .accessibilityIdentifier("profile-avatar")

            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Text(session.nickname).font(.title3.weight(.semibold)).lineLimit(1)
                        .accessibilityIdentifier("profile-nickname-display")
                    Text(session.role == "member" ? "成员" : "超级管理员")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.accent)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 2)
                        .background(Theme.accentSoft, in: .capsule)
                        .overlay(Capsule().strokeBorder(Color.white.opacity(0.12)))
                }
                Text("@\(session.username)").font(.subheadline).foregroundStyle(Theme.textMuted)
                if let avatarError {
                    Text(avatarError).font(.footnote).foregroundStyle(Theme.danger)
                }
            }
        }
        .padding(.vertical, 6)
    }

    // MARK: 账号信息

    private func accountSection(_ session: API.SessionView) -> some View {
        Section("账号信息") {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 10) {
                    Text("昵称")
                    Spacer(minLength: 8)
                    if editingNickname {
                        TextField("昵称", text: $nicknameDraft)
                            .multilineTextAlignment(.trailing)
                            .submitLabel(.done)
                            .onSubmit { Task { await saveNickname() } }
                            .onChange(of: nicknameDraft) { _, value in
                                if value.count > 32 { nicknameDraft = String(value.prefix(32)) }
                            }
                            .accessibilityIdentifier("profile-nickname-field")
                    } else {
                        Text(session.nickname).foregroundStyle(Theme.textMuted).lineLimit(1)
                        Button("编辑") {
                            nicknameDraft = session.nickname
                            nicknameError = nil
                            editingNickname = true
                        }
                        .buttonStyle(.glass)
                        .controlSize(.small)
                        .accessibilityIdentifier("profile-nickname-edit")
                    }
                }
                if editingNickname {
                    HStack {
                        Spacer()
                        Button("取消") { editingNickname = false }
                            .buttonStyle(.glass)
                            .disabled(nicknameBusy)
                            .accessibilityIdentifier("profile-nickname-cancel")
                        Button(nicknameBusy ? "保存中…" : "保存") { Task { await saveNickname() } }
                            .settingsProminentButton()
                            .disabled(nicknameBusy)
                            .accessibilityIdentifier("profile-nickname-save")
                    }
                    .controlSize(.small)
                }
                if let nicknameError {
                    Text(nicknameError).font(.footnote).foregroundStyle(Theme.danger)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
            }
            HStack {
                SettingsRowText(title: "用户名", detail: "登录凭证，不可修改")
                Spacer()
                Text(session.username).foregroundStyle(Theme.textMuted)
            }
        }
    }

    // MARK: 安全

    private var securitySection: some View {
        Section("安全") {
            SecureField("当前密码", text: $oldPassword)
                .textContentType(.password)
                .accessibilityIdentifier("profile-old-password")
            SecureField("新密码（至少 8 位）", text: $newPassword)
                .textContentType(.newPassword)
                .accessibilityIdentifier("profile-new-password")
            SecureField("确认新密码", text: $confirmPassword)
                .textContentType(.newPassword)
                .accessibilityIdentifier("profile-confirm-password")
            if let passwordError {
                Text(passwordError).font(.footnote).foregroundStyle(Theme.danger)
            }
            if passwordDone {
                Text("密码已修改，其他设备的登录已全部失效；当前会话保持有效。")
                    .font(.footnote).foregroundStyle(Theme.textMuted)
            }
            HStack {
                Spacer()
                Button(passwordBusy ? "提交中…" : "修改密码") { Task { await changePassword() } }
                    .settingsProminentButton()
                    .disabled(passwordBusy || oldPassword.isEmpty || newPassword.isEmpty || confirmPassword.isEmpty)
                    .accessibilityIdentifier("profile-change-password")
            }
        }
    }

    // MARK: 观看历史

    private var historySection: some View {
        Section("观看历史") {
            HStack(spacing: 12) {
                SettingsRowText(
                    title: "清空全部观看记录",
                    detail: "续播进度、已看标记与播放次数一并清除；单部作品或单个库的记录可在对应页面的 ⋯ 菜单里清"
                )
                Spacer(minLength: 8)
                Button(clearing ? "清空中…" : "清空", role: .destructive) { Task { await clearHistory() } }
                    .buttonStyle(.glass)
                    .tint(Theme.danger)
                    .disabled(clearing)
                    .accessibilityIdentifier("profile-clear-history")
            }
        }
    }

    // MARK: 动作

    /// 选图后：压到 512px JPEG 再上传（头像不需要大图），成功即同步全局会话
    private func uploadAvatar(_ item: PhotosPickerItem) async {
        avatarBusy = true
        avatarError = nil
        defer { avatarBusy = false }
        do {
            guard let data = try await item.loadTransferable(type: Data.self) else {
                avatarError = "读取图片失败"
                return
            }
            guard let jpeg = APIClient.compressedJPEG(data, maxEdge: 512) else {
                avatarError = "图片解码失败，请换一张试试"
                return
            }
            let session = try await api.upload(
                "/auth/avatar",
                file: (name: "file", filename: "avatar.jpg", mimeType: "image/jpeg", data: jpeg),
                as: API.SessionView.self
            )
            model.update(session: session)
        } catch is CancellationError {
        } catch {
            avatarError = error.localizedDescription.isEmpty ? "上传失败，请重试" : error.localizedDescription
        }
    }

    private func saveNickname() async {
        let nickname = nicknameDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !nickname.isEmpty else {
            nicknameError = "昵称不能为空"
            return
        }
        nicknameBusy = true
        nicknameError = nil
        defer { nicknameBusy = false }
        do {
            let session = try await api.authProfileUpdate(body: .init(nickname: nickname))
            model.update(session: session)
            editingNickname = false
        } catch {
            nicknameError = error.localizedDescription
        }
    }

    private func changePassword() async {
        if newPassword.count < 8 {
            passwordError = "新密码至少 8 位"
            return
        }
        if newPassword != confirmPassword {
            passwordError = "两次输入的新密码不一致"
            return
        }
        passwordBusy = true
        passwordError = nil
        passwordDone = false
        defer { passwordBusy = false }
        do {
            let session = try await api.authPasswordUpdate(body: .init(oldPassword: oldPassword, newPassword: newPassword))
            model.update(session: session)
            oldPassword = ""
            newPassword = ""
            confirmPassword = ""
            passwordDone = true
        } catch {
            passwordError = error.localizedDescription
        }
    }

    /// 只删当前登录身份自己的记录；成功提示用后端返回的中文 message（同 Web）
    private func clearHistory() async {
        let ok = await feedback.confirm(
            "清空全部观看记录？",
            message: "所有作品的续播进度、已看标记和播放次数都会清除，首页「最近观看」与播放器的「继续观看」随即清空，无法恢复。只影响你自己的记录；应用更新前的自动备份仍包含历史记录。",
            confirmTitle: "清空",
            destructive: true
        )
        guard ok else { return }
        clearing = true
        defer { clearing = false }
        do {
            let envelope: APIEnvelope<API.PlaybackHistoryClearView> = try await api.raw(
                "DELETE", "/playback/history", query: [URLQueryItem(name: "scope", value: "all")]
            )
            feedback.success(envelope.message ?? "观看记录已清空")
        } catch {
            feedback.error(error)
        }
    }
}
