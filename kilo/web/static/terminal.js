// The screen and the prompt: output lines, input history, completion, and the keys that stop a reply.
// Everything is written as text (textContent / text nodes), never as HTML: the agent's output and
// LINE messages are untrusted, and a script injected into this page could use the agent's shell.

import { attachmentLine, compactionLine, runNotice, runStartedNotice, toolCallLine, toolFailed } from "./render.js";

const HISTORY_KEY = "kilo.history";
const HISTORY_SIZE = 500;
const SHOWN_LINES = 300; // a longer block shows this many lines and a button for the rest
const NOTICE_LINES = 8; // a scheduled run's notice: its outcome and the start of its final reply

// Ctrl+C or Esc while a question is asked: the command that asked it is abandoned.
export class Cancelled extends Error {}

function loadHistory() {
  try {
    const saved = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    return Array.isArray(saved) ? saved.filter((entry) => typeof entry === "string") : [];
  } catch {
    return [];
  }
}

function saveHistory(history) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(-HISTORY_SIZE)));
  } catch {
    // storage unavailable (private window, blocked site data): history lasts until the tab closes
  }
}

function textSelected() {
  const selection = window.getSelection();
  return selection && !selection.isCollapsed && selection.toString() !== "";
}

// Lines as render.js returns them (strings, or {text, cls}), one entry per screen line.
function flatten(lines, cls) {
  const out = [];
  for (const line of lines) {
    const text = typeof line === "string" ? line : String(line.text ?? "");
    const lineCls = typeof line === "string" ? cls : line.cls || cls;
    for (const part of text.split("\n")) out.push({ text: part, cls: lineCls });
  }
  return out;
}

export class Terminal {
  constructor() {
    this.output = document.getElementById("output");
    this.form = document.getElementById("prompt-line");
    this.label = document.getElementById("prompt");
    this.input = document.getElementById("input");

    this.onSubmit = null; // (text) => void, for a line typed at the prompt
    this.onStop = null; // () => void, for Esc / Ctrl+C while busy
    this.onIdleStop = null; // () => bool, for Esc / Ctrl+C at the prompt: whether it stopped something
    this.completer = null; // (text) => {text, options} | null, for Tab
    this.busy = false;
    this.locked = true; // logged out: no prompt
    this.asking = null; // {resolve, reject, label} while a command asks a question
    this.pinned = true; // follow new output while the view is at the bottom
    this.scrollQueued = false;

    this.history = loadHistory();
    this.historyIndex = this.history.length;
    this.draft = "";

    this.form.addEventListener("submit", (e) => {
      e.preventDefault();
      this.submit();
    });
    this.input.addEventListener("keydown", (e) => this.inputKey(e));
    this.input.addEventListener("input", () => this.fit());
    document.addEventListener("keydown", (e) => this.screenKey(e));
    window.addEventListener("scroll", () => {
      this.pinned = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 48;
    });
    // A click on the screen (not a selection, not a control) puts the cursor back at the prompt.
    document.addEventListener("mouseup", (e) => {
      if (!this.form.hidden && !textSelected() && !e.target.closest("button, input, a")) {
        this.input.focus({ preventScroll: true });
      }
    });
  }

  // ------------------------------------------------------------------
  // Output

  line(text, cls) {
    const el = document.createElement("div");
    el.className = cls ? `line ${cls}` : "line";
    el.textContent = text;
    this.output.append(el);
    this.follow();
    return el;
  }

  print(text, cls) {
    return this.line(text, cls);
  }

