/* Notion ↔ Mnemo Converter — page logic.
 *
 * Everything real happens in Python; this file drives the screens, gathers the
 * parameters, and mirrors progress the Python side pushes back through
 * appProgress/appDone. It deliberately holds no conversion knowledge.
 *
 * The whole app is one small state machine: `show(name)` is the only way a
 * screen becomes visible, and the step rail is derived from the screen rather
 * than tracked alongside it, so the two cannot disagree.
 */

"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  direction: "pull",     // "pull" (Notion → Mnemo) or "push"
  screen: "start",
  items: [],             // pages + databases from list_content
  selected: new Set(),   // ids chosen for pull
  loadedFor: "",         // the key those items were fetched with
  parentItems: [],
  parentsLoadedFor: "",
  parentId: null,
  parentTitle: "",
  packagePath: null,
  packageInfo: null,
  outputPath: "",
  running: false,
  startedAt: 0,
  unit: "page",          // what the working screen counts: "page" or "note"
  errorDetail: "",
  errorBack: "connect",
  retry: null,
};

/* pywebview injects window.pywebview.api asynchronously. */
function api() {
  return window.pywebview.api;
}

window.addEventListener("pywebviewready", async () => {
  const s = await api().get_state();
  $("version").textContent = "v" + s.version;
  if (s.token) $("token").value = s.token;
  $("remember-token").checked = s.rememberToken;
  setOutputPath(s.defaultOutput);
});

/* ---------------- window chrome ---------------- */

$("win-min").addEventListener("click", () => api().window_minimize());
$("win-max").addEventListener("click", () => api().window_toggle_maximize());
$("win-close").addEventListener("click", () => api().window_close());

/* External links open in the real browser, not inside the app window. */
document.addEventListener("click", (event) => {
  const link = event.target.closest("a[data-url]");
  if (link) {
    event.preventDefault();
    api().open_url(link.dataset.url);
  }
});

/* ---------------- screens ---------------- */

const SCREENS = ["start", "connect", "pages", "ready", "source", "push-ready",
                 "working", "done", "error"];

/* Which rail step each screen belongs to; absent means no rail at all. */
const RAIL = { connect: 1, pages: 2, source: 2, ready: 3, "push-ready": 3 };

function show(name) {
  state.screen = name;
  for (const id of SCREENS) $("screen-" + id).hidden = id !== name;

  const step = RAIL[name];
  $("rail").hidden = !step;
  if (step) {
    $("step-2-name").textContent = state.direction === "pull" ? "Choose pages" : "Choose notes";
    for (const n of [1, 2, 3]) {
      const el = $("step-" + n);
      el.classList.toggle("now", n === step);
      el.classList.toggle("done", n < step);
      el.querySelector(".dot").textContent = n < step ? "✓" : String(n);
    }
  }
}

document.querySelectorAll("[data-go]").forEach((el) => {
  el.addEventListener("click", () => show(el.dataset.go));
});

/* ---------------- start: direction ---------------- */

function setDirection(which) {
  state.direction = which;
  $("dir-pull").classList.toggle("selected", which === "pull");
  $("dir-push").classList.toggle("selected", which === "push");
  $("dir-pull").setAttribute("aria-pressed", String(which === "pull"));
  $("dir-push").setAttribute("aria-pressed", String(which === "push"));
}
$("dir-pull").addEventListener("click", () => setDirection("pull"));
$("dir-push").addEventListener("click", () => setDirection("push"));

/* ---------------- connect ---------------- */

function token() {
  return $("token").value.trim();
}

$("token").addEventListener("change", persistToken);
$("remember-token").addEventListener("change", persistToken);
function persistToken() {
  api().remember_token(token(), $("remember-token").checked);
}

