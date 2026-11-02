// The web terminal: log in, then chat with the agent and run slash commands at the prompt.

import { api, ApiError, LoggedOut } from "./api.js";
import { complete, runCommand } from "./commands.js";
import { approvalRequest, settings } from "./render.js";
import { Cancelled, ChatView, Terminal } from "./terminal.js";

const term = new Terminal();
const view = new ChatView(term);
const loginForm = document.getElementById("login");
const loginError = document.getElementById("login-error");

const app = {
  api,
  term,
  sessionId: null,
  signal: null, // aborted by Esc / Ctrl+C while a reply or a long command is under way
  notices: null, // the server's stream of runs ending and of turns the system starts
  held: [], // runs that ended while a reply or a command was under way, shown after it
  systemTurn: false, // a turn the system started (a scheduled run) is streaming in the open session
  approving: false, // the user is being asked about the schedules the agent proposed

  // Make a session the active one and show its conversation so far.
  async openSession(id) {
    const { session, events } = await api.sessionEvents(id);
    app.sessionId = session.id;
    app.systemTurn = false;
    term.setPrompt(`kilo [${session.id.slice(-8)}]>`);
    term.line(`── ${session.title} (${session.id}) ──`, "dim");
    view.reset();
    for (const event of events) view.render(event, { replay: true });
    view.end();
  },

  async logout() {
    try {
      await api.logout();
    } finally {
      term.forgetHistory();
      showLogin("Logged out.");
    }
  },
};

// Scheduled runs that are skipped or fail, as it happens: shown in the session their schedule belongs to, when
// that session is open (another session's runs are shown when it is opened). Turns the system starts stream
// the same way. The stream ends with the login.
function listenForRuns() {
  app.notices?.close();
  app.held = [];
  app.notices = api.notices();
  app.notices.addEventListener("run_finished", (e) => {
    try {
      app.held.push(JSON.parse(e.data));
    } catch {
      return;
    }
    if (!term.busy && !term.locked) showHeldRuns();
  });
  // A turn the system started (a scheduled run): shown as it streams, in its session when open.
  app.notices.addEventListener("session_event", (e) => {
    let notice;
    try {
      notice = JSON.parse(e.data);
    } catch {
      return;
    }
    if (notice.session_id !== app.sessionId || term.locked) return;
    app.systemTurn = !["done", "error"].includes(notice.event.event);
    view.render(notice.event);
    if (!app.systemTurn) askApprovalsWhenIdle();
  });
}

const LOGIN_ENDED = "Your login has ended (the server restarted, or 7 days passed). Log in again.";

// The schedules the agent proposed in the open session: the user approves or refuses each. Nothing the agent
// proposes takes effect before that (kilo/server/routes/approvals.py). Esc leaves the rest for after the next reply.
async function askApprovals() {
  if (app.approving || !app.sessionId || term.locked) return;
  app.approving = true;
  try {
    for (const proposal of await api.listApprovals(app.sessionId)) {
      term.lines(approvalRequest(proposal));
      let answer;
      do answer = (await term.ask("Approve? [y/n]")).trim().toLowerCase();
      while (!["y", "yes", "n", "no"].includes(answer));
      try {
        if (answer.startsWith("y")) {
          const s = await api.approve(proposal.id);
          term.line(`Approved "${s.name}": ${s.enabled ? `next run ${s.next_run || "-"}` : "paused"}.`, "ok");
        } else {
          term.line((await api.reject(proposal.id)).message, "dim");
        }
      } catch (error) {
        if (error instanceof LoggedOut) throw error;
        term.line(`${error.message}`, "err");
      }
    }
  } finally {
    app.approving = false;
  }
}

// After a turn the system started, or on logging in: ask at once, unless the user is busy or typing (then the
// questions come after their next reply).
async function askApprovalsWhenIdle() {
  if (term.busy || term.locked || term.input.value) return;
  term.setBusy(true);
  try {
    await askApprovals();
  } catch (error) {
    if (error instanceof LoggedOut) showLogin(LOGIN_ENDED);
    else if (!(error instanceof Cancelled)) term.line(`Checking for schedules to approve failed: ${error.message}`, "err");
  } finally {
    term.setBusy(false);
  }
}

