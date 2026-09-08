export type RunStatus =
  | "scheduled" // queued for a future time (the Timeline releases it when due)
  | "queued"
  | "starting"
  | "running"
  | "succeeded"
  | "failed"
  | "canceled"
  | "controlled"; // paused, browser handed to the human

export type ParamType = "string" | "number" | "boolean" | "select" | "file" | "file_list";

export interface WorkflowParam {
  name: string;
  label: string;
  type: ParamType;
  required?: boolean;
  default?: string | number | boolean;
  placeholder?: string;
  help?: string;
  options?: { value: string; label: string }[]; // for "select"
}

export type ColumnType = "text" | "number" | "boolean" | "file" | "file_list";

export interface FileRecord {
  id: string;
  sha256: string;
  ext: string;
  name: string;
  mime: string;
  size: number;
  created_at: number;
  source?: string | null;
  tags: string[];
  meta?: unknown;
  path: string;
}

export interface FileList {
  count: number;
  files: FileRecord[];
}

export interface FileReference {
  datasetId: string;
  datasetName: string;
  column: string;
  rid: number;
}

export interface ContractColumn {
  name: string;
  type: ColumnType;
}

export interface WorkflowDef {
  id: string;
  name: string;
  description: string;
  icon: string; // key understood by the <Icon> component
  module: string; // python module, e.g. "automations.google_search"
  profile: "shared" | "ephemeral";
  profileName?: string; // for shared profiles (e.g. "default")
  needsAuth?: boolean;
  timeoutSec?: number;
  params: WorkflowParam[];
  outputContract?: ContractColumn[]; // columns the result CSV carries
  inputContract?: ContractColumn[]; // when set, consumes a dataset of these columns as input
  builtin?: boolean; // false for user/agent-authored workflows
  createdBy?: string; // "builtin" | "user" | "agent"
  buildArgs: (p: Record<string, unknown>) => string[];
}

export type PublicWorkflow = Omit<WorkflowDef, "buildArgs">;

export interface Progress {
  collected: number;
  total: number;
  message: string;
  page?: number | null;
}

export interface Run {
  id: string;
  workflowId: string;
  workflowName: string;
  params: Record<string, unknown>;
  status: RunStatus;
  watch: boolean; // started with a visible (headed) browser
  createdAt: number;
  startedAt?: number;
  finishedAt?: number;
  progress?: Progress;
  lastUrl?: string;
  csvPath?: string;
  rows?: number;
  error?: string;
  serverPort?: number;
  profileKey: string;
  profileId: string; // "temporary" or a profile id
  profileName: string; // display name ("Temporary" or the profile's name)
  profileDir: string;
  browserOpen: boolean; // control server / window still alive
  agentId?: string; // the agent session that launched this run, if any
  timeoutSec: number;
  startAt?: number; // when status === "scheduled": fire (epoch seconds)
  everySeconds?: number; // recurring: re-arm the next occurrence
}

export interface Settings {
  maxConcurrency: number;
}

// ---- Datasets: the persistent, cross-run SQLite data layer --------------------
export interface DatasetColumn {
  display: string; // user-facing name
  name: string; // physical SQL column name (for agent SQL)
  type: ColumnType;
}

export interface DatasetSource {
  kind: "manual" | "import" | "run" | "project" | "merge";
  runId?: string;
  workflow?: string;
  file?: string;
  from?: string | string[];
}

export interface Dataset {
  id: string;
  name: string;
  table: string; // physical table name (for SQL)
  columns: DatasetColumn[];
  dedupKeys: string[]; // display names used to skip duplicates on append
  source: DatasetSource | null;
  rowCount: number | null;
  createdAt: number;
  updatedAt: number;
}

export interface DatasetRow {
  _rid: number;
  [col: string]: unknown;
}

export interface DatasetRows {
  columns: DatasetColumn[];
  rows: DatasetRow[];
  count: number;
}