$("connect-continue").addEventListener("click", () => {
  if (!token()) {
    $("token").focus();
    return showError({ title: "Paste your integration key first.", checks: [] }, "connect");
  }
  persistToken();
  // A different key sees a different workspace, so a list fetched with the old
  // one is not just stale, it is wrong. Re-fetch whenever the key has changed.
  if (state.direction === "pull") {
    show("pages");
    if (!state.items.length || state.loadedFor !== token()) loadPages();
  } else {
    show("source");
    if (!state.parentItems.length || state.parentsLoadedFor !== token()) loadParents();
  }
});

/* ---------------- choosing pages (Notion → Mnemo) ---------------- */

$("reload-pages").addEventListener("click", (e) => { e.preventDefault(); loadPages(); });

async function loadPages() {
  placeholder($("page-list"), "Looking for pages you've shared…");
  // "Try again" has to put the user back where the work was, not merely repeat
  // the call behind whatever screen the failure left showing.
  state.retry = () => { show("pages"); loadPages(); };
  const result = await api().list_content(token());
  if (result.error) {
    placeholder($("page-list"), "Nothing loaded.");
    return showError(result.error, "connect");
  }
  state.items = result.items;
  state.loadedFor = token();
  // Sub-pages ride along with their parent, so the useful default is every
  // top-level thing and nothing else.
  state.selected = new Set(result.items.filter((i) => !i.nested).map((i) => i.id));
  renderPages();
}

$("filter").addEventListener("input", renderPages);
$("select-all").addEventListener("change", () => {
  const top = state.items.filter((i) => !i.nested);
  if ($("select-all").checked) top.forEach((i) => state.selected.add(i.id));
  else state.selected.clear();
  renderPages();
});

function renderPages() {
  const needle = $("filter").value.trim().toLowerCase();
  const list = $("page-list");
  list.replaceChildren();

  const shown = state.items.filter((i) => !needle || i.title.toLowerCase().includes(needle));
  if (!shown.length) {
    placeholder(list, needle ? "Nothing matches that." : "No pages are shared with this key yet.");
  }

  for (const item of shown) {
    const li = document.createElement("li");
    li.classList.toggle("on", state.selected.has(item.id));

    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = state.selected.has(item.id);
    box.tabIndex = -1;

    li.append(box, emoji(item.emoji), name(item.title), tag(describe(item)));
    li.addEventListener("click", (event) => {
      const on = event.target === box ? box.checked : !box.checked;
      box.checked = on;
      if (on) state.selected.add(item.id);
      else state.selected.delete(item.id);
      li.classList.toggle("on", on);
      updateCount();
    });
    list.append(li);
  }
  updateCount();
}

/* Only what the API actually told us — never an invented page count. */
function describe(item) {
  if (item.kind === "database") return item.nested ? "database, inside a page" : "database";
  return item.nested ? "inside another page" : "";
}

function updateCount() {
  const total = state.items.length;
  const n = state.selected.size;
  $("selection-count").textContent = total ? `${n} of ${total} chosen` : "";
  const top = state.items.filter((i) => !i.nested);
  $("select-all").checked = top.length > 0 && top.every((i) => state.selected.has(i.id));
  $("pages-continue").disabled = n === 0;
}

$("pages-continue").addEventListener("click", () => {
  const n = state.selected.size;
  $("ready-summary").textContent =
    `${plural(n, "page")} and everything inside them, with pictures, tables and equations.`;
  show("ready");
});

/* ---------------- ready (Notion → Mnemo) ---------------- */

function setOutputPath(path) {
  state.outputPath = path || "";
  const parts = state.outputPath.split(/[\\/]/);
  const file = parts.pop() || "notion-export.mnemo";
  const dir = parts.pop() || "";
  $("output-name").textContent = file;
  $("output-dir").textContent = dir ? "in " + dir : "";
}

$("pick-output").addEventListener("click", async () => {
  const path = await api().pick_output_path(state.outputPath);
  if (path) setOutputPath(path);
});

$("rename-folder").addEventListener("click", () => {
  $("opt-folder").focus();
  $("opt-folder").select();
});

