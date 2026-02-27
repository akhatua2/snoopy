# Snoopy Logging Improvement Proposal

## Making `p(next_action | context)` actually learnable

---

## 1. The Problem: We Log Events, Not Behavior

Today's data: **14 hours, 12,051 raw events across 9 active tables.** When we built a
next-action prediction dataset from this, we got 1,764 rows. Here's what they look like:

### Current dataset composition

```
Action type distribution (1,764 rows):
  focus (app switch)        706  (40.0%)  ####################
  claude (protocol)         362  (20.5%)  ##########
  browse (page visit)       281  (15.9%)  #######
  app launch/quit           269  (15.2%)  #######
  clipboard                 123  ( 7.0%)  ###
  shell command              15  ( 0.9%)
  message sent                8  ( 0.5%)
```

**60% of the dataset is focus switches and claude protocol events.** Only 0.5% captures
the user actually communicating (sending a message). The prediction target is dominated
by "which app got focused next" — not a meaningful behavioral signal.

### Current stimuli breakdown

```
Stimuli in the dataset:
  app_launch (system noise)   3,536  (95%)
  msg_recv (real trigger)       185  ( 5%)
```

**95% of stimuli are system app launches** — Nudge, CoreLocationAgent, liquiddetectiond,
Safari/Music/TV auto-launching. Only 5% are real behavioral triggers.

---

## 2. Evidence: What The Raw Data Actually Looks Like

### 2.1 A real 5-minute window from today (22:38–22:43)

Here's every event snoopy logged during a 5-minute span where you received a message
about ClickHouse, researched it, then asked Claude about it:

```
22:38:38  window   Messages:                              ← you opened Messages
22:38:46  window   Google Chrome:                         ← switched to Chrome
22:38:46  browser  Clickhouse - Google Search              ← searched for it
22:38:50  browser  Fast Open-Source OLAP DBMS - ClickHouse ← clicked result
22:39:04  window   Code:                                  ← switched to VS Code
22:39:05  file     created: .git/refs/remotes/origin/HEAD.lock   ← NOISE: git auto-fetch
22:39:05  file     modified: .git/FETCH_HEAD                     ← NOISE: git auto-fetch
22:39:05  file     created: .git/objects/maintenance.lock        ← NOISE: git maintenance
22:39:12  claude   assistant_text: No response requested.
22:39:22  claude   user: clickhouse coudl we use this for better pd?
22:39:28  window   Messages:                              ← quick check
22:39:30  window   Google Chrome:                         ← back to Chrome
22:39:39  claude   assistant_text: Depends on what you mean...
22:39:40  file     modified: snoopy.db-wal                       ← NOISE: our own DB write
22:39:40  claude   user: db**
22:39:47  window   Code:                                  ← back to VS Code
22:39:47  claude   assistant_text: Ah — DuckDB...
22:39:48  file     modified: snoopy.db-wal                       ← NOISE: our own DB write
22:39:51  window   Google Chrome:                         ← tab switch
22:39:57  window   iTerm2:                                ← terminal
22:40:19  window   Google Chrome:                         ← back
22:40:23  window   Code:                                  ← back
22:40:29  claude   user: no clickhouse.
22:40:33  claude   assistant_text: Got it — ClickHouse as backend...
22:40:37  claude   tool_use:AskUserQuestion: ...
22:40:47  claude   user:                                  ← NOISE: empty turn
22:40:58  claude   assistant_text: Ah, you're just evaluating...
22:40:59  file     modified: snoopy.db-wal                       ← NOISE: DB write
22:41:15  window   Messages:                              ← check messages
22:41:33  window   Code:                                  ← back
22:41:54  claude   user: well can you see what all we have...
22:41:57  window   Google Chrome:                         ← tab
22:41:58  claude   tool_use:Bash: sqlite3 ...
22:42:00  claude   user:                                  ← NOISE: empty turn
22:42:07  app      launch: Safari                         ← NOISE: system auto-launch
22:42:07  app      launch: Music                          ← NOISE: system auto-launch
22:42:07  app      launch: TV                             ← NOISE: system auto-launch
22:42:07  claude   tool_use:Bash: sqlite3 ...
22:42:07  claude   user:                                  ← NOISE: empty turn
22:42:15  claude   assistant_text: Data is still there...
22:42:16  file     modified: snoopy.db-wal                       ← NOISE: DB write
22:42:27  app      quit: Safari                           ← NOISE: system auto-quit
22:42:27  app      quit: Music                            ← NOISE: system auto-quit
22:42:27  app      quit: TV                               ← NOISE: system auto-quit
22:43:03  window   iTerm2:                                ← terminal
```

