import { useEffect, useState } from "react";
import { Film, FolderDown, PlayCircle, Radio } from "lucide-react";

import { apiBaseUrl } from "../config";

type ReplayModeProps = {
  sessionId: string | null;
  onReplay: (sessionId: string) => void;
  onRecordingCreated?: () => void;
};

type RecordingsResponse = {
  recordings: string[];
};

const DEMO_MODE = import.meta.env.VITE_DEMO_MODE === "true";

export default function ReplayMode({ sessionId, onReplay, onRecordingCreated }: ReplayModeProps) {
  const [recordings, setRecordings] = useState<string[]>([]);
  const [speed, setSpeed] = useState(1);
  const [status, setStatus] = useState<"idle" | "loading" | "error">("idle");
  const [error, setError] = useState<string | null>(null);

  const fetchRecordings = async () => {
    try {
      const response = await fetch(`${apiBaseUrl}/api/demo/recordings`);
      const data = (await response.json()) as RecordingsResponse;
      setRecordings(data.recordings || []);
    } catch {
      setRecordings([]);
    }
  };

  useEffect(() => {
    if (!DEMO_MODE) return;
    fetchRecordings();
  }, []);

  if (!DEMO_MODE) return null;

  const handleReplay = async (name: string) => {
    setStatus("loading");
    setError(null);
    try {
      const response = await fetch(
        `${apiBaseUrl}/api/demo/replay/${name}?speed=${speed}`,
        { method: "POST" }
      );
      if (!response.ok) {
        throw new Error("Unable to start replay");
      }
      const data = (await response.json()) as { replay_session_id: string };
      onReplay(data.replay_session_id);
    } catch (err) {
      setStatus("error");
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setStatus("idle");
    }
  };

  const handleRecord = async () => {
    if (!sessionId) return;
    setStatus("loading");
    setError(null);
    try {
      const response = await fetch(
        `${apiBaseUrl}/api/demo/record/${sessionId}`,
        { method: "POST" }
      );
      if (!response.ok) {
        throw new Error("Recording failed");
      }
      await fetchRecordings();
      onRecordingCreated?.();
    } catch (err) {
      setStatus("error");
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setStatus("idle");
    }
  };

  return (
    <div className="rounded-2xl border border-slate-800/70 bg-slate-950/70 p-5 text-slate-100 shadow-[0_12px_40px_rgba(15,23,42,0.45)]">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-amber-500/20">
            <Film className="h-5 w-5 text-amber-200" />
          </div>
          <div>
            <p className="text-xs uppercase tracking-[0.3em] text-slate-500">Demo Control</p>
            <p className="text-lg font-semibold">Replay Mode</p>
          </div>
        </div>
        <div className="rounded-full border border-amber-500/30 px-3 py-1 text-xs uppercase tracking-[0.3em] text-amber-200">
          Demo
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        {[1, 2, 4].map((value) => (
          <button
            key={value}
            onClick={() => setSpeed(value)}
            className={`rounded-full border px-3 py-1 text-xs uppercase tracking-[0.3em] transition-all duration-300 ${
              speed === value
                ? "border-sky-400 text-sky-200"
                : "border-slate-800/70 text-slate-400 hover:border-slate-600"
            }`}
          >
            {value}x
          </button>
        ))}
        <div className="ml-auto flex items-center gap-2 text-xs uppercase tracking-[0.3em] text-slate-500">
          <Radio className="h-3 w-3" />
          {status === "loading" ? "Working" : "Ready"}
        </div>
      </div>

      <div className="mt-4 grid gap-3">
        <button
          onClick={() => handleReplay("solar_energy_demo")}
          className="flex items-center justify-between rounded-xl border border-slate-800/70 bg-slate-950/80 px-4 py-3 text-sm transition-all duration-300 hover:border-sky-400"
        >
          <span className="flex items-center gap-2">
            <PlayCircle className="h-4 w-4 text-sky-300" />
            Load Demo
          </span>
          <span className="text-xs uppercase tracking-[0.3em] text-slate-400">Preset</span>
        </button>

        <button
          onClick={handleRecord}
          disabled={!sessionId}
          className="flex items-center justify-between rounded-xl border border-slate-800/70 bg-slate-950/80 px-4 py-3 text-sm transition-all duration-300 hover:border-amber-400 disabled:cursor-not-allowed disabled:opacity-60"
        >
          <span className="flex items-center gap-2">
            <FolderDown className="h-4 w-4 text-amber-300" />
            Record Current Session
          </span>
          <span className="text-xs uppercase tracking-[0.3em] text-slate-400">
            {sessionId ? "Active" : "No session"}
          </span>
        </button>

        <div className="rounded-xl border border-slate-800/70 bg-slate-950/80 px-4 py-3">
          <p className="text-xs uppercase tracking-[0.3em] text-slate-500">Recordings</p>
          <div className="mt-3 space-y-2">
            {recordings.length === 0 ? (
              <p className="text-sm text-slate-500">No recordings yet.</p>
            ) : (
              recordings.map((name) => (
                <button
                  key={name}
                  onClick={() => handleReplay(name)}
                  className="flex w-full items-center justify-between rounded-lg border border-slate-800/60 px-3 py-2 text-left text-sm transition-all duration-300 hover:border-sky-400"
                >
                  <span>{name}</span>
                  <PlayCircle className="h-4 w-4 text-slate-400" />
                </button>
              ))
            )}
          </div>
        </div>
      </div>

      {error && (
        <div className="mt-4 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          {error}
        </div>
      )}
    </div>
  );
}
