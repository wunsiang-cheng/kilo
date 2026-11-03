// The slash commands typed at the prompt.

import {
  contextLine, compactionLine, help, mcpServers, mcpTools, sessionsTable, settings, tasksTable, time,
} from "./render.js";

// Commands and their subcommands, for Tab completion.
const COMMANDS = {
  "/session": ["list", "new", "switch", "info", "rename", "delete"],
  "/upload": ["clear"],
  "/compact": [],
  "/tasks": [],
  "/task": ["logs"],
  "/mcp": ["list", "tools"],
  "/clear": [],
  "/help": [],
  "/logout": [],
  "/exit": [],
};

function commonPrefix(words) {
  let prefix = words[0];
  for (const word of words) while (!word.startsWith(prefix)) prefix = prefix.slice(0, -1);
  return prefix;
}

// Tab at the prompt: complete a command or subcommand, or list the candidates when several fit.
export function complete(text) {
  if (!text.startsWith("/") || text.includes("\n")) return null;
  const parts = text.split(" ");
  let candidates;
  let typed;
  let before;
  if (parts.length === 1) {
    [candidates, typed, before] = [Object.keys(COMMANDS), parts[0], ""];
  } else if (parts.length === 2 && COMMANDS[parts[0]]) {
    [candidates, typed, before] = [COMMANDS[parts[0]], parts[1], `${parts[0]} `];
  } else {
    return null;
  }
  const matches = candidates.filter((c) => c.startsWith(typed)).sort();
  if (!matches.length) return null;
  if (matches.length === 1) return { text: `${before}${matches[0]} `, options: [] };
  const prefix = commonPrefix(matches);
  return { text: before + prefix, options: prefix === typed ? matches : [] };
}

async function session(parts, sub, app) {
  const { api, term } = app;
  const target = parts[2];
  if (sub === "list" || !sub) {
    term.lines(sessionsTable(await api.listSessions(), app.sessionId));
  } else if (sub === "new") {
    const created = await api.createSession(parts.slice(2).join(" ") || null);
    term.line(`Created and switched to new session: ${created.id} (${created.title})`);
    await app.openSession(created.id);
  } else if (sub === "switch") {
    if (!target) return term.line("Usage: /session switch <session_id>");
    await app.openSession(target);
  } else if (sub === "info") {
    const s = await api.getSession(app.sessionId);
    term.lines([
      `Session ID: ${s.id}`,
      `Title: ${s.title}`,
      `Created: ${time(s.created_at)} | Updated: ${time(s.updated_at)}`,
      `Total messages: ${(s.messages || []).length}`,
      contextLine(s.metadata || {}, settings.compactAt),
    ]);
  } else if (sub === "rename") {
    const title = parts.slice(2).join(" ");
    if (!title) return term.line("Usage: /session rename <title>");
    const renamed = await api.renameSession(app.sessionId, title);
    term.line(`Renamed session ${renamed.id} to: ${renamed.title}`);
  } else if (sub === "delete") {
    if (!target) return term.line("Usage: /session delete <session_id>");
    await api.deleteSession(target);
    term.line(`Session ${target} deleted.`);
    if (target === app.sessionId) {
      const remaining = await api.listSessions();
      const next = remaining.length ? remaining[0].id : (await api.createSession()).id;
      term.line(`Active session changed to: ${next}`);
      await app.openSession(next);
    }
  } else {
    term.line(`Unknown session command: ${sub}`, "err");
  }
}

async function task(parts, sub, app) {
  const { api, term } = app;
  const target = parts[2];
  if (sub === "logs") {
    if (!target) return term.line("Usage: /task logs <task_id> [tail_lines]");
    const tail = /^\d+$/.test(parts[3] || "") ? Number(parts[3]) : 50;
    const data = await api.taskLogs(target, tail);
    term.lines([`=== Logs for ${target} (tail ${tail}) ===`, data.logs || ""], "", { block: true });
  } else {
    term.line(`Unknown task command: ${sub}`, "err");
  }
}

async function mcp(parts, sub, app) {
  const { api, term } = app;
  if (sub === "tools" && parts[2]) term.lines(mcpTools(await api.mcpTools(parts[2])));
  else if (sub === "" || sub === "list") term.lines(mcpServers(await api.mcpServers()));
  else term.line("Usage: /mcp [list] | /mcp tools <name>");
}

// Run one slash command. `app` holds the API, the terminal, the active session and the actions on them.
export async function runCommand(text, app) {
  const parts = text.trim().split(/\s+/);
  const cmd = parts[0].toLowerCase();
  const sub = (parts[1] || "").toLowerCase();
  const { api, term } = app;

  switch (cmd) {
    case "/logout":
    case "/exit":
    case "/quit":
      return app.logout();
    case "/clear":
      return term.clear();
    case "/help":
      return term.lines(help());
    case "/session":
      return session(parts, sub, app);
    case "/upload":
      if (sub === "clear") return app.clearPending();
      if (!sub) return app.pickFiles();
      return term.line("Usage: /upload | /upload clear");
    case "/compact": {
      term.line("Compacting… (summarizing a long conversation can take a minute or two; Esc stops waiting)", "dim");
      const outcome = await api.compactSession(app.sessionId, app.signal);
      return term.line(compactionLine(outcome), "dim");
    }
    case "/tasks":
      return term.lines(tasksTable(await api.listTasks()));
    case "/task":
      return task(parts, sub, app);
    case "/mcp":
      return mcp(parts, sub, app);
    default:
      return term.line(`Unknown command: ${cmd} (/help lists them)`, "err");
  }
}
