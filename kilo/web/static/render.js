// Plain-text rendering of the API's data and of the agent's tool calls.
// Each function returns lines: a string, or {text, cls} for a line in its own color.

// Set from /api/v1/health once logged in.
export const settings = { timezone: "UTC", compactAt: 0 };

// An API timestamp (UTC, ISO 8601) in local time: the given timezone, else KILO_TIMEZONE.
export function time(value, tz) {
  if (!value) return "-";
  const moment = new Date(String(value).replace(/(\.\d{3})\d+/, "$1"));
  if (Number.isNaN(moment.getTime())) throw new RangeError(`Not a time: ${value}`);
  const parts = {};
  const format = new Intl.DateTimeFormat("en-US", {
    timeZone: tz || settings.timezone, weekday: "short", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  for (const part of format.formatToParts(moment)) parts[part.type] = part.value;
  return `${parts.weekday} ${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

// A time from the API shown in local time, or text that is not one, as it is.
function stamp(value) {
  if (!value) return "-";
  try {
    return time(value);
  } catch {
    return value;
  }
}

function seconds(task) {
  const end = task.end_time ? new Date(task.end_time) : new Date();
  return Math.max(0, (end - new Date(task.start_time)) / 1000);
}

const errorLine = (text) => ({ text, cls: "err" });
const indent = (text) => text.replace(/\s+$/, "").split("\n").map((line) => `  ${line}`).join("\n");

// A file size for people, as the server writes it for the model (kilo/uploads.py format_size).
export function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} bytes`;
  let value = bytes / 1024;
  let unit = "KB";
  for (const bigger of ["MB", "GB"]) {
    if (value < 1024) break;
    value /= 1024;
    unit = bigger;
  }
  return `${value.toFixed(1)} ${unit}`;
}

// A file sent with a message, under the message.
export function attachmentLine(attachment) {
  return `  📎 ${attachment.path} (${formatSize(attachment.size)})`;
}

// ---------------------------------------------------------------------------
// API data

export function help() {
  const commands = [
    ["/session list", "List all conversation sessions"],
    ["/session new [title]", "Create a new session (optionally with title)"],
    ["/session switch <id>", "Switch to an existing session and show its conversation"],
    ["/session info", "Show current session details and message count"],
    ["/session rename <title>", "Rename the current session"],
    ["/session delete <id>", "Delete a session and its message history"],
    ["/upload", "Attach files to your next message (or drop them on the page, or paste them)"],
    ["/upload clear", "Forget the files waiting to be sent (they stay in uploads/)"],
    ["/compact", "Summarize the current session's history now, to free its context"],
    ["/tasks", "List recent tasks"],
    ["/task logs <id> [tail]", "View task stdout/stderr logs (default tail=50)"],
    ["/mcp [list]", "List MCP servers and their connection status"],
    ["/mcp tools <name>", "List tools exposed by an MCP server"],
    ["/clear", "Clear the screen (also Ctrl+L)"],
    ["/help", "Show this command list"],
    ["/logout, /exit", "Log out, and forget this browser's input history"],
  ];
  const width = Math.max(...commands.map(([command]) => command.length));
  return [
    ...commands.map(([command, description]) => `${command.padEnd(width)}  ${description}`),
    "",
    { text: "Enter sends · Shift+Enter new line · ↑/↓ history · Tab completes · Esc or Ctrl+C stops a reply", cls: "dim" },
  ];
}

export function sessionsTable(sessions, activeId) {
  if (!sessions.length) return ["No sessions found."];
  return sessions.map((s) => `${s.id === activeId ? "*" : " "} ${s.id}  ${stamp(s.updated_at)}  ${s.title || "Untitled"}`);
}

export function tasksTable(tasks) {
  if (!tasks.length) return ["No tasks recorded yet."];
  return tasks.map((t) => {
    return `${t.task_id}  pid=${t.pid || "-"}  ${(t.status || "unknown").toUpperCase()}  ${seconds(t).toFixed(1)}s  ${t.command || ""}`;
  });
}

export function mcpServers(servers) {
  if (!servers.length) return ["No MCP servers configured."];
  const lines = [];
  for (const s of servers) {
    const enabled = s.enabled === false ? " (disabled)" : "";
    lines.push(`${s.name}  ${s.type || ""}  ${s.server_status || ""}${enabled}  ${s.tool_count || 0} tools  ${s.target || ""}`);
    if (s.error) lines.push(errorLine(`  error: ${s.error}`));
  }
  return lines;
}

export function mcpTools(info) {
  const lines = [`${info.name || ""} (${info.server_status || ""}, ${info.tool_count || 0} tools)`];
  for (const tool of info.tools || []) {
    if (tool && typeof tool === "object") {
      const first = (tool.description || "").split("\n")[0];
      lines.push(`  ${tool.name || ""}: ${first}`);
    } else {
      lines.push(`  ${tool}`);
    }
  }
  return lines;
}

// A schedule's active dates, when it has any.
function window(s) {
  return s.active_from || s.active_until ? `  [${s.active_from || "…"} to ${s.active_until || "…"}]` : "";
}

// A proposal the user is asked to approve: what would run, and for a change, what runs now.
export function approvalRequest(s) {
  const p = s.proposal || {};
  const head = s.change === "update" ? `◆ Kilo asks to change schedule "${s.name}" (${s.id}) to:` : `◆ Kilo asks to create a schedule (${s.id}):`;
  const lines = [
    { text: head, cls: "warn" },
    `  ${p.name || ""}: ${p.when || ""} ${p.timezone || ""}${window(p)}${p.enabled === false ? "  (paused)" : ""}`,
    indent(`Instructions: ${p.instructions || ""}`),
  ];
  if (s.change === "update") {
    lines.push({ text: `  Now: ${s.when} ${s.timezone}${window(s)}${s.enabled ? "" : "  (paused)"}`, cls: "dim" });
  }
  return lines;
}

const OUTCOMES = {
  succeeded: ["succeeded", "ok"],
  failed: ["failed", "err"],
  timed_out: ["timed out", "err"],
  skipped: ["was skipped", "warn"],
};

// How a scheduled run ended, shown in the session its schedule was made in: the outcome, then the
// agent's final reply (or why the run failed or was skipped).
export function runNotice(r) {
  const [outcome, cls] = OUTCOMES[r.status] || [r.status || "", ""];
  const lines = [{ text: `◆ Scheduled run of "${r.schedule_name || ""}" ${outcome} at ${stamp(r.finished_at)} (${r.id})`, cls }];
  if (r.summary && r.summary.trim()) lines.push(indent(r.summary));
  return lines;
}

// The message that starts a scheduled run in its session (kilo/scheduling/launcher.py), as which run it is: no time, no job.
export function runStartedNotice(content) {
  const [first] = (content || "").split("\n");
  const head = first.replace(/^\[System notice, not from the user: s/, "S").replace(/ · [^·]*$/, "");
  return [{ text: `◆ ${head}`, cls: "ok" }];
}

// A token count for people: 512K, 1.2M, 900.
export function tokens(count) {
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M`;
  return count >= 1000 ? `${Math.round(count / 1000)}K` : String(count);
}

// A session's context size, e.g. 'Context: 212K tokens (compacts at 500K) · compacted 1 time'.
export function contextLine(metadata, compactAt) {
  const limit = compactAt ? `compacts at ${tokens(compactAt)}` : "auto-compaction off";
  const line = `Context: ${tokens(metadata.context_tokens || 0)} tokens (${limit})`;
  const count = metadata.compactions || 0;
  return count ? `${line} · compacted ${count} time${count !== 1 ? "s" : ""}` : line;
}

// One line for a compaction event (from a chat stream, a replayed conversation, or /compact).
export function compactionLine(event) {
  const data = event.data || {};
  if (event.event === "compaction_start") return `── compacting context (${tokens(data.context_tokens || 0)} tokens)… ──`;
  if (event.event === "compaction_done") {
    if (data.summary_tokens === undefined) return "── the conversation before this was compacted into a summary ──";
    return `── context compacted: ${tokens(data.context_tokens || 0)} tokens → a ${tokens(data.summary_tokens || 0)}-token summary ──`;
  }
  return `── context not compacted: ${data.error || "unknown error"} ──`;
}

// ---------------------------------------------------------------------------
// Tool calls, keyed by the canonical tool name carried on chat events

// A tool's name for people: an MCP tool's as server/tool.
function toolLabel(name) {
  return name.startsWith("mcp__") ? name.slice("mcp__".length).replace("__", "/") : name;
}

// A tool call: the tool and what the agent says the call is for (its `intent`, kilo/tools/toolbox.py), not its input.
export function toolCallLine(name, args) {
  return `● ${toolLabel(name)}${args.intent ? `: ${args.intent}` : ""}`;
}

// A tool call's result is not shown, only that it failed: an error, a command ending badly or stopped.
export function toolFailed(result) {
  return ["error", "failed", "timed_out", "killed"].includes(result.status);
}
