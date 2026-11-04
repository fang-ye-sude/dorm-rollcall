import SwiftUI

// MARK: - 主页：竖屏房卡 / 横屏表格

struct RollCallView: View {
    let session: Session
    @EnvironmentObject var model: AppModel
    @State private var showMarkSheet = false
    @State private var showHistory = false
    @State private var showAbsentList = false
    @State private var showWriteConfirm = false
    @State private var confirmAllAbsent = false
    @State private var confirmReset = false
    @State private var confirmUpload = false
    @State private var confirmBack = false
    @State private var dragX: CGFloat = 0        // 侧滑跟手位移

    var body: some View {
        GeometryReader { geo in
            let wide = geo.size.width > geo.size.height
            NavigationStack {
                content(wide: wide)
                .navigationTitle(session.label + "点名")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar { toolbar }
                .navigationBarBackButtonHidden(true)
                .safeAreaInset(edge: .bottom) { bottomBar }
                .task { await model.loadRoster() }
                .gesture(
                    DragGesture(minimumDistance: 6)
                        .onChanged { v in
                            guard v.startLocation.x < 64, abs(v.translation.height) < 140 else { return }
                            dragX = max(0, v.translation.width)
                        }
                        .onEnded { v in
                            let w = max(1, geo.size.width)
                            let speed = v.predictedEndTranslation.width - v.translation.width
                            let fast = speed > 120                       // 一甩就回
                            let passed = dragX > w * 0.26                // 拖过约 1/4 屏
                            if v.startLocation.x < 64, abs(v.translation.height) < 140, dragX > 24, (passed || fast) {
                                commitBack(w)
                            } else {
                                withAnimation(.spring(response: 0.28, dampingFraction: 0.86)) { dragX = 0 }
                            }
                        }
                )
                .refreshable { await model.loadRoster() }
                .alert("提示", isPresented: Binding(
                    get: { model.error != nil },
                    set: { if !$0 { model.error = nil } })) {
                    Button("好", role: .cancel) {}
                } message: { Text(model.error ?? "") }
                    .confirmationDialog("还有 \(model.absentList.count) 人标了缺勤，确定返回？",
                                        isPresented: $confirmBack, titleVisibility: .visible) {
                        Button("返回选层", role: .destructive) { model.logout() }
                        Button("继续点名", role: .cancel) {}
                    }
                    .sheet(isPresented: $showMarkSheet) { MarkSheet() }
                    .sheet(isPresented: $showHistory) { HistorySheet() }
                    .sheet(isPresented: $showAbsentList) { AbsentSheet() }
                    .confirmationDialog("上传本次点名（\(model.absentList.count) 人）？", isPresented: $confirmUpload, titleVisibility: .visible) {
                        Button("上传", role: .destructive) { Task { await model.upload() } }
                        Button("取消", role: .cancel) {}
                    } message: {
                        Text(model.absentList.prefix(3).map { $0.name }.joined(separator: "、") + (model.absentList.count > 3 ? " 等" : ""))
                    }
                    .confirmationDialog("把本层所有人标为缺勤？", isPresented: $confirmAllAbsent, titleVisibility: .visible) {
                        Button("全部标缺勤", role: .destructive) { model.selectAllAbsent() }
                        Button("取消", role: .cancel) {}
                    }
                    .confirmationDialog("清空本次点名与四维标记？", isPresented: $confirmReset, titleVisibility: .visible) {
                        Button("重置", role: .destructive) { model.resetMarks() }
                        Button("取消", role: .cancel) {}
                    }
                    .confirmationDialog("确认写入共享表？", isPresented: $showWriteConfirm, titleVisibility: .visible) {
                        Button("写入 \(model.absentList.count) 人缺勤", role: .destructive) {
                            Task { await model.writeNow() }
                        }
                        Button("取消", role: .cancel) {}
                    } message: {
                        Text("将把本层今天的缺勤直接写进钉钉表（\(model.absentList.count) 人），写后不易撤回。")
                    }
            }
            .offset(x: dragX)
            .shadow(color: .black.opacity(dragX > 0 ? 0.18 : 0), radius: 14, x: -6)
        }
    }

    @ViewBuilder
    private func content(wide: Bool) -> some View {
        if model.rows.isEmpty {
            ContentUnavailableView("名册为空", systemImage: "tablecells",
                                   description: Text(model.loading ? "正在加载…" : "该层暂无名单"))
        } else if wide {
            bedTable
        } else {
            roomList
        }
    }

    // MARK: 竖屏：房卡

