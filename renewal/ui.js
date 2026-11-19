"use strict";

const byId = (id) => document.getElementById(id);
const messages = {
  idle: "Start a private Bookker browser to sign in on this phone.",
  starting: "Opening Bookker…",
  sign_in: "Complete Bookker sign-in and MFA, then tap Verify and save.",
  verifying: "Checking the new session in a fresh browser…",
  closing: "Closing the private browser…",
  verified: "Bookker sign-in verified. The next parking run will use this session.",
  sign_in_incomplete: "Bookker sign-in could not be verified. Check that MFA completed and try again.",
  timed_out: "The sign-in window expired. Start again to retry.",
  cancelled: "Sign-in cancelled. The installed session was preserved.",
  failed: "Renewal failed. The installed session was preserved. Start again to retry.",
};
const activePhases = new Set(["starting", "sign_in", "verifying", "closing"]);
const frame = byId("frame");
let state = {phase: "idle", attempt: null};
let frameUrl;
let pointer;
let timer;
let actionInFlight = false;

function render(next) {
  state = next;
  const active = activePhases.has(state.phase);
  byId("status").textContent = messages[state.phase];
  byId("start").hidden = active;
  byId("viewer").hidden = state.phase !== "sign_in";
  byId("cancel").hidden = !active || state.phase === "closing";
  if (state.phase !== "sign_in") {
    frame.hidden = true;
    frame.removeAttribute("src");
    if (frameUrl) URL.revokeObjectURL(frameUrl);
    frameUrl = undefined;
    byId("text").value = "";
  }
}

async function request(path, data) {
  const options = {cache: "no-store", signal: AbortSignal.timeout(8000)};
  if (data !== undefined) {
    options.method = "POST";
    options.headers = {"Content-Type": "application/json"};
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  if (!response.ok) throw new Error("Request failed");
  return response;
}

function showError() {
  byId("error").textContent = "The renewal page could not complete the action. Reconnect to Tailscale or try again.";
  byId("error").hidden = false;
}

function pollSoon() {
  clearTimeout(timer);
  if (activePhases.has(state.phase) && !document.hidden) timer = setTimeout(update, 500);
}

async function update() {
  const previous = state;
  try {
    if (actionInFlight) return;
    const next = await (await request("/status")).json();
    if (actionInFlight || state !== previous) return;
    render(next);
    if (next.phase === "sign_in" && !document.hidden) {
      const pixels = await (await request("/frame", {attempt: next.attempt})).blob();
      if (state !== next || actionInFlight || document.hidden) return;
      if (frameUrl) URL.revokeObjectURL(frameUrl);
      frameUrl = URL.createObjectURL(pixels);
      frame.src = frameUrl;
      frame.hidden = false;
    }
  } catch {
    if (state === previous && !activePhases.has(state.phase)) showError();
  } finally {
    pollSoon();
  }
}

async function action(path, details = {}) {
  if (actionInFlight) return;
  actionInFlight = true;
  byId("error").hidden = true;
  clearTimeout(timer);
  try {
    const result = await request(path, {attempt: state.attempt, ...details});
    render(await result.json());
  } catch {
    showError();
  } finally {
    actionInFlight = false;
    if (activePhases.has(state.phase)) update();
  }
}

byId("start").onclick = () => action("/start");
byId("finish").onclick = () => action("/finish");
byId("cancel").onclick = () => action("/cancel");

function typeText() {
  const input = byId("text");
  const text = input.value;
  input.value = "";
  input.blur();
  if (text) action("/input", {text});
}
byId("type").onclick = typeText;
byId("text").onkeydown = (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    typeText();
  }
};
for (const button of document.querySelectorAll("[data-key]")) {
  button.onclick = () => action("/input", {key: button.dataset.key});
}

frame.onpointerdown = (event) => {
  frame.setPointerCapture(event.pointerId);
  pointer = {x: event.clientX, y: event.clientY};
};
frame.onpointerup = (event) => {
  if (!pointer) return;
  const start = pointer;
  pointer = undefined;
  const box = frame.getBoundingClientRect();
  const delta = start.y - event.clientY;
  if (Math.abs(delta) > 16) {
    action("/input", {scroll: Math.round(delta * 690 / box.height)});
  } else {
    const x = Math.max(0, Math.min(419, Math.floor((start.x - box.left) * 420 / box.width)));
    const y = Math.max(0, Math.min(689, Math.floor((start.y - box.top) * 690 / box.height)));
    action("/input", {tap: [x, y]});
  }
};
frame.onpointercancel = () => { pointer = undefined; };
document.addEventListener("visibilitychange", () => {
  clearTimeout(timer);
  if (!document.hidden) update();
});
update();
