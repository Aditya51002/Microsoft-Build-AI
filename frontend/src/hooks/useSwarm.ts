import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { apiBaseUrl, wsBaseUrl } from "../config";

export type AgentStatus = "idle" | "running" | "done" | "error" | "retry";

export type AgentState = {
  status: AgentStatus;
  taskId: string | null;
  confidence: number | null;
  lastUpdate: string | null;
  logs: string[];
  startedAt: number | null;
  elapsedSeconds: number | null;
};

type SwarmEvent = {
  event?: string;
  agent_type?: string;
  task_id?: string;
  status?: string;
  content?: string | null;
  confidence?: number | null;
  timestamp?: string | null;
  data?: Record<string, unknown>;
};

export type ClaimRecord = {
  claim: string;
  source: string;
  confidence: number;
  task_id?: string;
};

export type ReportEnvelope = {
  report: string;
  sources: string[];
  confidence: number;
  critic_notes: string[];
  retry_questions: string[];
  claim_ledger: ClaimRecord[];
};

const AGENT_TYPES = ["PLANNER", "RESEARCHER", "ANALYST", "CRITIC", "WRITER"] as const;

const idleState: AgentState = {
  status: "idle",
  taskId: null,
  confidence: null,
  lastUpdate: null,
  logs: [],
  startedAt: null,
  elapsedSeconds: null,
};

const makeInitialAgents = () =>
  AGENT_TYPES.reduce<Record<string, AgentState>>((acc, agent) => {
    acc[agent] = { ...idleState };
    return acc;
  }, {});

const mapStatus = (status?: string): AgentStatus => {
  switch ((status || "").toUpperCase()) {
    case "RUNNING":
      return "running";
    case "DONE":
      return "done";
    case "FAILED":
    case "ERROR":
      return "error";
    case "RETRY":
      return "retry";
    default:
      return "idle";
  }
};

export const useSwarm = (sessionId: string | null) => {
  const [agents, setAgents] = useState<Record<string, AgentState>>(makeInitialAgents);
  const [streamingText, setStreamingText] = useState("");
  const [sessionStatus, setSessionStatus] = useState("idle");
  const [reportMarkdown, setReportMarkdown] = useState("");
  const [report, setReport] = useState<ReportEnvelope | null>(null);
  const [isConnected, setIsConnected] = useState(false);

  const socketRef = useRef<WebSocket | null>(null);
  const reconnectRef = useRef(0);
  const reconnectTimer = useRef<number | null>(null);
  const typewriterTimer = useRef<number | null>(null);
  const typewriterQueue = useRef<string[]>([]);

  const enqueueTypewriter = useCallback((text: string) => {
    if (!text) return;
    typewriterQueue.current.push(text);
    if (typewriterTimer.current) return;
    typewriterTimer.current = window.setInterval(() => {
      const current = typewriterQueue.current.shift();
      if (!current) {
        if (typewriterTimer.current) {
          window.clearInterval(typewriterTimer.current);
          typewriterTimer.current = null;
        }
        return;
      }
      const nextChar = current.charAt(0);
      setReportMarkdown((prev: string) => prev + nextChar);
      if (current.length > 1) {
        typewriterQueue.current.unshift(current.slice(1));
      }
    }, 18);
  }, []);

  const updateAgent = useCallback((agentType: string, event: SwarmEvent) => {
    const nextStatus = mapStatus(event.status);
    setAgents((prev: Record<string, AgentState>) => {
      const current = prev[agentType] ?? { ...idleState };
      const now = Date.now();
      const startedAt =
        nextStatus === "running" && !current.startedAt ? now : current.startedAt;
      const elapsedSeconds =
        nextStatus === "done" || nextStatus === "error"
          ? startedAt
            ? (now - startedAt) / 1000
            : current.elapsedSeconds
          : current.elapsedSeconds;

      const nextLogs = current.logs.slice(0, 6);
      if (event.content) {
        const trimmed = event.content.slice(0, 120);
        nextLogs.unshift(trimmed);
      } else if (event.status) {
        nextLogs.unshift(`Status: ${event.status}`);
      }

      return {
        ...prev,
        [agentType]: {
          ...current,
          status: nextStatus,
          taskId: event.task_id ?? current.taskId,
          confidence: typeof event.confidence === "number" ? event.confidence : current.confidence,
          lastUpdate: event.timestamp ?? current.lastUpdate,
          logs: nextLogs,
          startedAt,
          elapsedSeconds,
        },
      };
    });
  }, []);

  const fetchReport = useCallback(async () => {
    if (!sessionId) return;
    try {
      const response = await fetch(`${apiBaseUrl}/api/sessions/${sessionId}/report`);
      if (!response.ok) return;
      const data = (await response.json()) as ReportEnvelope;
      setReport(data);
      if (data.report) {
        setReportMarkdown(data.report);
      }
    } catch {
      // The report endpoint is expected to 404 until the writer finishes.
    }
  }, [sessionId]);

  const handleMessage = useCallback(
    (event: MessageEvent) => {
      let payload: SwarmEvent;
      try {
        payload = JSON.parse(event.data) as SwarmEvent;
      } catch {
        return;
      }

      if (payload.event === "session_state" && payload.data) {
        const status = (payload.data.status as string) || "running";
        setSessionStatus(status);
        return;
      }

      if (payload.event !== "agent_update") return;

      const agentType = payload.agent_type || "";
      if (!agentType) return;

      updateAgent(agentType, payload);

      if (agentType === "WRITER" && typeof payload.content === "string") {
        if (payload.status?.toUpperCase() === "RUNNING") {
          setStreamingText((prev: string) => prev + payload.content);
          enqueueTypewriter(payload.content);
        }
        if (payload.status?.toUpperCase() === "DONE") {
          try {
            const parsed = JSON.parse(payload.content);
            if (parsed?.report) {
              setReportMarkdown(parsed.report);
            }
          } catch {
            // Keep current report markdown.
          }
          void fetchReport();
        }
      }
    },
    [enqueueTypewriter, fetchReport, updateAgent]
  );

  useEffect(() => {
    if (!sessionId) {
      setAgents(makeInitialAgents());
      setStreamingText("");
      setReportMarkdown("");
      setReport(null);
      setSessionStatus("idle");
      setIsConnected(false);
      if (socketRef.current) {
        socketRef.current.close();
        socketRef.current = null;
      }
      return;
    }

    const connect = () => {
      const url = `${wsBaseUrl}/ws/${sessionId}`;
      const socket = new WebSocket(url);
      socketRef.current = socket;

      socket.onopen = () => {
        setIsConnected(true);
        reconnectRef.current = 0;
      };

      socket.onmessage = handleMessage;

      socket.onclose = () => {
        setIsConnected(false);
        if (!sessionId) return;
        const attempt = reconnectRef.current + 1;
        reconnectRef.current = attempt;
        const delay = Math.min(1000 * 2 ** attempt, 8000);
        reconnectTimer.current = window.setTimeout(connect, delay);
      };

      socket.onerror = () => {
        socket.close();
      };
    };

    connect();

    return () => {
      if (reconnectTimer.current) {
        window.clearTimeout(reconnectTimer.current);
      }
      if (socketRef.current) {
        socketRef.current.close();
        socketRef.current = null;
      }
    };
  }, [handleMessage, sessionId]);

  const derivedAgents = useMemo(() => agents, [agents]);

  return {
    agents: derivedAgents,
    streamingText,
    sessionStatus,
    reportMarkdown,
    report,
    isConnected,
  };
};
