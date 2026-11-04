import SwiftUI

struct UpdateInfo: Identifiable {
    let id = UUID()
    let version: String
    let notes: String
    let url: URL?
}

// MARK: - 全局状态

@MainActor
final class AppModel: ObservableObject {
    @Published var session: Session?
    @Published var rows: [DormRow] = []
    @Published var loading = false
    @Published var error: String?
    @Published var today = Date()

    /// 点名结果：房号 → 缺勤床位索引集合
    @Published var absent: [String: Set<Int>] = [:]
    /// 四维标记：key = 房号#床号
    @Published var marks: [String: String] = [:]
    @Published var weekendFlag = false
    @Published var lastUpload: String?
    @Published var syncNote: String?      // 同步提示（成功/失败都走这个）
    @Published var offline = false
    @Published var updateAvailable: UpdateInfo?
    @Published var updateCheckNote: String?      // "已是最新 1.5" / "检查中…" / 失败原因
    @Published var checkingUpdate = false
    private let skipKey = "rc.skipUpdateVersion"

    /// 启动时问一次有没有新版（iOS 包由服务器发布，走 OTA 通道）
    func checkUpdate(manual: Bool = false) async {
        do {
            var req = URLRequest(url: API.url("/api/app/latest-ios"))
            req.timeoutInterval = 12
            let (data, _) = try await URLSession.shared.data(for: req)
            let r = (try JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
            guard let v = r["version"] as? String else {
                if manual { updateCheckNote = "服务器没有版本信息" }
                return
            }
            let cur = (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "0"
            guard v.compare(cur, options: .numeric) == .orderedDescending else {
                if manual { updateCheckNote = "已是最新 \(cur)" }
                return
            }
            if UserDefaults.standard.string(forKey: skipKey) == v {
                if manual { updateCheckNote = "已跳到 \(v)，不再提示" }
                return
            }
            let u = (r["manifest_url"] as? String).flatMap { URL(string: $0) }
            updateAvailable = UpdateInfo(version: v, notes: (r["notes"] as? String) ?? "", url: u)
            if manual { updateCheckNote = "发现新版本 \(v)" }
        } catch {
            if manual { updateCheckNote = "检查失败：网络或服务不可用" }
        }
    }

    /// 本次运行不再提示（下次启动照常检查）
    func remindLater() {
        updateAvailable = nil
    }

    /// 跳过这个版本，直到有更新的版本再提示
    func skipThisVersion() {
        if let v = updateAvailable?.version {
            UserDefaults.standard.set(v, forKey: skipKey)
        }
        updateAvailable = nil
    }

    /// 手动检查：带"检查中…"与结果提示
    func checkUpdateManually() async {
        checkingUpdate = true
        updateCheckNote = "检查中…"
        await checkUpdate(manual: true)
        checkingUpdate = false
    }

    init() {
        session = Store.load()
        RosterStore.seedFromBundleIfNeeded()
        if let s = session {
            if let c = RosterStore.load(building: s.building, floor: s.floor) {
                rows = c.rows.map { $0.toDormRow() }
            }
        }
    }

    var absentList: [AbsentItem] {
        var out: [AbsentItem] = []
        for row in rows {
            guard let idxs = absent[row.room] else { continue }
            for i in idxs.sorted() where i < row.students.count {
                let b = row.students[i]
                if !b.name.isEmpty {
                    out.append(AbsentItem(room: row.room, bed: i, name: b.name, className: row.className))
                }
            }
        }
        return out
    }

    var totalOccupied: Int { rows.reduce(0) { $0 + $1.occupied.count } }

    func markKey(_ room: String, _ bed: Int) -> String { "\(room)#\(bed)" }

    // 全选/重置

    func selectAllPresent() { absent = [:]; marks = [:] }

    func selectAllAbsent() {
        var a: [String: Set<Int>] = [:]
        for row in rows {
            for (i, b) in row.students.enumerated() where !b.name.isEmpty {
                a[row.room, default: []].insert(i)
            }
        }
        absent = a
    }

    func resetMarks() { absent = [:]; marks = [:]; lastUpload = nil }

    func q(_ room: String, _ bed: Int) -> String {
        marks[markKey(room, bed)] ?? "DS"
    }

    func setQ(_ room: String, _ bed: Int, _ q: String) {
        marks[markKey(room, bed)] = q
    }

    // MARK: 登录

    func login(floor id: String) async {
        guard !loading else { return }                 // 连点两层只认第一次
        loading = true; error = nil
        defer { loading = false }
        do {
            let resp: LoginResp = try await API.post("/auth/login",
                body: ["id": id, "password": id + "-CHANGE_ME"], as: LoginResp.self)
            let parts = id.dropFirst()
            let b = String(id.prefix(1)).uppercased()
            let f = Int(parts) ?? 1
            let s = Session(token: resp.access_token, label: resp.label, account: resp.account,
                            building: b, floor: f)
            Store.save(s); session = s
            absent = [:]; marks = [:]
            lastUpload = nil; syncNote = nil; offline = false; weekendFlag = false
            // 先显示本地那份，立刻有表可看
            if let c = RosterStore.load(building: b, floor: f) {
                rows = c.rows.map { $0.toDormRow() }
            }
            await loadRoster()
        } catch {
            self.error = error.localizedDescription
        }
    }

    func logout() {
        Store.clear(); session = nil
        rows = []; absent = [:]; marks = [:]; lastUpload = nil
    }

    // MARK: 名册

    /// 软更新：本地优先，联网拉到新版本才替换；接不上网显示本地并提示
    func loadRoster() async {
        guard let s = session else { return }
        loading = true; error = nil
        defer { loading = false }

        // 1) 先把本地那份摆出来，保证任何情况下都有表
        var localVer = 0
        if let c = RosterStore.load(building: s.building, floor: s.floor) {
            rows = c.rows.map { $0.toDormRow() }
            localVer = c.version
        }

        // 2) 拉服务端版本
        do {
            let r: RosterResp = try await API.get("/api/roster", token: s.token, as: RosterResp.self)
            guard session?.token == s.token else { return }        // 中途切层：丢弃这次响应
            guard !r.dormData.isEmpty else {
                syncNote = "服务端名册为空，继续用本地"
                return
            }
            if r.roster_version > localVer || rows.isEmpty {
                let cached = r.dormData.map { $0.toCached() }
                RosterStore.save(RosterCache(building: s.building, floor: s.floor,
                                             version: r.roster_version, savedAt: Date(), rows: cached))
                let changed = !rows.isEmpty && r.roster_version > localVer
                rows = r.dormData
                if changed && !absent.isEmpty {
                    absent = [:]; marks = [:]
            lastUpload = nil; syncNote = nil; offline = false; weekendFlag = false                    // 床位换了人：旧标记一律作废
                    syncNote = "名册已更新，已清除旧标记"
                }
                syncNote = "名册已更新（\(r.dormData.count) 间房）"
            } else {
                syncNote = "名册已是最新"
            }
            offline = false
        } catch {
            let nsErr = error as NSError
            if nsErr.domain == NSURLErrorDomain && nsErr.code == NSURLErrorCancelled {
                return                      // 请求被取消（切层/刷新打断），不是断网，别误报
            }
            let isNetwork = (nsErr.domain == NSURLErrorDomain)
            offline = isNetwork
            if isNetwork {
                syncNote = "网络不通，正在显示本地名册"
            } else {
                syncNote = "服务端返回异常，已用本地名册（\(nsErr.localizedDescription)）"
            }
            if rows.isEmpty {
                self.error = isNetwork ? "本地没有这层的名册，且连不上网"
                                     : "服务端异常且本地无此层名册：\(nsErr.localizedDescription)"
            }
        }
    }

    // MARK: 上传

    func upload() async {
        guard let s = session else { return }
        let list = absentList
        guard !list.isEmpty else { error = "还没有缺勤，先在表上点人"; return }
        let df = DateFormatter()
        df.locale = Locale(identifier: "zh_CN")
        df.dateFormat = "yyyy/M/d HH:mm:ss"     // 带时刻，控制台列表才看得出是哪一次上传
        let payload: [String: Any] = [
            "token": s.token, "type": "marked", "v": 4, "ref": "",
            "weekend": weekendFlag,
            "d": df.string(from: today),
            "n": list.map { [$0.name, $0.room, q($0.room, $0.bed)] }
        ]
        loading = true; error = nil
        defer { loading = false }
        do {
            let r: UploadResp = try await API.post("/upload", body: payload, token: s.token, as: UploadResp.self)
            lastUpload = r.saved ?? "已上传"
            // 本地留档，便于查看/追溯
            HistoryStore.append(HistoryRecord(
                id: UUID(), date: Date(), label: s.label,
                absent: list.map { HistoryAbsent(name: $0.name, room: $0.room, q: q($0.room, $0.bed)) }))
        } catch {
            let e = error as NSError
            if e.domain == NSURLErrorDomain && e.code == NSURLErrorCancelled { return }
            self.error = "上传失败：" + error.localizedDescription
        }
    }

    func writeNow() async {
        guard let s = session else { return }
        loading = true; error = nil
        defer { loading = false }
        do {
            let r: WriteNowResp = try await API.post("/write-now", body: [:], token: s.token, as: WriteNowResp.self)
            if r.skipped == true {
                lastUpload = nil
                self.error = "这次没有写：信箱为空或已有任务在写。请先点「上传」"
            } else if r.ok {
                lastUpload = "已写入共享表"
                HistoryStore.append(HistoryRecord(
                    id: UUID(), date: Date(), label: s.label,
                    absent: absentList.map { HistoryAbsent(name: $0.name, room: $0.room, q: q($0.room, $0.bed)) }))
            } else {
                self.error = "写入未完成：" + (r.error ?? "请稍后重试")
                lastUpload = nil
            }
        } catch {
            let e = error as NSError
            if e.domain == NSURLErrorDomain && e.code == NSURLErrorCancelled { return }
            self.error = "写入失败：" + error.localizedDescription
        }
    }
}