  // Lines from render.js; a long result shows its first `shown` lines and a button for the rest.
  lines(lines, cls, { block = false, shown = SHOWN_LINES } = {}) {
    const all = flatten(lines, cls);
    const container = document.createElement("div");
    container.className = block ? "block" : "group";
    const add = (items, before = null) => {
      const fragment = document.createDocumentFragment();
      for (const item of items) {
        const el = document.createElement("div");
        el.className = item.cls ? `line ${item.cls}` : "line";
        el.textContent = item.text;
        fragment.append(el);
      }
      container.insertBefore(fragment, before);
    };
    add(all.slice(0, shown));
    if (all.length > shown) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "more";
      more.textContent = `… ${all.length - shown} more lines (show them)`;
      more.addEventListener("click", () => {
        add(all.slice(shown), more);
        more.remove();
      });
      container.append(more);
    }
    this.output.append(container);
    this.follow();
  }

  // A line that text is added to as it streams in, after a colored label.
  open(label, cls) {
    const el = document.createElement("div");
    el.className = cls ? `line ${cls}` : "line";
    if (label) {
      const span = document.createElement("span");
      span.className = "label";
      span.textContent = label;
      el.append(span);
    }
    this.output.append(el);
    return el;
  }

  append(el, text) {
    el.append(text);
    this.follow();
  }

  // A typed line as it stays on the screen: the prompt (or a question), then what was typed.
  echo(label, text, cls = "user") {
    const el = this.open(`${label} `, cls);
    el.append(text);
    this.follow();
  }

  clear() {
    this.output.replaceChildren();
  }

  follow() {
    if (!this.pinned || this.scrollQueued) return;
    this.scrollQueued = true;
    requestAnimationFrame(() => {
      this.scrollQueued = false;
      window.scrollTo(0, document.documentElement.scrollHeight);
    });
  }

  // ------------------------------------------------------------------
  // The prompt

  setPrompt(label) {
    this.promptLabel = label;
    if (!this.asking) this.label.textContent = label;
  }

  setBusy(busy) {
    this.busy = busy;
    this.refresh();
  }

  setLocked(locked) {
    this.locked = locked;
    if (locked && this.asking) this.cancelQuestion();
    this.refresh();
  }

  refresh() {
    const show = !this.locked && (!this.busy || Boolean(this.asking));
    this.form.hidden = !show;
    if (show) {
      this.label.textContent = this.asking ? this.asking.label : this.promptLabel || "kilo>";
      this.input.focus({ preventScroll: true });
      this.fit();
    }
    this.pinned = true;
    this.follow();
  }

  // A question asked at the prompt (e.g. whether to approve a schedule), answered there.
  ask(label) {
    return new Promise((resolve, reject) => {
      this.asking = { resolve, reject, label };
      this.refresh();
    });
  }

  cancelQuestion() {
    const { reject, label } = this.asking;
    this.asking = null;
    this.echo(label.trimEnd(), "^C", "answer");
    reject(new Cancelled());
    this.refresh();
  }

  submit() {
    const text = this.input.value;
    this.input.value = "";
    this.fit();
    if (this.asking) {
      const { resolve, label } = this.asking;
      this.asking = null;
      this.echo(label.trimEnd(), text, "answer");
      resolve(text);
      this.refresh();
      return;
    }
    this.echo(this.label.textContent, text);
    const entry = text.trim();
    if (entry && this.history[this.history.length - 1] !== entry) {
      this.history.push(entry);
      saveHistory(this.history);
    }
    this.historyIndex = this.history.length;
    this.draft = "";
    if (this.onSubmit) this.onSubmit(text);
  }

  forgetHistory() {
    this.history = [];
    this.historyIndex = 0;
    try {
      localStorage.removeItem(HISTORY_KEY);
    } catch {
      // nothing stored
    }
  }

  // The input grows with its text, so a pasted paragraph stays visible.
  fit() {
    this.input.style.height = "auto";
    this.input.style.height = `${this.input.scrollHeight}px`;
  }

  recall(step) {
    const index = this.historyIndex + step;
    if (index < 0 || index > this.history.length) return;
    if (this.historyIndex === this.history.length) this.draft = this.input.value;
    this.historyIndex = index;
    this.input.value = index === this.history.length ? this.draft : this.history[index];
    this.fit();
    const end = this.input.value.length;
    this.input.setSelectionRange(end, end);
  }

  complete() {
    const result = this.completer && this.completer(this.input.value);
    if (!result) return;
    this.input.value = result.text;
    if (result.options.length) this.line(result.options.join("   "), "dim");
  }

  inputKey(e) {
    if (e.isComposing) return; // an input method is composing a character
    const multiline = this.input.value.includes("\n");
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      this.submit();
    } else if (e.key === "ArrowUp" && !multiline && !this.asking) {
      e.preventDefault();
      this.recall(-1);
    } else if (e.key === "ArrowDown" && !multiline && !this.asking) {
      e.preventDefault();
      this.recall(1);
    } else if (e.key === "Tab" && !this.asking) {
      e.preventDefault();
      this.complete();
    }
  }

  // Keys anywhere on the page: Ctrl+C (when no text is selected, so copying still works) and Esc stop
  // a reply or abandon a question; at an idle prompt Ctrl+C drops the line. Ctrl+L clears the screen.
  screenKey(e) {
    if (this.locked) return;
    const ctrlC = e.ctrlKey && !e.altKey && !e.metaKey && e.key.toLowerCase() === "c";
    const inputSelection = this.input.selectionStart !== this.input.selectionEnd;
    if ((ctrlC && !textSelected() && !inputSelection) || e.key === "Escape") {
      if (this.asking) {
        e.preventDefault();
        this.cancelQuestion();
      } else if (this.busy) {
        e.preventDefault();
        if (this.onStop) this.onStop();
      } else if (this.onIdleStop && this.onIdleStop()) {
        e.preventDefault();
      } else if (ctrlC) {
        e.preventDefault();
        this.echo(this.label.textContent, `${this.input.value}^C`);
        this.input.value = "";
        this.historyIndex = this.history.length;
        this.fit();
      }
    } else if (e.ctrlKey && !e.altKey && !e.metaKey && e.key.toLowerCase() === "l") {
      e.preventDefault();
      this.clear();
    }
  }
}

