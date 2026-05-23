export type RunStatus =
  | "queued"
  | "starting"
  | "running"
  | "succeeded"
  | "failed"
  | "canceled"
  | "controlled"; // paused, browser handed to the human

export type ParamType = "string" | "number" | "boolean";

export interface WorkflowParam {
  name: string;
  label: string;
  type: ParamType;
  required?: boolean;
  default?: string | number | boolean;
  placeholder?: string;
  help?: string;
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
  params: WorkflowParam[];
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
  profileDir: string;
  browserOpen: boolean; // control server / window still alive
}

export interface Settings {
  maxConcurrency: number;
}
