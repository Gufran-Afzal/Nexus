export type Mode = "assistant" | "coding";

export interface ContextFile {
  path: string;
  tokens: number;
  reason: string;
  truncated: boolean;
}

export interface Message {
  id: number;
  role: "user" | "assistant" | "system";
  content: string;
  model: string | null;
  status: "complete" | "stopped" | "error" | "streaming";
  context_files: ContextFile[];
  meta: {
    error?: string;
    omitted?: { path: string; reason: string }[];
    stats?: { tokens_per_s?: number; output_tokens?: number; total_s?: number };
    action?: { kind: string; status: string };
    source?: string;
    history_dropped?: number;
    tool_trace?: ToolStep[];
    semantic_note?: string | null;
  };
  created_at: string;
}

export interface ToolStep {
  name: string;
  arguments: Record<string, unknown>;
  status?: "succeeded" | "failed" | "awaiting_permission" | "running";
  summary?: string;
  ref?: { changeset_id?: number; command_id?: number; path?: string };
}

export interface Conversation {
  id: number;
  title: string;
  mode: Mode;
  workspace_id: number | null;
  created_at: string;
  updated_at: string;
  messages?: Message[];
}

export interface Workspace {
  id: number;
  name: string;
  path: string;
  exists: boolean;
  allow_edits: boolean;
  allow_commands: boolean;
  exclude_globs: string[];
  summary: string | null;
  has_instructions: boolean;
}

export interface TreeNode {
  name: string;
  path: string;
  type: "dir" | "file";
  size?: number;
  children?: TreeNode[];
}

export interface FileChange {
  id: number;
  path: string;
  status: string;
  error: string | null;
  is_new: boolean;
  is_delete: boolean;
  diff: string;
  added: number;
  removed: number;
}

export interface ChangeSet {
  id: number;
  workspace_id: number;
  title: string;
  description: string;
  status: string;
  source: string;
  created_at: string;
  applied_at: string | null;
  file_count: number;
  files?: FileChange[];
}

