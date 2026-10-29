# Athena windows in the app

Records, Today, Meetings and Wisdom open as tool windows inside the app; the Athena sidebar group.

Each section below was a row of the "Changes from upstream" table in
[FORK.md](../../../FORK.md) until 2026-10-29, moved here word for word; FORK.md now lists
the features and the hooks into upstream files.

## Records (was Conversations) and Today as Odysseus windows

**Records (was Conversations) and Today as Odysseus windows.** The sidebar entries Records and Today open their views in tool windows inside the app instead of leaving it: the chat and other windows stay put, and the windows drag, resize, snap, minimize to a dock chip, close with Esc and become bottom sheets on the phone like the built-in tools (upstream's `modalManager.js` and `windowDrag.js`). They are also on the collapsed sidebar's icon rail, in `/open records` (also `/open conversations`) and `/open today`, and in Settings → Shortcuts ("Open Records", "Open Today", unbound by default; "Toggle Window" includes them). A link to a moment of a module transcript (`<CONVO_PUBLIC_URL>/view/<id>?sig=…#t=<s>`) clicked in chat or in the view opens the Records reader at that line instead of the module's page in a new tab; the **Transcript** buttons still open the module's page. **Ask in chat** in the window fills a new chat directly (no page load). One stylesheet gives both views the look of the built-in windows (Inter, Memory-style tabs, card and button styles, theme colors only). `/modules/athena` (with `#c=<id>[&t=<s>]`) and `/today` stay as standalone pages that mount the same views; a modified click on a sidebar entry still opens them.

Files: `static/js/forkModuleWindows.js`, `static/js/forkModuleShortcuts.js`, `static/css/fork-modules.css` (new); `static/js/soundModule.js` and `static/js/today.js` now export `mountSoundModule` / `mountToday`, `static/js/forkChatAsk.js` exports `askInChat`; hooks in `static/index.html` (one script tag), `static/js/slashCommands.js` (`/open` targets, spread from `forkModuleShortcuts.js` since handwritten notes H1), `static/js/keyboard-shortcuts.js` and `static/js/settings.js` (shortcut tables spread from `forkModuleShortcuts.js`), `tests/helpers/test_settings_shell_coordinator.mjs` (that import in its module list); test `tests/fork/test_fork_module_windows.py` (new)

Upstreamable: no

## Meetings window

**Meetings window.** Recording a call and sending Athena to a meeting have their own **Meetings** sidebar entry (after Conversations), window and rail button, instead of a "Record a call" button inside the Conversations window that left the app. The entry is a link to `/modules/athena/record` (now titled Meetings), so a modified click still opens the standalone page; also `/open meetings` (or `/open calls`) and "Open Meetings" in Settings → Shortcuts (unbound). `static/js/forkMeetings.js` holds the cards and exports `mountMeetings(root, { inApp, openConversations })`: the page mounts them after its top row, the window into its body (one markup source; framing the page is blocked by `frame-ancestors 'none'`), then the cards' scripts load, so closing the window only hides it and a recording or a meeting goes on while the owner chats. The standalone Conversations page keeps a **Meetings** link; the window drops it. Conversations shows a meeting as "Online meeting" (Athena MCP 0.17.0 marks it `(Online meeting: <topic>)`; the parser reads that and the older `(Zoom meeting…)`).

Files: `static/js/forkMeetings.js` (new); fork files `static/js/forkModuleWindows.js`, `static/js/forkModuleShortcuts.js` (data row), `static/css/fork-modules.css`, `static/call-recorder.html`, `static/js/soundModule.js`, `src/sound_module_view.py`; tests in `tests/fork/test_fork_module_windows.py`, `tests/fork/test_sound_module_view.py`. No new upstream hook.

Upstreamable: no

## Athena group in the sidebar

**Athena group in the sidebar.** Conversations, Meetings, Today, Write and Beliefs move out of Tools into a collapsible **Athena** group just above it, with a header like the Tools header (waveform icon, chevron). Click or Enter/Space on the header opens or closes it; the choice is kept per browser (`localStorage` `fork-athena-collapsed`, open by default). The entries keep their ids and handlers, so windows, rail buttons, shortcuts and `/open` work while it is closed. It is not a `.section`, so upstream's section toggle and drag reorder leave it alone. All five entries are added by `static/js/forkModuleWindows.js` after upstream's Notes entry, then moved into the group; `static/index.html` only loads the script.

Files: `static/js/forkAthenaMenu.js` (new); fork files `static/js/forkModuleWindows.js` (one call), `static/css/fork-modules.css`; test in `tests/fork/test_fork_module_windows.py`. No new upstream hook.

Upstreamable: no

## Wisdom (beliefs) as an Odysseus window

**Wisdom (beliefs) as an Odysseus window.** The Wisdom entry (was Beliefs) opens "What I know about you" in a window like Conversations and Meetings instead of leaving the app; a modified click still opens `/beliefs`. Also a rail button, `/open beliefs` and "Open Beliefs" in Settings → Shortcuts (unbound). `static/js/beliefs.js` exports `mountBeliefs(root, { inApp })`: the page mounts it into its body, the window into its body with `inApp: true` (no back link or title; the window has its own title bar), so page and window stay one source. Opening a subject scrolls the window body to the top.

Files: fork files `static/js/beliefs.js` (`mountBeliefs`, scroll), `static/beliefs.html`, `static/js/forkModuleWindows.js`, `static/js/forkModuleShortcuts.js` (data row), `static/css/fork-modules.css`; tests in `tests/fork/test_fork_module_windows.py`. No new upstream hook.

Upstreamable: no

## One pattern for the views and pages

**One pattern for the Athena views and their pages.** Each view is one `mountX(root, opts)` function in its own script: `mountSoundModule` (Records), `mountToday`, `mountMeetings` (`static/js/forkMeetings.js`), `mountBeliefs` (`static/js/beliefs.js`) and `mountInk` (Write, window only); the window and the standalone page both call it, so they can't drift apart. The pages (`static/sound-module.html`, `static/today.html`, `static/call-recorder.html`, `static/beliefs.html`, `static/phone-pairing.html`) are thin shells: `<html class="fm-page">`, the one theme snippet `static/js/forkPageTheme.js` (the colors, font and density the owner picked in Odysseus, from `localStorage` `odysseus-theme`, applied before the first paint; dark by default), `00-tokens.css`, `static/css/fork-modules.css`, and the script that mounts the view. Every rule in `fork-modules.css` starts at a fork class or id (`.fm-*`, `#fork-*`, `.fork-athena-*`, `html.fm-page`), so loading it into the app restyles nothing upstream (`tests/fork/test_fork_module_windows.py` checks it). The Wisdom view's look is under `.fm-beliefs`, the Meetings and pairing pages' under `.fm-card-page` (`.fm-meetings-page`, `.fm-pairing-page`), the Meetings window's under `.fm-meetings`. `tests/fork/test_fork_views_look.py` pins how the Wisdom and Meetings windows and pages and the pairing page look (DOM and computed styles, desktop and phone, dark and light).

Files: `static/js/forkPageTheme.js`, `tests/fork/test_fork_views_look.py`, `tests/fork/fork_views_look_golden.json`, `tests/helpers/fork_views_look.mjs` (new); fork files above; `static/js/forkPageView.js` (the runtime CSS scoper) and `static/js/forkBeliefsWindow.js` removed. No new upstream hook.

Upstreamable: no
