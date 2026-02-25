// calendar_helper.swift — reads macOS Calendar events via EventKit.
//
// Usage (from CLI):
//   calendar_helper events                    → stdout JSON
//   calendar_helper events /path/to/out.json  → write to file
//   calendar_helper calendars                 → list calendars
//   calendar_helper auth                      → request access
//
// Launch via app bundle for TCC permissions:
//   open CalendarHelper.app --args events /path/to/out.json
//
// Build:
//   swiftc calendar_helper.swift -o calendar_helper

import EventKit
import Foundation

let store = EKEventStore()

// Wait for CalendarAgent to sync sources.
func waitForSources() {
    let semaphore = DispatchSemaphore(value: 0)
    store.requestFullAccessToEvents { _, _ in semaphore.signal() }
    semaphore.wait()

    for _ in 0..<40 {
        store.refreshSourcesIfNecessary()
        if store.sources.count > 1 { break }
        Thread.sleep(forTimeInterval: 0.25)
    }
}

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
    return "[]"
}

func requestAccess(outFile: String?) {
    let semaphore = DispatchSemaphore(value: 0)
    var granted = false
    store.requestFullAccessToEvents { ok, error in
        granted = ok
        if let error = error {
            fputs("error: \(error.localizedDescription)\n", stderr)
        }
        semaphore.signal()
    }
    semaphore.wait()

    let status = EKEventStore.authorizationStatus(for: .event)
    let sources = store.sources.map { $0.title }
    let result: [String: Any] = [
        "granted": granted,
        "status": status.rawValue,
        "status_name": statusName(status),
        "sources": sources,
        "calendars_count": store.calendars(for: .event).count
    ]
    writeOutput(toJSON(result), toFile: outFile)
}

func listCalendars(outFile: String?) {
    waitForSources()
    var items: [[String: Any]] = []
    for cal in store.calendars(for: .event) {
        items.append([
            "id": cal.calendarIdentifier,
            "title": cal.title,
            "source": cal.source?.title ?? "",
            "type": cal.type.rawValue
        ])
    }
    writeOutput(toJSON(items), toFile: outFile)
}

func listEvents(outFile: String?) {
    waitForSources()
    let now = Date()
    let start = now.addingTimeInterval(-86400)
    let end = now.addingTimeInterval(7 * 86400)
    let predicate = store.predicateForEvents(withStart: start, end: end, calendars: nil)
    let events = store.events(matching: predicate)

    var items: [[String: Any]] = []
    for ev in events {
        var attendeeNames: [String] = []
        if let attendees = ev.attendees {
            for a in attendees {
                attendeeNames.append(a.name ?? a.url.absoluteString)
            }
        }
        items.append([
            "uid": ev.eventIdentifier ?? "",
            "title": ev.title ?? "",
            "start": iso8601(ev.startDate),
            "end": iso8601(ev.endDate),
            "calendar": ev.calendar?.title ?? "",
            "location": ev.location ?? "",
            "all_day": ev.isAllDay,
            "recurring": ev.hasRecurrenceRules,
            "attendees": attendeeNames
        ])
    }
    items.sort { ($0["start"] as? String ?? "") < ($1["start"] as? String ?? "") }
    writeOutput(toJSON(items), toFile: outFile)
}

func statusName(_ status: EKAuthorizationStatus) -> String {
    switch status {
    case .notDetermined: return "notDetermined"
    case .restricted: return "restricted"
    case .denied: return "denied"
    case .fullAccess: return "fullAccess"
    case .writeOnly: return "writeOnly"
    case .authorized: return "authorized"
    @unknown default: return "unknown(\(status.rawValue))"
    }
}

func iso8601(_ date: Date) -> String {
    let fmt = ISO8601DateFormatter()
    fmt.formatOptions = [.withInternetDateTime]
    return fmt.string(from: date)
}

// Main
let args = CommandLine.arguments
let command = args.count > 1 ? args[1] : "events"
let outFile: String? = args.count > 2 ? args[2] : nil

switch command {
case "auth":
    requestAccess(outFile: outFile)
case "calendars":
    listCalendars(outFile: outFile)
case "events":
    listEvents(outFile: outFile)
default:
    fputs("usage: calendar_helper [auth|calendars|events] [output_file]\n", stderr)
    exit(1)
}
