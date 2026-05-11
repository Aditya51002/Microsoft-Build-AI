export enum AgentType {
  PLANNER = "PLANNER",
  RESEARCHER = "RESEARCHER",
  ANALYST = "ANALYST",
  CRITIC = "CRITIC",
  WRITER = "WRITER",
}

export type AgentStatus = "PENDING" | "RUNNING" | "DONE" | "FAILED" | "RETRY";

export enum MessageType {
  TASK_ASSIGN = "TASK_ASSIGN",
  TASK_RESULT = "TASK_RESULT",
  STATUS_UPDATE = "STATUS_UPDATE",
  ERROR = "ERROR",
  HEARTBEAT = "HEARTBEAT",
}

export interface AgentMessage {
  id: string;
  type: MessageType;
  from_agent: AgentType;
  to_agent: AgentType;
  payload: Record<string, unknown>;
  timestamp: string;
  status: AgentStatus;
  confidence: number;
}

export interface TaskMessage extends AgentMessage {
  task_id: string;
  parent_task_id: string | null;
  depth: number;
}

export interface ResearchQuery {
  user_query: string;
  session_id: string;
  created_at: string;
}

export interface AgentResult {
  task_id: string;
  agent_type: AgentType;
  content: string;
  sources: string[];
  confidence: number;
}
