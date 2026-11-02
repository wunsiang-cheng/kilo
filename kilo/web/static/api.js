// The server's API, called from the same origin so the login cookie goes along. A 401 means logged out.

export class LoggedOut extends Error {}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

const LOGIN = "/api/v1/auth/login";
const enc = encodeURIComponent;

async function request(method, path, body, signal) {
  const init = { method, headers: {}, signal, credentials: "same-origin" };
  if (body !== undefined) {
    init.headers["content-type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  if (response.status === 401 && path !== LOGIN) throw new LoggedOut("Login required.");
  if (!response.ok) throw new ApiError(await detail(response), response.status);
  return response;
}

// The API's reason for an error (e.g. why a schedule was rejected), else the status.
async function detail(response) {
  try {
    const body = await response.json();
    if (body.detail) return typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    // not JSON
  }
  return `${response.status} ${response.statusText}`;
}

function withQuery(path, params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") query.set(key, value);
  }
  const text = query.toString();
  return text ? `${path}?${text}` : path;
}

const get = async (path, params) => (await request("GET", withQuery(path, params))).json();
const post = async (path, body, signal) => (await request("POST", path, body, signal)).json();
const patch = async (path, body) => (await request("PATCH", path, body)).json();
const remove = async (path) => (await request("DELETE", path)).json();

// One server-sent event block ("event: x" and "data: {...}" lines) as {event, data}.
function parseEvent(raw) {
  let event = "message";
  const data = [];
  for (const line of raw.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue; // comments are keep-alive pings
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  if (!data.length) return null;
  const text = data.join("\n");
  try {
    return { event, data: JSON.parse(text) };
  } catch {
    return { event, data: { raw: text } };
  }
}

export const api = {
  login: (username, password) => post(LOGIN, { username, password }),
  logout: () => post("/api/v1/auth/logout"),
  me: () => get("/api/v1/auth/me"),
  health: () => get("/api/v1/health"),

  listSessions: () => get("/api/v1/sessions"),
  createSession: (title) => post("/api/v1/sessions", title ? { title } : {}),
  getSession: (id) => get(`/api/v1/sessions/${enc(id)}`),
  sessionEvents: (id) => get(`/api/v1/sessions/${enc(id)}/events`),
  renameSession: (id, title) => patch(`/api/v1/sessions/${enc(id)}`, { title }),
  deleteSession: (id) => remove(`/api/v1/sessions/${enc(id)}`),
  // A summary of a long conversation can take minutes to write.
  compactSession: (id, signal) => post(`/api/v1/sessions/${enc(id)}/compact`, undefined, signal),
  // Stop the session's turn in progress, whoever started it.
  stopSession: (id) => post(`/api/v1/sessions/${enc(id)}/stop`),

  listTasks: () => get("/api/v1/tasks"),
  taskLogs: (id, tail) => get(`/api/v1/tasks/${enc(id)}/logs`, { tail }),

  // Schedules the agent proposed, awaiting the user's answer (kilo/server/routes/approvals.py)
  listApprovals: (sessionId) => get("/api/v1/approvals", { session_id: sessionId }),
  approve: (id) => post(`/api/v1/approvals/${enc(id)}/approve`),
  reject: (id) => post(`/api/v1/approvals/${enc(id)}/reject`),

  // Scheduled runs that are skipped or fail, and the turns the system starts, as they happen (server-sent
  // events; the browser reconnects after a dropped connection).
  notices: () => new EventSource("/api/v1/notices"),

  mcpServers: () => get("/api/v1/mcp/servers"),
  mcpTools: (name) => get(`/api/v1/mcp/servers/${enc(name)}/tools`),

  // The chat's events as the server streams them. Aborting `signal` closes the stream,
  // and the server then stops the agent's reply.
  async *chat(sessionId, message, signal) {
    const response = await request("POST", `/api/v1/sessions/${enc(sessionId)}/chat`, { message, stream: true }, signal);
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      let end;
      while ((end = /\r?\n\r?\n/.exec(buffer))) {
        const event = parseEvent(buffer.slice(0, end.index));
        buffer = buffer.slice(end.index + end[0].length);
        if (event) yield event;
      }
    }
  },
};
