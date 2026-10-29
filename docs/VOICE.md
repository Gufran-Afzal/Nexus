# Local voice

Voice is **off by default** and every part is optional. Typing always works.

| Part | Engine | Licence (checked Oct 2026) | Download needed |
| --- | --- | --- | --- |
| Microphone | `sounddevice` (PortAudio) | MIT | no (pip wheel) |
| Speech-to-text | `faster-whisper` (CTranslate2, int8 on CPU) | MIT; Whisper weights MIT | yes, once: Voice → Download (`base.en` ≈ 150 MB) |
| Speech output (default) | Windows System.Speech voices | part of Windows | no |
| Speech output (optional) | Piper, user-installed executable | current Piper (OHF-Voice/piper1-gpl) is GPL-3.0; older rhasspy/piper releases were MIT | piper.exe + a voice `.onnx` |
| Wake word (default) | “Whisper keyword”: energy gate + Whisper on short speech bursts | — | uses the STT model |
| Wake word (optional) | openWakeWord (ONNX on Windows) | code Apache-2.0; **pre-trained models CC BY-NC-SA 4.0 (non-commercial)** | pre-trained models or your own trained model |

## Enable

```powershell
scripts\setup.ps1 -Voice          # installs numpy, sounddevice, faster-whisper into .venv
```

1. NEXUS → **Voice** → *Enable voice*.
2. Download a speech model (`base.en` recommended; `tiny.en` is fastest on CPU).
3. Windows Settings → Privacy & security → Microphone → allow desktop apps (and your browser).
4. Test: *Test voice* button; then the microphone button in the chat box (push-to-talk). The
   transcript is placed in the message box so you can check it before sending.

## Wake word (“Hey Nexus”)

- Voice → Listening → Mode **Wake word** (starts listening whenever NEXUS starts), or use
  **Listen for “hey nexus”** / the tray's **Start listening**.
- Default engine (*Whisper keyword*) works with any phrase you type in *Wake phrase*. It runs Whisper
  only when the energy gate hears ~0.3–3.5 s of sound, so CPU use is low in a quiet room and higher with
  constant talk/music nearby. You can say the request in one breath: “Hey Nexus, set a timer for 5 minutes”.
- *openWakeWord* is lighter but needs a model. There is no pre-trained “hey nexus”; either:
  - train one with openWakeWord's Colab notebook and set the `.onnx` path, or
  - use a built-in phrase such as `hey_jarvis` (`scripts\setup.ps1 -WakeWord`, then run
    `.venv\Scripts\python -c "import openwakeword.utils as u; u.download_models()"` once, with internet).
  On Windows only ONNX models are supported by openWakeWord.
- A short beep confirms the wake word (toggle in settings). The header shows **Listening** / **Recording**.

**Safety:** spoken requests go through the same path as typed ones. They can set timers/reminders, take
notes, show the agenda and open apps/sites/folders you approved in Settings. They can never approve
edits or commands. Audio is never written to disk.

## Release 0.6.0: fewer wrong words, and a conversation

- **Your own vocabulary.** NEXUS tells Whisper the names of your integrated apps, your people and the words you add under
  Voice -> "My words", and repairs common mishearings afterwards ("what's app" -> WhatsApp, "spotifi" -> Spotify). Names of
  people are only used as hints, never auto-corrected, because turning one real name into another would message the wrong person.
- **Models.** Added `distil-small.en`, `distil-medium.en`, `distil-large-v3`: better accuracy than the same-size models.
  Try `distil-medium.en` first if `base.en` still gets words wrong. Bigger is slower on this CPU.
- **Confidence.** Each request has a recognition confidence (shown on the Voice page). Below the threshold, messages, emails and
  app commands are read back and need a "yes".
- **Follow-up listening.** After an answer NEXUS keeps listening (default 8 s) so you can reply without "Hey Nexus". It stops on
  silence, on "thanks" / "that's all", or when what it heard looks like noise. Set to 0 to turn off.
- **Live captions.** While you speak, a small model (`tiny.en`, only if already downloaded) shows your words so a misheard
  word is visible before NEXUS acts. It never downloads anything mid-sentence.
- **Not included:** talking over NEXUS to interrupt it (barge-in). Speakers echo into the microphone and this could not be
  tested without hardware, so it was left out rather than shipped half-working. The Interrupt button still works.
- The accuracy changes could not be measured in the build environment (no microphone or model download). Use the
  microphone test and the "Recent wake-ups" list on your laptop to judge them.

## Accuracy and reliable wake-up

1. Voice -> **Test your microphone**, say “Hey Nexus, set a timer for ten minutes”. It shows what was heard, your level and advice.
2. Level too low is the most common cause: Windows Settings -> System -> Sound -> Input -> Input volume 70-90.
3. Still wrong words: Voice -> Speech recognition models -> download **small.en** and click Use. It is slower, but much better
   on names and accents. `tiny.en` is fastest and mishears most.
4. **Recent wake-ups** lists what NEXUS understood each time. If it hears “hey next us”, add that spelling to the wake phrase
   (comma-separated: `hey nexus, ok nexus`).
5. Prefer a headset microphone in a noisy room.

## Piper (optional, higher-quality voices)

1. Download a Piper release for Windows and a voice (`.onnx` + `.onnx.json`) from the Piper project.
2. Voice → Speech output → **Piper**, set the path to `piper.exe` and the voice `.onnx`.
3. Press *Test voice*.

## Status

The voice pipeline is covered by automated tests with simulated audio (energy detection, utterance
segmentation, wake-phrase matching, end-to-end wake → transcript → answer → speech with fakes).
**It has not yet been tested with a real microphone on Windows** — see [STATUS.md](STATUS.md).
