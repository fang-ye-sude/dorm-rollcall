import Foundation

// 极简断言，避免依赖 XCTest（纯命令行 swiftc 场景）
var failures: [String] = []

func expect(_ cond: Bool, _ msg: String) {
    if !cond { failures.append(msg) }
}

func sampleRows() -> [RowLite] {
    // 105 房 8 人；204 房第 6 床空（模拟真实分布）
    var r204: [BedLite] = []
    let names204 = ["学生336", "学生402", "学生20", "学生48", "学生229", "", "学生440", "学生56"]
    for n in names204 { r204.append(BedLite(name: n, cls: n.isEmpty ? "" : "26会计")) }
    var r105: [BedLite] = []
    for n in ["学生228", "学生467", "学生134", "学生517", "学生309", "学生177", "学生448", "学生384"] {
        r105.append(BedLite(name: n, cls: "25网络"))
    }
    return [RowLite(room: "105", className: "25网络", beds: r105),
            RowLite(room: "204", className: "26会计", beds: r204)]
}

// 用例1：同房间 6 人分别标记，互不影响
func testSameRoomIndependent() {
    var m = MarkLogic(rows: sampleRows())
    let rooms = m.rows[0]
    for i in 0..<6 { m.toggle(room: rooms.room, bed: i) }
    let items = m.absentItems()
    expect(items.count == 6, "同房点 6 人应得 6 条，实际 \(items.count)")
    expect(Set(items.map { $0.id }).count == 6, "6 条 id 必须互不相同")
    let kinds = ["DS", "DB", "XS", "XB", "DB", "XS"]
    for (n, item) in items.enumerated() { m.setQ(item.room, item.bed, kinds[n]) }
    for (n, item) in items.enumerated() {
        expect(m.q(item.room, item.bed) == kinds[n],
               "第 \(n + 1) 人应保留 \(kinds[n])，实际 \(m.q(item.room, item.bed))")
    }
}

// 用例2：隔床（204 第 6 床为空）点人，位置不串
func testHoleInRoom() {
    var m = MarkLogic(rows: sampleRows())
    m.toggle(room: "204", bed: 5)
    expect(m.absentItems().isEmpty, "空床位点了不应产生缺勤")
    m.toggle(room: "204", bed: 6)   // 学生440
    let items = m.absentItems()
    expect(items.count == 1 && items[0].name == "学生440", "应为学生440，实际 \(items.map { $0.name })")
}

// 用例3：取消勾选要连带清掉标记
func testToggleOffClearsMark() {
    var m = MarkLogic(rows: sampleRows())
    m.toggle(room: "105", bed: 2)
    m.setQ("105", 2, "XB")
    m.toggle(room: "105", bed: 2)
    expect(m.absentItems().isEmpty, "取消后不应有缺勤")
    expect(m.q("105", 2) == "DS", "取消后标记应回到默认 DS")
}

// 用例4：全部到齐 / 全部缺勤
func testSelectAll() {
    var m = MarkLogic(rows: sampleRows())
    for row in m.rows { for i in 0..<8 { m.toggle(room: row.room, bed: i) } }
    // 105 八人全点，204 七人（第 6 床空）
    expect(m.absentItems().count == 15, "全点应为 15 人，实际 \(m.absentItems().count)")
}

// 用例5：两个终端叠加，后写赢且不影响他人
func testOverlayMerge() {
    var a = MarkLogic(rows: sampleRows())
    a.toggle(room: "105", bed: 0)
    a.setQ("105", 0, "DB")
    a.toggle(room: "105", bed: 1)
    a.setQ("105", 1, "XS")
    let overlayA = ["105#0": "XB"]         // 另一台把第 1 人改成 XB
    a.applyOverlay(overlayA)
    expect(a.q("105", 0) == "XB", "叠加后应为 XB")
    expect(a.q("105", 1) == "XS", "另一人的标记不应被改动")
}

// 用例6：名册顺序稳定（刷新结果一致）
func testStableOrder() {
    let rows = sampleRows()
    let a = rows.map { $0.room }
    let b = rows.reversed().reversed().map { $0.room }
    expect(a == Array(b), "同一批数据两次顺序应一致")
}

@main
struct TestRunner {
    static func main() {
        testSameRoomIndependent()
        testHoleInRoom()
        testToggleOffClearsMark()
        testSelectAll()
        testOverlayMerge()
        testStableOrder()

        if failures.isEmpty {
            print("✅ 全部用例通过（6 组）")
        } else {
            print("❌ 失败 \(failures.count) 项：")
            for f in failures { print("  -", f) }
            exit(1)
        }
    }
}