**42 events in 5 minutes.** Of those:
- 14 are noise (git maintenance, DB writes, system app launches/quits, empty claude turns)
- 10 are window focus with empty titles (we know "Chrome" but not what tab)
- The actual story: "Got a message about ClickHouse → Googled it → asked Claude about it
  as a DB alternative → Claude misunderstood → clarified → moved on"

### 2.2 What we should have logged instead

```
22:38  STIMULUS   iMessage from [contact]: "Clickhouse" + "See if you might need that"
22:38  ACTION     Searched Google: "Clickhouse"
22:38  ACTION     Read: clickhouse.com (Fast Open-Source OLAP DBMS)
22:39  ACTION     Asked Claude: "clickhouse could we use this for better db?"
22:40  ACTION     Claude discussion: evaluated ClickHouse vs SQLite for snoopy
22:41  ACTION     Checked Messages briefly, returned to coding
22:42  ACTION     Asked Claude to inspect current DB schema
```

**7 events instead of 42.** Every one is meaningful. The causal chain is clear.

---

## 3. Quantified Problems in Today's Data

### 3.1 Window titles are empty 65% of the time

```
App             Events   % Empty Title
─────────────   ──────   ─────────────
Google Chrome     689       66.6%
iTerm2            554       54.2%
VS Code           352       68.8%
Slack             169       67.5%
Mail               35       68.6%
Messages           31       61.3%
Notes              16       81.3%
```

**We know the app but not what's in it** for 2 out of 3 events. "Google Chrome" could mean
reading an arxiv paper, scrolling Instagram, or writing an email in Gmail. These are
completely different behaviors.

### 3.2 App events are 97% system noise

```
App events today (594 total):
  Cisco Socket Filter     114  (19%)   ← VPN daemon cycling
  TV                       86  (14%)   ← macOS auto-launch
  Safari                   86  (14%)   ← macOS auto-launch
  Music                    84  (14%)   ← macOS auto-launch
  Nudge                    71  (12%)   ← update reminder
  CoreServicesUIAgent      44  ( 7%)   ← system UI
  CoreLocationAgent        44  ( 7%)   ← location daemon
  liquiddetectiond         21  ( 4%)   ← hardware sensor
  ManagedClient            18  ( 3%)   ← MDM
  ────────────────────────────────────
  User-initiated apps      26  ( 4%)   ← the actual signal
```

### 3.3 File events are 9,500+ per day, mostly noise

```
File events today: 9,552
  .git/ internal files      264   ← auto-fetch, maintenance
  __pycache__/               26   ← Python bytecode
  Other (user files)      9,228   ← but dominated by build artifacts, DB writes
```

The `snoopy.db-wal` file alone triggers dozens of file events per hour from our own writes.

### 3.4 Claude events: 62% of user turns are empty

```
Claude events today: 371
  user turns         115    (62% are empty — protocol acknowledgments)
  assistant_text      63
  tool_use:Bash       35
  tool_use:Edit       15
  tool_use:Read       12
  tool_use:Grep        7
```

The `user: ` empty events are Claude Code's protocol — the user didn't type anything,
the system auto-sent an empty turn after a tool result. These are not behavior.

### 3.5 Notification app IDs are opaque numbers

```
Notification app_name values:
  53  → Slack (54 notifications)
  78  → Cursor/Claude SSH sessions (33 notifications)
  26  → macOS Software Update (1 notification)
```

