import Foundation

// MARK: - 网络层

enum API {
    static let base = "https://example.com/dianming/api"

    static func url(_ path: String) -> URL { URL(string: base + path)! }

    static func get<T: Decodable>(_ path: String, token: String, as type: T.Type) async throws -> T {
        var req = URLRequest(url: url(path))
        req.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        req.cachePolicy = .reloadIgnoringLocalCacheData
        req.timeoutInterval = 20
        let (data, resp) = try await URLSession.shared.data(for: req)
        try check(resp, data)
        return try JSONDecoder().decode(T.self, from: data)
    }

    static func post<T: Decodable>(_ path: String, body: [String: Any], token: String? = nil,
                                   as type: T.Type) async throws -> T {
        var req = URLRequest(url: url(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let token { req.setValue("Bearer " + token, forHTTPHeaderField: "Authorization") }
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        req.timeoutInterval = 30
        let (data, resp) = try await URLSession.shared.data(for: req)
        try check(resp, data)
        return try JSONDecoder().decode(T.self, from: data)
    }

    private static func check(_ resp: URLResponse, _ data: Data) throws {
        guard let http = resp as? HTTPURLResponse else { return }
        guard (200..<300).contains(http.statusCode) else {
            let msg = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])??["error"] as? String
            throw APIError.server(http.statusCode, msg ?? "服务端错误")
        }
    }
}

enum APIError: LocalizedError {
    case server(Int, String)
    var errorDescription: String? {
        if case let .server(code, msg) = self { return "\(msg)（\(code)）" }
        return nil
    }
}

// MARK: - 模型

struct LoginResp: Decodable {
    let ok: Bool
    let access_token: String
    let label: String
    let account: String
    let roster_version: Int?
}

struct RosterResp: Decodable {
    let label: String
    let roster_version: Int
    let dormData: [DormRow]
}

struct DormRow: Decodable, Identifiable {
    let department: String
    let className: String
    let room: String
    let students: [Bed]

    var id: String { room }
    var occupied: [Bed] { students.filter { !$0.name.isEmpty } }
}

struct Bed: Decodable, Hashable {
    let name: String
    let `class`: String

    /// 只显示人名，不带班级（表格整齐优先）
    func display(mainClass: String) -> String { name }
}

struct UploadResp: Decodable {
    let ok: Bool
    let saved: String?
}

struct AbsentItem: Identifiable, Hashable {
    let room: String
    let bed: Int
    let name: String
    let className: String

    /// 唯一标识：同房间不同人必须可区分
    var id: String { "\(room)#\(bed)" }
}

struct SimpleResp: Decodable {
    let ok: Bool
    let error: String?
    let skipped: Bool?
    let log: [String]?
}

struct WriteNowResp: Decodable {
    let ok: Bool
    let error: String?
    let skipped: Bool?
    let log: [String]?
}

// MARK: - 本地会话

struct Session: Codable {
    let token: String
    let label: String
    let account: String
    let building: String
    let floor: Int
}

enum Store {
    private static let sessionKey = "dm.session.v2"

    static func save(_ s: Session) {
        if let d = try? JSONEncoder().encode(s) { UserDefaults.standard.set(d, forKey: sessionKey) }
    }
    static func load() -> Session? {
        guard let d = UserDefaults.standard.data(forKey: sessionKey) else { return nil }
        return try? JSONDecoder().decode(Session.self, from: d)
    }
    static func clear() { UserDefaults.standard.removeObject(forKey: sessionKey) }
}

extension DormRow {
    func toCached() -> CachedRow {
        CachedRow(department: department, className: className, room: room,
                  students: students.map { CachedBed(name: $0.name, cls: $0.class) })
    }
}

extension CachedRow {
    func toDormRow() -> DormRow {
        DormRow(department: department, className: className, room: room,
                students: students.map { Bed(name: $0.name, class: $0.cls) })
    }
}
