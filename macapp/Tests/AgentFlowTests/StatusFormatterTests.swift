import XCTest
@testable import AgentFlow

final class StatusFormatterTests: XCTestCase {
    func testBudget_basic() {
        XCTAssertEqual(StatusFormatter.budget(spent: 1.23, cap: 5.00), "$1.23 / $5.00 (25%)")
    }

    func testBudget_zeroCap_zeroPercent() {
        XCTAssertEqual(StatusFormatter.budget(spent: 0.5, cap: 0), "$0.50 / $0.00 (0%)")
    }

    func testBudget_overspend_overHundred() {
        let s = StatusFormatter.budget(spent: 7.5, cap: 5.0)
        XCTAssertTrue(s.contains("150%"), "got: \(s)")
    }

    func testBudget_negativeClampedToZero() {
        XCTAssertEqual(StatusFormatter.budget(spent: -1, cap: 5.0), "$0.00 / $5.00 (0%)")
    }

    func testMilestoneRatio_empty() {
        let g = Goal(id: "1", title: "t", milestones: [])
        XCTAssertEqual(StatusFormatter.milestoneRatio(g), "—")
    }

    func testMilestoneRatio_nil() {
        let g = Goal(id: "1", title: "t", milestones: nil)
        XCTAssertEqual(StatusFormatter.milestoneRatio(g), "—")
    }

    func testMilestoneRatio_counts() {
        let g = Goal(id: "1", title: "t", milestones: [
            Milestone(title: "a", done: true),
            Milestone(title: "b", done: true),
            Milestone(title: "c", done: false),
        ])
        XCTAssertEqual(StatusFormatter.milestoneRatio(g), "2/3")
        XCTAssertEqual(StatusFormatter.percentLabel(g), "67%")
    }

    func testAgentBadge_known() {
        XCTAssertEqual(StatusFormatter.agentBadge(status: "running").label, "работает")
        XCTAssertEqual(StatusFormatter.agentBadge(status: "idle").label, "ожидает")
        XCTAssertEqual(StatusFormatter.agentBadge(status: "paused").label, "пауза")
        XCTAssertEqual(StatusFormatter.agentBadge(status: "error").label, "ошибка")
    }

    func testAgentBadge_unknown() {
        XCTAssertEqual(StatusFormatter.agentBadge(status: "weird").label, "weird")
    }
}
