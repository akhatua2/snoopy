// slack_helper.swift — reads Slack messages via macOS Accessibility API.
//
// Slack (Electron) requires AXEnhancedUserInterface to expose its DOM
// through the accessibility tree. This helper sets that attribute and
// walks the AX tree to extract visible messages.
//
// Usage:
//   slack_helper messages [output_file]  → scrape visible messages
//   slack_helper check                   → check if Slack is running
//
// Build:
//   swiftc slack_helper.swift -o slack_helper
//
// Requires: Accessibility permission in System Preferences

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

func findByRole(_ e: AXUIElement, _ role: String, maxDepth: Int = 12, depth: Int = 0) -> AXUIElement? {
    if depth > maxDepth { return nil }
    if axStr(e, "AXRole") == role { return e }
    for child in axChildren(e) {
        if let found = findByRole(child, role, maxDepth: maxDepth, depth: depth + 1) {
            return found
        }
    }
    return nil
}

// ── Time Detection ───────────────────────────────────────────────────

func hasTimeMarker(_ s: String) -> Bool {
    s.contains("AM.") || s.contains("PM.") ||
    s.contains("AM,") || s.contains("PM,") ||
    s.contains("Yesterday") || s.contains("Today") ||
    s.range(of: "\\d{1,2}:\\d{2}", options: .regularExpression) != nil
}

// ── Slack Helpers ────────────────────────────────────────────────────

func findSlackPid() -> pid_t? {
    for app in NSWorkspace.shared.runningApplications {
        if app.bundleIdentifier == "com.tinyspeck.slackmacgap" {
            return app.processIdentifier
        }
    }
    return nil
}

func parseWindowTitle(_ title: String) -> (workspace: String, channel: String) {
    // Format: "Channel - Workspace - Slack"
    // or:     "Channel - Workspace - N new item(s) - Slack"
    // or:     "Workspace - Slack"
    let parts = title.components(separatedBy: " - ")
    if parts.count >= 4, parts.last == "Slack" {
        return (workspace: parts[1], channel: parts[0])
    } else if parts.count == 3, parts.last == "Slack" {
        return (workspace: parts[1], channel: parts[0])
    } else if parts.count == 2, parts.last == "Slack" {
        return (workspace: parts[0], channel: "")
    }
    return (workspace: "", channel: "")
}

// ── Message Parsing ──────────────────────────────────────────────────

func parseReactions(_ group: AXUIElement) -> [[String: Any]] {
    var reactions: [[String: Any]] = []
    for child in axChildren(group) {
        let desc = axStr(child, "AXDescription")
        guard desc.contains("emoji") || desc.contains("reaction") else { continue }
        var emoji = ""
        var count = ""
        for gc in axChildren(child) {
            let gcRole = axStr(gc, "AXRole")
            if gcRole == "AXImage" {
                emoji = axStr(gc, "AXDescription")
            } else if gcRole == "AXStaticText" {
                count = axStr(gc, "AXValue")
            }
        }
        if !emoji.isEmpty {
            reactions.append(["emoji": emoji, "count": count])
        }
    }
    return reactions
}

func parseMessage(_ element: AXUIElement) -> [String: Any]? {
    let groupTitle = axStr(element, "AXTitle")
    guard !groupTitle.isEmpty, hasTimeMarker(groupTitle) else { return nil }

    // Extract sender from title prefix ("Sender: rest...")
    var sender = ""
    if let colonIdx = groupTitle.firstIndex(of: ":") {
        sender = String(groupTitle[groupTitle.startIndex..<colonIdx])
            .trimmingCharacters(in: .whitespaces)
    }

    var textParts: [String] = []
    var timestamp = ""
    var reactions: [[String: Any]] = []
    var isEdited = false
    var threadReplies = ""

    // Recursively walk children — data is nested 2-3 levels deep in AXGroup wrappers
    func walk(_ e: AXUIElement, _ depth: Int) {
        if depth > 8 { return }
        for child in axChildren(e) {
            let role = axStr(child, "AXRole")
            let title = axStr(child, "AXTitle")
            let desc = axStr(child, "AXDescription")
            let val = axStr(child, "AXValue")

            switch role {
            case "AXLink":
                if timestamp.isEmpty,
                   desc.contains(" at ") || desc.contains("AM") || desc.contains("PM") ||
                   desc.contains("Yesterday") || desc.contains("Today") {
                    timestamp = desc
                }
                // Don't recurse into links (they contain display-only AXStaticText)
            case "AXStaticText":
                let trimmed = val.trimmingCharacters(in: .whitespacesAndNewlines)
                if trimmed == "(edited)" {
                    isEdited = true
                } else if !trimmed.isEmpty {
                    textParts.append(trimmed)
                }
            case "AXGroup":
                if desc == "Reactions" {
                    reactions = parseReactions(child)
                } else if desc.lowercased().contains("repl") {
                    threadReplies = desc
                } else {
                    walk(child, depth + 1)
                }
            case "AXButton":
                if title.lowercased().contains("repl") || desc.lowercased().contains("repl") {
                    threadReplies = title.isEmpty ? desc : title
                }
                // Don't recurse into buttons
            default:
                walk(child, depth + 1)
            }
        }
    }

    walk(element, 0)

    let text = textParts.joined(separator: "\n")
    if sender.isEmpty && text.isEmpty { return nil }

    return [
        "sender": sender,
        "text": text,
        "timestamp": timestamp,
        "reactions": reactions,
        "is_edited": isEdited,
        "thread_replies": threadReplies,
    ]
}

