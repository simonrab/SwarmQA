import Foundation
import XCTest

let protocolVersion = 1
let runnerVersion = "0.1.0"

enum ErrorCode: String {
    case badRequest = "bad_request"
    case notFound = "not_found"
    case notLaunched = "not_launched"
    case appCrashed = "app_crashed"
    case timeout = "timeout"
    case protocolMismatch = "protocol_mismatch"
    case `internal` = "internal"

    var status: Int {
        switch self {
        case .badRequest: return 400
        case .notFound: return 404
        case .notLaunched, .appCrashed: return 409
        case .timeout: return 504
        case .protocolMismatch: return 426
        case .internal: return 500
        }
    }
}

struct RunnerFailure: Error {
    let code: ErrorCode
    let message: String

    init(_ code: ErrorCode, _ message: String) {
        self.code = code
        self.message = message
    }

    var response: HTTPResponse {
        .json(code.status, encodeJSONObject(["error": ["code": code.rawValue, "message": message]]))
    }
}

/// Split a `/key` chord into modifier flags and the remaining key names.
func splitKeys(_ keys: [String]) throws -> (XCUIElement.KeyModifierFlags, [String]) {
    var modifiers: XCUIElement.KeyModifierFlags = []
    var rest: [String] = []
    for raw in keys {
        let key = raw.count == 1 ? raw : raw.lowercased()
        switch key {
        case "cmd", "command": modifiers.insert(.command)
        case "ctrl", "control": modifiers.insert(.control)
        case "alt", "option": modifiers.insert(.option)
        case "shift": modifiers.insert(.shift)
        default: rest.append(key)
        }
    }
    if rest.isEmpty {
        throw RunnerFailure(.badRequest, "keys needs at least one non-modifier key")
    }
    return (modifiers, rest)
}

/// The `typeKey` string for a protocol key name.
func keyString(_ key: String) throws -> String {
    switch key {
    case "return": return XCUIKeyboardKey.return.rawValue
    case "escape": return XCUIKeyboardKey.escape.rawValue
    case "tab": return XCUIKeyboardKey.tab.rawValue
    case "delete": return XCUIKeyboardKey.delete.rawValue
    case "up": return XCUIKeyboardKey.upArrow.rawValue
    case "down": return XCUIKeyboardKey.downArrow.rawValue
    case "left": return XCUIKeyboardKey.leftArrow.rawValue
    case "right": return XCUIKeyboardKey.rightArrow.rawValue
    case "space": return XCUIKeyboardKey.space.rawValue
    case "home": return XCUIKeyboardKey.home.rawValue
    default:
        if key.count == 1 { return key }
        throw RunnerFailure(.badRequest, "unknown key \(key)")
    }
}

/// Thread-safe holder for the last XCTest issue message.
final class IssueBox: @unchecked Sendable {
    private let lock = NSLock()
    private var message: String?

    func set(_ text: String) {
        lock.lock()
        message = text
        lock.unlock()
    }

    func take() -> String? {
        lock.lock()
        defer { lock.unlock() }
        let text = message
        message = nil
        return text
    }
}

/// Holds the driven app and implements every endpoint of PROTOCOL.md.
@MainActor
final class Runner {
    private var app: XCUIApplication?
    private var bundleID: String?
    private var terminatedByUs = false
    /// XCTest issues recorded while an XCUITest call ran. The test case
    /// forwards issues here instead of failing the (endless) test.
    let issues = IssueBox()

