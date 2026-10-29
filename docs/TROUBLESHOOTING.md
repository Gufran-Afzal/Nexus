# Troubleshooting (Windows)

All commands run in PowerShell from the NEXUS folder (for example `C:\Users\you\Downloads\nexus`) as your normal user.
None of them delete anything, and none need administrator rights unless a step says so.

## 1. NEXUS restarts by itself (exit code 3221225477 = 0xC0000005)

`nexus.log` line: `NEXUS exited unexpectedly (code 3221225477)`. That code is a Windows *access violation*: native code
(a compiled library, not Python) touched invalid memory, so Windows killed the process. Python cannot catch it.
The watchdog restarted NEXUS, which is why you saw "restart 1 of 5".

`MSVCP140.dll` in the Event Viewer entry only says *where* it crashed (the C++ standard library). Several packages use
it (onnxruntime, CTranslate2, scikit-learn, numpy). It does not by itself say which package caused it, and it does
not prove the runtime is the cause.

**What 0.3.2 does about it:** speech recognition and the openWakeWord engine now run in a separate *speech worker*
process. If a native library crashes, only the worker dies; NEXUS shows "The speech engine crashed (exit code ...)"
and keeps running. After 3 crashes in a row the worker is paused for 10 minutes instead of looping.

**Find the culprit:**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\diagnose.ps1
```

It prints the venv's Python, the Visual C++ runtime versions, then tests each package in its own process and marks
the one that crashes (`[CRASH ]`). The report is saved to `%LOCALAPPDATA%\NEXUS\logs\diagnose-<time>.txt`.
To test just one package: `scripts\diagnose.ps1 -Only onnxruntime`. Without the script:

```powershell
.\.venv\Scripts\python.exe backend\native_check.py
```

How to read it:

| Result | Meaning |
| --- | --- |
| Exactly one test shows `CRASH` | That package (or its C++ runtime) is the cause. The others are not. |
| `onnxruntime (import)` crashes but `numpy` passes | onnxruntime's native code fails to initialise on this machine. |
| Only `voice stack together` crashes | Two packages conflict when loaded into the same process. |
| Everything passes | The crash needs real use, not just loading. Send `logs\crash.log` and `logs\speech-worker-crash.log`. |

**If a test crashes and `msvcp140.dll` in System32 is old** (diagnose prints it; your Event Viewer showed 14.32, from
2022): newer packages are built with a newer C++ toolset and are known to crash with an older runtime. Install the
current "Microsoft Visual C++ Redistributable (x64)" (`https://aka.ms/vs/17/release/vc_redist.x64.exe`; it needs
administrator approval from you, NEXUS never does that), then run `diagnose.ps1` again. If the crash disappears, that
confirms it. If it does not, the runtime was not the cause, and the output of `diagnose.ps1` is what to send.

**Native crash dump (optional, needs an elevated PowerShell that you open yourself):**

```powershell
$k = "HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\python.exe"
New-Item -Path $k -Force | Out-Null
New-ItemProperty -Path $k -Name DumpFolder -Value "C:\NexusDumps" -PropertyType ExpandString -Force | Out-Null
New-ItemProperty -Path $k -Name DumpType   -Value 1 -PropertyType DWord -Force | Out-Null   # 1 = mini dump
New-ItemProperty -Path $k -Name DumpCount  -Value 3 -PropertyType DWord -Force | Out-Null
```

Reproduce the crash; a `python.exe.<pid>.dmp` appears in `C:\NexusDumps`. Remove the setting afterwards with
`Remove-Item -Path $k`. NEXUS also writes the Python stack at the moment of any crash to `logs\crash.log`
(server) and `logs\speech-worker-crash.log` (speech worker).

## 2. No tray icon

`scripts\start.ps1 -Console` deliberately runs **without** a tray icon (it passes `--no-tray`, so logs print in the
window). Start normally to get the tray icon:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

If it still isn't visible, click the `^` arrow in the taskbar's notification area (Windows hides new icons there) and
check `logs\console.log` for "Tray icon unavailable". `scripts\diagnose.ps1 -Only tray` tests pystray and Pillow.

## 3. "running scripts is disabled" when activating the venv

You never need to activate the venv. Call its Python directly: `.\.venv\Scripts\python.exe ...`. All NEXUS scripts are
started with `-ExecutionPolicy Bypass`, which affects only that one process. If you want to activate it anyway,
`Set-ExecutionPolicy -Scope Process Bypass` changes the policy for the current window only.

## 4. Which Python is used?

`scripts\setup.ps1` (0.3.2 and later) prefers Python 3.12, then 3.11, then 3.13, and never the newest installed
Python (voice packages have no wheels for brand-new versions). It prints the interpreter it chose and the venv's version
and refuses a `.venv` built with an unsupported Python without deleting anything. At runtime NEXUS always uses
`.venv\Scripts\python.exe`; the Python on your PATH is ignored. Force a version with `scripts\setup.ps1 -Python 3.12`.
Check yours: `.\.venv\Scripts\python.exe --version`.

## 5. Voice shows an error

The Voice page shows the exact reason per component (microphone, speech recognition, speech output, wake word).
Typical ones: "Speech model 'base.en' has not been downloaded yet" (click Download once), "faster-whisper is not
installed" (`scripts\setup.ps1 -Voice`), or "The speech engine crashed" (see section 1).


## Crash at `transcribe.py` / `ctranslate2.models.Whisper(...)` (model load)

If `logs\crash.log` shows the fault inside `faster_whisper/transcribe.py` at the `ctranslate2.models.Whisper` line, the
native model load itself crashes. NEXUS keeps running (the speech worker dies, not the server) and walks a ladder of
safer settings automatically. To find out which one works on your PC, **download a Whisper model first**
(Voice page -> Download), then run `scripts\diagnose.ps1`: its model-load section tries every setting in its own
process and names the first that survives. Send me the new `diagnose-*.txt`, `crash.log` and `speech-worker-crash.log`.
Installing the latest Visual C++ x64 Redistributable is a reasonable first try, but it is not confirmed as the cause.

## Wakes up unreliably or hears wrong words

Use Voice -> **Test your microphone** and read the advice. Most often: raise the Windows input volume, pick the right
device, or download and use the **small.en** model. **Recent wake-ups** shows exactly what NEXUS heard. See docs/VOICE.md.

## Voice cuts me off or waits too long

Voice page -> Listening: "Wait after I stop talking" (seconds of silence that end a request) and "Unfinished
sentence" (keep listening when it sounds unfinished). Raise the first for slow speakers; set the second to "Send after
the pause" if you prefer the old behaviour.
