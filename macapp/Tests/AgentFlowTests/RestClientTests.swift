import XCTest
@testable import AgentFlow

/// Captures URLRequests + returns canned responses. Registered globally for
/// the test session so an injected URLSessionConfiguration picks it up.
final class MockURLProtocol: URLProtocol {
    static var handler: ((URLRequest) -> (HTTPURLResponse, Data))?
    static var lastRequest: URLRequest?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        MockURLProtocol.lastRequest = request
        guard let handler = MockURLProtocol.handler else {
            client?.urlProtocol(self, didFailWithError: NSError(domain: "test", code: -1))
            return
        }
        let (resp, data) = handler(request)
        client?.urlProtocol(self, didReceive: resp, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

final class RestClientTests: XCTestCase {
    private func makeClient(apiKey: String? = "af_live_test") -> RestClient {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.protocolClasses = [MockURLProtocol.self]
        let session = URLSession(configuration: cfg)
        let base = URL(string: "https://example.test/_agents")!
        return RestClient(base: base, session: session, apiKey: { apiKey })
    }

    override func tearDown() {
        MockURLProtocol.handler = nil
        MockURLProtocol.lastRequest = nil
        super.tearDown()
    }

    func testFetchGoals_envelopeShape() async throws {
        MockURLProtocol.handler = { req in
            let body = #"""
            {"goals":[{"id":"g1","title":"ship","milestones":[{"title":"a","done":true}]}]}
            """#.data(using: .utf8)!
            let resp = HTTPURLResponse(url: req.url!, statusCode: 200,
                                      httpVersion: nil, headerFields: nil)!
            return (resp, body)
        }
        let client = makeClient()
        let goals = try await client.fetchGoals()
        XCTAssertEqual(goals.count, 1)
        XCTAssertEqual(goals[0].id, "g1")
        XCTAssertEqual(goals[0].progress, 1.0)
    }

    func testFetchGoals_bareListShape() async throws {
        MockURLProtocol.handler = { req in
            let body = #"""
            [{"id":"g2","title":"thing"}]
            """#.data(using: .utf8)!
            let resp = HTTPURLResponse(url: req.url!, statusCode: 200,
                                      httpVersion: nil, headerFields: nil)!
            return (resp, body)
        }
        let client = makeClient()
        let goals = try await client.fetchGoals()
        XCTAssertEqual(goals.count, 1)
        XCTAssertEqual(goals[0].title, "thing")
    }

    func testFetchBudget_decodes() async throws {
        MockURLProtocol.handler = { req in
            let body = #"""
            {"spent_usd":2.5,"cap_usd":10.0}
            """#.data(using: .utf8)!
            let resp = HTTPURLResponse(url: req.url!, statusCode: 200,
                                      httpVersion: nil, headerFields: nil)!
            return (resp, body)
        }
        let client = makeClient()
        let b = try await client.fetchBudget()
        XCTAssertEqual(b.spentUsd, 2.5, accuracy: 0.001)
        XCTAssertEqual(b.capUsd, 10.0, accuracy: 0.001)
    }

    func testFetchGoals_setsApiKeyHeader() async throws {
        MockURLProtocol.handler = { req in
            let body = "[]".data(using: .utf8)!
            let resp = HTTPURLResponse(url: req.url!, statusCode: 200,
                                      httpVersion: nil, headerFields: nil)!
            return (resp, body)
        }
        let client = makeClient(apiKey: "af_live_check")
        _ = try await client.fetchGoals()
        XCTAssertEqual(
            MockURLProtocol.lastRequest?.value(forHTTPHeaderField: "x-api-key"),
            "af_live_check"
        )
    }

    func testFetchGoals_notAuthenticatedWhenKeyMissing() async {
        let client = makeClient(apiKey: nil)
        do {
            _ = try await client.fetchGoals()
            XCTFail("expected throw")
        } catch let e as RestError {
            XCTAssertEqual(e, .notAuthenticated)
        } catch {
            XCTFail("wrong error: \(error)")
        }
    }

    func testFetchGoals_httpError401() async {
        MockURLProtocol.handler = { req in
            let resp = HTTPURLResponse(url: req.url!, statusCode: 401,
                                      httpVersion: nil, headerFields: nil)!
            return (resp, "nope".data(using: .utf8)!)
        }
        let client = makeClient()
        do {
            _ = try await client.fetchGoals()
            XCTFail("expected throw")
        } catch let RestError.http(code, _) {
            XCTAssertEqual(code, 401)
        } catch {
            XCTFail("wrong error: \(error)")
        }
    }
}
