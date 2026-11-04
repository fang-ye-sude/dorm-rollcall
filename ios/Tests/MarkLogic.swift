import Foundation

// 把纯逻辑抽出来，便于命令行测试（不依赖 UIKit / SwiftUI）

struct BedLite: Equatable {
    let name: String
    let cls: String
}

struct RowLite: Equatable {
    let room: String
    let className: String
    let beds: [BedLite]   // 固定 8

    var occupiedCount: Int { beds.filter { !$0.name.isEmpty }.count }
}

/// 缺勤与四维标记的纯逻辑核心
struct MarkLogic {
    var rows: [RowLite]
    private(set) var absent: [String: Set<Int>] = [:]
    private(set) var marks: [String: String] = [:]

    init(rows: [RowLite]) { self.rows = rows }

    mutating func toggle(room: String, bed: Int) {
        guard let row = rows.first(where: { $0.room == room }), bed < row.beds.count else { return }
        guard !row.beds[bed].name.isEmpty else { return }
        var s = absent[room] ?? []
        if s.contains(bed) {
            s.remove(bed)
            marks.removeValue(forKey: key(room, bed))
        } else {
            s.insert(bed)
        }
        absent[room] = s
    }

    func key(_ room: String, _ bed: Int) -> String { "\(room)#\(bed)" }

    func q(_ room: String, _ bed: Int) -> String { marks[key(room, bed)] ?? "DS" }

    mutating func setQ(_ room: String, _ bed: Int, _ q: String) { marks[key(room, bed)] = q }

    /// 缺勤条目，按 房号#床位 唯一
    func absentItems() -> [(id: String, room: String, bed: Int, name: String)] {
        var out: [(String, String, Int, String)] = []
        for row in rows {
            guard let idxs = absent[row.room] else { continue }
            for i in idxs.sorted() where i < row.beds.count {
                let b = row.beds[i]
                if !b.name.isEmpty { out.append(("\(row.room)#\(i)", row.room, i, b.name)) }
            }
        }
        return out
    }

    /// 一台 iPad 标完、另一台接着改：同人取最新（这里模拟"后写赢"）
    mutating func applyOverlay(_ other: [String: String]) {
        for (k, v) in other { marks[k] = v }
    }
}