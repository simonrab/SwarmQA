import Foundation
import XCTest

/// One element of the protocol's tree, copied out of an `XCUIElementSnapshot`.
struct Node {
    var role: String
    var label: String?
    var identifier: String?
    var value: String?
    var enabled: Bool
    var frame: CGRect?
    var children: [Node]

    init(_ snapshot: XCUIElementSnapshot) {
        role = roleName(snapshot.elementType)
        let text = snapshot.label
        if !text.isEmpty {
            label = text
        } else {
            // macOS controls often carry their visible text in `title`.
            let title = snapshot.title
            label = title.isEmpty ? nil : title
        }
        identifier = snapshot.identifier.isEmpty ? nil : snapshot.identifier
        value = stringValue(snapshot.value)
        enabled = snapshot.isEnabled
        let rect = snapshot.frame
        frame = (rect.isNull || rect.isInfinite) ? nil : rect
        children = snapshot.children.map(Node.init)
    }

    func write(to json: inout JSONWriter) {
        json.raw("{\"role\":")
        json.string(role)
        json.raw(",\"label\":")
        json.string(label)
        json.raw(",\"identifier\":")
        json.string(identifier)
        json.raw(",\"value\":")
        json.string(value)
        json.raw(",\"enabled\":")
        json.bool(enabled)
        json.raw(",\"frame\":")
        if let frame {
            json.raw("[")
            json.number(Double(frame.origin.x))
            json.raw(",")
            json.number(Double(frame.origin.y))
            json.raw(",")
            json.number(Double(frame.size.width))
            json.raw(",")
            json.number(Double(frame.size.height))
            json.raw("]")
        } else {
            json.raw("null")
        }
        json.raw(",\"children\":[")
        for (i, child) in children.enumerated() {
            if i > 0 { json.raw(",") }
            child.write(to: &json)
        }
        json.raw("]}")
    }

    var count: Int { 1 + children.reduce(0) { $0 + $1.count } }

    /// First node in tree (pre-)order that matches the query.
    func first(matching query: ElementQuery) -> Node? {
        if query.matches(self) { return self }
        for child in children {
            if let found = child.first(matching: query) { return found }
        }
        return nil
    }
}

/// The protocol's Query, with the same rules as `swarmqa.driver.query`:
/// role equal ignoring case, label a case-insensitive substring,
/// identifier and value exact.
struct ElementQuery: CustomStringConvertible {
    var role: String?
    var label: String?
    var identifier: String?
    var value: String?

    init(_ body: JSONBody) throws {
        role = try body.string("role")
        label = try body.string("label")
        identifier = try body.string("identifier")
        value = try body.string("value")
        if [role, label, identifier, value].allSatisfy({ ($0 ?? "").isEmpty }) {
            throw RunnerFailure(.badRequest, "query needs at least one field")
        }
    }

    func matches(_ node: Node) -> Bool {
        if let role, !role.isEmpty, node.role.lowercased() != role.lowercased() { return false }
        if let label, !label.isEmpty {
            guard let have = node.label, have.range(of: label, options: .caseInsensitive) != nil else {
                return false
            }
        }
        if let identifier, !identifier.isEmpty, node.identifier != identifier { return false }
        if let value, node.value != value { return false }
        return true
    }

    var description: String {
        var fields: [String: String] = [:]
        if let role { fields["role"] = role }
        if let label { fields["label"] = label }
        if let identifier { fields["identifier"] = identifier }
        if let value { fields["value"] = value }
        let data = (try? JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys])) ?? Data()
        return String(data: data, encoding: .utf8) ?? "{}"
    }
}

private func stringValue(_ value: Any?) -> String? {
    switch value {
    case nil:
        return nil
    case let text as String:
        return text
    case let number as NSNumber:
        if CFGetTypeID(number) == CFBooleanGetTypeID() {
            return number.boolValue ? "1" : "0"
        }
        return number.stringValue
    case is NSNull:
        return nil
    case let other?:
        return String(describing: other)
    }
}

/// Lower-cased `XCUIElement.ElementType` names, indexed by raw value.
/// `staticText` becomes `text`, as PROTOCOL.md specifies.
private let roleNames: [String] = [
    "any", "other", "application", "group", "window", "sheet", "drawer", "alert", "dialog",
    "button", "radiobutton", "radiogroup", "checkbox", "disclosuretriangle", "popupbutton",
    "combobox", "menubutton", "toolbarbutton", "popover", "keyboard", "key", "navigationbar",
    "tabbar", "tabgroup", "toolbar", "statusbar", "table", "tablerow", "tablecolumn", "outline",
    "outlinerow", "browser", "collectionview", "slider", "pageindicator", "progressindicator",
    "activityindicator", "segmentedcontrol", "picker", "pickerwheel", "switch", "toggle", "link",
    "image", "icon", "searchfield", "scrollview", "scrollbar", "text", "textfield",
    "securetextfield", "datepicker", "textview", "menu", "menuitem", "menubar", "menubaritem",
    "map", "webview", "incrementarrow", "decrementarrow", "timeline", "ratingindicator",
    "valueindicator", "splitgroup", "splitter", "relevanceindicator", "colorwell", "helptag",
    "matte", "dockitem", "ruler", "rulermarker", "grid", "levelindicator", "cell", "layoutarea",
    "layoutitem", "handle", "stepper", "tab", "touchbar", "statusitem",
]

func roleName(_ type: XCUIElement.ElementType) -> String {
    let raw = Int(type.rawValue)
    return raw >= 0 && raw < roleNames.count ? roleNames[raw] : "other"
}