We store `53` instead of `Slack`. Unusable without a lookup table that doesn't exist in
the notification DB and changes across macOS versions.

### 3.6 Focus events: 205 consecutive duplicates

205 times today, we logged the exact same app + title within 10 seconds of the previous
event. These are pure waste — the 2-second polling interval creates duplicates when
nothing changes.

### 3.7 Idle events: zero

```
Idle events today: 0
```

We have the table but the collector isn't producing data. We can't distinguish
"staring at screen thinking" from "walked away" from "in a meeting."

---

## 4. Proposed Changes

### 4.1 P0: Fix the data we already collect (no new collectors)

#### A. Filter system noise from app_events

**Current:**
```
22:42:07  app  launch: Safari
22:42:07  app  launch: Music
22:42:07  app  launch: TV
22:42:27  app  quit: Safari
22:42:27  app  quit: Music
22:42:27  app  quit: TV
```

**Proposed:** Add an exclusion list in the collector (like clipboard already does):
```python
APP_LIFECYCLE_EXCLUDED = frozenset({
    "Safari", "Music", "TV", "News",             # macOS auto-launch
    "CoreServicesUIAgent", "CoreLocationAgent",   # system daemons
    "Cisco/Cisco Secure Client - Socket Filter",  # VPN
    "Utilities/Nudge",                            # update nagger
    "liquiddetectiond", "ManagedClient",          # hardware/MDM
    "XProtect",                                   # security
})
```

**Result:** 594 events/day → ~26 events/day. 96% noise removed.

#### B. Deduplicate consecutive window events

**Current:**
```
12:23:22  window  Code: patch.diff — SWE-Forge
12:23:22  window  Code                          ← duplicate, no title
12:23:24  window  iTerm2
12:23:26  window  Code: snoopy.log — snoopy
12:23:26  window  Code                          ← duplicate, no title
```

**Proposed:** In the collector, skip if app_name + window_title hasn't changed since last
event. Only emit on actual change.

**Result:** ~1,939 events/day → ~1,000 events/day. Removes 205 exact duplicates + ~700
empty-title echoes.

#### C. Filter claude empty turns

**Current:**
```
22:41:58  claude  tool_use:Bash: sqlite3 ...
22:42:00  claude  user:                    ← empty, not real input
22:42:07  claude  tool_use:Bash: sqlite3 ...
22:42:07  claude  user:                    ← empty, not real input
```

**Proposed:** In the claude collector, skip events where `message_type == "user"` and
content is empty.

**Result:** 115 user events → 44 real user messages. 62% noise removed.

#### D. Filter snoopy's own file events

**Current:** Every buffer flush triggers `modified: snoopy.db-wal`. Every git auto-fetch
triggers `.git/FETCH_HEAD`, `.git/objects/maintenance.lock`, etc.

**Proposed:** Exclude paths matching:
```python
FS_EXCLUDED_PATTERNS = [
    "*/snoopy/data/*",         # our own DB
    "*/.git/objects/*",        # git internals
    "*/.git/refs/remotes/*",   # git fetch
    "*/.git/FETCH_HEAD",
    "*/__pycache__/*",
]
```

**Result:** ~9,552 events/day → ~9,000. Modest, but removes the most confusing noise
(our own writes appearing as "file activity").

#### E. Resolve notification app IDs to names

**Current:** `app_name: 53`

**Proposed:** The macOS notification DB has an `app` table mapping IDs to bundle IDs.
Join on it during collection:
```sql
SELECT r.rec_id, a.identifier, r.delivered_date, r.data
FROM record r LEFT JOIN app a ON r.app_id = a.app_id
```

Store `com.tinyspeck.slackmacgap` (Slack) instead of `53`.

---

### 4.2 P1: Add missing high-value signals (small new collectors)

#### F. Active browser tab (not just history)

**Problem:** Browser history fires when pages are *added to history*. You spent 8 minutes
reading an arxiv paper opened yesterday — we logged nothing.

