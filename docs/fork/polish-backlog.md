# Polish backlog: Athena, Odysseus and Odysseus Echo

_As of 2026-11-12._ Things to add so the three projects feel polished and clean to use,
and the new features that would put the Echo and the hub beside the commercial products. It
covers this repo (the hub), [Athena](https://github.com/SDarioosh/Athena) and
[odysseus-echo](https://github.com/SDarioosh/odysseus-echo). Tick an item when its PR is
merged.

## How to use this

- **Source:** a read-only survey of each repo on 2026-11-12. File and line references were
  reported by the survey and not re-checked one by one, so confirm each before starting
  work on it.
- **Left out on purpose:** everything about CI (owner's decision).
- **Both repos:** where an item needs Athena and the hub, do the Athena side first, then
  the hub, and link the two PRs.

Suggested order:

1. Failure alerts and the status page (Athena, then the hub window, then the Echo screen).
2. READMEs, changelogs, stale names and the Echo branding strings.
3. Scheduled backups and a tested restore.
4. Feature work: correction tools, global search, setup checklist, Echo setup test and
   screen features.

## Across all three

The same three gaps show up in every repo, so they are best built as one system.

### Failures are silent

Goal: one owner alert path (ntfy is already in the hub) and a visible "last failed" state.

- [ ] Athena: alert when a session fails. A failed recording alerts nobody today; the
  GPU-full incident went unnoticed. Nothing lists failed sessions (`/v1/sessions` has no
  status filter).
- [ ] Hub: alert when a fork background task fails. All six seeded fork tasks are
  `silent=True` (`fork/tasks.py`), and failed pushes only log a warning
  (`fork/insight/push.py`, `fork/news/push.py`).
- [ ] Echo: tell the user when a voice turn fails or times out. Failures are only logged
  (`TurnCard.kt`, `OdysseusTurn.kt`). Add an error tone, a short spoken message and a card.

### No status page

Goal: one System status window in the hub, fed by a real Athena status and each Echo's
hello.

- [ ] Athena: a real `/status` (and an `athena doctor` command). `/health` always returns
  `{"ok": true}`. Cover queue depth, failed sessions, last successful transcription,
  whisper / LLM / pyannote reachability and disk use.
- [ ] Hub: a System status window. Upstream's `/api/diagnostics/services` does not cover
  Athena, whisper, Home Assistant, the Echos or the fork tasks' last run and last failure.
- [ ] Echo: a status screen. Show hub link, Home Assistant link, upload queue, update state
  and version. Say so when the hub or Home Assistant token has expired; nothing does today.

### No front door

- [ ] Athena: README with a quick start (install, first recording, first question), an
  architecture picture and screenshots. It is 17 lines today.
- [ ] Athena: one version scheme and a CHANGELOG. Three unrelated versions today: package
  0.1.0, MCP 0.22.0 (hardcoded in `mcp_server.py`), Android relay 0.2.1.
- [ ] Hub: a fork section at the top of the README, or a pointer to FORK.md. The README is
  upstream's and never mentions the fork.
- [ ] Hub: a day-to-day user guide. All fork docs are written for developers and agents.
- [ ] Hub: a fork changelog and version.
- [ ] Echo: a real README with screenshots. It says "Readme coming soon." The Roborazzi
  screenshots already exist and could be used.
- [ ] Echo: a CHANGELOG.

## Athena

### Data safety

- [ ] Scheduled backup of the database. The only backup runs at deploy (`backup_db` in
  `server/scripts/deploy.sh`) and is off by default (`ATHENA_DB_BACKUPS=0`).
- [ ] A restore command, a restore doc and a restore test. None exist.

### Correcting records

These add MCP tools or API routes, so each needs a paired hub PR and a contract version
bump ([modules.md](modules.md#versioning)).

- [ ] Delete a whole conversation.
- [ ] Merge two conversations, and split one.
- [ ] Edit a conversation's title or summary.

### Tokens and links

- [ ] Per-device tokens that can be revoked. One shared ingest token authenticates every
  device today.
- [ ] A separate key for signing transcript links, with an expiry. Links are signed with
  the ingest token (`links.py`) and never expire, so rotating the token breaks every shared
  link.
- [ ] Rate limiting on the API. None found.

### Transcript page (`/view`)

- [ ] A colour per speaker. Only the owner has one today.
- [ ] Search within the transcript.
- [ ] Audio playback for recordings whose audio is kept.

### Android relay app

- [ ] Release-signed APK. The release workflow attaches a debug-signed one.
- [ ] Updates delivered from the hub, as the Echo app already does. There is no update
  path today.

### Settings

- [ ] Section headers in `.env.example`. About 135 settings in one flat list.
- [ ] Startup validation with clear errors. Only the ingest token is checked today.

### Stale names and docs

- [ ] README hub link: it points at branch `barvis` and the moved `odysseus-sound.md`; the
  doc is now `athena.md`.
- [ ] AGENTS.md "Where work belongs" still names the hub repo `SDarioosh/Barvis`.
- [ ] PROGRESS.md: two rows numbered 11d, two items numbered 0 in Next, and an old
  `barvis-sound#31` reference.
- [ ] `server/README.md` mixes `CONVO_*`, `ATHENA_*` and `BARVIS_SOUND_*` names; say once
  which to write.
- [ ] A "first recording to first question" path at the top of `server/README.md` (813
  lines of reference today).

## Odysseus (hub)

### When Athena is down

- [ ] One banner with Retry and a pointer to the fix. Each section prints the raw error
  today (`soundModule.js`, `news.js`); the server already returns a clear 502 "Athena is
  not reachable".

### Setup and settings

- [ ] A first-run checklist in Settings for the fork features. Setup is the CLI script
  `scripts/modules_setup.py` today.
- [ ] The most-used fork settings editable in the UI. 37 are env-only
  ([settings.md](settings.md)).
- [ ] No need to know the server name `convo`: the default and the docs still tell the user
  to type it in Settings → MCP.

### Search

- [ ] One search across records, notes, beliefs and news ([roadmap](roadmap.md) item 7,
  "Shared search"). Each page only searches itself today.

### Phone

- [ ] Manifest shortcuts for Today, News, Records and Record.
- [ ] A share target, so text or a link can be shared into Odysseus from Android.
- [ ] Offline fallback for `/today`, `/news` and `/beliefs`. The service worker only covers
  `/`, so they show a browser error offline.

### Consistency and accessibility

- [ ] One page layout: Meetings and Pair use `bs-wrap` / `bs-top`, the other pages
  `fm-page-wrap` / `fm-page-top`.
- [ ] Back links go to the same place. Meetings goes to `/modules/athena`, the others to `/`.
- [ ] `aria-label` on the back links of `call-recorder.html` and `phone-pairing.html`, and
  aria attributes in `callRecorder.js`, `phonePairing.js` and `meetingBot.js` (none today).
- [ ] Visible focus styles. Only 3 focus rules in `fork-modules.css` and 2 in
  `fork-ink.css`.
- [ ] Check the Write and voice-settings pages for empty and error states. The survey did
  not cover them.

### Data safety

- [ ] Scheduled backup of the fork's data. `scripts/fork_deploy.sh` backs up `data/` only
  at deploy.
- [ ] A restore script with a test. Restore is three manual shell lines in
  [deploy.md](deploy.md).
- [ ] Import for the belief export. `GET /api/beliefs/export` exists; there is no import
  route.

### Docs

- [ ] Roadmap: remove the note that the Athena page uses default dark tokens only. Pages
  follow the owner's theme now (`forkPageTheme.js`).

## Odysseus Echo

### Upstream branding the user still sees

- [ ] Notification channel and text: "View Assist", "View Assist Running…", "View Assist
  Companion App" (`strings.xml`), and "VACA Foreground Service Channel"
  (`VACAApplication.kt`).
- [ ] First-run dialog "Welcome to VACA" (`MainActivity.kt`).
- [ ] Update dialog "A new version of View Assist Companion is available…"
  (`VAViewModel.kt`).
- [ ] Default device name "VACA <id>" and the Bluetooth proxy name "VACA … BT Proxy".
- [ ] The UUID warning that names "VACA and View Assist" (`UUIDEditDialog.kt`).

### Setup

- [ ] An in-app "Test my setup" after the QR setup: microphone, wake word, speaker, hub and
  Home Assistant. `scripts/odysseus/verify_device.sh` does this over adb only.

### Settings

- [ ] Split the Odysseus settings into sections. `OdysseusSettingsLayout.kt` is one long
  scrolling page.
- [ ] An About entry: version, hub URL, last update check. The version only shows inside
  the update section today.

### Crashes and logs

- [ ] Keep a local crash log and send it to the hub. `AppExceptionHandler.kt` logs and
  restarts the app. This replaces the removed Crashlytics without anything leaving the
  network.
- [ ] A way back to the previous build after a bad update. No rollback exists.

### Always-on screen

- [ ] Burn-in protection for the clock (a small pixel shift). None under `home/clock`.
- [ ] A do-not-disturb schedule. There is a toggle, and lock mode is the nearest thing to a
  schedule.

Timers, the photo frame and the weather detail page are under
[New features](#new-features-for-odysseus-echo).

### Accessibility and strings

- [ ] Content descriptions on icons: 21 are null under `odysseus/`, including the mic-off
  icon (`ListeningIndicator.kt`).
- [ ] Check the touch-target size of the 20–30 dp icons (`UpdateBanner.kt`,
  `AnnouncementCard.kt`, `RecordingUi.kt`).
- [ ] Move the fork's UI strings into `strings.xml`. About 103 are hardcoded in Kotlin.

### Open items from the plans

- [ ] A queue backend on Music Assistant's own API, and `WeatherClient` moved onto `HaApi`
  (`docs/odysseus/plans/2026-11-music.md` in odysseus-echo).

## New features for Odysseus Echo

What to add so the Echo stands beside the commercial smart displays (Amazon Echo Show,
Meta Portal, Google Nest Hub). It already matches them on wake word, barge-in, follow-up
"yes", home clock, weather, Today, multi-room music, announcements and the recorder. None
of the items below exists in the app today (checked in the code on 2026-11-12) unless the
item says so.

Start with streaming speech, instant local commands and timers: they are noticed in every
interaction. Then captions, the smart-home page and the photo frame.

### Smoothness and speed

- [ ] Speak the answer as it is generated. Today the thinking sound plays after 5 s.
  Streaming the first sentence to speech while the rest is written needs the hub and the
  Home Assistant pipeline to stream too, so it is a three-part change.
- [ ] Instant local answers for simple commands: time, timers, volume, "stop", "next song",
  lights. Route them to Home Assistant's built-in intents or handle them on the device,
  never the large model.
- [ ] Live captions: your words as they are recognised and the answer as it is spoken. The
  turn card only shows results after the turn.
- [ ] Conversation mode: a short open-mic window after every answer, with a visible
  indicator. Follow-up without the wake word exists only for a "yes" confirmation.

### Hands-free basics

- [ ] Timers and alarms on screen: several named timers, a countdown on the clock, tap or
  say "stop" to dismiss, snooze. The most-used smart-display feature and the biggest
  functional gap.
- [ ] Volume and brightness by voice, with an on-screen bar, and a sleep timer for music.
- [ ] Whisper mode with the do-not-disturb schedule: quiet answers at night, no
  announcements during set hours.
- [ ] Shopping list and to-dos as a page to see and tick. The hub already files them by
  voice.

### Glanceable screens

- [ ] Smart-home page: favourite lights, scenes, thermostat and locks as large native
  tiles. The Home Assistant dashboard is one swipe away, but a native page is faster and
  matches the look.
- [ ] Photo frame: a slideshow from a folder on the server when idle. It also solves
  burn-in.
- [ ] Morning brief card: the hub's news brief and Today insight on the home screen, read
  aloud on "good morning".
- [ ] Weather detail page: hourly and weekly. The clock only shows a badge and forecasts.
- [ ] Suggested things to say: a rotating "Try: …" hint line.

### Camera

The app does not use the camera at all today.

- [ ] Doorbell and camera feeds: pop up the feed when the doorbell rings, and "show the
  front door". Home Assistant already has the streams.
- [ ] Wake on approach: brighten and show Today when someone walks up, dim when the room is
  empty. Upstream's pixel-difference motion detection could drive it.
- [ ] Video calls and drop-in between rooms. The largest item here; leave it for last.

### One household system

- [ ] Know who is speaking. Athena's speaker check exists on the server
  (`POST /v1/speakers/identify`). Use it so "my calendar" means the speaker's, and show the
  name on the turn card. The one feature where this system can beat the commercial ones.
- [ ] Intercom: "tell the kitchen dinner is ready", room to room, using the announcement
  cards already built.
- [ ] Routines: one phrase or a time triggers several things ("good night": lights off,
  alarm set, do-not-disturb on). Home Assistant can run them; the Echo needs a way to start
  and see them.
- [ ] Bluetooth speaker mode: play from a phone to the Echo.

## New features for Odysseus (odysseus-expanded)

What to add so the hub stands beside the commercial assistants (ChatGPT, Gemini, Limitless,
Alexa). It already has more features than most of them; what they do better is answer fast
and correctly, speak up on their own, and be one place rather than many pages. Items marked
*planned* are already in [context-growth.md](context-growth.md) or the
[roadmap](roadmap.md). The rest were checked with keyword searches on 2026-11-12, so "not
found" means that, not a full audit.

Start with answer quality and shared search, because every other feature sits on them.
Then the Review inbox and source cards, which are hub-only. Then briefings and people
pages.

### Answers that feel right

- [ ] Answer quality first. The Athena eval is at 9/20 against a 15/20 target
  ([modules/athena.md](modules/athena.md#eval-and-current-score)), and "regular tools" with
  every tool on still overflows the 32k context.
- [ ] One search across everything (*planned*, roadmap item 7). The model has to pick the
  right module tool today, and picking wrong is where answers fail.
- [ ] Source cards under answers, built by the hub from the tool results. A model-written
  "Sources:" footer was tried and dropped.
- [ ] Use what it knows without being asked (*planned*): confirmed beliefs in the chat
  context.

### Speaking up on its own

- [ ] Briefing before a meeting, and a page per person (*planned*,
  [people-context.md](people-context.md)).
- [ ] Weekly review: what you promised, what is still open, who you haven't answered, what
  changed in what the system believes. No recap found in the fork.
- [ ] Evening wrap-up: what got done, what moves to tomorrow, tomorrow's first meeting. The
  morning has Today and the news brief; the evening has nothing.
- [ ] In the moment (*planned*, later): offer to file a promise or a date while the
  conversation is still going.

### One place, not many pages

- [ ] One Review inbox. Things waiting on the owner are spread over the Athena page
  (suggested action items, "who is this?"), the beliefs page, handwritten-note review and
  tool approvals. One queue with a count.
- [ ] A day timeline: conversations, texts, meetings, notes and to-dos done, in order, on
  one scrollable day.
- [ ] Command palette and a global search box. Upstream has a palette for the editor only.
- [ ] Notification history in the app. Pushes go out through ntfy and are gone; a bell with
  the last week's items.

### Capture from anywhere

- [ ] A real phone app: the hub in a WebView plus the relay in one app (planned in Athena's
  `docs/android-app-plan.md`). Brings a share target, a home-screen widget for a voice note
  and proper notifications.
- [ ] Hands-free voice conversation on the phone and Mac. Dictation exists
  (`static/js/voiceRecorder.js`); no spoken back-and-forth mode was found. The Echo
  pipeline ([voice-assistant.md](voice-assistant.md)) could be reused.
- [ ] Save this page: a browser button or share action that files an article or a selection
  as a note with its link.
- [ ] More sources (*planned*, owner picks): places, photos, browser history, health,
  screen.

### Trust and control

- [ ] Privacy dashboard: what was recorded today, by which device, what is kept and for how
  long, and one switch that pauses every microphone (watch, Echos, phone).
- [ ] Household members: what a guest or family member can ask and see, with their own
  calendar and lists. Athena knows voices and the Echo is in shared rooms.
