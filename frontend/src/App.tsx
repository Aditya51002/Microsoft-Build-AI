import { useState } from "react";
import { Rocket, Sparkles } from "lucide-react";

import Dashboard from "./components/Dashboard";
import ReplayMode from "./components/ReplayMode";
import { apiBaseUrl } from "./config";

export default function App() {
  const [query, setQuery] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [isReplay, setIsReplay] = useState(false);
  const [status, setStatus] = useState<"idle" | "loading" | "error">("idle");
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async () => {
    if (!query.trim()) return;
    setStatus("loading");
    setError(null);
    try {
      const response = await fetch(`${apiBaseUrl}/api/sessions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: query.trim() }),
      });
      if (!response.ok) {
        throw new Error("Unable to start session");
      }
      const data = (await response.json()) as { session_id: string };
      setSessionId(data.session_id);
      setIsReplay(false);
      setStatus("idle");
    } catch (err) {
      setStatus("error");
      setError(err instanceof Error ? err.message : "Unknown error");
    }
  };

  const resetSession = () => {
    setSessionId(null);
    setQuery("");
    setStatus("idle");
    setError(null);
    setIsReplay(false);
  };

  if (sessionId) {
    return (
      <div className="min-h-screen bg-slate-950">
        <nav className="flex items-center justify-between border-b border-slate-800/70 px-8 py-5 text-slate-100">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-sky-500/20">
              <Sparkles className="h-5 w-5 text-sky-200" />
            </div>
            <div>
              <p className="text-xs uppercase tracking-[0.3em] text-slate-400">ResearchSwarm</p>
              <p className="text-lg font-semibold">Orchestration</p>
            </div>
          </div>
          <button
            onClick={resetSession}
            className="rounded-full border border-slate-700/70 px-4 py-2 text-xs uppercase tracking-[0.3em] text-slate-200 transition-all duration-300 hover:border-sky-400 hover:text-sky-200"
          >
            New Research
          </button>
        </nav>
        <div className="px-8 pt-6">
          <ReplayMode
            sessionId={sessionId}
            onReplay={(id) => {
              setSessionId(id);
              setIsReplay(true);
            }}
          />
        </div>
        <Dashboard sessionId={sessionId} isReplay={isReplay} />
      </div>
    );
  }

  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-slate-950 px-8 text-slate-100">
      <div className="absolute inset-0 -z-10 bg-[radial-gradient(circle_at_top,_rgba(14,165,233,0.2),_transparent_55%),radial-gradient(circle_at_bottom,_rgba(59,130,246,0.18),_transparent_60%)]" />
      <div className="w-full max-w-2xl rounded-3xl border border-slate-800/70 bg-slate-950/70 p-10 shadow-[0_30px_80px_rgba(15,23,42,0.6)] backdrop-blur">
        <div className="flex items-center gap-4">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-sky-500/20">
            <Rocket className="h-6 w-6 text-sky-200" />
          </div>
          <div>
            <p className="text-xs uppercase tracking-[0.4em] text-slate-400">ResearchSwarm</p>
            <h1 className="text-3xl font-semibold text-slate-50">Live Research Orchestration</h1>
          </div>
        </div>

        <p className="mt-6 text-lg text-slate-300">
          What do you want to research?
        </p>
        <div className="mt-4 flex flex-col gap-4">
          <textarea
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="What do you want to research?"
            rows={4}
            className="w-full rounded-2xl border border-slate-800/70 bg-slate-950/80 px-4 py-3 text-base text-slate-100 placeholder:text-slate-500 transition-all duration-300 focus:border-sky-400 focus:outline-none"
          />
          {error && (
            <div className="rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-2 text-sm text-rose-200">
              {error}
            </div>
          )}
          <button
            onClick={handleSubmit}
            disabled={status === "loading"}
            className="flex items-center justify-center gap-3 rounded-2xl bg-sky-500 px-5 py-3 text-sm uppercase tracking-[0.3em] text-slate-950 transition-all duration-300 hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-70"
          >
            {status === "loading" ? "Launching" : "Start Research"}
            <Sparkles className="h-5 w-5" />
          </button>
        </div>
        <div className="mt-6">
          <ReplayMode
            sessionId={sessionId}
            onReplay={(id) => {
              setSessionId(id);
              setIsReplay(true);
            }}
          />
        </div>
      </div>
    </main>
  );
}
