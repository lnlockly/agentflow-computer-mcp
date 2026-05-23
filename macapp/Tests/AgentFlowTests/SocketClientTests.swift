import XCTest
import Darwin
@testable import AgentFlow

/// Spins up a real AF_UNIX socket bound to a temp path, accepts one client,
/// reads one line of JSON, writes back a canned response line. The client
/// under test is then pointed at that path.
final class FakeUnixServer {
    let path: String
    private var listenFd: Int32 = -1
    private var acceptThread: Thread?
    var receivedRequest: String?
    var responseProducer: (String) -> String

    init(path: String, responseProducer: @escaping (String) -> String) {
        self.path = path
        self.responseProducer = responseProducer
    }

    func start() throws {
        unlink(path)
        listenFd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        XCTAssertGreaterThanOrEqual(listenFd, 0, "socket() failed: \(errno)")

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = path.utf8CString
        let maxLen = MemoryLayout.size(ofValue: addr.sun_path)
        guard pathBytes.count <= maxLen else { throw NSError(domain: "test", code: 1) }
        withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
            ptr.withMemoryRebound(to: CChar.self, capacity: maxLen) { dst in
                for i in 0..<pathBytes.count {
                    dst[i] = pathBytes[i]
                }
            }
        }

        let bindRes = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { saPtr in
                Darwin.bind(listenFd, saPtr, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        XCTAssertEqual(bindRes, 0, "bind() failed: \(errno)")
        XCTAssertEqual(Darwin.listen(listenFd, 4), 0, "listen() failed: \(errno)")

        let thread = Thread { [weak self] in
            guard let self else { return }
            let clientFd = Darwin.accept(self.listenFd, nil, nil)
            if clientFd < 0 { return }
            defer { Darwin.close(clientFd) }

            // Read until newline.
            var buf = [UInt8](repeating: 0, count: 4096)
            var got = Data()
            while true {
                let n = buf.withUnsafeMutableBufferPointer { ptr in
                    Darwin.recv(clientFd, ptr.baseAddress, ptr.count, 0)
                }
                if n <= 0 { break }
                got.append(contentsOf: buf.prefix(n))
                if got.contains(0x0a) { break }
            }
            let req = String(data: got, encoding: .utf8) ?? ""
            self.receivedRequest = req
            let resp = self.responseProducer(req) + "\n"
            let respBytes = Array(resp.utf8)
            _ = respBytes.withUnsafeBufferPointer { ptr in
                Darwin.send(clientFd, ptr.baseAddress, ptr.count, 0)
            }
        }
        thread.start()
        acceptThread = thread
    }

    func stop() {
        if listenFd >= 0 { Darwin.close(listenFd); listenFd = -1 }
        unlink(path)
    }
}

final class SocketClientTests: XCTestCase {
    private var tempPath: String!
    private var server: FakeUnixServer!

    override func setUp() {
        super.setUp()
        // AF_UNIX path limit is 104 bytes on Darwin — keep it short.
        tempPath = "/tmp/af-mac-\(UUID().uuidString.prefix(8)).sock"
    }

    override func tearDown() {
        server?.stop()
        super.tearDown()
    }

    func testListAgents_decodesResult() throws {
        server = FakeUnixServer(path: tempPath, responseProducer: { _ in
            #"""
            {"ok":true,"result":[{"id":"s1","name":"alpha","persona":"writer","status":"idle"}]}
            """#
        })
        try server.start()

        let client = SocketClient(path: tempPath)
        let agents = try client.listAgents()
        XCTAssertEqual(agents.count, 1)
        XCTAssertEqual(agents[0].id, "s1")
        XCTAssertEqual(agents[0].status, "idle")
    }

    func testCall_sendsCorrectMethod() throws {
        server = FakeUnixServer(path: tempPath, responseProducer: { _ in
            #"""
            {"ok":true,"result":[]}
            """#
        })
        try server.start()

        let client = SocketClient(path: tempPath)
        _ = try client.listAgents()
        // Give the accept thread time to settle.
        Thread.sleep(forTimeInterval: 0.05)
        XCTAssertTrue(server.receivedRequest?.contains("\"method\":\"list\"") == true,
                      "got: \(server.receivedRequest ?? "<nil>")")
    }

    func testCall_daemonErrorPropagated() throws {
        server = FakeUnixServer(path: tempPath, responseProducer: { _ in
            #"""
            {"ok":false,"error":"slot not found"}
            """#
        })
        try server.start()

        let client = SocketClient(path: tempPath)
        XCTAssertThrowsError(try client.listAgents()) { err in
            guard case SocketError.daemon(let msg) = err else {
                XCTFail("wrong error: \(err)"); return
            }
            XCTAssertTrue(msg.contains("slot not found"))
        }
    }

    func testCall_missingSocket_unavailable() {
        let missing = "/tmp/af-mac-does-not-exist-\(UUID().uuidString.prefix(6)).sock"
        let client = SocketClient(path: missing)
        XCTAssertThrowsError(try client.listAgents()) { err in
            guard case SocketError.unavailable = err else {
                XCTFail("wrong error: \(err)"); return
            }
        }
    }

    func testParseEnvelope_noTrailingNewline() throws {
        let raw = #"{"ok":true,"result":42}"#.data(using: .utf8)!
        let result = try SocketClient.parseEnvelope(raw) as? Int
        XCTAssertEqual(result, 42)
    }
}
