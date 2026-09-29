import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers
import XCTest

/// A captured screen plus its geometry in points.
struct Capture {
    let size: CGSize  // points
    let scale: Double  // pixels per point
    let format: String  // "png" or "jpeg"
    let data: Data
}

enum ImageFormat: String {
    case png, jpeg, none
}

/// A screenshot of the main screen taken on a background thread, so that
/// `/observe` can overlap it with the accessibility snapshot on the main
/// thread. Both are round trips to the testing daemon and dominate the call.
///
/// `XCUIScreen.screenshot()` is annotated main-actor in the SDK, so it is
/// called through the Objective-C runtime here. XCTest services any work it
/// needs on the main thread while the main thread spins its run loop in
/// `wait()`. Set SWARM_RUNNER_SERIAL_SCREENSHOT=1 to capture on the main
/// thread instead.
final class ScreenshotTask: @unchecked Sendable {
    static let serial = ProcessInfo.processInfo.environment["SWARM_RUNNER_SERIAL_SCREENSHOT"] == "1"

    private let done = DispatchSemaphore(value: 0)
    private var shot: XCUIScreenshot?
    private let screen: NSObject

    @MainActor
    init() {
        screen = XCUIScreen.main
        if Self.serial {
            shot = XCUIScreen.main.screenshot()
            done.signal()
        } else {
            let thread = Thread { [self] in
                let selector = NSSelectorFromString("screenshot")
                shot = screen.perform(selector)?.takeUnretainedValue() as? XCUIScreenshot
                done.signal()
            }
            thread.qualityOfService = .userInteractive
            thread.start()
        }
    }

    /// Wait for the capture, spinning the main run loop meanwhile.
    @MainActor
    func wait(timeout: TimeInterval = 30) throws -> XCUIScreenshot {
        let deadline = Date().addingTimeInterval(timeout)
        while done.wait(timeout: .now()) == .timedOut {
            if Date() > deadline {
                throw RunnerFailure(.timeout, "screenshot did not finish in \(Int(timeout)) s")
            }
            CFRunLoopRunInMode(CFRunLoopMode.defaultMode, 0.001, true)
        }
        done.signal()  // keep it signalled for repeated waits
        guard let shot else { throw RunnerFailure(.internal, "screenshot failed") }
        return shot
    }
}

/// Capture the main screen synchronously.
@MainActor
func captureScreen(format: ImageFormat, jpegQuality: Double, points: CGSize) throws -> Capture {
    try encodeScreenshot(ScreenshotTask().wait(), format: format, jpegQuality: jpegQuality, points: points)
}

/// PNG uses XCTest's own encoding; JPEG re-encodes the CGImage with ImageIO.
@MainActor
func encodeScreenshot(_ shot: XCUIScreenshot, format: ImageFormat, jpegQuality: Double, points: CGSize) throws -> Capture {
    switch format {
    case .png, .none:
        let data = shot.pngRepresentation
        let pixelWidth = pngPixelWidth(data) ?? Double(points.width)
        return Capture(size: points, scale: scale(pixelWidth, points), format: "png", data: data)
    case .jpeg:
        guard let image = PlatformBridge.cgImage(of: shot) else {
            throw RunnerFailure(.internal, "screenshot has no CGImage")
        }
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, UTType.jpeg.identifier as CFString, 1, nil) else {
            throw RunnerFailure(.internal, "cannot create JPEG encoder")
        }
        let quality = max(0.0, min(1.0, jpegQuality))
        CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        guard CGImageDestinationFinalize(dest) else {
            throw RunnerFailure(.internal, "JPEG encoding failed")
        }
        return Capture(size: points, scale: scale(Double(image.width), points), format: "jpeg", data: out as Data)
    }
}

private func scale(_ pixelWidth: Double, _ points: CGSize) -> Double {
    points.width > 0 ? (pixelWidth / Double(points.width) * 100).rounded() / 100 : 1.0
}

/// Read the width from a PNG's IHDR chunk without decoding the image.
private func pngPixelWidth(_ data: Data) -> Double? {
    guard data.count >= 24 else { return nil }
    let bytes = [UInt8](data.prefix(24))
    guard bytes[12] == 0x49, bytes[13] == 0x48, bytes[14] == 0x44, bytes[15] == 0x52 else { return nil }
    let width = UInt32(bytes[16]) << 24 | UInt32(bytes[17]) << 16 | UInt32(bytes[18]) << 8 | UInt32(bytes[19])
    return Double(width)
}
