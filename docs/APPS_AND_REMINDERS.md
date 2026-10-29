# Apps, reminders and voice control

## Apps panel (sidebar → Apps)

NEXUS can only launch, close or switch to apps you **integrate**. You never type a program name:

1. Click **Scan this PC**. NEXUS reads (read-only) your Start Menu shortcuts, Microsoft Store apps and the registry's
   "App Paths" list in one short PowerShell run, and always offers the built-in Windows tools (Notepad, Calculator,
   File Explorer, Task Manager, Snipping Tool, Settings…).
2. Click **Add** next to an app, or **Add all** for everything shown. Not in the list? Type its name (NEXUS looks for the
   program itself) or paste the `.exe` path.
3. Optionally give an app other names (“music”, “browser”). Use the switch to turn an app off without removing it.

Then say or type: *“Open Spotify”*, *“Close Chrome”*, *“Switch to Teams”*, *“Search for cats in Chrome”*,
*“Play lo-fi beats on Spotify”*, *“Open my projects folder in VS Code”* (folders must be approved in Settings).
Names are matched loosely (“chrome” → Google Chrome, “spotifi” → Spotify).

Limits, honestly: Store apps (Spotify from the Store, Calculator…) can be opened and switched to but **not closed by
name** (they have no program name NEXUS can use). Closing sends a normal "please close" request, never a force-kill, so
the app can ask to save. Spotify "play" opens the search results; it cannot press play inside Spotify.

## Also by voice or text (no app needed)

| Say | Does |
|---|---|
| pause / resume the music · next song · previous track | media keys (work with any player) |
| volume up · volume down by 20 · set the volume to 30 · mute the sound | volume keys |
| lock my computer · take a screenshot · show the desktop | Windows |
| open Bluetooth / Wi-Fi / display / sound / update … settings | Windows Settings |
| google python dataclasses · search the web for … | your browser, search address from Settings |
| write an email to sam@example.com about the report | opens a **draft**; nothing is ever sent |

There is deliberately no shutdown, restart or sleep command.

## Reminders, alarms, timers

- **Natural language:** *“Remind me every weekday at 9 to check email”*, *“…tomorrow morning…”*, *“…on Friday at 3pm…”*,
  *“…at 5”* (the next 5 o'clock), *“Set an alarm for 7am”*, *“Set a pasta timer for 10 minutes”*.
- **What happens when one rings:** a pop-up in the NEXUS page (Done / Snooze 5, 10, 30 min, 1 h), a Windows
  notification from the tray, a sound, and the reminder **spoken aloud** (also with the microphone and wake word off;
  turn off in Settings → *Speak reminders aloud*). If you ignore it, it rings again every minute, up to 6 times.
- **By voice:** *“Snooze for 10 minutes”*, *“Stop the alarm”* / *“Dismiss”*.
- **Do something when it rings:** *“Remind me to join the standup at 9:55 and open Teams”* (Teams must be integrated), or
  pick *Open <app>* on the Notes & reminders page.
- **Repeats:** every day, weekday, week, month. Missed-while-asleep reminders still ring when NEXUS wakes up.

NEXUS must be running (tray icon) for reminders to ring.

## What was and wasn't tested

Tested automatically (fakes stand in for Windows): time parsing, repeats, ringing/snooze, database upgrade, app-list parsing,
name matching, every command's behaviour and its refusals, the Apps/Reminders HTTP API, the Apps page and alarm pop-up.
**Not tested on a real Windows PC:** the PowerShell scan, launching Store apps, closing/switching windows, media/volume
keys, lock, screenshot, Settings links and speaking the alert. They use standard Windows calls, but send the Apps page error
text or `logs\nexus.log` if one misbehaves.