// Esc / Ctrl+C: stop the turn the system started in the open session, as for a reply of the user's own.
function stopSystemTurn() {
  if (!app.systemTurn) return false;
  app.systemTurn = false;
  view.end();
  term.line("^C — stopped.", "dim");
  api.stopSession(app.sessionId).catch((error) => term.line(`Stopping failed: ${error.message}`, "err"));
  return true;
}

function showHeldRuns() {
  for (const run of app.held.splice(0)) {
    if (run.origin_session_id === app.sessionId) view.render({ event: "run_finished", data: run });
  }
}

function showLogin(message = "") {
  app.notices?.close();
  app.notices = null;
  app.held = [];
  term.setLocked(true);
  term.clear();
  app.sessionId = null;
  loginError.textContent = message;
  loginError.className = message === "Logged out." ? "line dim" : "line err";
  loginForm.hidden = false;
  document.getElementById(document.getElementById("username").value ? "password" : "username").focus();
}

loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = document.getElementById("username");
  const password = document.getElementById("password");
  loginError.textContent = "";
  try {
    const me = await api.login(username.value.trim(), password.value);
    password.value = "";
    loginForm.hidden = true;
    await start(me);
  } catch (error) {
    loginForm.hidden = false;
    loginError.className = "line err";
    loginError.textContent = error instanceof ApiError ? error.message : `Cannot reach the Kilo server: ${error.message}`;
    password.select();
  }
});

// After logging in: the banner, then the latest conversation (not a scheduled run's session).
async function start(me) {
  term.clear();
  const health = await api.health();
  settings.timezone = health.timezone;
  settings.compactAt = health.compact_at_tokens;
  term.lines([
    { text: "Kilo Autonomous AI Agent", cls: "bright" },
    `Model: ${health.model} · Workspace: ${health.workspace}`,
    `Logged in as ${me.username} · times in ${health.timezone}`,
    { text: "Type a message, or /help for commands. Esc stops a reply.", cls: "dim" },
  ]);
  term.line("");

  listenForRuns(); // before the replay, so no run ends unseen between the two
  const sessions = (await api.listSessions()).filter((s) => !("run_id" in (s.metadata || {})));
  const id = sessions.length ? sessions[0].id : (await api.createSession("Welcome Session")).id;
  await app.openSession(id);
  term.setLocked(false);
  showHeldRuns();
  askApprovalsWhenIdle();
}

async function chat(message) {
  if (!app.sessionId) await app.openSession((await api.createSession()).id);
  try {
    for await (const event of api.chat(app.sessionId, message, app.signal)) view.render(event);
  } catch (error) {
    if (error.name === "AbortError") term.line("^C — reply stopped.", "dim");
    else if (error instanceof LoggedOut) throw error;
    else term.line(`Stream connection error: ${error.message}`, "err");
  } finally {
    view.end();
  }
}

term.completer = complete;
term.onStop = () => {
  stopSystemTurn(); // a reply of the user's waits behind it
  app.controller?.abort();
};
term.onIdleStop = stopSystemTurn;
term.onSubmit = async (text) => {
  const line = text.trim();
  if (!line) return;
  app.controller = new AbortController();
  app.signal = app.controller.signal;
  term.setBusy(true);
  try {
    if (line.startsWith("/")) await runCommand(line, app);
    else await chat(line);
    if (!term.locked) showHeldRuns();
    await askApprovals();
  } catch (error) {
    if (error instanceof LoggedOut) return showLogin(LOGIN_ENDED);
    if (error instanceof Cancelled) return;
    if (error.name === "AbortError") term.line("^C — stopped waiting.", "dim");
    else term.line(`${line.split(/\s+/)[0]} failed: ${error.message}`, "err");
  } finally {
    app.controller = null;
    app.signal = null;
    if (!term.locked) showHeldRuns();
    term.setBusy(false);
  }
};

(async () => {
  try {
    await start(await api.me());
  } catch (error) {
    if (error instanceof LoggedOut) showLogin();
    else term.line(`Cannot reach the Kilo server: ${error.message}`, "err");
  }
})();
