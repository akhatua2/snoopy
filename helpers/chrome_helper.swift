// chrome_helper.swift — reads visible page content from Chrome via macOS Accessibility API.
//
// Chrome (Chromium/Electron) requires AXEnhancedUserInterface to expose its DOM
// through the accessibility tree. This helper sets that attribute and walks the
// AX tree to extract all visible text content from the current page.
//
// Captures: headings, paragraphs, links, input fields, button labels, image alt text.
//
// Usage:
//   chrome_helper content [output_file]  → scrape visible page content
//   chrome_helper check                  → check if Chrome is running
//
// Build:
//   swiftc chrome_helper.swift -o chrome_helper

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

// ── Chrome Process ──────────────────────────────────────────────────

let chromeBundleID = "com.google.Chrome"

func findChromePid() -> pid_t? {
    for app in NSWorkspace.shared.runningApplications {
        if app.bundleIdentifier == chromeBundleID {
            return app.processIdentifier
        }
    }
    return nil
}

// ── Content Extraction ──────────────────────────────────────────────

struct ContentItem {
    let type: String
    let text: String
}

func extractPageContent(_ webArea: AXUIElement) -> [ContentItem] {
    var items: [ContentItem] = []
    walkContent(webArea, &items, depth: 0)
    return items
}

func walkContent(_ e: AXUIElement, _ items: inout [ContentItem], depth: Int) {
    if depth > 50 { return }

    let role = axStr(e, "AXRole")
    let value = axStr(e, "AXValue")
    let title = axStr(e, "AXTitle")
    let desc = axStr(e, "AXDescription")

    switch role {
    case "AXHeading":
        // Headings: use title or collect child text
        let headingText = !title.isEmpty ? title : collectText(e)
        let trimmed = headingText.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            items.append(ContentItem(type: "heading", text: trimmed))
        }
        return  // Don't recurse — already collected child text

    case "AXStaticText":
        let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty {
            items.append(ContentItem(type: "text", text: text))
        }
        return

    case "AXLink":
        let linkText = !title.isEmpty ? title : collectText(e)
        let trimmed = linkText.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            items.append(ContentItem(type: "link", text: trimmed))
        }
        return  // Don't recurse — already collected child text

    case "AXTextField", "AXTextArea", "AXSearchField", "AXComboBox":
        let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty {
            items.append(ContentItem(type: "input", text: text))
        } else {
            // Check placeholder
            let placeholder = axStr(e, "AXPlaceholderValue").trimmingCharacters(in: .whitespacesAndNewlines)
            if !placeholder.isEmpty {
                items.append(ContentItem(type: "input", text: "[placeholder: \(placeholder)]"))
            }
        }
        return

    case "AXButton":
        let btnText = !title.isEmpty ? title : desc
        let trimmed = btnText.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            items.append(ContentItem(type: "button", text: trimmed))
        }
        return

    case "AXImage":
        let altText = !desc.isEmpty ? desc : title
        let trimmed = altText.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            items.append(ContentItem(type: "image", text: trimmed))
        }
        return

    case "AXWebArea":
        // Nested web area (iframe) — recurse into it
        break

    default:
        break
    }

    // Recurse into children
    for child in axChildren(e) {
        walkContent(child, &items, depth: depth + 1)
    }
}

func collectText(_ e: AXUIElement) -> String {
    var parts: [String] = []
    gatherText(e, &parts, depth: 0)
    return parts.joined(separator: " ")
}

func gatherText(_ e: AXUIElement, _ parts: inout [String], depth: Int) {
    if depth > 10 { return }
    let role = axStr(e, "AXRole")
    if role == "AXStaticText" {
        let val = axStr(e, "AXValue").trimmingCharacters(in: .whitespacesAndNewlines)
        if !val.isEmpty { parts.append(val) }
    }
    for child in axChildren(e) {
        gatherText(child, &parts, depth: depth + 1)
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

func scrapeContent(outFile: String?) {
    guard let pid = findChromePid() else {
        writeOutput(toJSON(["error": "chrome_not_running"]), toFile: outFile)
        return
    }

    let ax = AXUIElementCreateApplication(pid)
    AXUIElementSetAttributeValue(ax, "AXEnhancedUserInterface" as CFString, true as CFBoolean)

    guard let windows = axAttr(ax, "AXWindows") as? [AXUIElement], !windows.isEmpty else {
        writeOutput(toJSON(["error": "no_windows"]), toFile: outFile)
        return
    }

    let mainWindow = windows[0]

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

    let url = axStr(wa, "AXURL")
    let pageTitle = axStr(wa, "AXTitle")
    let contentItems = extractPageContent(wa)

    let contentArray = contentItems.map { item -> [String: String] in
        ["type": item.type, "text": item.text]
    }

    let result: [String: Any] = [
        "url": url,
        "title": pageTitle,
        "content": contentArray,
    ]

    writeOutput(toJSON(result), toFile: outFile)
}

func checkChrome(outFile: String?) {
    let result: [String: Any] = ["running": findChromePid() != nil]
    writeOutput(toJSON(result), toFile: outFile)
}

// ── CLI ──────────────────────────────────────────────────────────────

let args = CommandLine.arguments
let command = args.count > 1 ? args[1] : "content"
let outFile: String? = args.count > 2 ? args[2] : nil

switch command {
case "content":
    scrapeContent(outFile: outFile)
case "check":
    checkChrome(outFile: outFile)
default:
    fputs("usage: chrome_helper [content|check] [output_file]\n", stderr)
    exit(1)
}
