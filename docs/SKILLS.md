# Skills: NEXUS inside WhatsApp, Gmail, Calendar and the web

Release 0.6.0. Say (or type) what you want done in another app; NEXUS prepares it in **your own signed-in app** and, for
messages and emails, waits for you to confirm before anything is sent.

## How it works (and what it deliberately does not do)

- NEXUS does **not** read your screen, log in for you, or scrape apps. A local 7B model cannot drive a screen reliably on a
  CPU, and a wrong click in a chat app is costly.
- Instead each skill opens a **vetted deep link** that pre-fills a draft: `whatsapp://send?phone=…&text=…`, Gmail's compose
  URL, Google Calendar's "new event" URL, Maps, YouTube, `docs.new` / `sheets.new` / `slides.new`, a Teams chat link, `mailto:`.
- **Sending is a separate step.** After the draft opens NEXUS says what it prepared and holds a *Send* step. Say **“send it”**
  (or click the card, bottom right). It then checks that the right window (WhatsApp, Gmail, Teams) is in front and presses
  Enter (Ctrl+Enter for Gmail). If the window is not there it does not press anything and tells you.
- Say it all in one breath ("…and send it") to skip the second step. Only the key presses `Enter`, `Ctrl+Enter`, `Ctrl+V`
  (and a few navigation keys) can ever be sent; shortcuts such as Alt+F4 are impossible by construction.
- **Wrong-person protection:** only people on the **People** page can be messaged. An exact name or nickname goes ahead; a
  close spelling ("Alexandre" for "Alexandra") is read back and needs a "yes". Two people with the same first name must be
  told apart ("Alex Reed").
- **Heard it wrong?** When speech recognition was unsure (below the threshold in Voice settings) a message, email or app
  command is read back first: “I heard … Should I do that?”. Timers, reminders and notes are never held back.

## What you can say

| Skill | Examples |
|---|---|
| Message | “Message Mom on WhatsApp I'll be late” · “Tell Alex I'm ten minutes away” · “WhatsApp Sam happy birthday and send it” · “Ask Priya if she is free tomorrow” |
| Email | “Write an email to Sam asking for the project update” · “Email alex at example dot com about Friday” |
| Calendar | “Schedule a meeting with Sam on Friday at 2pm” · “Add dentist to my calendar tomorrow at 3pm for 30 minutes” |
| Maps | “Directions to the airport” · “Find coffee near me” |
| YouTube | “Play lo-fi beats on YouTube” (opens the results; it does not press play) |
| New file | “Create a new Google Doc” · “New spreadsheet” |
| Type | “Type thanks, see you at five in Notepad” (only into an integrated app, never into a terminal) |
| Your own | Add “search amazon for {q}” → `https://www.amazon.com/s?k={q}` on the People page |

Email bodies: if you describe what the email should say, the local model writes the subject and body, and it opens as a
draft for you to read. If the model is unavailable the draft opens empty.

## Free-form requests

If no fixed phrase matches ("can you ping my mum that dinner is at seven") and **Understand free-form requests** is on, the
local model picks one skill from the list above as a small JSON answer. NEXUS validates it (known skill, known person, https
only) and runs it through the same confirmations. Questions and chat go to the model as usual. On an i7-8650U this adds
roughly 5–15 seconds; turn it off on the People page if you prefer speed.

## Setup

1. **People** page: add the people you message, with the country code (+44…), and an email if you write to them.
2. **Apps** page: integrate **WhatsApp** (desktop app) so NEXUS can press Send for you. Without it, `https://wa.me/…`
   opens WhatsApp Web with the text filled in and you press Send yourself.
3. Gmail is used in your default browser, which must already be signed in to Gmail.

## Limits (please read)

- **No reading.** NEXUS cannot read incoming WhatsApp messages or emails, or know whether a message was delivered.
- **Sending depends on timing and window titles.** After opening the draft NEXUS waits (default 3 s, adjustable) and checks
  the front window's title. A slow PC may need a longer wait. These were not run against real WhatsApp/Gmail/Teams here.
- Telegram, SMS and Outlook-in-the-browser are not supported. Teams needs the person's email address.
- Windows only for the key presses; the draft links open on any OS.