func findMessages(_ element: AXUIElement, depth: Int = 0) -> [[String: Any]] {
    if depth > 15 { return [] }
    var messages: [[String: Any]] = []

    let role = axStr(element, "AXRole")
    let title = axStr(element, "AXTitle")

    if role == "AXGroup" && !title.isEmpty && hasTimeMarker(title) {
        if let msg = parseMessage(element) {
            messages.append(msg)
        }
        return messages
    }

    for child in axChildren(element) {
        messages += findMessages(child, depth: depth + 1)
    }
    return messages
}

// ── Sidebar Parsing ──────────────────────────────────────────────

func scrapeSidebar(_ webArea: AXUIElement) -> [[String: Any]] {
    var items: [[String: Any]] = []
    findSidebarRows(webArea, &items, depth: 0)
    return items
}

func findSidebarRows(_ e: AXUIElement, _ items: inout [[String: Any]], depth: Int) {
    if depth > 20 { return }
    let role = axStr(e, "AXRole")
    let desc = axStr(e, "AXDescription")

    // Sidebar entries are AXRow with a desc like:
    //   "@Kevin Xiang Li (active) (has 1 notification)"
    //   "general, Stanford NLP Group"
    //   "cooperator-swat (private, is a member, 10 members, Stanford University)"
    if role == "AXRow" && !desc.isEmpty {
        // Check for notification badge in desc
        var notifications = 0
        if let range = desc.range(of: "has (\\d+) notification", options: .regularExpression) {
            let match = desc[range]
            if let numRange = match.range(of: "\\d+", options: .regularExpression) {
                notifications = Int(match[numRange]) ?? 0
            }
        }

        // Also check children for standalone badge counts
        if notifications == 0 {
            func findBadge(_ elem: AXUIElement, _ d: Int) -> Int {
                if d > 4 { return 0 }
                for child in axChildren(elem) {
                    let cr = axStr(child, "AXRole")
                    let cv = axStr(child, "AXValue")
                    // Badge is usually a standalone AXStaticText with a numeric value
                    // inside a small container, not part of the channel name text
                    if cr == "AXStaticText" {
                        let trimmed = cv.trimmingCharacters(in: .whitespaces)
                        if let n = Int(trimmed), n > 0, n < 10000 {
                            // Verify it's a badge by checking it's short
                            if trimmed.count <= 5 {
                                return n
                            }
                        }
                    }
                    let found = findBadge(child, d + 1)
                    if found > 0 { return found }
                }
                return 0
            }
            notifications = findBadge(e, 0)
        }

        // Only include items that have notifications
        if notifications > 0 {
            // Extract name from desc
            var name = desc
            // Strip @ prefix for DMs
            if name.hasPrefix("@") {
                name = String(name.dropFirst())
            }
            // Strip parenthetical status info for cleaner name
            if let parenIdx = name.firstIndex(of: "(") {
                name = String(name[name.startIndex..<parenIdx])
                    .trimmingCharacters(in: .whitespaces)
            }
            // Strip comma-separated workspace suffix
            if let commaIdx = name.firstIndex(of: ",") {
                let afterComma = name[name.index(after: commaIdx)...]
                    .trimmingCharacters(in: .whitespaces)
                // If after comma looks like a workspace name (not a person), truncate
                if !afterComma.isEmpty && afterComma.first?.isUppercase == true {
                    // Could be "Person1, Person2" (group DM) or "channel, Workspace"
                    // Keep full name for group DMs
                }
            }

            items.append([
                "name": name,
                "description": desc,
                "unread_count": notifications,
            ])
        }
    }

    for child in axChildren(e) {
        findSidebarRows(child, &items, depth: depth + 1)
    }
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

func scrapeMessages(outFile: String?) {
    guard let pid = findSlackPid() else {
        writeOutput(toJSON(["error": "slack_not_running"]), toFile: outFile)
        return
    }

    let ax = AXUIElementCreateApplication(pid)
    AXUIElementSetAttributeValue(ax, "AXEnhancedUserInterface" as CFString, true as CFBoolean)

    guard let windows = axAttr(ax, "AXWindows") as? [AXUIElement], !windows.isEmpty else {
        writeOutput(toJSON(["error": "no_windows"]), toFile: outFile)
        return
    }

    let mainWindow = windows[0]
    let windowTitle = axStr(mainWindow, "AXTitle")
    let (workspace, channel) = parseWindowTitle(windowTitle)

    // Find WebArea — retry with sleep if AXEnhancedUserInterface just took effect
    var webArea = findByRole(mainWindow, "AXWebArea")
    if webArea == nil {
        Thread.sleep(forTimeInterval: 1.5)
        webArea = findByRole(mainWindow, "AXWebArea")
    }

    guard let wa = webArea else {
        writeOutput(toJSON(["error": "no_web_area"]), toFile: outFile)
        return
    }

    let messages = findMessages(wa)
    let unread = scrapeSidebar(wa)

    let result: [String: Any] = [
        "workspace": workspace,
        "channel_name": channel,
        "messages": messages,
        "unread": unread,
    ]

    writeOutput(toJSON(result), toFile: outFile)
}

func checkSlack(outFile: String?) {
    let result: [String: Any] = ["running": findSlackPid() != nil]
    writeOutput(toJSON(result), toFile: outFile)
}

// ── CLI ──────────────────────────────────────────────────────────────

let args = CommandLine.arguments
let command = args.count > 1 ? args[1] : "messages"
let outFile: String? = args.count > 2 ? args[2] : nil

switch command {
case "messages":
    scrapeMessages(outFile: outFile)
case "check":
    checkSlack(outFile: outFile)
default:
    fputs("usage: slack_helper [messages|check] [output_file]\n", stderr)
    exit(1)
}