**Current data gap:**
```
Browsing today: 406 history events
Window focus on Chrome: 689 events, 66.6% with empty title

So for 459 Chrome focus events, we don't know what tab was active.
```

**Proposed:** Poll active tab via AppleScript every 5 seconds:
```applescript
tell application "Google Chrome" to get {URL, title} of active tab of front window
```

**New table:**
```sql
active_tab_events(timestamp, browser, url, title, domain)
```

**What this enables:**
```
BEFORE                              AFTER
──────                              ─────
12:08:01  window  Google Chrome     12:08:01  tab  openreview.net/pdf?id=gs9DaR4GgU
12:08:02  window  Google Chrome              "Towards Scalable Oversight with
                                              Collaborative Multi-Agent Debate"
                                    12:08:45  tab  openreview.net/pdf?id=gs9DaR4GgU
                                              (still reading — 44 seconds)
                                    12:09:30  tab  arxiv.org/abs/2504.21798
                                              (switched to different paper)
```

#### G. Search query extraction

**Problem:** Google searches are the purest intent signal. We have the URLs but don't
parse them.

**Today's searches (extracted from browser_events URLs):**
```
"qwen3 8B"                    → researching models
"colm deadline"               → planning paper submission
"fsdp2 tutorial"              → learning distributed training
"tautological meaning"        → vocabulary lookup
"Kumbaya"                     → random/break
"blasphemy"                   → random/break
"hukuna matata"               → random/break
"Clickhouse"                  → evaluating databases
"andrew ng stanford paper review" → finding review tools
```

**Proposed:** Parse `q=` from Google/YouTube/GitHub URLs in a post-processing step.
No new collector needed — just enrich existing `browser_events`.

**New table:**
```sql
search_events(timestamp, query, engine)
```

#### H. Git activity

**Problem:** Git operations are the strongest task-boundary signal for a developer.
Branch switch = task switch. Commit = task completion. We see `.git/` file events but
can't interpret them.

**Current (from file_events):**
```
22:39:05  file  created: .git/refs/remotes/origin/HEAD.lock
22:39:05  file  modified: .git/FETCH_HEAD
22:39:05  file  created: .git/objects/maintenance.lock
```

This is auto-fetch noise. But a real `git checkout` or `git commit` would look identical
in file_events — we can't tell the difference.

**Proposed:** Watch `.git/logs/HEAD` (the reflog). Each line is a parsed operation:
```
0000000 abc1234 User <email> 1772137000 +0000    checkout: moving from main to experiments
abc1234 def5678 User <email> 1772138000 +0000    commit: fix table formatting
```

**New table:**
```sql
git_events(timestamp, repo_path, operation, branch, message_preview)
```

**What this enables:**
```
BEFORE                                    AFTER
──────                                    ─────
(nothing — git ops invisible)             09:28  git  checkout experiments (SWE-Forge)
                                          09:53  git  commit "fix output dir defaults"
                                          10:28  git  checkout main (snoopy)
                                                      ← TASK SWITCH DETECTED
```

#### I. Shell working directory

**Problem:** `curl http://ampere4.stanford.edu:30000/v1/chat/completions` — is this
snoopy work or SWE-Forge work? We don't know because we don't log `$PWD`.

**Current:**
```
09:02:19  shell  curl http://ampere4.stanford.edu:30000/v1/chat/completions
09:28:15  shell  source /Users/arpan/Desktop/SWE-Forge/.venv/bin/activate
```

**Proposed:** Add `working_directory` column to `shell_events`. Parse from zsh
`EXTENDED_HISTORY` or read from terminal state.

```
BEFORE                                    AFTER
──────                                    ─────
09:02  shell  curl http://ampere4...      09:02  shell  [~/Desktop/SWE-Forge]
                                                        curl http://ampere4...
09:28  shell  source .venv/bin/activate   09:28  shell  [~/Desktop/SWE-Forge]
                                                        source .venv/bin/activate
```

