import Foundation
import Darwin

public enum SocketError: Error, Equatable {
    case unavailable(String)
    case daemon(String)
    case decode(String)
}

/// Sync one-shot client for the daemon's UNIX socket (line-JSON, one req → one resp).
///
/// Wire format matches `agents/socket.py` in agentflow-computer-mcp:
///     { "method": "list" } → { "ok": true, "result": [ ... ] }
public final class SocketClient {
    public static let defaultPath = "/tmp/agentflow.sock"

    private let path: String

    public init(path: String = SocketClient.defaultPath) {
        self.path = path
    }

    /// Returns the parsed `result` payload, or throws.
    public func call(method: String, args: [String: Any] = [:]) throws -> Any {
        var payload: [String: Any] = ["method": method]
        for (k, v) in args { payload[k] = v }
        let data = try JSONSerialization.data(withJSONObject: payload, options: [])
        var line = data
        line.append(0x0a) // \n

        guard FileManager.default.fileExists(atPath: path) else {
            throw SocketError.unavailable("socket not found: \(path)")
        }

        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        if fd < 0 {
            throw SocketError.unavailable("socket(): errno \(errno)")
        }
        defer { Darwin.close(fd) }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = path.utf8CString
        // sockaddr_un.sun_path is a 104-byte tuple on Darwin.
        let maxLen = MemoryLayout.size(ofValue: addr.sun_path)
        if pathBytes.count > maxLen {
            throw SocketError.unavailable("socket path too long")
        }
        withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
            ptr.withMemoryRebound(to: CChar.self, capacity: maxLen) { dst in
                for i in 0..<pathBytes.count {
                    dst[i] = pathBytes[i]
                }
            }
        }

        let connectResult = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { saPtr in
                Darwin.connect(fd, saPtr, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        if connectResult != 0 {
            throw SocketError.unavailable("connect: errno \(errno)")
        }

        // Send.
        try line.withUnsafeBytes { buf in
            var sent = 0
            while sent < buf.count {
                let n = Darwin.send(fd, buf.baseAddress!.advanced(by: sent), buf.count - sent, 0)
                if n <= 0 {
                    throw SocketError.daemon("send: errno \(errno)")
                }
                sent += n
            }
        }

        // Read until \n.
        var response = Data()
        var chunk = [UInt8](repeating: 0, count: 4096)
        while true {
            let n = chunk.withUnsafeMutableBufferPointer { ptr in
                Darwin.recv(fd, ptr.baseAddress, ptr.count, 0)
            }
            if n < 0 { throw SocketError.daemon("recv: errno \(errno)") }
            if n == 0 { break }
            response.append(contentsOf: chunk.prefix(n))
            if response.contains(0x0a) { break }
        }

        return try Self.parseEnvelope(response)
    }

    /// Visible for tests so a fake server can produce raw bytes.
    static func parseEnvelope(_ raw: Data) throws -> Any {
        guard let nlIdx = raw.firstIndex(of: 0x0a) else {
            // Some servers omit the trailing newline; parse the whole thing.
            return try parseEnvelopeJSON(raw)
        }
        return try parseEnvelopeJSON(raw.prefix(upTo: nlIdx))
    }

    private static func parseEnvelopeJSON(_ slice: Data) throws -> Any {
        let obj: Any
        do {
            obj = try JSONSerialization.jsonObject(with: slice, options: [])
        } catch {
            throw SocketError.decode("bad json: \(error)")
        }
        guard let dict = obj as? [String: Any] else {
            throw SocketError.decode("envelope is not an object")
        }
        if let ok = dict["ok"] as? Bool, ok {
            return dict["result"] ?? NSNull()
        }
        let msg = (dict["error"] as? String) ?? "unknown daemon error"
        throw SocketError.daemon(msg)
    }
}

/// High-level helper.
public extension SocketClient {
    func listAgents() throws -> [Agent] {
        let raw = try call(method: "list")
        guard let arr = raw as? [[String: Any]] else { return [] }
        return arr.map { dict in
            Agent(
                id: dict["id"] as? String ?? "",
                name: dict["name"] as? String ?? "",
                persona: dict["persona"] as? String ?? "",
                status: dict["status"] as? String ?? ""
            )
        }
    }
}