    func handle(_ request: HTTPRequest) -> HTTPResponse {
        if let version = request.header("x-swarm-protocol"),
           version.trimmingCharacters(in: .whitespaces) != String(protocolVersion) {
            return RunnerFailure(.protocolMismatch,
                                 "runner speaks protocol \(protocolVersion), client sent \(version)").response
        }
        do {
            switch (request.method, request.path) {
            case ("GET", "/health"): return ok(health())
            case ("POST", "/launch"): return try launch(JSONBody(request.body))
            case ("POST", "/terminate"): return try terminate()
            case ("GET", "/tree"): return try tree()
            case ("POST", "/observe"): return try observe(JSONBody(request.body))
            case ("POST", "/tap"): return try tap(JSONBody(request.body))
            case ("POST", "/type"): return try type(JSONBody(request.body))
            case ("POST", "/swipe"): return try swipe(JSONBody(request.body))
            case ("POST", "/key"): return try key(JSONBody(request.body))
            case ("GET", "/screenshot"): return try screenshot()
            default:
                throw RunnerFailure(.badRequest, "unknown endpoint \(request.method) \(request.path)")
            }
        } catch let failure as RunnerFailure {
            return failure.response
        } catch {
            return RunnerFailure(.internal, String(describing: error)).response
        }
    }

    // MARK: Endpoints

    private func health() -> [String: Any] {
        [
            "protocol_version": protocolVersion,
            "runner_version": runnerVersion,
            "platform": PlatformBridge.platformName,
            "app_state": appState(),
        ]
    }

    private func launch(_ body: JSONBody) throws -> HTTPResponse {
        let bundle = try body.requiredString("bundle_id")
        if bundle.isEmpty { throw RunnerFailure(.badRequest, "bundle_id is empty") }
        let args = try body.stringList("args") ?? []
        let env = try body.stringMap("env") ?? [:]
        let terminateExisting = try body.bool("terminate_existing") ?? true
        if PlatformBridge.isInstalled(bundle) == false {
            throw RunnerFailure(.badRequest, "\(bundle) is not installed")
        }

        let target = XCUIApplication(bundleIdentifier: bundle)
        target.launchArguments = args
        target.launchEnvironment = env
        app = target
        bundleID = bundle
        terminatedByUs = false
        do {
            try perform {
                if terminateExisting || target.state == .notRunning || target.state == .unknown {
                    target.launch()  // terminates a running instance first
                } else {
                    target.activate()
                }
            }
        } catch let failure as RunnerFailure {
            // A launch that fails to activate (for example on a locked Mac)
            // can leave the app running in the background; stop it.
            if target.state != .notRunning && target.state != .unknown {
                try? perform { target.terminate() }
            }
            app = nil
            bundleID = nil
            throw RunnerFailure(failure.code, "launch \(bundle) failed: \(failure.message)")
        }
        if target.state == .notRunning || target.state == .unknown {
            app = nil
            bundleID = nil
            throw RunnerFailure(.internal, "launch \(bundle) failed: app is not running")
        }
        return ok()
    }

    private func terminate() throws -> HTTPResponse {
        guard let app else { return ok() }
        terminatedByUs = true
        try perform { app.terminate() }
        return ok()
    }

    private func tree() throws -> HTTPResponse {
        let app = try requireRunning()
        let (ts, root) = try snapshot(app)
        var json = JSONWriter()
        json.raw("{\"ts\":")
        json.number(ts)
        json.raw(",\"elements\":[")
        root.write(to: &json)
        json.raw("]}")
        return .json(200, json.data)
    }