#### J. Meeting detection

**Problem:** You were in a meeting or call = completely different mode. Without detecting
it, the model sees a 45-minute gap with no typing, no browsing, no commits — unexplained.

**Proposed:** Combine existing signals:
- `audio_events`: mic/speaker active
- `app_events`: Zoom/Meet/Teams/FaceTime running
- `calendar_events`: scheduled meeting at this time

**New table:**
```sql
meeting_events(timestamp, app, calendar_title, duration_s)
```

No new collector needed — a post-processing join across existing tables.

---

### 4.3 P2: Rich context signals (more involved)

#### K. Screen content via Accessibility APIs

**Problem:** The single biggest gap. We know "VS Code is focused" but not that you're
editing `experiments.tex` line 142 with a table about "Pass@1 results."

**Today's window titles are empty 65% of the time.** Even when present, they're shallow:
`Code: snoopy.log — snoopy` tells us the file but not what's on screen.

**Proposed:** Use `AXUIElement` to extract focused text content every 15 seconds:
```python
from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
)
# Get focused element's AXValue (text content)
```

**New table:**
```sql
screen_content_events(timestamp, app_name, content_preview, selected_text)
```

**What this enables:**
```
BEFORE                                    AFTER
──────                                    ─────
11:33  window  Code                       11:33  screen  Code: experiments.tex
                                                  "Table 3: Pass@1 results across
                                                   languages for Qwen3-8B..."
                                                  selected: "23.4"
```

**Privacy:** Exclude `AXSecureTextField` (password fields). Allow per-app opt-out.

#### L. Keystroke density (not content)

**Problem:** We can't distinguish active typing from passive reading from idle. These
are fundamentally different cognitive states.

**Proposed:** Count keystrokes per 10-second window via `CGEventTap`. Never log content.

**New table:**
```sql
typing_events(timestamp, app_name, keys_per_min, backspace_ratio, mouse_distance_px)
```

**What this enables:**
```
BEFORE                                    AFTER
──────                                    ─────
11:33  window  Code (385s)                11:33  Code  385s
                                                  typing: 65 keys/min (active drafting)
                                                  backspace: 8% (confident writing)
11:40  window  Chrome (721s)              11:40  Chrome  721s
                                                  typing: 0 keys/min (reading)
                                                  mouse: 2400px (scrolling)
```

---

### 4.4 P3: Representation layer (post-processing)

#### M. Task segmentation

**Problem:** Events are independent. In reality, they cluster into tasks.

**Proposed heuristics for task boundaries:**
- Git branch/repo change
- Shell working directory change
- >5 minute idle gap
- Calendar event start
- App cluster change (Chrome+Overleaf → VS Code+Terminal)

**What this enables:**
```
BEFORE (raw events)                       AFTER (task-segmented)
───────────────────                       ──────────────────────
22:25  claude  Write experiments.tex      TASK: "COLM paper writing" (22:25–22:43)
22:25  claude  user:                        apps: Code, iTerm2, Chrome
22:26  window  iTerm2                       files: experiments.tex, related_work.tex
22:28  window  iTerm2                       duration: 18 min
22:29  window  Code                         git branch: colm-paper
22:30  window  Chrome
22:31  claude  user: fix AI-ish writing   TASK: "ClickHouse evaluation" (22:38–22:41)
22:31  claude  assistant: auditing...       trigger: iMessage about ClickHouse
...                                         apps: Chrome, Code
22:38  message recv: "Clickhouse"           duration: 3 min
22:38  browse  Clickhouse - Google Search   outcome: decided against it
22:39  claude  user: clickhouse for db?
```

#### N. Action abstraction

**Problem:** Predicting `[focus] Google Chrome` is meaningless.

**Proposed taxonomy:**
```
deep_coding    — sustained Code/Terminal with typing
research       — reading papers, docs, tutorials (arxiv, pytorch.org, HF)
communication  — Slack, Messages, Email, responding
review         — reading without typing (PRs, papers on OpenReview)
admin          — calendar, email triage, settings
break          — social media (Instagram, YouTube music), idle
meeting        — Zoom + calendar event active
```