// Draws a chat's events, as they stream or as a stored conversation replays (kilo/agent/history.py).
export class ChatView {
  constructor(term) {
    this.term = term;
    this.current = null; // the line streaming text goes to
    this.kind = null; // "message" or "progress"
    this.shownRuns = new Set(); // runs whose notice the open session shows, sent live and replayed alike
  }

  // A session is opened: its replay shows its runs' notices again.
  reset() {
    this.end();
    this.shownRuns.clear();
  }

  render(ev, { replay = false } = {}) {
    const data = ev.data || {};
    switch (ev.event) {
      case "user_message":
        this.end();
        if (replay) this.term.echo(this.term.promptLabel || "kilo>", data.content || "");
        // The files sent with it: live, this event comes as soon as the server has taken the message.
        for (const attachment of data.attachments || []) this.term.line(attachmentLine(attachment), "dim");
        break;
      case "progress":
        this.stream("progress", "... ", "progress", data.delta || "");
        break;
      case "message_delta":
        this.stream("message", "Kilo: ", "agent", data.delta || "");
        break;
      case "tool_call_start":
        this.end();
        this.term.line(toolCallLine(data.name || "", data.arguments || {}), "tool");
        break;
      case "tool_call_result":
        this.end();
        if (toolFailed(data.result || {})) this.term.line("  ✗ failed", "err");
        break;
      case "compaction_start":
      case "compaction_done":
      case "compaction_failed":
        this.end();
        this.term.line(compactionLine(ev), "dim");
        break;
      case "run_finished":
        this.end();
        if (this.shownRuns.has(data.id)) break;
        this.shownRuns.add(data.id);
        this.term.lines(runNotice(data), "", { block: true, shown: NOTICE_LINES });
        break;
      case "run_started":
        this.end();
        this.term.lines(runStartedNotice(data.content), "", { block: true, shown: NOTICE_LINES });
        break;
      case "done":
        this.end();
        break;
      case "error":
        this.end();
        this.term.line(`Error: ${data.error}`, "err");
        break;
      default:
        break;
    }
  }

  stream(kind, label, cls, text) {
    if (this.kind !== kind) {
      this.end();
      this.current = this.term.open(label, cls);
      this.kind = kind;
    }
    this.term.append(this.current, text);
  }

  end() {
    this.current = null;
    this.kind = null;
  }
}