    private func observe(_ body: JSONBody) throws -> HTTPResponse {
        let formatName = try body.string("screenshot") ?? "png"
        guard let format = ImageFormat(rawValue: formatName) else {
            throw RunnerFailure(.badRequest, "screenshot must be png, jpeg or none")
        }
        let quality = try body.number("jpeg_quality") ?? 0.7
        let app = try requireRunning()
        let t0 = CFAbsoluteTimeGetCurrent()
        // The screenshot runs on a background thread while the snapshot runs
        // here; together they are close to max(snapshot, screenshot).
        let shotTask = format == .none ? nil : ScreenshotTask()
        let (ts, root) = try snapshot(app)
        let t1 = CFAbsoluteTimeGetCurrent()
        let size = PlatformBridge.screenSizeInPoints(appFrame: root.frame)
        var capture: Capture?
        var t2 = t1
        if let shotTask {
            let shot = try shotTask.wait()
            t2 = CFAbsoluteTimeGetCurrent()
            capture = try encodeScreenshot(shot, format: format, jpegQuality: quality, points: size)
        }
        let t3 = CFAbsoluteTimeGetCurrent()
        let scale = capture?.scale ?? PlatformBridge.screenScale()

        var json = JSONWriter(capacity: 256 * 1024 + (capture?.data.count ?? 0) * 4 / 3)
        json.raw("{\"ts\":")
        json.number(ts)
        json.raw(",\"elements\":[")
        root.write(to: &json)
        json.raw("],\"size\":[")
        json.number(Double(size.width))
        json.raw(",")
        json.number(Double(size.height))
        json.raw("],\"scale\":")
        json.number(scale)
        json.raw(",\"screenshot\":")
        if let capture {
            json.raw("{\"format\":")
            json.string(capture.format)
            json.raw(",\"data\":\"")
            json.raw(capture.data.base64EncodedData())
            json.raw("\"}")
        } else {
            json.raw("null")
        }
        json.raw("}")
        var response = HTTPResponse.json(200, json.data)
        let ms = { (a: Double, b: Double) in String(format: "%.1f", (b - a) * 1000) }
        // Diagnostic only, not part of the protocol.
        response.extraHeaders["X-Swarm-Timing"] =
            "snapshot=\(ms(t0, t1)) screenshot_wait=\(ms(t1, t2)) image=\(ms(t2, t3)) " +
            "json=\(ms(t3, CFAbsoluteTimeGetCurrent())) elements=\(root.count)"
        return response
    }

    private func tap(_ body: JSONBody) throws -> HTTPResponse {
        let query = try body.object("query")
        let hasPoint = body.has("x") || body.has("y")
        if (query == nil) == !hasPoint {
            throw RunnerFailure(.badRequest, "tap needs exactly one of x/y and query")
        }
        let app = try requireRunning()
        if let query {
            try tapElement(ElementQuery(query), in: app)
        } else {
            let point = CGPoint(x: try body.requiredNumber("x"), y: try body.requiredNumber("y"))
            let coordinate = try self.coordinate(for: point, in: app)
            try perform { PlatformBridge.tap(coordinate) }
        }
        return ok()
    }

    private func type(_ body: JSONBody) throws -> HTTPResponse {
        let text = try body.requiredString("text")
        let query = try body.object("query")
        let app = try requireRunning()
        if let query {
            try tapElement(ElementQuery(query), in: app)
        }
        try perform { app.typeText(text) }
        return ok()
    }

    private func swipe(_ body: JSONBody) throws -> HTTPResponse {
        let from = try body.point("from")
        let to = try body.point("to")
        let duration = try body.number("duration_s") ?? 0.3
        if duration < 0 { throw RunnerFailure(.badRequest, "duration_s must be >= 0") }
        let app = try requireRunning()
        let origin = try appOrigin(app)
        let start = coordinate(for: from, origin: origin, in: app)
        let end = coordinate(for: to, origin: origin, in: app)
        let distance = hypot(Double(to.x - from.x), Double(to.y - from.y))
        try perform { PlatformBridge.swipe(start, end, distance: distance, duration: duration) }
        return ok()
    }

    private func key(_ body: JSONBody) throws -> HTTPResponse {
        guard let keys = try body.stringList("keys"), !keys.isEmpty else {
            throw RunnerFailure(.badRequest, "keys must be a non-empty list")
        }
        let app = try requireRunning()
        try perform { try PlatformBridge.press(keys: keys, app: app) }
        return ok()
    }

    private func screenshot() throws -> HTTPResponse {
        var appFrame: CGRect?
        if let app, appState() == "running" {
            try perform { appFrame = app.frame }
        }
        let capture = try captureScreen(format: .png, jpegQuality: 1,
                                        points: PlatformBridge.screenSizeInPoints(appFrame: appFrame))
        return HTTPResponse(status: 200, contentType: "image/png", body: capture.data)
    }

    // MARK: Helpers

