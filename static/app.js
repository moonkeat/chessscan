import { Chessground } from "https://cdn.jsdelivr.net/npm/chessground@9.1.1/dist/chessground.js";
import { parsePgn, moveNumberLabel, isWhiteMove, NAG_TEXT } from "./pgn.js";

const $ = (id) => document.getElementById(id);
const MAX_SIDE = 2576; // downscale big phone photos before upload
const POLL_MS = 1000;

let pages = [];        // data URLs of the selected page photos
let games = [];        // results from the server
let gameIndex = 0;
let parsed = null;     // { headers, root, result, error }
let current = null;    // current node in the tree
let orientation = "white";
let nodeEls = new Map();

// ---------- board (Chessground draws, chess.js in pgn.js handles the rules) ----------
const cg = Chessground($("board"), {
  viewOnly: true,
  coordinates: true,
  animation: { enabled: true, duration: 180 },
  highlight: { lastMove: true, check: true },
  drawable: { enabled: false, visible: true },
});
new ResizeObserver(() => cg.redrawAll()).observe($("board"));

// ---------- image input ----------
const drop = $("drop");
$("file").addEventListener("change", (e) => { addImages(e.target.files); e.target.value = ""; });
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault(); drop.classList.remove("over");
  addImages(e.dataTransfer.files);
});
document.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.items || [])].filter((i) => i.type.startsWith("image/")).map((i) => i.getAsFile());
  if (files.length) addImages(files);
});

async function addImages(files) {
  for (const file of [...files].filter((f) => f.type.startsWith("image/"))) {
    pages.push(await downscale(file));
  }
  renderThumbs();
  setStatus("");
}

async function downscale(file) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg", 0.9);
}

function renderThumbs() {
  const box = $("thumbs");
  box.innerHTML = "";
  pages.forEach((url, i) => {
    const t = document.createElement("div");
    t.className = "thumb";
    const img = Object.assign(document.createElement("img"), { src: url, alt: `Page photo ${i + 1}` });
    const rm = Object.assign(document.createElement("button"), { textContent: "×", title: "Remove" });
    rm.addEventListener("click", () => { pages.splice(i, 1); renderThumbs(); });
    t.append(img, rm);
    box.append(t);
  });
  $("extract").disabled = !pages.length;
  $("extract").textContent = pages.length > 1 ? `Extract PGN from ${pages.length} pages` : "Extract PGN";
}

