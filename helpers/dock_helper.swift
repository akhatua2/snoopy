// dock_helper.swift — reads Dock badge counts and running status via macOS Accessibility API.
//
// Walks the Dock's AX tree to find AXApplicationDockItem children,
// extracting app name (AXTitle), badge text (AXStatusLabel), and
// running state (AXIsApplicationRunning).
//
// Usage:
//   dock_helper
//
// Output: JSON array to stdout:
//   [{"app":"Mail","badge":"3","running":true}, ...]
//
// Build:
//   swiftc dock_helper.swift -o dock_helper
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

// ── Dock reading ─────────────────────────────────────────────────────

func findDockPid() -> pid_t? {
    for app in NSWorkspace.shared.runningApplications {
        if app.bundleIdentifier == "com.apple.dock" {
            return app.processIdentifier
        }
    }
    return nil
}

func readDockItems() -> [[String: Any]] {
    guard let pid = findDockPid() else {
        fputs("Dock not running\n", stderr)
        return []
    }

    let ax = AXUIElementCreateApplication(pid)
    let topChildren = axChildren(ax)

    // The Dock's AX tree has multiple AXList children.
    // List 1 (index 0) contains running/pinned app dock items.
    // We check all lists for AXApplicationDockItem children.
    var items: [[String: Any]] = []

    for list in topChildren {
        let role = axStr(list, "AXRole")
        if role != "AXList" { continue }

        for child in axChildren(list) {
            let subrole = axStr(child, "AXSubrole")
            if subrole != "AXApplicationDockItem" { continue }

            let title = axStr(child, "AXTitle")
            if title.isEmpty { continue }

            let badge = axStr(child, "AXStatusLabel")
            let runningRef = axAttr(child, "AXIsApplicationRunning")
            let running = (runningRef as? Bool) ?? false

            items.append([
                "app": title,
                "badge": badge,
                "running": running,
            ])
        }
    }

    return items
}

// ── Output ───────────────────────────────────────────────────────────

let items = readDockItems()

if let data = try? JSONSerialization.data(withJSONObject: items, options: [.sortedKeys]),
   let str = String(data: data, encoding: .utf8) {
    print(str)
} else {
    print("[]")
}