    private var roomList: some View {
        ScrollView {
            LazyVStack(spacing: 12) {
                statsBar
                ForEach(model.rows) { row in roomCard(row) }
            }
            .padding(.horizontal, 14)
            .padding(.bottom, 10)
        }
    }

    private func roomCard(_ row: DormRow) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(row.room).font(.system(size: 20, weight: .bold)).monospacedDigit()
                Text(row.className.isEmpty ? "空宿舍" : row.className)
                    .font(.subheadline).foregroundStyle(.secondary)
                Spacer()
                let present = row.occupied.count - (model.absent[row.room]?.count ?? 0)
                Text("\(present)/\(row.occupied.count)")
                    .font(.system(size: 15, weight: .semibold)).monospacedDigit()
            }
            if row.occupied.isEmpty {
                Text("空房").font(.footnote).foregroundStyle(.tertiary)
            } else {
                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 8), count: 4), spacing: 8) {
                    ForEach(0..<8, id: \.self) { idx in
                        let bed = idx < row.students.count ? row.students[idx] : Bed(name: "", class: "")
                        bedChip(row: row, index: idx, bed: bed)
                    }
                }
            }
        }
        .padding(12)
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 14))
        .opacity(row.occupied.isEmpty ? 0.55 : 1)
    }

    private func bedChip(row: DormRow, index: Int, bed: Bed) -> some View {
        let isAbsent = model.absent[row.room]?.contains(index) ?? false
        return Button {
            guard !bed.name.isEmpty else { return }
            var s = model.absent[row.room] ?? []
            if s.contains(index) { s.remove(index) } else { s.insert(index) }
            model.absent[row.room] = s
            if isAbsent { model.marks.removeValue(forKey: model.markKey(row.room, index)) }
            UIImpactFeedbackGenerator(style: .rigid).impactOccurred()
        } label: {
            Text(bed.name.isEmpty ? "空" : bed.display(mainClass: row.className))
                .font(.system(size: 14, weight: isAbsent ? .semibold : .regular))
                .foregroundStyle(isAbsent ? Color.white
                                 : (bed.name.isEmpty ? Color.secondary.opacity(0.45) : Color.primary))
                .lineLimit(1)
                .minimumScaleFactor(0.65)
                .frame(maxWidth: .infinity, minHeight: 42)
                .background(isAbsent ? Color.red.opacity(0.85) : Color(.quaternarySystemFill),
                            in: RoundedRectangle(cornerRadius: 8))
                .overlay(alignment: .topLeading) {
                    Text("\(index + 1)").font(.system(size: 9)).foregroundStyle(.tertiary)
                        .padding(.leading, 4).padding(.top, 2)
                }
        }
        .buttonStyle(.plain)
    }

    // MARK: 横屏：表格

    private var bedTable: some View {
        ScrollView([.horizontal, .vertical]) {
            VStack(spacing: 0) {
                headerRow
                ForEach(model.rows) { row in
                    roomRow(row)
                    Divider()
                }
            }
        }
    }

    private var headerRow: some View {
        HStack(spacing: 0) {
            cell("系部", w: 92, bold: true)
            cell("班级", w: 88, bold: true)
            cell("宿舍", w: 56, bold: true)
            ForEach(1...8, id: \.self) { i in cell("\(i)", w: 84, bold: true) }
            cell("到/应", w: 62, bold: true)
        }
        .background(Color(.secondarySystemGroupedBackground))
    }

    private func roomRow(_ row: DormRow) -> some View {
        HStack(spacing: 0) {
            cell(row.department, w: 92)
            cell(row.className, w: 88)
            cell(row.room, w: 56, bold: true)
            ForEach(0..<8, id: \.self) { i in
                let bed = i < row.students.count ? row.students[i] : Bed(name: "", class: "")
                let isAbsent = model.absent[row.room]?.contains(i) ?? false
                Button {
                    guard !bed.name.isEmpty else { return }
                    var s = model.absent[row.room] ?? []
                    if s.contains(i) { s.remove(i) } else { s.insert(i) }
                    model.absent[row.room] = s
                    if isAbsent { model.marks.removeValue(forKey: model.markKey(row.room, i)) }
                } label: {
                    Text(bed.display(mainClass: row.className))
                        .font(.system(size: 15, weight: isAbsent ? .semibold : .regular))
                        .foregroundStyle(isAbsent ? Color.white : Color.primary)
                        .lineLimit(1).minimumScaleFactor(0.7)
                        .frame(width: 84, height: 46)
                        .background(isAbsent ? Color.red.opacity(0.85) : Color.clear)
                }
                .buttonStyle(.plain)
            }
            let present = row.occupied.count - (model.absent[row.room]?.count ?? 0)
            cell("\(present)/\(row.occupied.count)", w: 62)
        }
        .background(row.occupied.isEmpty ? Color(.systemGray6).opacity(0.5) : Color.clear)
    }

    private func cell(_ text: String, w: CGFloat, bold: Bool = false) -> some View {
        Text(text)
            .font(.system(size: 13, weight: bold ? .semibold : .regular))
            .lineLimit(1).minimumScaleFactor(0.7)
            .frame(width: w, height: 46)
    }

    // MARK: 统计

    private var statsBar: some View {
        HStack(spacing: 10) {
            stat("应到", model.totalOccupied, .blue)
            stat("实到", model.totalOccupied - model.absentList.count, .green)
            stat("缺勤", model.absentList.count, .red)
        }
    }

    private func stat(_ title: String, _ value: Int, _ color: Color) -> some View {
        VStack(spacing: 2) {
            Text("\(value)").font(.system(size: 20, weight: .bold, design: .rounded))
                .foregroundStyle(color).monospacedDigit()
            Text(title).font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 10))
    }

    // MARK: 工具栏

    private func commitBack(_ width: CGFloat) {
        if model.absentList.isEmpty {
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
            withAnimation(.easeOut(duration: 0.18)) { dragX = width }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.18) {
                dragX = 0
                model.logout()
            }
        } else {
            withAnimation(.spring(response: 0.3, dampingFraction: 0.82)) { dragX = 0 }
            UIImpactFeedbackGenerator(style: .rigid).impactOccurred()
            confirmBack = true
        }
    }

    private func requestBack() {
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        if model.absentList.isEmpty {
            model.logout()
        } else {
            confirmBack = true
        }
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .topBarLeading) {
            Button { requestBack() } label: {
                Label("返回", systemImage: "chevron.left")
                    .labelStyle(.titleAndIcon)
                    .font(.footnote.weight(.semibold))
            }
        }
        ToolbarItem(placement: .topBarTrailing) {
            Menu {
                Button { Task { await model.loadRoster() } } label: {
                    Label("刷新名册", systemImage: "arrow.clockwise")
                }
                Button { showAbsentList = true } label: {
                    Label("缺勤名单", systemImage: "list.bullet")
                }
                Button { showHistory = true } label: {
                    Label("点名历史", systemImage: "clock.arrow.circlepath")
                }
                Divider()
                Button { model.selectAllPresent() } label: {
                    Label("全部到齐", systemImage: "checkmark.circle")
                }
                Button { confirmAllAbsent = true } label: {
                    Label("全部缺勤", systemImage: "xmark.circle")
                }
                Button(role: .destructive) { confirmReset = true } label: {
                    Label("重置点名", systemImage: "arrow.counterclockwise")
                }
                Divider()
                Toggle("含周末返校统计", isOn: $model.weekendFlag)
            } label: { Image(systemName: "ellipsis.circle") }
        }
    }

    // MARK: 底栏

    private var bottomBar: some View {
        VStack(spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: model.offline ? "wifi.slash" : "checkmark.icloud").font(.caption2)
                Text(model.syncNote ?? (model.loading ? "正在同步…" : "已就绪")).font(.caption2)
                Spacer()
                if let up = model.lastUpload {
                    Text(up).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            .foregroundStyle(model.offline ? Color.orange : Color.secondary)

            HStack(spacing: 10) {
                Button { showMarkSheet = true } label: {
                    Label("标记", systemImage: "square.grid.2x2").font(.system(size: 15, weight: .semibold))
                }
                .buttonStyle(.bordered)
                .disabled(model.absentList.isEmpty)

                Button { confirmUpload = true } label: {
                    Label(model.lastUpload == nil ? "上传" : "重传", systemImage: "arrow.up.circle")
                        .font(.system(size: 15, weight: .semibold))
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.absentList.isEmpty || model.loading)

                Button { showWriteConfirm = true } label: {
                    Text("写入表").font(.system(size: 15, weight: .semibold))
                }
                .buttonStyle(.bordered)
                if model.loading { ProgressView().scaleEffect(0.8) }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(.regularMaterial)
    }
}

// MARK: - 缺勤名单

struct AbsentSheet: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                if model.absentList.isEmpty {
                    Text("本层无人缺勤").foregroundStyle(.secondary)
                }
                ForEach(model.absentList) { item in
                    HStack {
                        Text(item.name)
                        Text(item.room).font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        Text(dimLabel(model.q(item.room, item.bed)))
                            .font(.caption).foregroundStyle(dimColor(model.q(item.room, item.bed)))
                    }
                }
            }
            .navigationTitle("缺勤名单（\(model.absentList.count) 人）")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
        }
    }

    private func dimLabel(_ q: String) -> String {
        switch q {
        case "DS": return "宿管·事假"
        case "DB": return "宿管·病假"
        case "XS": return "部系·事假"
        case "XB": return "部系·病假"
        default: return q
        }
    }

    private func dimColor(_ q: String) -> Color {
        switch q {
        case "DS": return .blue
        case "DB": return .orange
        case "XS": return .purple
        default: return .pink
        }
    }
}