// ---------- extraction (background job + progress) ----------
$("extract").addEventListener("click", async () => {
  if (!pages.length) return;
  setBusy(true);
  setStatus("");
  showProgress(0, "Uploading…");
  try {
    const res = await fetch("/api/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ images: pages, translate: $("translate").checked, backends: $("backend").value }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    const result = await pollJob(data.job);
    games = result.games || [];
    if (!games.length) throw new Error("No chess game found on these pages.");
    setStatus((result.log || []).join("\n"));
    pages = [];          // finished: clear the photos from the page
    renderThumbs();
    setupGameSelect();
    showGame(0, result.notes);
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    setBusy(false);
    $("progress").hidden = true;
  }
});

async function pollJob(id) {
  const started = Date.now();
  while (true) {
    await new Promise((r) => setTimeout(r, POLL_MS));
    const res = await fetch(`/api/jobs/${id}`, { cache: "no-store" });
    const job = await res.json();
    if (!res.ok) throw new Error(job.error || `HTTP ${res.status}`);
    showProgress(job.progress, job.message, Date.now() - started);
    if (job.status === "done") return job.result;
    if (job.status === "error") throw new Error(job.error);
  }
}

function showProgress(frac, msg, elapsedMs = 0) {
  $("progress").hidden = false;
  const pct = Math.round((frac || 0) * 100);
  $("progressFill").style.width = `${Math.max(pct, 2)}%`;
  document.querySelector(".progress-bar").setAttribute("aria-valuenow", String(pct));
  $("progressMsg").textContent = msg || "";
  const s = Math.round(elapsedMs / 1000);
  $("progressTime").textContent = s ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "";
}

function setBusy(busy) {
  $("extract").disabled = busy || !pages.length;
  $("file").disabled = busy;
  drop.classList.toggle("disabled", busy);
}

function setStatus(msg, isError = false) {
  $("status").textContent = msg;
  $("status").classList.toggle("error", isError);
}

function setupGameSelect() {
  const sel = $("gameSelect");
  sel.innerHTML = "";
  games.forEach((g, i) => {
    const h = g.headers;
    sel.add(new Option(`Game ${i + 1}: ${h.White} – ${h.Black}`, i));
  });
  sel.hidden = games.length < 2;
}
$("gameSelect").addEventListener("change", (e) => showGame(Number(e.target.value)));

function showGame(i, notes = "") {
  gameIndex = i;
  const g = games[i];
  $("pgn").value = g.pgn;
  $("viewer").hidden = false;
  renderPgn(g.pgn, notes, g.errors);
}

// ---------- rendering ----------
function renderPgn(text, notes = "", serverErrors = []) {
  parsed = parsePgn(text);
  const h = parsed.headers;
  $("players").textContent = `${h.White || "?"} – ${h.Black || "?"}`;
  $("meta").textContent = [h.Event, h.Round && h.Round !== "?" ? `Round ${h.Round}` : "", h.Date,
    h.ECO && `${h.ECO}${h.Opening ? " " + h.Opening : ""}`]
    .filter((x) => x && x !== "?" && !/^\?+\.\?+\.\?+$/.test(x)).join(" · ");

  const problems = [];
  if (parsed.error) problems.push(parsed.error);
  serverErrors?.forEach((e) => { if (!parsed.error) problems.push(`Validator: ${e}`); });
  const w = $("warnings");
  const items = [...problems, ...(notes ? [`Note: ${notes}`] : [])];
  w.hidden = !items.length;
  w.classList.toggle("error", problems.length > 0);
  w.innerHTML = items.length
    ? `<strong>${problems.length ? "Check the transcription" : "Notes"}</strong><ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`
    : "";

  renderMoves();
  renderChecks();
  goTo(parsed.root);
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function renderMoves() {
  const box = $("moves");
  box.innerHTML = "";
  nodeEls = new Map();
  const main = document.createElement("div");
  main.className = "main";
  if (parsed.root.comment) main.append(span("comment", parsed.root.comment));
  if (parsed.root.children[0]) renderLine(parsed.root.children[0], main);
  main.append(span("result", parsed.result));
  box.append(main);
}

function span(cls, text) {
  const s = document.createElement("span");
  s.className = cls;
  s.textContent = text;
  return s;
}

function renderLine(first, container) {
  let node = first;
  let needNumber = true;
  while (node) {
    if (node.preComment) { container.append(span("comment", node.preComment)); needNumber = true; }
    if (isWhiteMove(node) || needNumber) container.append(span("num", moveNumberLabel(node)));
    const nags = node.nags.map((n) => NAG_TEXT[n] || `$${n}`).join("");
    const el = span("mv", node.san + nags);
    if (diagramAt(node)) el.classList.add("has-diagram");
    if (suggestionAt(node)) el.classList.add("suspect");
    const target = node;
    el.addEventListener("click", () => goTo(target));
    nodeEls.set(node, el);
    container.append(el, " ");
    needNumber = false;

    if (node.comment) { container.append(span("comment", node.comment)); needNumber = true; }

    const siblings = node.parent.children;
    if (siblings[0] === node && siblings.length > 1) {
      for (const alt of siblings.slice(1)) {
        const v = document.createElement("span");
        v.className = "var";
        v.append("( ");
        renderLine(alt, v);
        v.append(") ");
        container.append(v);
      }
      needNumber = true;
    }
    node = node.children[0];
  }
}

// ---------- diagram check ----------
function mainlinePly(node) {
  // ply of a main-line node, or -1 for nodes inside a variation
  let n = node;
  while (n.parent) {
    if (n.parent.children[0] !== n) return -1;
    n = n.parent;
  }
  return node.ply;
}

function nodeAtPly(ply) {
  let n = parsed.root;
  for (let i = 0; i < ply && n.children[0]; i++) n = n.children[0];
  return n;
}

function diagramAt(node) {
  const ply = mainlinePly(node);
  return ply < 0 ? null : (games[gameIndex]?.diagrams || []).find((d) => d.ply === ply) || null;
}

function suggestionAt(node) {
  const ply = mainlinePly(node);
  return ply < 0 ? null : (games[gameIndex]?.suggestions || []).find((s) => s.ply === ply) || null;
}

function moveLabel(number, white, san) {
  return `${number}${white ? "." : "..."} ${san}`;
}

function renderChecks() {
  const g = games[gameIndex];
  const box = $("checks");
  const diagrams = g?.diagrams || [];
  const suggestions = g?.suggestions || [];
  box.innerHTML = "";
  box.hidden = !diagrams.length;
  if (!diagrams.length) return;

  const head = document.createElement("div");
  head.className = "checks-head";
  head.textContent = suggestions.length
    ? `Book diagrams: ${diagrams.length} matched, ${suggestions.length} move(s) to check`
    : `Book diagrams: ${diagrams.length} matched to moves, no conflicts found`;
  box.append(head);
  box.classList.toggle("bad", suggestions.length > 0);

  for (const s of suggestions) {
    const row = document.createElement("button");
    row.className = "check-row suspect";
    row.innerHTML = `Diagram suggests <b>${esc(moveLabel(s.move_number, s.white, s.suggested))}</b> ` +
      `instead of <b>${esc(moveLabel(s.move_number, s.white, s.played))}</b>`;
    row.addEventListener("click", () => goTo(nodeAtPly(s.ply)));
    box.append(row);
  }
  const list = document.createElement("div");
  list.className = "check-chips";
  for (const d of diagrams) {
    const node = nodeAtPly(d.ply);
    const chip = document.createElement("button");
    chip.className = "chip";
    chip.textContent = node.san ? moveLabel(...labelParts(node)) : "start";
    chip.title = d.diff ? `${d.diff} square(s) read differently (often photo noise)` : "Exact match";
    chip.addEventListener("click", () => goTo(node));
    list.append(chip);
  }
  box.append(list);
}

function labelParts(node) {
  const [, side, , , , full] = node.parent.fen.split(" ");
  return [Number(full), side === "w", node.san];
}

// ---------- navigation ----------
function goTo(node) {
  if (!node) return;
  current = node;
  const fen = node.fen;
  const turn = fen.split(" ")[1] === "w" ? "white" : "black";
  const inCheck = /[+#]$/.test(node.san || "");
  cg.set({
    fen,
    orientation,
    turnColor: turn,
    lastMove: node.from && node.to ? [node.from, node.to] : undefined,
    check: inCheck ? turn : false,
  });

  const d = diagramAt(node);
  const box = $("diagramBox");
  box.hidden = !d;
  if (d) {
    $("diagramImg").src = d.image;
    $("diagramImg").alt = "Diagram from the book at this move";
    $("diagramInfo").textContent = d.diff
      ? `Book diagram · ${d.diff} square(s) read differently (circled)`
      : "Book diagram · matches this position";
    cg.setAutoShapes(d.squares.map((sq) => ({ orig: sq, brush: "red" })));
  } else {
    cg.setAutoShapes([]);
  }

  nodeEls.forEach((el) => el.classList.remove("active"));
  const el = nodeEls.get(node);
  if (el) { el.classList.add("active"); el.scrollIntoView({ block: "nearest" }); }
}

function nav(action) {
  if (!current) return;
  if (action === "start") goTo(parsed.root);
  if (action === "prev" && current.parent) goTo(current.parent);
  if (action === "next" && current.children[0]) goTo(current.children[0]);
  if (action === "end") { let n = current; while (n.children[0]) n = n.children[0]; goTo(n); }
}
document.querySelectorAll("[data-nav]").forEach((b) => b.addEventListener("click", () => nav(b.dataset.nav)));
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT" || $("viewer").hidden) return;
  const map = { ArrowLeft: "prev", ArrowRight: "next", Home: "start", End: "end" };
  if (map[e.key]) { e.preventDefault(); nav(map[e.key]); }
});
$("flip").addEventListener("click", () => {
  orientation = orientation === "white" ? "black" : "white";
  cg.set({ orientation });
});

// ---------- PGN box ----------
$("apply").addEventListener("click", () => renderPgn($("pgn").value));
$("copy").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("pgn").value);
  $("copy").textContent = "Copied";
  setTimeout(() => ($("copy").textContent = "Copy"), 1200);
});
$("download").addEventListener("click", () => {
  const h = parsed?.headers || {};
  const name = `${h.White || "white"}_vs_${h.Black || "black"}`.replace(/[^\w\-]+/g, "_");
  const blob = new Blob([$("pgn").value], { type: "application/x-chess-pgn" });
  const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: `${name}.pgn` });
  a.click();
  URL.revokeObjectURL(a.href);
});

// Allow opening the viewer with a PGN directly (no photo) - handy for manual entry.
if (location.hash === "#manual") {
  $("viewer").hidden = false;
  games = [{ diagrams: [], suggestions: [] }];
  $("pgn").value = '[White "?"]\n[Black "?"]\n\n1. e4 e5 *';
  renderPgn($("pgn").value);
}