$("run-pull").addEventListener("click", runPull);

async function runPull() {
  const chosen = state.items.filter((i) => state.selected.has(i.id));
  state.retry = () => { show("ready"); runPull(); };
  beginRun("Copying your notes…", "page");
  const result = await api().start_pull({
    token: token(),
    output: state.outputPath,
    pageIds: chosen.filter((i) => i.kind === "page").map((i) => i.id),
    databaseIds: chosen.filter((i) => i.kind === "database").map((i) => i.id),
    folder: $("opt-folder").value.trim(),
    covers: $("opt-covers").checked,
    dbProperties: $("opt-dbprops").checked,
    limit: $("opt-limit").value || null,
  });
  if (result.error) appDone({ error: result.error });
}

/* ---------------- source & destination (Mnemo → Notion) ---------------- */

$("pick-package").addEventListener("click", async () => {
  const info = await api().pick_package();
  if (!info) return;
  if (info.error) return showError(info.error, "source");
  state.packagePath = info.path;
  state.packageInfo = info;
  const file = info.path.split(/[\\/]/).pop();
  $("package-value").textContent =
    `${file} — ${plural(info.notes.length, "note")}, ${plural(info.images, "picture")}`;
  updateSourceReady();
});

$("reload-parents").addEventListener("click", (e) => { e.preventDefault(); loadParents(); });

async function loadParents() {
  placeholder($("parent-list"), "Looking for pages you've shared…");
  state.retry = () => { show("source"); loadParents(); };
  const result = await api().list_content(token());
  if (result.error) {
    placeholder($("parent-list"), "Nothing loaded.");
    return showError(result.error, "connect");
  }
  state.parentItems = result.items.filter((i) => i.kind === "page");
  state.parentsLoadedFor = token();
  // The page picked under the old key may not exist under this one.
  state.parentId = null;
  state.parentTitle = "";
  updateSourceReady();
  renderParents();
}

$("parent-filter").addEventListener("input", renderParents);

function renderParents() {
  const needle = $("parent-filter").value.trim().toLowerCase();
  const list = $("parent-list");
  list.replaceChildren();

  const shown = state.parentItems.filter((i) => !needle || i.title.toLowerCase().includes(needle));
  if (!shown.length) placeholder(list, needle ? "Nothing matches that." : "No pages are shared with this key yet.");

  for (const item of shown) {
    const li = document.createElement("li");
    li.classList.toggle("on", state.parentId === item.id);
    li.append(emoji(item.emoji), name(item.title), tag(item.nested ? "inside another page" : ""));
    li.addEventListener("click", () => {
      state.parentId = item.id;
      state.parentTitle = item.title;
      renderParents();
      updateSourceReady();
    });
    list.append(li);
  }
}

function updateSourceReady() {
  $("source-continue").disabled = !state.packagePath || !state.parentId;
}

$("source-continue").addEventListener("click", () => {
  const info = state.packageInfo;
  $("push-summary").textContent =
    `${plural(info.notes.length, "note")} on their way into Notion as new pages.`;
  $("push-package-value").textContent = state.packagePath.split(/[\\/]/).pop();
  $("push-parent-value").textContent = state.parentTitle;
  show("push-ready");
});

$("run-push").addEventListener("click", runPush);

async function runPush() {
  state.retry = () => { show("push-ready"); runPush(); };
  beginRun("Creating your pages…", "note");
  const result = await api().start_push({
    token: token(),
    package: state.packagePath,
    parent: state.parentId,
    uploadImages: $("opt-upload-images").checked,
  });
  if (result.error) appDone({ error: result.error });
}

/* ---------------- running ---------------- */

