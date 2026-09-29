#if os(macOS)
import AppKit
import XCTest

/// macOS-specific pieces of the runner. Only compiled into the macOS UI-test target.
@MainActor
enum PlatformBridge {
    static let platformName = "macos"

    /// The main display, whose top-left corner is the protocol's origin.
    static func screenSizeInPoints(appFrame: CGRect?) -> CGSize {
        (NSScreen.screens.first ?? NSScreen.main)?.frame.size ?? .zero
    }

    static func screenScale() -> Double {
        Double((NSScreen.screens.first ?? NSScreen.main)?.backingScaleFactor ?? 1)
    }

    /// Whether an app is installed, or nil if that cannot be determined.
    static func isInstalled(_ bundleID: String) -> Bool? {
        NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) != nil
    }

    /// Screen point -> coordinate. On macOS the application element has a
    /// zero-size frame, and moving the mouse relative to it trips an XCTest
    /// assertion (`-[XCPointerEventPath moveMouseToPoint:...]`) that ends the
    /// runner. The menu bar starts at the main display's top-left, the
    /// protocol's origin, so offsets from it are screen points.
    static func coordinate(for point: CGPoint, appOrigin: CGPoint, app: XCUIApplication) -> XCUICoordinate {
        let menuBar = app.menuBars.firstMatch
        let anchor: XCUIElement = menuBar.exists ? menuBar : app.windows.firstMatch
        let origin = menuBar.exists ? CGPoint.zero : anchor.frame.origin
        return anchor.coordinate(withNormalizedOffset: CGVector(dx: 0, dy: 0))
            .withOffset(CGVector(dx: point.x - origin.x, dy: point.y - origin.y))
    }

    static func cgImage(of shot: XCUIScreenshot) -> CGImage? {
        var rect = CGRect(origin: .zero, size: shot.image.size)
        return shot.image.cgImage(forProposedRect: &rect, context: nil, hints: nil)
    }

    static func tap(_ coordinate: XCUICoordinate) {
        coordinate.click()
    }

    /// A swipe on the Mac is a scroll-wheel gesture at `from`, moving the
    /// content by (to - from) the way a trackpad swipe would. Dragging with the
    /// mouse would select text or move windows instead of scrolling.
    static func swipe(_ from: XCUICoordinate, _ to: XCUICoordinate, distance: Double, duration: Double) {
        let a = from.screenPoint
        let b = to.screenPoint
        from.hover()
        from.scroll(byDeltaX: b.x - a.x, deltaY: b.y - a.y)
    }

    static func press(keys: [String], app: XCUIApplication) throws {
        let (modifiers, rest) = try splitKeys(keys)
        for key in rest {
            app.typeKey(try keyString(key), modifierFlags: modifiers)
        }
    }
}
#endif
