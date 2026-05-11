import { useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import {
  Activity,
  Bot,
  Brain,
  CheckCircle,
  Cpu,
  Feather,
  Link2,
  Loader,
  Search,
  ShieldAlert,
  Sparkles,
  TriangleAlert,
} from "lucide-react";

import { AgentState, useSwarm } from "../hooks/useSwarm";

type DashboardProps = {
  sessionId: string;
  isReplay?: boolean;
};

const AGENT_CONFIG = [
  { id: "PLANNER", label: "Planner", icon: Brain },
  { id: "RESEARCHER", label: "Researcher Pool", icon: Search },
  { id: "ANALYST", label: "Analyst", icon: Cpu },
  { id: "CRITIC", label: "Critic", icon: ShieldAlert },
  { id: "WRITER", label: "Writer", icon: Feather },
];

const statusStyles: Record<string, string> = {
  idle: "bg-slate-500/70",
  running: "bg-sky-400 shadow-[0_0_20px_rgba(56,189,248,0.9)] animate-pulse",
  done: "bg-emerald-400 shadow-[0_0_20px_rgba(52,211,153,0.7)]",
  error: "bg-rose-500 shadow-[0_0_20px_rgba(244,63,94,0.7)]",
  retry: "bg-amber-400 shadow-[0_0_20px_rgba(251,191,36,0.7)]",
};

const statusLabel: Record<string, string> = {
  idle: "Idle",
  running: "Running",
  done: "Done",
  error: "Error",
  retry: "Retry",
};

const statusPillStyles: Record<string, string> = {
  idle: "bg-slate-600/20 text-slate-200 border-slate-500/40",
  running: "bg-sky-500/20 text-sky-100 border-sky-400/40",
  done: "bg-emerald-500/20 text-emerald-100 border-emerald-400/40",
  error: "bg-rose-500/20 text-rose-100 border-rose-400/40",
  retry: "bg-amber-500/20 text-amber-100 border-amber-400/40",
};

const formatElapsed = (seconds: number | null) => {
  if (seconds === null) return "--";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const mins = Math.floor(seconds / 60);
  const remaining = seconds % 60;
  return `${mins}m ${remaining.toFixed(0)}s`;
};

const AgentCard = ({
  label,
  icon: Icon,
  state,
  variant,
}: {
  label: string;
  icon: typeof Bot;
  state: AgentState;
  variant?: "wide";
}) => {
  const log = state.logs[0] || "No activity yet";
  const confidence = typeof state.confidence === "number" ? state.confidence : null;

  return (
    <div
      className={`transition-all duration-300 rounded-2xl border border-slate-800/70 bg-slate-950/70 p-5 shadow-[0_12px_40px_rgba(15,23,42,0.35)] backdrop-blur ${
        variant === "wide" ? "col-span-2" : ""
      }`}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-slate-800/70">
            <Icon className="h-5 w-5 text-slate-100" />
          </div>
          <div>
            <p className="text-sm uppercase tracking-[0.2em] text-slate-400">{label}</p>
            <p className="text-lg font-semibold text-slate-100">
              {statusLabel[state.status]}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className={`h-3 w-3 rounded-full ${statusStyles[state.status]}`} />
        </div>
      </div>

      <div className="mt-4 space-y-3">
        <div className="flex items-center justify-between text-xs uppercase tracking-[0.2em] text-slate-500">
          <span>Last Log</span>
          <span>Elapsed</span>
        </div>
        <div className="flex items-center justify-between gap-4">
          <p className="line-clamp-2 text-sm text-slate-200">{log}</p>
          <p className="text-sm text-slate-400">{formatElapsed(state.elapsedSeconds)}</p>
        </div>

        {state.status === "running" && (
          <div className="h-2 w-full overflow-hidden rounded-full bg-slate-800">
            <div className="h-full w-2/3 animate-pulse bg-gradient-to-r from-sky-400 via-cyan-300 to-sky-400" />
          </div>
        )}

        {confidence !== null && state.status === "done" && (
          <div>
            <div className="flex items-center justify-between text-xs text-slate-400">
              <span>Confidence</span>
              <span>{Math.round(confidence * 100)}%</span>
            </div>
            <div className="mt-2 h-2 rounded-full bg-slate-800">
              <div
                className="h-2 rounded-full bg-emerald-400"
                style={{ width: `${Math.round(confidence * 100)}%` }}
              />
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

const DEMO_MODE = import.meta.env.VITE_DEMO_MODE === "true";

export default function Dashboard({ sessionId, isReplay = false }: DashboardProps) {
  const { agents, streamingText, reportMarkdown, report, sessionStatus, isConnected } = useSwarm(
    sessionId
  );
  const outputRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!outputRef.current) return;
    outputRef.current.scrollTop = outputRef.current.scrollHeight;
  }, [streamingText, reportMarkdown]);

  const statusKey = sessionStatus?.toLowerCase() || "running";
  const connectedLabel = isConnected ? "Live" : "Reconnecting";

  const trustClaims = report?.claim_ledger?.slice(0, 4) ?? [];
  const criticNotes = report?.critic_notes?.slice(0, 3) ?? [];
  const finalConfidence =
    typeof report?.confidence === "number" ? Math.round(report.confidence * 100) : null;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <div className="absolute inset-0 -z-10 bg-[radial-gradient(circle_at_top,_rgba(56,189,248,0.18),_transparent_55%),radial-gradient(circle_at_bottom,_rgba(129,140,248,0.16),_transparent_55%)]" />
      <header className="flex items-center justify-between border-b border-slate-800/70 px-8 py-6">
        <div className="flex items-center gap-4">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-sky-500/20">
            <Sparkles className="h-6 w-6 text-sky-200" />
          </div>
          <div>
            <p className="text-xs uppercase tracking-[0.3em] text-slate-400">ResearchSwarm</p>
            <p className="text-2xl font-semibold text-slate-50">Live Orchestration</p>
          </div>
        </div>
          <div className="flex items-center gap-4">
          <div className="text-right">
            <p className="text-xs uppercase tracking-[0.3em] text-slate-500">Session</p>
            <p className="text-sm text-slate-200">{sessionId}</p>
          </div>
            {DEMO_MODE && isReplay && (
              <div className="rounded-full border border-amber-400/40 bg-amber-500/10 px-3 py-2 text-xs uppercase tracking-[0.3em] text-amber-200">
                Demo Replay
              </div>
            )}
          <div
            className={`rounded-full border px-4 py-2 text-xs uppercase tracking-[0.2em] ${
              statusPillStyles[statusKey] || statusPillStyles.running
            }`}
          >
            {statusKey}
          </div>
          <div className="flex items-center gap-2 rounded-full border border-slate-800/80 px-3 py-2 text-xs uppercase tracking-[0.2em] text-slate-300">
            <span className="h-2 w-2 rounded-full bg-emerald-400" />
            {connectedLabel}
          </div>
        </div>
      </header>

      <main className="grid min-h-[calc(100vh-96px)] grid-cols-[40%_60%] gap-6 px-8 py-8">
        <section className="flex h-full flex-col gap-6">
          <div className="rounded-3xl border border-slate-800/70 bg-slate-950/70 p-6 shadow-[0_12px_40px_rgba(15,23,42,0.45)]">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs uppercase tracking-[0.3em] text-slate-500">Agent Grid</p>
                <p className="text-xl font-semibold text-slate-100">Pipeline</p>
              </div>
              <div className="flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-slate-400">
                <Activity className="h-4 w-4" />
                {statusLabel[statusKey] || "Running"}
              </div>
            </div>

            <div className="mt-6 grid grid-cols-2 gap-4">
              {AGENT_CONFIG.map((agent) => (
                <AgentCard
                  key={agent.id}
                  label={agent.label}
                  icon={agent.icon}
                  state={agents[agent.id as keyof typeof agents]}
                  variant="wide"
                />
              ))}
            </div>
          </div>

          <div className="rounded-3xl border border-slate-800/70 bg-gradient-to-br from-slate-950/60 via-slate-950/90 to-slate-900/80 p-6 shadow-[0_12px_40px_rgba(15,23,42,0.45)]">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-rose-500/20">
                <TriangleAlert className="h-5 w-5 text-rose-200" />
              </div>
              <div>
                <p className="text-xs uppercase tracking-[0.3em] text-slate-500">Observability</p>
                <p className="text-lg font-semibold text-slate-100">Live Signals</p>
              </div>
            </div>
            <div className="mt-4 grid grid-cols-2 gap-4 text-sm text-slate-300">
              <div className="rounded-2xl border border-slate-800/60 bg-slate-950/70 p-4">
                <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Orchestration</p>
                <p className="mt-2 text-lg font-semibold text-slate-100">{statusKey}</p>
              </div>
              <div className="rounded-2xl border border-slate-800/60 bg-slate-950/70 p-4">
                <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Stream</p>
                <p className="mt-2 text-lg font-semibold text-slate-100">
                  {isConnected ? "Stable" : "Reconnecting"}
                </p>
              </div>
            </div>
          </div>
        </section>

        <section className="flex h-full flex-col gap-6">
          <div className="flex h-[30%] min-h-[220px] flex-col rounded-3xl border border-slate-800/70 bg-slate-950/70 p-6 shadow-[0_12px_40px_rgba(15,23,42,0.45)]">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs uppercase tracking-[0.3em] text-slate-500">Live Output</p>
                <p className="text-xl font-semibold text-slate-100">Writer Stream</p>
              </div>
              <div className="flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-slate-400">
                <Loader className="h-4 w-4 animate-spin" />
                Streaming
              </div>
            </div>

            <div
              ref={outputRef}
              className="mt-4 flex-1 overflow-y-auto rounded-2xl border border-slate-800/80 bg-slate-950/80 p-4 font-mono text-sm leading-relaxed text-slate-200"
            >
              {streamingText || "Waiting for writer output..."}
              <span className="ml-1 inline-block h-4 w-[2px] animate-pulse bg-sky-300" />
            </div>
          </div>

          <div className="flex h-[30%] min-h-[220px] flex-col rounded-3xl border border-slate-800/70 bg-slate-950/80 p-6 shadow-[0_12px_40px_rgba(15,23,42,0.45)]">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs uppercase tracking-[0.3em] text-slate-500">Trust Ledger</p>
                <p className="text-xl font-semibold text-slate-100">Claims + Critic</p>
              </div>
              <div className="rounded-full border border-emerald-400/40 bg-emerald-500/10 px-3 py-2 text-xs uppercase tracking-[0.2em] text-emerald-100">
                {finalConfidence === null ? "Pending" : `${finalConfidence}%`}
              </div>
            </div>

            <div className="mt-4 grid min-h-0 flex-1 grid-cols-2 gap-4 overflow-hidden">
              <div className="overflow-y-auto rounded-2xl border border-slate-800/80 bg-slate-950/70 p-4">
                <p className="text-xs uppercase tracking-[0.3em] text-slate-500">Evidence</p>
                <div className="mt-3 space-y-3">
                  {trustClaims.length === 0 ? (
                    <p className="text-sm text-slate-500">Claims appear when research completes.</p>
                  ) : (
                    trustClaims.map((item, index) => (
                      <div key={`${item.task_id ?? "claim"}-${index}`} className="text-sm text-slate-200">
                        <div className="flex items-start gap-2">
                          <Link2 className="mt-0.5 h-4 w-4 shrink-0 text-sky-300" />
                          <p className="line-clamp-2">{item.claim}</p>
                        </div>
                        <div className="mt-2 flex items-center justify-between text-xs text-slate-500">
                          <span>{Math.round(item.confidence * 100)}% confidence</span>
                          {item.source && (
                            <a className="text-sky-300" href={item.source} target="_blank" rel="noreferrer">
                              Source
                            </a>
                          )}
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </div>

              <div className="overflow-y-auto rounded-2xl border border-slate-800/80 bg-slate-950/70 p-4">
                <p className="text-xs uppercase tracking-[0.3em] text-slate-500">Critic Notes</p>
                <div className="mt-3 space-y-3">
                  {criticNotes.length === 0 ? (
                    <p className="text-sm text-slate-500">The critic review appears before the final report.</p>
                  ) : (
                    criticNotes.map((note, index) => (
                      <div key={`${note}-${index}`} className="rounded-xl border border-rose-400/20 bg-rose-500/10 px-3 py-2 text-sm text-rose-100">
                        {note}
                      </div>
                    ))
                  )}
                </div>
              </div>
            </div>
          </div>

          <div className="flex h-[40%] min-h-[260px] flex-col rounded-3xl border border-slate-800/70 bg-slate-950/80 p-6 shadow-[0_12px_40px_rgba(15,23,42,0.45)]">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs uppercase tracking-[0.3em] text-slate-500">Final Report</p>
                <p className="text-xl font-semibold text-slate-100">Markdown</p>
              </div>
              <div className="flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-slate-400">
                <CheckCircle className="h-4 w-4" />
                {agents.WRITER.status === "done" ? "Completed" : "Pending"}
              </div>
            </div>

            <div className="mt-4 flex-1 overflow-y-auto rounded-2xl border border-slate-800/80 bg-slate-950/70 p-4 text-sm leading-relaxed text-slate-200">
              {reportMarkdown ? (
                <ReactMarkdown className="prose prose-invert max-w-none">
                  {reportMarkdown}
                </ReactMarkdown>
              ) : (
                <div className="flex h-full flex-col items-center justify-center gap-3 text-slate-500">
                  <Sparkles className="h-6 w-6" />
                  Awaiting synthesized report
                </div>
              )}
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}
