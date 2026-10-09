import Foundation

struct HTTPRequest {
    let method: String
    let path: String
    let headers: [String: String]  // keys lower-cased
    let body: Data

    func header(_ name: String) -> String? { headers[name.lowercased()] }
}

struct HTTPResponse {
    var status: Int
    var contentType: String
    var body: Data
    var extraHeaders: [String: String] = [:]

    static func json(_ status: Int, _ body: Data) -> HTTPResponse {
        HTTPResponse(status: status, contentType: "application/json", body: body)
    }
}

/// A minimal HTTP/1.1 server on a BSD socket, bound to 127.0.0.1.
///
/// It is driven from the main thread by `serve(handler:)`: XCUITest APIs must
/// run on the main thread, and the protocol handles one request at a time, so
/// there is no reason for a second thread. Between requests the loop spins the
/// main run loop so XCTest housekeeping keeps working. Keep-alive connections
/// are supported; the client never pipelines.
final class HTTPServer {
    private let port: UInt16
    private var listenFD: Int32 = -1
    private var clients: [Int32: Date] = [:]  // fd -> last activity
    private let idleTimeout: TimeInterval = 120
    private let maxBody = 16 * 1024 * 1024

    init(port: UInt16) {
        self.port = port
    }

    deinit {
        for fd in clients.keys { close(fd) }
        if listenFD >= 0 { close(listenFD) }
    }

