```                                                                
 .oooo.o ooo. .oo.    .ooooo.   .ooooo.  oo.ooooo.  oooo    ooo
d88(  "8 `888P"Y88b  d88' `88b d88' `88b  888' `88b  `88.  .8'
`"Y88b.   888   888  888   888 888   888  888   888   `88..8'
o.  )88b  888   888  888   888 888   888  888   888    `888'
8""888P' o888o o888o `Y8bod8P' `Y8bod8P'  888bod8P'     .8'
                                          888       .o..P'
                                         o888o      `Y8P'
```
Snoopy is a local-first macOS daemon that continuously records your digital
activity into a single SQLite database. Every app switch, browser visit,
terminal command, message, and clipboard copy — logged with timestamps,
durations, and context. The data never leaves your machine.

## What It Tracks

| | Source | Table | Interval |
|---|---|---|---|
| **Focus & Input** | | | |
| Active window | `CGWindowList` + `NSWorkspace` | `window_events` | 2s |
| Keyboard / mouse idle | `CGEventSource` | (in `window_events`) | |
| App launch / quit | `ps` process diffing | `app_events` | 10s |
| Screen lock | `IORegistry` | `system_events` | 5s |
| **Browsing** | | | |
| Page visits | Chrome / Arc / Safari / Firefox | `browser_events` | 30s |
| Active tab | AppleScript (Chromium) | (in `window_events`) | |
| **Communication** | | | |
| iMessages | `~/Library/Messages/chat.db` | `message_events` | 15s |
| Email metadata | Mail.app Envelope Index | `mail_events` | 60s |
| Notifications | macOS notification center | `notification_events` | 30s |
| **Development** | | | |
| Shell commands | `~/.zsh_history` (extended) | `shell_events` | 10s |
| Claude sessions | `~/.claude/projects/*.jsonl` | `claude_events` | 15s |
| File changes | `FSEvents` (push-based) | `file_events` | live |
| Clipboard | `NSPasteboard` | `clipboard_events` | 2s |
| **Environment** | | | |
| WiFi / Battery / Audio / Network / Media / Location | various macOS APIs | respective tables | 3s–5m |
| Calendar / Oura ring | EventKit / Oura API | `calendar_events` / `oura_daily` | 30m / 24h |

---

## Installation

```bash
git clone https://github.com/akhatua2/snoopy.git && cd snoopy
uv sync
./install.sh
```

### Permissions

Grant in **System Settings > Privacy & Security**:

| Permission | Why |
|---|---|
| Accessibility | Window titles, keyboard idle |
| Full Disk Access | Browser history, iMessage, notifications, Mail |
| Location Services | GPS coordinates |

Add to `~/.zshrc`: `setopt EXTENDED_HISTORY` and `setopt INC_APPEND_HISTORY`.
Optional: `brew install nowplaying-cli` · Set `OURA_PAT=your_token` in `.env`.

---

## Usage

```bash
launchctl list | grep snoopy                                       # status
tail -f data/snoopy.log                                            # logs
kill -HUP $(cat data/snoopy.pid)                                   # reload
launchctl unload ~/Library/LaunchAgents/com.snoopy.daemon.plist    # stop
./uninstall.sh                                                     # remove
```

### Querying

```bash
# What were you doing at 3 PM?
sqlite3 data/snoopy.db "
  SELECT datetime(timestamp, 'unixepoch', 'localtime') as t,
         app_name, window_title
  FROM window_events
  WHERE t BETWEEN '2026-02-25 15:00:00' AND '2026-02-25 15:30:00'
  ORDER BY timestamp;
"

# Top apps today
sqlite3 data/snoopy.db "
  SELECT app_name, ROUND(SUM(duration_s)/3600, 1) as hours
  FROM window_events
  WHERE date(timestamp, 'unixepoch', 'localtime') = date('now')
  GROUP BY app_name ORDER BY hours DESC;
"
```

---

## Privacy

All data stays local. No network calls except **Oura API** (opt-in) and
**Location Services** (opt-in). The DB is a plain SQLite file — delete it
and it's gone. Clipboard from password managers is auto-excluded. No telemetry.

**MIT License**