function beginRun(title, unit) {
  state.running = true;
  state.startedAt = Date.now();
  state.unit = unit;
  $("working-title").textContent = title;
  $("working-sub").textContent = "Getting started…";
  $("working-now").textContent = "";
  $("progress-fill").style.width = "0%";
  document.querySelector(".track").classList.add("indeterminate");
  $("stop-run").disabled = false;
  $("stop-run").textContent = "Stop";
  $("started-at").textContent = "Started " + clock(new Date());
  show("working");
}

$("stop-run").addEventListener("click", () => {
  $("stop-run").disabled = true;
  $("stop-run").textContent = "Stopping…";
  $("working-sub").textContent = "Finishing the page it's on…";
  api().cancel_run();
});

/* Called from Python: {text, index, total, label}. */
window.appProgress = function (payload) {
  if (typeof payload === "string") payload = { text: payload };
  const { index, total, label } = payload;

  if (index && total) {
    document.querySelector(".track").classList.remove("indeterminate");
    $("progress-fill").style.width = Math.round((index / total) * 100) + "%";
    const left = remaining(index, total);
    $("working-sub").textContent =
      `${cap(state.unit)} ${index} of ${total}` + (left ? ` · ${left}` : "");
    $("working-now").replaceChildren(emoji(emojiFor(label)), text(label || ""));
  } else if (payload.text) {
    $("working-now").replaceChildren(text(payload.text));
  }
};

/* An estimate is only worth showing once the rate has settled a little. */
function remaining(index, total) {
  const elapsed = Date.now() - state.startedAt;
  if (index < 2 || elapsed < 4000) return "";
  const per = elapsed / index;
  const left = Math.round((per * (total - index)) / 1000);
  if (left <= 5) return "nearly done";
  if (left < 60) return `about ${Math.max(10, Math.round(left / 10) * 10)} seconds left`;
  return `about ${Math.round(left / 60)} minute${Math.round(left / 60) === 1 ? "" : "s"} left`;
}

function emojiFor(label) {
  const hit = state.items.find((i) => i.title === label);
  return hit ? hit.emoji : "";
}

/* ---------------- finished ---------------- */

window.appDone = function (result) {
  state.running = false;

  if (result.error) return showError(result.error, state.direction === "pull" ? "ready" : "push-ready");

  const took = humanDuration(Date.now() - state.startedAt);
  const mark = $("screen-done").querySelector(".result-mark");

  if (result.cancelled) {
    mark.className = "result-mark stopped";
    mark.textContent = "–";
    $("done-title").textContent = "Stopped before it finished";
    $("done-sub").textContent = "Nothing was saved. You can start again whenever you like.";
    $("done-next").hidden = true;
    $("open-folder").hidden = true;
    setWarnings([]);
    return show("done");
  }

  mark.className = "result-mark ok";
  mark.textContent = "✓";
  $("done-next").hidden = false;

  if (state.direction === "pull") {
    const parts = result.path.split(/[\\/]/);
    const file = parts.pop();
    const dir = parts.pop() || "";
    $("done-title").textContent = `All ${plural(result.notes, "note")} are ready`;
    $("done-sub").textContent =
      `Saved as ${file}${dir ? " in " + dir : ""} · ${result.sizeMb} MB · took ${took}`;
    $("next-title").textContent = "Now bring it into Mnemo";
    setSteps([
      "Open Mnemo and go to <strong>Notes → Import</strong>.",
      result.folder
        ? `Pick the file. Your notes land in a folder called <strong>${escapeHtml(result.folder)}</strong>.`
        : "Pick the file. Your notes land at the top level.",
    ]);
    $("open-folder").hidden = false;
    $("open-folder").onclick = () => api().open_containing_folder(result.path);
  } else {
    $("done-title").textContent = `${cap(plural(result.pages, "page"))} created in Notion`;
    $("done-sub").textContent =
      `${plural(result.blocks, "block")}, ${plural(result.images, "picture")} uploaded · took ${took}`;
    $("next-title").textContent = "Now check it in Notion";
    setSteps([
      `Open <strong>${escapeHtml(state.parentTitle)}</strong> in Notion.`,
      "Your notes are inside it, as new pages.",
    ]);
    $("open-folder").hidden = true;
  }

  setWarnings(result.warnings || []);
  show("done");
};

