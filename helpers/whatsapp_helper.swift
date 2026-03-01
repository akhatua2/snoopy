// whatsapp_helper.swift — reads WhatsApp content via macOS Accessibility API.
//
// WhatsApp (Catalyst app) exposes a well-structured AX tree without needing
// AXEnhancedUserInterface. This helper walks the tree to extract:
// - Chat list sidebar: AXButtons inside ChatListView_TableView
//   Each button has desc=contact_name, value=message_preview_with_metadata
// - Current open chat messages (when a conversation is open)
// - Chat header info (name, members for group chats)
//
// Usage:
//   whatsapp_helper messages [output_file]  → scrape visible content
//   whatsapp_helper check                   → check if WhatsApp is running
//
// Build:
//   swiftc whatsapp_helper.swift -o whatsapp_helper

import Cocoa

// ── AX Helpers ───────────────────────────────────────────────────────

func axAttr(_ e: AXUIElement, _ attr: String) -> CFTypeRef? {
    var ref: CFTypeRef?
    AXUIElementCopyAttributeValue(e, attr as CFString, &ref)
    return ref
}

func axStr(_ e: AXUIElement, _ attr: String) -> String {
    (axAttr(e, attr) as? String) ?? ""
}

func axChildren(_ e: AXUIElement) -> [AXUIElement] {
    (axAttr(e, "AXChildren") as? [AXUIElement]) ?? []
}

func axRole(_ e: AXUIElement) -> String { axStr(e, "AXRole") }
func axValue(_ e: AXUIElement) -> String { axStr(e, "AXValue") }
func axDesc(_ e: AXUIElement) -> String { axStr(e, "AXDescription") }
func axTitle(_ e: AXUIElement) -> String { axStr(e, "AXTitle") }
func axIdentifier(_ e: AXUIElement) -> String { axStr(e, "AXIdentifier") }

// Strip invisible Unicode markers (LTR marks etc) that WhatsApp inserts
func clean(_ s: String) -> String {
    s.replacingOccurrences(of: "\u{200E}", with: "")
     .replacingOccurrences(of: "\u{200F}", with: "")
     .trimmingCharacters(in: .whitespacesAndNewlines)
}

// ── WhatsApp Process ────────────────────────────────────────────────

let whatsappBundleID = "net.whatsapp.WhatsApp"

func findWhatsAppPid() -> pid_t? {
    for app in NSWorkspace.shared.runningApplications {
        if app.bundleIdentifier == whatsappBundleID {
            return app.processIdentifier
        }
    }
    return nil
}

// ── Find element by identifier ──────────────────────────────────────

func findByIdentifier(_ e: AXUIElement, _ id: String, maxDepth: Int = 20, depth: Int = 0) -> AXUIElement? {
    if depth > maxDepth { return nil }
    if axIdentifier(e) == id { return e }
    for child in axChildren(e) {
        if let found = findByIdentifier(child, id, maxDepth: maxDepth, depth: depth + 1) {
            return found
        }
    }
    return nil
}

func findByDesc(_ e: AXUIElement, _ desc: String, maxDepth: Int = 20, depth: Int = 0) -> AXUIElement? {
    if depth > maxDepth { return nil }
    if clean(axDesc(e)) == desc { return e }
    for child in axChildren(e) {
        if let found = findByDesc(child, desc, maxDepth: maxDepth, depth: depth + 1) {
            return found
        }
    }
    return nil
}

// ── Chat List Parsing ───────────────────────────────────────────────
// Structure: AXGroup id=ChatListView_TableView > AXButton children
// Each AXButton: desc="Contact Name", value="message preview with metadata"

func scrapeChatList(_ root: AXUIElement) -> [[String: Any]] {
    guard let tableView = findByIdentifier(root, "ChatListView_TableView") else {
        return []
    }

    var chats: [[String: Any]] = []
    for child in axChildren(tableView) {
        let role = axRole(child)
        // Active chat shows as AXStaticText, others as AXButton
        guard role == "AXButton" || role == "AXStaticText" else { continue }

        let name = clean(axDesc(child))
        let value = clean(axValue(child))

        if name.isEmpty { continue }

        var entry: [String: Any] = ["name": name]
        if !value.isEmpty {
            entry["preview"] = value
        }
        chats.append(entry)
    }
    return chats
}

// ── Chat Messages Parsing ───────────────────────────────────────────
// Structure: AXGroup id=ChatMessagesTableView > children
// Message bubbles are AXStaticText id=WAMessageBubbleTableViewCell
// with desc containing full message text, sender, timestamp, status.