**Classification rule:** dominant app + domain + typing density in a 1-minute bin.

**What this enables:**
```
BEFORE (current dataset row)              AFTER (abstracted)
────────────────────────────              ──────────────────
past_5min:                                past_5min:
  [focus] Google Chrome                     research (arxiv, 3 min)
  [focus] Google Chrome                     research (pytorch docs, 2 min)
  [browse] FSDP2 tutorial                 stimuli:
  [browse] Getting Started with FSDP2       slack_msg from Kevin (unread, 2 min ago)
  [clipboard] https://docs.pytorch...       calendar: "CS224N Staff Mtg" in 45 min
stimuli:                                  temporal:
  [app_launch] Utilities/Nudge              time: 11:30 AM
  [app_launch] CoreLocationAgent            task_duration: 12 min
action: [focus] iTerm2                      since_break: 35 min
                                          action: deep_coding
                                            (switched to terminal to try FSDP2)
```

#### O. Temporal features

**No new collection needed.** Derive from existing timestamps + calendar + oura:
```python
{
    "time_of_day": "morning",           # from timestamp
    "day_of_week": "wednesday",         # from timestamp
    "minutes_since_break": 35,          # last instagram/youtube/idle
    "minutes_since_meeting": 120,       # last meeting_event
    "minutes_until_next_meeting": 45,   # next calendar_event
    "current_task_duration_min": 12,    # time in current task cluster
    "session_duration_hours": 2.1,      # time since wake/unlock
    "oura_readiness_score": 82,         # from oura_daily
}
```

**Research basis:** Eagle & Pentland (2006) "Reality Mining" showed time-of-day and
day-of-week are among the strongest predictors of human behavior, often outperforming
content-based features.

---

## 5. Before vs. After: Full Example

### Current dataset row (actual data from today, 12:04 PM)

```json
{
  "timestamp": 1772136297,
  "time": "12:04:17",
  "past_5min": [
    "[browse] colm deadline - Google Search",
    "[focus] Google Chrome",
    "[browse] ChatGPT",
    "[browse] Review Feedback Interpretation",
    "[browse] tautological meaning - Google Search",
    "[focus] Slack",
    "[focus] Slack"
  ],
  "stimuli": [
    "[app_launch] CoreLocationAgent",
    "[app_launch] CoreServicesUIAgent"
  ],
  "action": "[focus] Messages"
}
```

**Assessment:** Past context is a grab-bag of focus switches and page titles. Stimuli are
system noise. Action is a bare app name. No temporal features, no task context, no
engagement signal, no content. A model trained on this learns "after browsing, sometimes
you open Messages" — not useful.

### Proposed dataset row (same moment, enriched)

```json
{
  "timestamp": 1772136297,
  "time": "12:04:17",
  "task": {
    "id": "iclr-review",
    "label": "Paper review for ICLR MALGAI workshop",
    "duration_min": 28,
    "apps": ["Chrome (OpenReview, ChatGPT)", "VS Code"],
    "git_branch": null
  },
  "past_5min": [
    {"type": "search", "query": "colm deadline", "intent": "planning"},
    {"type": "browse", "domain": "chatgpt.com", "title": "Review Feedback Interpretation",
     "duration_s": 45, "typing": "active"},
    {"type": "search", "query": "tautological meaning", "intent": "vocabulary"},
    {"type": "focus", "app": "Slack", "channel": "Kevin Xiang Li (DM)", "duration_s": 65}
  ],
  "stimuli": [],
  "temporal": {
    "time_of_day": "noon",
    "day_of_week": "wednesday",
    "minutes_since_break": 35,
    "minutes_until_next_meeting": null,
    "session_hours": 3.7,
    "oura_readiness": 82
  },
  "engagement": {
    "keys_per_min": 0,
    "mouse_active": true,
    "screen_content": "ChatGPT: 'The reviewer's feedback is not tautological...'"
  },
  "action": {
    "category": "communication",
    "app": "Messages",
    "detail": "Opened Messages to text about trip/conference conflict",
    "trigger": "break from review task, deadline stress (searched 'colm deadline')"
  }
}
```

