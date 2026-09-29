#if os(iOS)
import UIKit
import XCTest

/// iOS-specific pieces of the runner. Only compiled into the iOS UI-test target.
@MainActor
enum PlatformBridge {
    static let platformName = "ios"

    /// The screen in points. The runner app has no launch screen, so iOS
    /// runs it in a legacy 320x480 compatibility mode and `UIScreen.bounds`
    /// is wrong; the application element's frame is the real screen.
    static func screenSizeInPoints(appFrame: CGRect?) -> CGSize {
        if let appFrame, appFrame.width > 0, appFrame.height > 0 { return appFrame.size }
        let native = UIScreen.main.nativeBounds.size
        let scale = UIScreen.main.nativeScale
        return CGSize(width: native.width / scale, height: native.height / scale)
    }

    static func screenScale() -> Double {
        Double(UIScreen.main.nativeScale)
    }

    /// Whether an app is installed, or nil if that cannot be determined.
    /// XCUIApplication.launch() on a missing bundle id waits minutes for
    /// accessibility and then kills the test runner, so /launch checks first.
    /// Uses LaunchServices' LSApplicationWorkspace through the ObjC runtime
    /// (not public API on iOS; absent -> nil, and the launch goes ahead).
    static func isInstalled(_ bundleID: String) -> Bool? {
        guard let cls = NSClassFromString("LSApplicationWorkspace") as? NSObject.Type else { return nil }
        let defaultSel = NSSelectorFromString("defaultWorkspace")
        guard cls.responds(to: defaultSel),
              let workspace = cls.perform(defaultSel)?.takeUnretainedValue() as? NSObject
        else { return nil }
        let sel = NSSelectorFromString("applicationIsInstalled:")
        guard workspace.responds(to: sel) else { return nil }
        typealias Fn = @convention(c) (AnyObject, Selector, NSString) -> Bool
        let fn = unsafeBitCast(workspace.method(for: sel), to: Fn.self)
        return fn(workspace, sel, bundleID as NSString)
    }

    /// Screen point -> coordinate. XCUICoordinate offsets are relative to the
    /// app element's frame origin, so subtract it.
    static func coordinate(for point: CGPoint, appOrigin: CGPoint, app: XCUIApplication) -> XCUICoordinate {
        app.coordinate(withNormalizedOffset: CGVector(dx: 0, dy: 0))
            .withOffset(CGVector(dx: point.x - appOrigin.x, dy: point.y - appOrigin.y))
    }

    static func cgImage(of shot: XCUIScreenshot) -> CGImage? {
        shot.image.cgImage
    }

    static func tap(_ coordinate: XCUICoordinate) {
        coordinate.tap()
    }

    static func swipe(_ from: XCUICoordinate, _ to: XCUICoordinate, distance: Double, duration: Double) {
        let velocity = XCUIGestureVelocity(CGFloat(max(distance / max(duration, 0.01), 50)))
        from.press(forDuration: 0.05, thenDragTo: to, withVelocity: velocity, thenHoldForDuration: 0)
    }

    /// Keys on iOS go through the software keyboard where possible. Modifier
    /// chords (`cmd`, `ctrl`, ...) need a hardware keyboard in the simulator
    /// and are sent with `typeKey`.
    static func press(keys: [String], app: XCUIApplication) throws {
        let (modifiers, rest) = try splitKeys(keys)
        for key in rest {
            if key == "home" {
                XCUIDevice.shared.press(.home)
            } else if modifiers.isEmpty, let text = typedText(for: key) {
                app.typeText(text)
            } else {
                app.typeKey(try keyString(key), modifierFlags: modifiers)
            }
        }
    }

    private static func typedText(for key: String) -> String? {
        switch key {
        case "return": return "\n"
        case "tab": return "\t"
        case "delete": return XCUIKeyboardKey.delete.rawValue
        default: return key.count == 1 ? key : nil
        }
    }
}
#endif