func scrapeMessages(_ root: AXUIElement) -> [[String: Any]] {
    guard let msgTable = findByIdentifier(root, "ChatMessagesTableView") else {
        return []
    }

    var messages: [[String: Any]] = []
    walkMessageChildren(msgTable, &messages, depth: 0)
    return messages
}

func walkMessageChildren(_ e: AXUIElement, _ messages: inout [[String: Any]], depth: Int) {
    if depth > 10 { return }

    for child in axChildren(e) {
        let role = axRole(child)
        let identifier = axIdentifier(child)
        let desc = clean(axDesc(child))

        // Message bubbles: AXStaticText id=WAMessageBubbleTableViewCell
        if identifier == "WAMessageBubbleTableViewCell" {
            if !desc.isEmpty {
                messages.append(["text": desc])
            }
            continue
        }

        // Day separator headings (e.g. "Today", "Yesterday")
        if role == "AXHeading" {
            if !desc.isEmpty {
                messages.append(["text": desc, "type": "date_separator"])
            }
            // Check nested headings
            for gc in axChildren(child) {
                let gcDesc = clean(axDesc(gc))
                if axRole(gc) == "AXHeading" && !gcDesc.isEmpty {
                    messages.append(["text": gcDesc, "type": "date_separator"])
                }
            }
            continue
        }

        // System messages (encryption notice, etc) inside AXGroup
        if role == "AXGroup" {
            for gc in axChildren(child) {
                let gcDesc = clean(axDesc(gc))
                if axRole(gc) == "AXStaticText" && !gcDesc.isEmpty {
                    messages.append(["text": gcDesc, "type": "system"])
                }
            }
            continue
        }

        walkMessageChildren(child, &messages, depth: depth + 1)
    }
}

// ── Chat Header Parsing ─────────────────────────────────────────────
// Structure: AXHeading id=NavigationBar_HeaderViewButton
//   desc="Chat Name", value="Members: Person1, Person2, You"

func scrapeChatHeader(_ root: AXUIElement) -> [String: Any]? {
    guard let headerBtn = findByIdentifier(root, "NavigationBar_HeaderViewButton") else {
        return nil
    }

    let chatName = clean(axDesc(headerBtn))
    let membersRaw = clean(axValue(headerBtn))

    if chatName.isEmpty { return nil }

    var header: [String: Any] = ["name": chatName]
    if !membersRaw.isEmpty {
        // Strip "Members: " prefix if present
        var members = membersRaw
        if members.hasPrefix("Members:") {
            members = String(members.dropFirst("Members:".count)).trimmingCharacters(in: .whitespaces)
        }
        header["members"] = members
    }
    return header
}

// ── Output ───────────────────────────────────────────────────────────

func writeOutput(_ str: String, toFile path: String?) {
    if let path = path {
        try? str.write(toFile: path, atomically: true, encoding: .utf8)
        fputs("wrote \(str.count) bytes to \(path)\n", stderr)
    } else {
        print(str)
    }
}

func toJSON(_ obj: Any) -> String {
    if let data = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys]),
       let str = String(data: data, encoding: .utf8) {
        return str
    }
    return "{}"
}

// ── Commands ─────────────────────────────────────────────────────────

func scrapeAll(outFile: String?) {
    guard let pid = findWhatsAppPid() else {
        writeOutput(toJSON(["error": "whatsapp_not_running"]), toFile: outFile)
        return
    }

    let ax = AXUIElementCreateApplication(pid)

    guard let windows = axAttr(ax, "AXWindows") as? [AXUIElement], !windows.isEmpty else {
        writeOutput(toJSON(["error": "no_windows"]), toFile: outFile)
        return
    }

    let mainWindow = windows[0]

    let chatList = scrapeChatList(mainWindow)
    let messages = scrapeMessages(mainWindow)
    let header = scrapeChatHeader(mainWindow)

    var result: [String: Any] = [
        "chat_list": chatList,
        "messages": messages,
    ]

    if let header = header {
        result["chat_name"] = header["name"] ?? ""
        result["chat_members"] = header["members"] ?? ""
    }

    writeOutput(toJSON(result), toFile: outFile)
}

func checkWhatsApp(outFile: String?) {
    let result: [String: Any] = ["running": findWhatsAppPid() != nil]
    writeOutput(toJSON(result), toFile: outFile)
}

// ── CLI ──────────────────────────────────────────────────────────────

let args = CommandLine.arguments
let command = args.count > 1 ? args[1] : "messages"
let outFile: String? = args.count > 2 ? args[2] : nil

switch command {
case "messages":
    scrapeAll(outFile: outFile)
case "check":
    checkWhatsApp(outFile: outFile)
default:
    fputs("usage: whatsapp_helper [messages|check] [output_file]\n", stderr)
    exit(1)
}
