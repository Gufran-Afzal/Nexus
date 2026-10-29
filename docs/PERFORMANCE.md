# Performance on a CPU-only laptop

NEXUS can't make a 7-billion-parameter model fast on a CPU; it can stop wasting time around it. Version 0.4.0 does that
(see `docs/STATUS.md`). What remains is the model's own speed, which you control by choosing its size.

## What to expect (estimates, not measurements from your machine)

Generation speed on a 4-core laptop CPU is roughly proportional to model size: a 7B model typically produces a few
words per second, a 3B model about two to three times that, a 1.5B model faster still. The dashboard's
**Intelligence** widget shows your real words per second and last load time after your first answers: use those, not
these estimates.

## The change that matters most: a fast model for the assistant and voice

1. Install a small model yourself (NEXUS never downloads models on its own): `ollama pull qwen2.5:3b`
2. Settings -> Models -> **Fast model (Assistant and voice)** -> `qwen2.5:3b`
3. Keep `qwen2.5-coder:7b` for coding conversations and tasks; they use their own role.

Trade-off: smaller models are less capable. Timers, notes, maths, conversions, status and memory are answered by plain
code and are unaffected. Having two models loaded uses more RAM (you have 32 GB) and switching between them reloads the
model once.

## Voice

- Speech recognition: `base.en` is a good balance; `tiny.en` is fastest. If `tiny.en` is downloaded, the wake phrase
  is spotted with it automatically.
- Voice -> Listening -> "Wait after I stop talking": lower is snappier (default 0.9 s); the unfinished-sentence check
  keeps long sentences from being cut off.
- Replies are spoken sentence by sentence, so you hear the start while the rest is still being written.

## Checking where time goes

- First answer after start-up slow: the model was still loading. Open the dashboard and press **Wake up**, or wait
  for the "Loaded and ready" light.
- Every answer slow: check **words per second**. If it is low, use a smaller fast model (above).
- Voice slow to respond: look at the Voice page status; `logs\speech-worker.log` shows decode problems.