// MARK: - 点名历史（本地留档，可删）

struct HistoryRecord: Codable, Identifiable {
    let id: UUID
    let date: Date
    let label: String
    let absent: [HistoryAbsent]
}

struct HistoryAbsent: Codable, Hashable {
    let name: String
    let room: String
    let q: String
}

enum HistoryStore {
    private static var file: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("history.json")
    }
    static func load() -> [HistoryRecord] {
        guard let d = try? Data(contentsOf: file) else { return [] }
        return (try? JSONDecoder().decode([HistoryRecord].self, from: d)) ?? []
    }
    static func save(_ records: [HistoryRecord]) {
        guard let d = try? JSONEncoder().encode(records) else { return }
        try? d.write(to: file, options: .atomic)
    }
    static func append(_ r: HistoryRecord) {
        var list = load()
        list.insert(r, at: 0)
        if list.count > 60 { list = Array(list.prefix(60)) }
        save(list)
    }
}

struct HistorySheet: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var records: [HistoryRecord] = []

    var body: some View {
        NavigationStack {
            List {
                if records.isEmpty {
                    Text("还没有点名记录").foregroundStyle(.secondary)
                }
                ForEach(records) { r in
                    DisclosureGroup {
                        ForEach(r.absent, id: \.self) { a in
                            HStack {
                                Text(a.name)
                                Text(a.room).font(.caption).foregroundStyle(.secondary)
                                Spacer()
                                Text(a.q).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(r.date.formatted(date: .numeric, time: .shortened))
                                .font(.subheadline).bold()
                            Text("\(r.label) · 缺勤 \(r.absent.count) 人")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                .onDelete { idx in
                    var list = records
                    list.remove(atOffsets: idx)
                    records = list
                    HistoryStore.save(list)
                }
            }
            .navigationTitle("点名历史")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
            .onAppear { records = HistoryStore.load() }
        }
    }
}
// MARK: - 四维标记

struct MarkSheet: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss

    private let dims: [(String, String, Color)] = [
        ("DS", "宿管·事假", .blue),
        ("DB", "宿管·病假", .orange),
        ("XS", "部系·事假", .purple),
        ("XB", "部系·病假", .pink)
    ]

    var body: some View {
        NavigationStack {
            List {
                if model.absentList.isEmpty {
                    Text("还没有缺勤，先在表上点人").foregroundStyle(.secondary)
                }
                Section {
                    ForEach(model.absentList) { item in
                        VStack(alignment: .leading, spacing: 8) {
                            HStack {
                                Text(item.name).font(.headline)
                                Text(item.room).font(.caption).foregroundStyle(.secondary)
                                Spacer()
                                let q = model.q(item.room, item.bed)
                                Text(dims.first { $0.0 == q }?.1 ?? "")
                                    .font(.caption)
                                    .foregroundStyle(dims.first { $0.0 == q }?.2 ?? .gray)
                            }
                            Picker("", selection: Binding(
                                get: { model.q(item.room, item.bed) },
                                set: { model.setQ(item.room, item.bed, $0) })) {
                                ForEach(dims, id: \.0) { d in
                                    Text(d.1).tag(d.0)
                                }
                            }
                            .pickerStyle(.segmented)
                        }
                        .padding(.vertical, 2)
                    }
                } footer: {
                    Text("点选每人请假类型；一次性套用可按右上「全部设为」。")
                }
            }
            .navigationTitle("请假类型（\(model.absentList.count) 人）")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Menu("全部设为") {
                        ForEach(dims, id: \.0) { d in
                            Button(d.1) {
                                for item in model.absentList {
                                    model.setQ(item.room, item.bed, d.0)
                                }
                            }
                        }
                    }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") { dismiss() }
                }
            }
        }
    }
}