function setSteps(lines) {
  const ol = $("next-steps");
  ol.replaceChildren();
  lines.forEach((line, i) => {
    const li = document.createElement("li");
    const num = document.createElement("span");
    num.className = "num";
    num.textContent = String(i + 1);
    const body = document.createElement("span");
    body.innerHTML = line;               // built here from escaped pieces only
    li.append(num, body);
    ol.append(li);
  });
}

function setWarnings(warnings) {
  const has = warnings.length > 0;
  $("warn-notice").hidden = !has;
  $("warn-list").hidden = true;
  $("warn-toggle").textContent = "See the list";
  if (!has) return;
  $("warn-text").textContent =
    `${cap(plural(warnings.length, "thing"))} couldn't come across exactly — everything else did.`;
  $("warn-list").textContent = warnings.join("\n");
}

$("warn-toggle").addEventListener("click", () => {
  const list = $("warn-list");
  list.hidden = !list.hidden;
  $("warn-toggle").textContent = list.hidden ? "See the list" : "Hide the list";
});

$("again").addEventListener("click", () => show("start"));
$("done-close").addEventListener("click", () => api().window_close());

/* ---------------- failed ---------------- */

/* `explanation` is Python's {title, checks[], detail} — never a raw exception. */
function showError(explanation, back) {
  state.running = false;
  state.errorDetail = explanation.detail || explanation.title || "";
  state.errorBack = back;

  $("error-title").textContent = explanation.title || "Something went wrong.";
  const checks = explanation.checks || [];
  $("error-checks-wrap").hidden = checks.length === 0;
  $("checks-title").textContent = checks.length > 1 ? "Two things to check" : "What to check";

  const host = $("error-checks");
  host.replaceChildren();
  for (const check of checks) {
    const div = document.createElement("div");
    div.className = "check-line";
    div.innerHTML = markup(check);
    host.append(div);
  }
  show("error");
}

$("error-back").addEventListener("click", () => show(state.errorBack));
$("error-retry").addEventListener("click", () => {
  if (state.retry) state.retry();
  else show(state.errorBack);
});

$("copy-detail").addEventListener("click", async (event) => {
  event.preventDefault();
  const link = event.target;
  try {
    await navigator.clipboard.writeText(state.errorDetail);
    link.textContent = "copied";
    setTimeout(() => (link.textContent = "copy the technical details"), 1600);
  } catch {
    // Clipboard access can be refused; showing the text is the honest fallback.
    $("warn-list").hidden = false;
    $("warn-list").textContent = state.errorDetail;
  }
});

/* ---------------- small helpers ---------------- */

function emoji(value) {
  const span = document.createElement("span");
  span.className = "emoji";
  span.textContent = value || "";
  return span;
}
function name(value) {
  const span = document.createElement("span");
  span.className = "name";
  span.textContent = value;
  return span;
}
function tag(value) {
  const span = document.createElement("span");
  span.className = "tag";
  span.textContent = value || "";
  return span;
}
function text(value) {
  return document.createTextNode(value);
}
function placeholder(list, message) {
  const li = document.createElement("li");
  li.className = "empty";
  li.textContent = message;
  list.replaceChildren(li);
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = String(value);
  return div.innerHTML;
}

/* Python writes **bold** in the prose it sends; nothing else is markup. */
function markup(value) {
  return escapeHtml(value).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}
function cap(value) {
  return value ? value[0].toUpperCase() + value.slice(1) : "";
}
function clock(date) {
  return date.toTimeString().slice(0, 5);
}
function humanDuration(ms) {
  const seconds = Math.max(1, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds} s`;
  return `${Math.floor(seconds / 60)} min ${String(seconds % 60).padStart(2, "0")} s`;
}
