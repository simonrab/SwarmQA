import Foundation

/// A small append-only JSON writer. The element tree is the bulk of every
/// `/observe` response, and writing it by hand is several times faster than
/// building `[String: Any]` for `JSONSerialization`.
struct JSONWriter {
    private(set) var bytes: [UInt8] = []

    init(capacity: Int = 64 * 1024) {
        bytes.reserveCapacity(capacity)
    }

    mutating func raw(_ text: String) {
        bytes.append(contentsOf: text.utf8)
    }

    mutating func raw(_ data: Data) {
        bytes.append(contentsOf: data)
    }

    mutating func string(_ value: String?) {
        guard let value else {
            raw("null")
            return
        }
        bytes.append(UInt8(ascii: "\""))
        for byte in value.utf8 {
            switch byte {
            case UInt8(ascii: "\""): raw("\\\"")
            case UInt8(ascii: "\\"): raw("\\\\")
            case 0x0A: raw("\\n")
            case 0x0D: raw("\\r")
            case 0x09: raw("\\t")
            case 0x00..<0x20:
                raw(String(format: "\\u%04x", Int(byte)))
            default:
                bytes.append(byte)
            }
        }
        bytes.append(UInt8(ascii: "\""))
    }

    mutating func number(_ value: Double) {
        if !value.isFinite {
            raw("0")
        } else if value == value.rounded() && abs(value) < 1e15 {
            raw(String(Int64(value)))
            raw(".0")
        } else {
            raw(String(value))
        }
    }

    mutating func bool(_ value: Bool) {
        raw(value ? "true" : "false")
    }

    var data: Data { Data(bytes) }
}

/// Encode a Foundation JSON value (dictionaries, arrays, strings, numbers).
func encodeJSONObject(_ object: Any) -> Data {
    (try? JSONSerialization.data(withJSONObject: object, options: [])) ?? Data("{}".utf8)
}

/// Accessors for decoded request bodies that turn shape errors into
/// `bad_request`.
struct JSONBody {
    let object: [String: Any]

    init(_ data: Data) throws {
        if data.isEmpty {
            object = [:]
            return
        }
        let parsed: Any
        do {
            parsed = try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
        } catch {
            throw RunnerFailure(.badRequest, "malformed JSON: \(error.localizedDescription)")
        }
        guard let dict = parsed as? [String: Any] else {
            throw RunnerFailure(.badRequest, "request body must be a JSON object")
        }
        object = dict
    }

    init(object: [String: Any]) {
        self.object = object
    }

    func has(_ key: String) -> Bool {
        if let value = object[key], !(value is NSNull) { return true }
        return false
    }

    func string(_ key: String) throws -> String? {
        guard let value = object[key], !(value is NSNull) else { return nil }
        guard let text = value as? String else {
            throw RunnerFailure(.badRequest, "\(key) must be a string")
        }
        return text
    }

    func requiredString(_ key: String) throws -> String {
        guard let text = try string(key) else {
            throw RunnerFailure(.badRequest, "missing field \(key)")
        }
        return text
    }

    func number(_ key: String) throws -> Double? {
        guard let value = object[key], !(value is NSNull) else { return nil }
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else {
            throw RunnerFailure(.badRequest, "\(key) must be a number")
        }
        return number.doubleValue
    }

    func requiredNumber(_ key: String) throws -> Double {
        guard let value = try number(key) else {
            throw RunnerFailure(.badRequest, "missing field \(key)")
        }
        return value
    }

    func bool(_ key: String) throws -> Bool? {
        guard let value = object[key], !(value is NSNull) else { return nil }
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else {
            throw RunnerFailure(.badRequest, "\(key) must be a boolean")
        }
        return number.boolValue
    }

    func stringList(_ key: String) throws -> [String]? {
        guard let value = object[key], !(value is NSNull) else { return nil }
        guard let list = value as? [Any], let strings = list as? [String] else {
            throw RunnerFailure(.badRequest, "\(key) must be a list of strings")
        }
        return strings
    }

    func stringMap(_ key: String) throws -> [String: String]? {
        guard let value = object[key], !(value is NSNull) else { return nil }
        guard let map = value as? [String: Any] else {
            throw RunnerFailure(.badRequest, "\(key) must be an object of strings")
        }
        var out: [String: String] = [:]
        for (k, v) in map {
            guard let s = v as? String else {
                throw RunnerFailure(.badRequest, "\(key).\(k) must be a string")
            }
            out[k] = s
        }
        return out
    }

    func point(_ key: String) throws -> CGPoint {
        guard let value = object[key], !(value is NSNull) else {
            throw RunnerFailure(.badRequest, "missing field \(key)")
        }
        guard let list = value as? [Any], list.count == 2,
              let x = list[0] as? NSNumber, let y = list[1] as? NSNumber,
              CFGetTypeID(x) != CFBooleanGetTypeID(), CFGetTypeID(y) != CFBooleanGetTypeID()
        else {
            throw RunnerFailure(.badRequest, "\(key) must be [x, y]")
        }
        return CGPoint(x: x.doubleValue, y: y.doubleValue)
    }

    func object(_ key: String) throws -> JSONBody? {
        guard let value = object[key], !(value is NSNull) else { return nil }
        guard let dict = value as? [String: Any] else {
            throw RunnerFailure(.badRequest, "\(key) must be an object")
        }
        return JSONBody(object: dict)
    }
}