    private func ok(_ object: [String: Any] = ["ok": true]) -> HTTPResponse {
        .json(200, encodeJSONObject(object))
    }

    private func appState() -> String {
        guard let app else { return "not_running" }
        switch app.state {
        case .notRunning, .unknown:
            return terminatedByUs ? "not_running" : "crashed"
        default:
            return "running"
        }
    }

    private func requireRunning() throws -> XCUIApplication {
        guard let app else {
            throw RunnerFailure(.notLaunched, "no app has been launched")
        }
        switch appState() {
        case "crashed":
            throw RunnerFailure(.appCrashed, "\(bundleID ?? "app") is no longer running")
        case "not_running":
            throw RunnerFailure(.notLaunched, "\(bundleID ?? "app") was terminated")
        default:
            return app
        }
    }

    private func snapshot(_ app: XCUIApplication) throws -> (Double, Node) {
        let ts = Date().timeIntervalSince1970
        var result: XCUIElementSnapshot?
        var snapError: Error?
        try perform {
            do {
                result = try app.snapshot()
            } catch {
                snapError = error
            }
        }
        if let snapError {
            if appState() == "crashed" {
                throw RunnerFailure(.appCrashed, "\(bundleID ?? "app") is no longer running")
            }
            throw RunnerFailure(.internal, "snapshot failed: \(snapError.localizedDescription)")
        }
        guard let result else { throw RunnerFailure(.internal, "snapshot returned nothing") }
        return (ts, Node(result))
    }

    private func tapElement(_ query: ElementQuery, in app: XCUIApplication) throws {
        let (_, root) = try snapshot(app)
        guard let node = root.first(matching: query) else {
            throw RunnerFailure(.notFound, "no element matches \(query)")
        }
        guard let frame = node.frame else {
            throw RunnerFailure(.notFound, "element matching \(query) has no frame")
        }
        let origin = root.frame?.origin ?? .zero
        let coordinate = self.coordinate(for: CGPoint(x: frame.midX, y: frame.midY), origin: origin, in: app)
        try perform { PlatformBridge.tap(coordinate) }
    }

    private func appOrigin(_ app: XCUIApplication) throws -> CGPoint {
        var frame = CGRect.zero
        try perform { frame = app.frame }
        return frame.isNull || frame.isInfinite ? .zero : frame.origin
    }

    private func coordinate(for point: CGPoint, in app: XCUIApplication) throws -> XCUICoordinate {
        coordinate(for: point, origin: try appOrigin(app), in: app)
    }

    /// Screen point -> coordinate. XCUICoordinate offsets are relative to the
    /// app element's frame origin, so subtract it.
    private func coordinate(for point: CGPoint, origin: CGPoint, in app: XCUIApplication) -> XCUICoordinate {
        app.coordinate(withNormalizedOffset: CGVector(dx: 0, dy: 0))
            .withOffset(CGVector(dx: point.x - origin.x, dy: point.y - origin.y))
    }

    /// Run an XCUITest call and turn any recorded XCTest issue into an error.
    private func perform(_ body: () throws -> Void) throws {
        _ = issues.take()
        try body()
        if let issue = issues.take() {
            let state = app != nil ? appState() : "not_running"
            if state == "crashed" {
                throw RunnerFailure(.appCrashed, issue)
            }
            let lower = issue.lowercased()
            // XCTest reports "<app> crashed in <symbol>" asynchronously, once
            // it has collected the crash report of an instance that is already
            // gone. If the app is running again, the issue is about the old
            // instance (which app_state already reported), not this call.
            if lower.contains(" crashed in ") && state == "running" {
                NSLog("swarm runner: ignoring stale crash issue: %@", issue)
                return
            }
            if lower.contains("timed out") || lower.contains("timeout") || lower.contains("idle") {
                throw RunnerFailure(.timeout, issue)
            }
            if lower.contains("no matches found") {
                throw RunnerFailure(.notFound, issue)
            }
            throw RunnerFailure(.internal, issue)
        }
    }
}