    func start() throws {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw posixError("socket") }
        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else {
            close(fd)
            throw posixError("bind 127.0.0.1:\(port)")
        }
        guard listen(fd, 16) == 0 else {
            close(fd)
            throw posixError("listen")
        }
        setNonBlocking(fd)
        listenFD = fd
    }

    /// Set to false to make `serve` return after the current poll.
    var running = true

    /// Serve until `running` is cleared. `handler` runs on the calling (main) thread.
    func serve(handler: (HTTPRequest) -> HTTPResponse) {
        while running {
            pollOnce(timeoutMs: 20, handler: handler)
            // Let XCTest and the run loop process anything pending.
            CFRunLoopRunInMode(CFRunLoopMode.defaultMode, 0, true)
        }
    }

    private func pollOnce(timeoutMs: Int32, handler: (HTTPRequest) -> HTTPResponse) {
        var fds = [pollfd(fd: listenFD, events: Int16(POLLIN), revents: 0)]
        for fd in clients.keys {
            fds.append(pollfd(fd: fd, events: Int16(POLLIN), revents: 0))
        }
        let ready = poll(&fds, nfds_t(fds.count), timeoutMs)
        if ready <= 0 {
            reapIdle()
            return
        }
        for entry in fds where entry.revents != 0 {
            if entry.fd == listenFD {
                acceptAll()
            } else {
                serveClient(entry.fd, handler: handler)
            }
        }
    }

    private func acceptAll() {
        while true {
            let fd = accept(listenFD, nil, nil)
            if fd < 0 { return }
            // BSD sockets inherit O_NONBLOCK from the listener; clear it.
            let flags = fcntl(fd, F_GETFL, 0)
            _ = fcntl(fd, F_SETFL, flags & ~O_NONBLOCK)
            var yes: Int32 = 1
            setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &yes, socklen_t(MemoryLayout<Int32>.size))
            setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &yes, socklen_t(MemoryLayout<Int32>.size))
            // Client sockets are blocking with a receive timeout, which keeps
            // the reader simple once a request has started arriving.
            var timeout = timeval(tv_sec: 10, tv_usec: 0)
            setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
            setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
            clients[fd] = Date()
        }
    }

    private func reapIdle() {
        let now = Date()
        for (fd, last) in clients where now.timeIntervalSince(last) > idleTimeout {
            closeClient(fd)
        }
    }

    private func closeClient(_ fd: Int32) {
        close(fd)
        clients.removeValue(forKey: fd)
    }

    private func serveClient(_ fd: Int32, handler: (HTTPRequest) -> HTTPResponse) {
        let request: HTTPRequest
        do {
            guard let parsed = try readRequest(fd) else {
                closeClient(fd)  // peer closed
                return
            }
            request = parsed
        } catch let failure as RunnerFailure {
            _ = writeResponse(fd, failure.response, keepAlive: false)
            closeClient(fd)
            return
        } catch {
            closeClient(fd)
            return
        }
        let response = handler(request)
        let keepAlive = (request.header("connection")?.lowercased() ?? "") != "close"
        if writeResponse(fd, response, keepAlive: keepAlive) && keepAlive {
            clients[fd] = Date()
        } else {
            closeClient(fd)
        }
    }

    private func readRequest(_ fd: Int32) throws -> HTTPRequest? {
        var buffer = [UInt8]()
        buffer.reserveCapacity(4096)
        var chunk = [UInt8](repeating: 0, count: 65536)
        var headerEnd: Int? = nil
        while headerEnd == nil {
            let n = recv(fd, &chunk, chunk.count, 0)
            if n == 0 { return nil }
            if n < 0 {
                if buffer.isEmpty { return nil }
                throw RunnerFailure(.badRequest, "timed out reading request")
            }
            buffer.append(contentsOf: chunk[0..<n])
            headerEnd = findHeaderEnd(buffer)
            if headerEnd == nil && buffer.count > 64 * 1024 {
                throw RunnerFailure(.badRequest, "request headers too large")
            }
        }
        let end = headerEnd!
        guard let head = String(bytes: buffer[0..<end], encoding: .utf8) else {
            throw RunnerFailure(.badRequest, "request head is not UTF-8")
        }
        var lines = head.components(separatedBy: "\r\n")
        let requestLine = lines.removeFirst().split(separator: " ")
        guard requestLine.count >= 2 else {
            throw RunnerFailure(.badRequest, "malformed request line")
        }
        var headers: [String: String] = [:]
        for line in lines where !line.isEmpty {
            guard let colon = line.firstIndex(of: ":") else { continue }
            let name = line[..<colon].trimmingCharacters(in: .whitespaces).lowercased()
            let value = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
            headers[name] = value
        }
        if headers["transfer-encoding"]?.lowercased().contains("chunked") == true {
            throw RunnerFailure(.badRequest, "chunked request bodies are not supported")
        }
        let length = Int(headers["content-length"] ?? "0") ?? -1
        guard length >= 0, length <= maxBody else {
            throw RunnerFailure(.badRequest, "bad Content-Length")
        }
        let bodyStart = end + 4
        if headers["expect"]?.lowercased() == "100-continue" && buffer.count - bodyStart < length {
            sendAll(fd, Array("HTTP/1.1 100 Continue\r\n\r\n".utf8))
        }
        while buffer.count - bodyStart < length {
            let n = recv(fd, &chunk, min(chunk.count, length - (buffer.count - bodyStart)), 0)
            if n <= 0 { throw RunnerFailure(.badRequest, "connection closed mid-body") }
            buffer.append(contentsOf: chunk[0..<n])
        }
        let body = Data(buffer[bodyStart..<(bodyStart + length)])
        var path = String(requestLine[1])
        if let q = path.firstIndex(of: "?") { path = String(path[..<q]) }
        return HTTPRequest(method: String(requestLine[0]).uppercased(), path: path, headers: headers, body: body)
    }

    private func findHeaderEnd(_ buffer: [UInt8]) -> Int? {
        guard buffer.count >= 4 else { return nil }
        for i in 0...(buffer.count - 4)
        where buffer[i] == 13 && buffer[i + 1] == 10 && buffer[i + 2] == 13 && buffer[i + 3] == 10 {
            return i
        }
        return nil
    }

    private func writeResponse(_ fd: Int32, _ response: HTTPResponse, keepAlive: Bool) -> Bool {
        let head = "HTTP/1.1 \(response.status) \(reason(response.status))\r\n"
            + "Content-Type: \(response.contentType)\r\n"
            + "Content-Length: \(response.body.count)\r\n"
            + "X-Swarm-Protocol: \(protocolVersion)\r\n"
            + response.extraHeaders.map { "\($0.key): \($0.value)\r\n" }.joined()
            + "Connection: \(keepAlive ? "keep-alive" : "close")\r\n\r\n"
        var bytes = Array(head.utf8)
        bytes.append(contentsOf: response.body)
        return sendAll(fd, bytes)
    }

    @discardableResult
    private func sendAll(_ fd: Int32, _ bytes: [UInt8]) -> Bool {
        var offset = 0
        while offset < bytes.count {
            let n = bytes.withUnsafeBytes { raw in
                send(fd, raw.baseAddress! + offset, bytes.count - offset, 0)
            }
            if n <= 0 { return false }
            offset += n
        }
        return true
    }

    private func setNonBlocking(_ fd: Int32) {
        let flags = fcntl(fd, F_GETFL, 0)
        _ = fcntl(fd, F_SETFL, flags | O_NONBLOCK)
    }

    private func posixError(_ what: String) -> Error {
        NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                userInfo: [NSLocalizedDescriptionKey: "\(what): \(String(cString: strerror(errno)))"])
    }

    private func reason(_ status: Int) -> String {
        switch status {
        case 200: return "OK"
        case 400: return "Bad Request"
        case 404: return "Not Found"
        case 409: return "Conflict"
        case 426: return "Upgrade Required"
        case 500: return "Internal Server Error"
        case 504: return "Gateway Timeout"
        default: return "Status"
        }
    }
}
