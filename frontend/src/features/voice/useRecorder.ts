import { useCallback, useRef, useState } from "react";
import { api } from "../../services/api";

/**
 * Browser push-to-talk: records with MediaRecorder, sends the audio to the local backend for
 * transcription (faster-whisper), and returns the text. Audio is never stored.
 */
export function useRecorder(onText: (t: string) => void, onError: (msg: string) => void) {
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);
  const recRef = useRef<MediaRecorder | null>(null);
  /** Live microphone loudness 0..1, updated ~60x/s while recording (read it from an animation loop; no re-renders). */
  const levelRef = useRef(0);
  const stopMeter = useRef<() => void>(() => {});
  const chunks = useRef<Blob[]>([]);

  const start = useCallback(async () => {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      onError("This browser can't record audio.");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
      stopMeter.current = startMeter(stream, levelRef);
      const rec = new MediaRecorder(stream);
      chunks.current = [];
      rec.ondataavailable = (e) => e.data.size && chunks.current.push(e.data);
      rec.onstop = async () => {
        stopMeter.current();
        stream.getTracks().forEach((t) => t.stop());
        setRecording(false);
        const blob = new Blob(chunks.current, { type: rec.mimeType || "audio/webm" });
        chunks.current = [];
        if (blob.size < 1000) return;
        setBusy(true);
        try {
          const r = await api.upload<{ text: string }>("/api/voice/transcribe", blob);
          if (r.text) onText(r.text);
          else onError("No speech was recognised.");
        } catch (e) {
          onError(e instanceof Error ? e.message : String(e));
        } finally {
          setBusy(false);
        }
      };
      recRef.current = rec;
      rec.start();
      setRecording(true);
    } catch (e) {
      onError(e instanceof Error ? `Microphone unavailable: ${e.message}` : "Microphone unavailable");
    }
  }, [onText, onError]);

  const stop = useCallback(() => {
    if (recRef.current && recRef.current.state !== "inactive") recRef.current.stop();
  }, []);

  return { recording, busy, start, stop, levelRef };
}

/** Measures microphone loudness locally with the Web Audio API. Nothing is recorded or sent anywhere. */
function startMeter(stream: MediaStream, levelRef: { current: number }): () => void {
  const Ctor: typeof AudioContext | undefined = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) return () => {};
  let raf = 0;
  let ctx: AudioContext | null = null;
  try {
    ctx = new Ctor();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const data = new Uint8Array(analyser.fftSize);
    const tick = () => {
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (const v of data) sum += ((v - 128) / 128) ** 2;
      const rms = Math.sqrt(sum / data.length);
      levelRef.current = levelRef.current * 0.6 + Math.min(1, rms * 4) * 0.4; // smooth, boosted
      raf = requestAnimationFrame(tick);
    };
    tick();
  } catch {
    /* the meter is cosmetic */
  }
  return () => {
    cancelAnimationFrame(raf);
    levelRef.current = 0;
    void ctx?.close().catch(() => {});
  };
}