**Assessment:** The causal chain is clear: user was reviewing a paper, looked up the CoLM
deadline (stress signal), took a Slack break, then switched to Messages to discuss
trip/deadline conflict. Every field adds predictive value.

---

## 6. Implementation Priority

| Priority | Change | Type | Noise Removed / Signal Added |
|----------|--------|------|------------------------------|
| **P0** | Filter system app_events | Edit collector | 568 junk events/day removed |
| **P0** | Dedup consecutive window events | Edit collector | ~900 duplicates/day removed |
| **P0** | Filter empty claude user turns | Edit collector | 71 empty events/day removed |
| **P0** | Filter .git/snoopy file events | Edit collector | ~300 junk events/day removed |
| **P0** | Resolve notification app IDs | Edit collector | Notifications become usable |
| **P0** | Extract search queries from URLs | Post-processing | Intent signal, no new collection |
| **P0** | Add temporal features | Post-processing | Strongest behavioral predictor |
| **P1** | Active browser tab | New collector (~60 LOC) | Fills 459 empty Chrome events/day |
| **P1** | Git activity | New collector (~50 LOC) | Task boundary ground truth |
| **P1** | Shell working directory | Extend collector | Project context for commands |
| **P1** | Meeting detection | Join existing tables | Explains behavioral gaps |
| **P1** | Task segmentation | Post-processing | Groups 12K events into ~50 tasks/day |
| **P1** | Action abstraction | Post-processing | Meaningful prediction targets |
| **P2** | Screen content (AX APIs) | New collector (~100 LOC) | Richest context, privacy-sensitive |
| **P2** | Keystroke density | New collector (~70 LOC) | Engagement/attention proxy |
| **P3** | On-device LLM summarization | Pipeline + local model | Highest quality, compute-heavy |

---

## 7. Expected Impact

| Metric | Current | After P0 | After P0+P1 | After All |
|--------|---------|----------|-------------|-----------|
| Raw events/day | 12,051 | 10,213 | 10,500 | 11,000 |
| Noise events/day | ~1,839 | ~0 | ~0 | ~0 |
| Useful events/day | ~10,212 | ~10,213 | ~10,500 | ~11,000 |
| Signal-to-noise ratio | 5.6:1 | >100:1 | >100:1 | >100:1 |
| Dataset rows | 1,764 | ~950 | ~950 | ~950 |
| Context features per row | 2 (past, stimuli) | 3 (+temporal) | 6 (+task, tab, search) | 9 (+screen, typing) |
| Action categories | 7 (raw types) | 7 (abstracted) | 7 (abstracted) | 7 (abstracted) |
| % empty window titles | 65% | 65% | ~10% (active tab fills gap) | ~5% (screen content) |
| Meaningful stimuli | 5% | 100% | 100% | 100% |

---

## 8. References

- Dumais et al. (2003). "Stuff I've Seen." SIGIR. — Contextual text at interaction
  time is the strongest recall cue for personal information retrieval.
- Dragunov et al. (2005). "TaskTracer." IUI. — ML prediction of task boundaries from
  window titles and app switches achieves ~75% accuracy.
- Eagle & Pentland (2006). "Reality Mining." Pervasive Computing. — Time-of-day and
  day-of-week outperform content features for behavior prediction.
- Bixler & D'Mello (2016). "Automatic Detection of Mind Wandering." UMUAI. — Keystroke
  dynamics predict attention state.
- Mark et al. (2008). "The Cost of Interrupted Work." CHI. — Interruption patterns are
  predictable from context; recovery time depends on task depth.
- Pirolli & Card (1999). "Information Foraging." Psychological Review. — Search queries
  and browsing patterns follow "information scent" that predicts next navigation.
