import Foundation

// MARK: - 名册本地库（本层一份，带版本）

struct RosterCache: Codable {
    let building: String
    let floor: Int
    let version: Int
    let savedAt: Date
    let rows: [CachedRow]
}

struct CachedRow: Codable {
    let department: String
    let className: String
    let room: String
    let students: [CachedBed]
}

struct CachedBed: Codable {
    let name: String
    let cls: String
}

enum RosterStore {
    private static var dir: URL {
        let d = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Roster", isDirectory: true)
        if !FileManager.default.fileExists(atPath: d.path) {
            try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        }
        return d
    }

    private static func file(_ building: String, _ floor: Int) -> URL {
        dir.appendingPathComponent("\(building)\(floor).json")
    }

    static func save(_ cache: RosterCache) {
        guard let data = try? JSONEncoder().encode(cache) else { return }
        try? data.write(to: file(cache.building, cache.floor), options: .atomic)
    }

    static func load(building: String, floor: Int) -> RosterCache? {
        guard let data = try? Data(contentsOf: file(building, floor)) else { return nil }
        return try? JSONDecoder().decode(RosterCache.self, from: data)
    }

    /// 预置：首次安装时把包里带的名单落到 Documents（断网也有表）
    static func seedFromBundleIfNeeded() {
        let bundle = Bundle.main
        for b in ["B", "A"] {
            for f in 1...5 {
                let name = "roster_\(b)\(f)f"
                // 支持打平放到 .app/Roster/ 或直接放在根
                let url = bundle.url(forResource: name, withExtension: "json", subdirectory: "Roster")
                    ?? bundle.url(forResource: name, withExtension: "json")
                guard let url = url,
                      let data = try? Data(contentsOf: url) else { continue }
                if load(building: b, floor: f) != nil { continue }
                guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let rooms = obj["rooms"] as? [[String: Any]] else { continue }
                let rows: [CachedRow] = rooms.map { rm -> CachedRow in
                    // 包内名册用 "beds"（固定 8 位，空床为 null）；兼容旧 "students"
                    let raw = (rm["beds"] as? [Any]) ?? (rm["students"] as? [Any]) ?? []
                    var beds: [CachedBed] = []
                    var main = ""
                    for i in 0..<8 {
                        var nm = "", cl = ""
                        if i < raw.count, let d = raw[i] as? [String: Any] {
                            nm = (d["name"] as? String) ?? ""
                            cl = (d["class"] as? String) ?? ""
                        }
                        if main.isEmpty && !nm.isEmpty && !cl.isEmpty { main = cl }
                        beds.append(CachedBed(name: nm, cls: cl))
                    }
                    return CachedRow(department: RosterStore.dept(main), className: main,
                                     room: (rm["room"] as? String) ?? "", students: beds)
                }
                guard rows.contains(where: { $0.students.contains { !$0.name.isEmpty } }) else { continue }
                save(RosterCache(building: b, floor: f, version: 0, savedAt: Date(), rows: rows))
            }
        }
    }

    static func dept(_ cls: String) -> String {
        if ["大数据", "网络", "电商", "直播", "智能", "数媒"].contains(where: { cls.contains($0) }) { return "信息技术系" }
        if ["民", "航空", "民航", "轨道"].contains(where: { cls.contains($0) }) { return "交通服务系" }
        if ["国商", "会计", "商英", "跨境", "会展"].contains(where: { cls.contains($0) }) { return "商务部" }
        if ["社区", "幼保", "养老"].contains(where: { cls.contains($0) }) { return "学前教育系" }
        return ""
    }
}