// ---- Agents: the AI layer that drives the app and the browser ----------------
export type AgentEngine = "codex" | "claude";
export type AgentStatus =
  | "starting" | "queued" | "running"
  | "waiting"    // turn ended with a workflow it launched still running — paused, woken on completion
  | "scheduled"  // ended a turn after scheduling a future wake (re-queues at scheduledAt)
  | "done" | "failed"
  | "stopped"    // user-stopped; at rest and continuable, like done/failed
  | "canceled";  // legacy name for "stopped", still present in old persisted sessions

// An agent is engine-agnostic — the engine (codex|claude) is chosen per session at launch.
export interface AgentDef {
  id: string;
  name: string;
  icon: string;
  description?: string; // optional, human-facing
  systemPrompt: string;
  scopes: string[]; // "studio" and/or "browser"
  createdAt: number;
  builtin?: boolean;
}

export interface AgentSession {
  id: string;
  agentId: string;
  agentName: string;
  engine: AgentEngine;
  scopes: string[];
  profileId: string;
  profileName: string;
  prompt: string;
  status: AgentStatus;
  watch: boolean;
  /** Engine model + reasoning effort for the NEXT turn — ids the installed CLI
   *  advertises (see /api/agents/models), changeable mid-conversation. */
  model?: string;
  effort?: string;
  createdAt: number;
  startedAt?: number;
  finishedAt?: number;
  error?: string | null;
  controlPort?: number | null;
  threadId?: string | null;
  usage?: Record<string, unknown> | null;
  turns: number;
  runIds: string[];
  scheduledAt?: number | null; // when status === "scheduled": the future wake time
  scheduledPrompt?: string | null;
  notifications?: AgentNotification[];
}

export interface AgentNotification {
  id: string;
  kind: string; // e.g. "workflow_finished"
  payload?: Record<string, unknown>;
  createdAt: number;
  delivered?: boolean;
}

export interface AgentEvent {
  t: number;
  kind: "message" | "reasoning" | "tool_call" | "tool_result" | "status" | "usage" | "system" | "error";
  /** Stable across a streamed block's partials and its final version — the UI
   *  replaces in place rather than stacking half-typed copies. */
  id?: string;
  text?: string;
  tool?: string;
  args?: unknown;
  ok?: boolean;
  result?: string;
  status?: string;
  usage?: Record<string, unknown>;
  role?: string;
  server?: string;
  /** live streaming chunk — superseded by the final event with the same id */
  partial?: boolean;
  /** the engine was cut off (stop / preempt) while writing this block */
  truncated?: boolean;
  /** severity for a `system` note: plain when absent */
  level?: "info" | "warn";
  /** this system note reports the engine compacting its own context */
  compact?: boolean;
}

// ---- Engine model catalogue (fetched FROM the installed CLIs, never hardcoded)
export interface EngineEffort {
  id: string;
  label: string;
  description?: string;
}

export interface EngineModel {
  id: string;
  label: string;
  description?: string;
  efforts: string[];
  effortLabels?: Record<string, string>;
  defaultEffort?: string;
  isDefault?: boolean;
}

export interface EngineCatalog {
  engine: AgentEngine;
  models: EngineModel[];
  defaultModel: string;
  /** where the list came from: the CLI itself, its own cache, or our fallback seed */
  source: "app-server" | "cache" | "cli" | "seed";
  error?: string | null;
  fetchedAt?: number;
  current?: string;
}

export interface InstallInfo {
  name: string;
  url: string;
  mac: string[];
  win: string[];
  linux: string[];
  note?: string;
}

export interface EngineInfo {
  available: boolean;
  path: string | null;
  install?: InstallInfo;
}

export interface ChromeInfo {
  available: boolean;
  path: string | null;
  install: InstallInfo;
}

export type OSPlatform = "mac" | "win" | "linux";

export interface SystemDeps {
  platform: OSPlatform;
  engines: Record<AgentEngine, EngineInfo>;
  chrome: ChromeInfo;
}

// A browser profile: a self-contained, persistent browser environment (cookies,
// logins, history, storage). Runs clone it so many can run in parallel.
export interface Profile {
  id: string;
  name: string;
  color: string;
  createdAt: number;
  lastUsedAt: number | null;
  sizeBytes: number;
  open: boolean; // a manual login/setup window is currently open on it
  openPort: number | null;
}
