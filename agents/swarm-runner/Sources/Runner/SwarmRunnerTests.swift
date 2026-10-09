import Foundation
import XCTest

/// The whole runner is one never-ending UI test. It binds an HTTP server on
/// 127.0.0.1:$SWARM_RUNNER_PORT (default 8765) and serves PROTOCOL.md until
/// the `xcodebuild test-without-building` process is killed.
///
/// Pass the port through xcodebuild with the TEST_RUNNER_ prefix:
///     TEST_RUNNER_SWARM_RUNNER_PORT=8766 xcodebuild test-without-building ...
final class SwarmRunnerTests: XCTestCase {
    private var issues: IssueBox?

    override func setUp() {
        super.setUp()
        continueAfterFailure = true
    }

    /// XCTest issues (a failed launch, a missing keyboard focus, ...) are
    /// handed to the runner, which reports them in the HTTP response. They are
    /// not recorded, so the test keeps serving instead of failing.
    override func record(_ issue: XCTIssue) {
        let message = issue.detailedDescription.map { "\(issue.compactDescription): \($0)" }
            ?? issue.compactDescription
        if let issues {
            issues.set(message)
        } else {
            super.record(issue)
        }
    }

    @MainActor
    func testRunServer() throws {
        let env = ProcessInfo.processInfo.environment
        let portText = env["SWARM_RUNNER_PORT"] ?? "8765"
        guard let port = UInt16(portText) else {
            XCTFail("SWARM_RUNNER_PORT is not a port: \(portText)")
            return
        }
        let runner = Runner()
        let server = HTTPServer(port: port)
        do {
            try server.start()
        } catch {
            print("SWARM_RUNNER_FAILED \(error.localizedDescription)")
            XCTFail("swarm runner could not listen on 127.0.0.1:\(port): \(error.localizedDescription)")
            return
        }
        // From here on, XCTest issues become HTTP errors instead of failures.
        issues = runner.issues
        // A fixed marker line that run.sh and drivers can wait for in the log.
        print("SWARM_RUNNER_READY port=\(port) platform=\(PlatformBridge.platformName) protocol=\(protocolVersion)")
        fflush(stdout)
        NSLog("SWARM_RUNNER_READY port=%d", Int(port))
        server.serve { request in runner.handle(request) }
    }
}