export interface CommandRun {
  id: number;
  workspace_id: number;
  argv: string[];
  cwd: string;
  risk: string;
  reasons: string[];
  requested_by: string;
  status: string;
  exit_code: number | null;
  stdout: string;
  stderr: string;
  truncated: boolean;
  timeout_s: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface TaskStep {
  id: number;
  idx: number;
  kind: string;
  description: string;
  status: string;
  attempts: number;
  output: string | null;
  ref: { changeset_id?: number; command_id?: number; files?: string[] };
  log: string[];
}

export interface AgentTask {
  id: number;
  workspace_id: number | null;
  template: string;
  title: string;
  goal: string;
  status: string;
  summary: string | null;
  created_at: string;
  finished_at: string | null;
  steps: TaskStep[];
}

export interface ModelInfo {
  name: string;
  size_bytes: number | null;
  modified_at: string | null;
  family: string | null;
  parameter_size: string | null;
  quantization: string | null;
}

export interface Health {
  version: string;
  uptime_s: number;
  paused: boolean;
  active_model: string;
  onboarding_complete: boolean;
  ollama: { available: boolean; version?: string; error?: string; url: string; loaded?: { name: string }[] };
  generations: { id: string; model: string; purpose: string; state: string; elapsed_s: number }[];
  database: { path: string; schema_version: number; recovered_from: string | null };
  voice: { enabled: boolean; state: string; muted: boolean };
  system: { platform: string; python: string; cpu_percent: number; cpu_count: number; memory_total: number; memory_available: number };
  startup: { supported: boolean; enabled: boolean; detail?: string; command?: string };
}

export interface Preferences {
  active_model: string;
  assistant_name: string;
  persona_prompt: string;
  engineer_prompt: string;
  generation: { temperature: number; top_p: number; num_ctx: number; max_output_tokens: number; keep_alive: string };
  model_roles: { coding: string; tasks: string; fast: string; embedding: string };
  voice: VoicePrefs;
  retention_days: number;
  allowed_apps: Record<string, string>;
  search_url: string;
  reminders_speak: boolean;
  allowed_url_domains: string[];
  approved_folders: string[];
  onboarding_complete: boolean;
  default_message_app: "whatsapp" | "teams";
  email_app: "gmail" | "mailto";
  smart_routing: boolean;
  send_key_delay_s: number;
}

export interface VoicePrefs {
  enabled: boolean;
  listening_mode: "push_to_talk" | "wake_word";
  wake_engine: "openwakeword" | "whisper_keyword";
  wake_phrase: string;
  openwakeword_model: string;
  wake_threshold: number;
  stt_model: string;
  input_device: number | null;
  tts_engine: "sapi" | "piper" | "none";
  tts_voice: string;
  tts_rate: number;
  piper_exe: string;
  piper_voice_model: string;
  speak_responses: boolean;
  ack_sound: boolean;
  wake_stt_model: string;
  store_transcripts: boolean;
  pause_tolerance_s: number;
  smart_endpoint: boolean;
  max_utterance_s: number;
  custom_words: string[];
  confirm_when_unsure: boolean;
  unsure_threshold: number;
  follow_up_s: number;
  live_captions: boolean;
  partial_stt_model: string;
}

export interface VoiceStatus {
  enabled: boolean;
  state: string;
  muted: boolean;
  paused: boolean;
  listening_mode: string;
  wake_listening: boolean;
  microphone: { available: boolean; detail: string };
  stt: { available: boolean; detail: string; model: string };
  tts: { available: boolean; detail: string; engine: string };
  wake: { engine: string; phrase: string };
  last_transcript: string | null;
  last_error: string | null;
  components: { faster_whisper: boolean; openwakeword: boolean };
  stt_models: { name: string; downloaded: boolean }[];
  wake_attempts?: WakeAttempt[];
  last_confidence?: number | null;
}

export interface Contact { id: number; name: string; aliases: string[]; phone: string; email: string; notes: string }
export interface SkillInfo { id: string; title: string; examples: string[]; enabled: boolean }
export interface CustomSkill { name: string; phrase: string; url: string }
export interface PendingInfo { kind: "ask" | "send" | "run"; title: string; preview: string; question: string; expires_in: number }
export interface SkillsResponse {
  skills: SkillInfo[]; custom: CustomSkill[]; default_message_app: string; email_app: string; smart_routing: boolean;
  send_key_delay_s: number; pending: PendingInfo | null;
}
export interface WakeAttempt { ts: number; heard: string; score: number; accepted: boolean }
export interface MicTestResult {
  heard: string; peak: number; level_db: number; noise: number; seconds: number; recognise_s: number | null; advice: string[];
}

export interface Note { id: number; title: string; body: string; pinned: boolean; updated_at: string }
export interface Todo { id: number; text: string; done: boolean; due_date: string | null }
export type Repeat = "" | "daily" | "weekdays" | "weekly" | "monthly";
export interface Reminder {
  id: number; kind: string; text: string; due_at: string; status: string; repeat_daily: boolean; repeat?: Repeat;
  persistent?: boolean; ring_count?: number; action?: { type: string; name: string } | null;
}
export interface AlarmEvent { id: number; kind: string; text: string; late: boolean; ring: number; repeat: Repeat; action: { type: string; name: string } | null }

export interface IntegratedApp {
  id: string; name: string; aliases: string[]; kind: "exe" | "uwp" | "shortcut" | "uri"; target: string; enabled: boolean;
  source: string; profile: string; examples: string[]; can_close: boolean;
}
export interface DiscoveredApp { id: string; name: string; kind: string; source: string; integrated: boolean }
export interface AppsResponse {
  platform: string; scanned_at: number | null; error: string | null; integrated: IntegratedApp[]; discovered: DiscoveredApp[];
}

export interface BusEvent {
  type: string;
  ts: number;
  [k: string]: unknown;
}

// ---- automation runtime
export interface RunStep {
  id: string; tool: string; args: Record<string, unknown>; when: string; on_fail: string; note: string;
  status: string; attempts: number; summary: string; verified: boolean | null; observed: string; error: string;
  error_class: string; already_done: boolean; approved: boolean | null; recovery: string;
}
export interface RunPending { kind: "approval" | "external" | "input" | "resume"; step: string; prompt: string; tool?: string; risk?: string; needs_decision?: boolean }
export interface RunView {
  id: number; goal: string; status: string; source: string; workflow: string; criteria: string[]; steps: RunStep[];
  pending: RunPending | null; metrics: Record<string, number | boolean>; summary: string; caveats: string[]; created_at: string; finished_at: string;
}
export interface ToolInfo {
  name: string; description: string; risk: "read" | "low" | "medium" | "high"; needs: string[]; tags: string[]; timeout_s: number;
  retries: number; idempotent: boolean; verifies: boolean; available: boolean; unavailable_reason: string; limits: string;
}
export interface CapabilitiesResponse {
  tools: ToolInfo[]; models: { active: string; installed: string[]; vision_capable_but_unused: string[]; reachable: boolean };
  environment: { platform: string; ram_gb: number; cpus: number }; agent: Record<string, number | boolean | string>; limitations: string[];
}
export interface DiagnoseResponse { ok: number; total: number; tools: { tool: string; ok: boolean; detail: string; risk: string; verifies: boolean }[] }
export interface WorkflowInfo { name: string; description: string; keywords: string[]; builtin: boolean; uses: number }
