import SwiftUI

// MARK: - 入口

@main
struct DormRollApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView().environmentObject(model)
        }
    }
}

struct RootView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            if let s = model.session {
                RollCallView(session: s)
            } else {
                FloorPickerView()
            }
        }
        .task { await model.checkUpdate() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await model.checkUpdate() } }
        }
        .alert("发现新版本 \(model.updateAvailable?.version ?? "")",
               isPresented: Binding(get: { model.updateAvailable != nil },
                                    set: { if !$0 { model.updateAvailable = nil } })) {
            if let u = model.updateAvailable?.url {
                Button("立即更新") { UIApplication.shared.open(u) }
            }
            Button("稍后") { model.remindLater() }
            Button("跳过此版本", role: .destructive) { model.skipThisVersion() }
        } message: {
            Text(model.updateAvailable?.notes.isEmpty == false
                 ? (model.updateAvailable?.notes ?? "")
                 : "更新不会丢数据。选「立即更新」会跳到安装页；装好后重新打开即可。")
        }
    }
}

// MARK: - 选层

struct FloorPickerView: View {
    @EnvironmentObject var model: AppModel

    private let floors: [(id: String, label: String, building: String)] = [
        ("b1", "B座一层", "B"), ("b2", "B座二层", "B"), ("b3", "B座三层", "B"),
        ("a2", "A座二层", "A"), ("a3", "A座三层", "A"), ("a4", "A座四层", "A"),
        ("a5", "A座五层", "A")
    ]

    var body: some View {
        ZStack {
            Color(.systemGroupedBackground).ignoresSafeArea()
            VStack(spacing: 20) {
                VStack(spacing: 6) {
                    Text("宿舍点名").font(.system(size: 30, weight: .bold))
                    Text("请选择所在楼层").font(.subheadline).foregroundStyle(.secondary)
                }
                .padding(.top, 40)

                LazyVGrid(columns: [GridItem(.flexible(), spacing: 14), GridItem(.flexible(), spacing: 14)],
                          spacing: 14) {
                    ForEach(floors, id: \.id) { f in
                        Button {
                            Task { await model.login(floor: f.id) }
                        } label: {
                            VStack(spacing: 10) {
                                Image(systemName: f.building == "B" ? "building.2.fill" : "building.columns.fill")
                                    .font(.system(size: 34))
                                    .foregroundStyle(f.building == "B" ? Color.blue : Color.pink)
                                Text(f.label).font(.system(size: 19, weight: .semibold))
                                    .foregroundStyle(.primary)
                            }
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 26)
                            .background(Color(.secondarySystemGroupedBackground),
                                        in: RoundedRectangle(cornerRadius: 18))
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 24)

                if model.loading { ProgressView("进入中…") }
                if let e = model.error {
                    Text(e).font(.footnote).foregroundStyle(.red).multilineTextAlignment(.center)
                        .padding(.horizontal, 24)
                }
                Spacer()

                VStack(spacing: 6) {
                    Button {
                        Task { await model.checkUpdateManually() }
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "arrow.triangle.2.circlepath")
                            Text(model.checkingUpdate ? "检查中…" : "检查更新")
                        }
                        .font(.footnote.weight(.medium))
                    }
                    .disabled(model.checkingUpdate)

                    Text(model.updateCheckNote ?? "v\((Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "?")")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                .padding(.bottom, 18)
            }
        }
    }
}