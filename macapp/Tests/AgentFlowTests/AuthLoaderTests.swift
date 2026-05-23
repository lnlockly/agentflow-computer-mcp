import XCTest
@testable import AgentFlow

final class AuthLoaderTests: XCTestCase {
    func testLoad_missingFile_returnsNil() {
        let url = URL(fileURLWithPath: "/tmp/agentflow-macapp-tests-missing-\(UUID().uuidString).json")
        XCTAssertNil(AuthLoader.load(from: url))
    }

    func testLoad_validFile_parses() throws {
        let url = URL(fileURLWithPath: "/tmp/agentflow-macapp-tests-\(UUID().uuidString).json")
        let body = #"""
        {"api_key":"af_live_xyz","device_id":"dev42","ws_url":null}
        """#
        try body.write(to: url, atomically: true, encoding: .utf8)
        defer { try? FileManager.default.removeItem(at: url) }

        let auth = AuthLoader.load(from: url)
        XCTAssertEqual(auth?.apiKey, "af_live_xyz")
        XCTAssertEqual(auth?.deviceId, "dev42")
        XCTAssertNil(auth?.wsUrl)
    }

    func testLoad_malformed_returnsNil() throws {
        let url = URL(fileURLWithPath: "/tmp/agentflow-macapp-tests-\(UUID().uuidString).json")
        try "not json".write(to: url, atomically: true, encoding: .utf8)
        defer { try? FileManager.default.removeItem(at: url) }

        XCTAssertNil(AuthLoader.load(from: url))
    }
}
