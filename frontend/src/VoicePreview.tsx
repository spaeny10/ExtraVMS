/**
 * ▶ Preview of a talk-down voice: asks a server for a short sample (POST /api/talkdown/preview: a WAV, never played on
 * a speaker) and plays it here, then says who spoke it (the voice, or the server's fallback). The server's own
 * Settings › Speakers and the hub's Site › Settings › Cameras use it.
 */
import { useEffect, useRef, useState } from "react";
import type { VoicePreview as Preview } from "./api";
import { previewText } from "./talkdownVoice";
import { toast } from "./ui";

export function VoicePreview({ preview, requested, disabled, title }: {
  preview: () => Promise<Preview>; requested: string | null; disabled?: boolean; title?: string;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [who, setWho] = useState("");
  const [busy, setBusy] = useState(false);
  const audio = useRef<HTMLAudioElement>(null);
  useEffect(() => () => { if (url) URL.revokeObjectURL(url); }, [url]);
  useEffect(() => { if (url) audio.current?.play().catch(() => { /* autoplay refused: the controls are there */ }); }, [url]);
  const go = async () => {
    setBusy(true);
    try {
      const p = await preview();
      setUrl(URL.createObjectURL(p.audio));
      setWho(previewText(p, requested));
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <span className="row voice-preview">
      <button className="ghost small" disabled={disabled || busy} onClick={go} title={title ?? "Hear a short sample here (it never plays on a speaker)"}>{busy ? "…" : "▶ Preview"}</button>
      {url && <audio ref={audio} src={url} controls />}
      {who && <span className="muted small">{who}</span>}
    </span>
  );
}
