import XCTest
@testable import AgentFlow

final class ModelsTests: XCTestCase {
    func testAgent_decodes() throws {
        let json = #"""
        {"id":"slot-1","name":"alpha","persona":"writer","status":"idle"}
        """#.data(using: .utf8)!
        let a = try JSONDecoder().decode(Agent.self, from: json)
        XCTAssertEqual(a.id, "slot-1")
        XCTAssertEqual(a.status, "idle")
    }

    func testGoal_decodes_withMilestones() throws {
        let json = #"""
        {"id":"g1","title":"ship app","status":"active",
         "milestones":[{"title":"spec","done":true},{"title":"code","done":false}]}
        """#.data(using: .utf8)!
        let g = try JSONDecoder().decode(Goal.self, from: json)
        XCTAssertEqual(g.milestones?.count, 2)
        XCTAssertEqual(g.progress, 0.5)
    }

    func testGoal_decodes_withoutMilestones() throws {
        let json = #"""
        {"id":"g1","title":"ship app"}
        """#.data(using: .utf8)!
        let g = try JSONDecoder().decode(Goal.self, from: json)
        XCTAssertNil(g.progress)
    }

    func testBudget_decodes_snakeCase() throws {
        let json = #"""
        {"spent_usd": 1.23, "cap_usd": 5.0}
        """#.data(using: .utf8)!
        let b = try JSONDecoder().decode(Budget.self, from: json)
        XCTAssertEqual(b.spentUsd, 1.23, accuracy: 0.001)
        XCTAssertEqual(b.capUsd, 5.0, accuracy: 0.001)
    }

    func testAuthFile_decodes() throws {
        let json = #"""
        {"api_key":"af_live_abc","device_id":"dev1","ws_url":"wss://x/y"}
        """#.data(using: .utf8)!
        let a = try JSONDecoder().decode(AuthFile.self, from: json)
        XCTAssertEqual(a.apiKey, "af_live_abc")
        XCTAssertEqual(a.deviceId, "dev1")
    }
}
