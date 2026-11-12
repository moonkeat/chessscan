// Minimal PGN parser that builds a move tree (with variations, comments, NAGs)
// and validates every move with chess.js.
import { Chess } from "https://cdn.jsdelivr.net/npm/chess.js@1.0.0/+esm";

export const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

const SUFFIX_NAG = { "!": 1, "?": 2, "!!": 3, "??": 4, "!?": 5, "?!": 6 };
const GLYPH_NAG = {
  "=": 10, "∞": 13, "+=": 14, "⩲": 14, "=+": 15, "⩱": 15,
  "±": 16, "+/-": 16, "∓": 17, "-/+": 17, "+-": 18, "+−": 18, "-+": 19, "−+": 19,
};
export const NAG_TEXT = {
  1: "!", 2: "?", 3: "!!", 4: "??", 5: "!?", 6: "?!", 7: "□", 10: "=", 13: "∞",
  14: "⩲", 15: "⩱", 16: "±", 17: "∓", 18: "+−", 19: "−+", 22: "⨀", 32: "⟳",
  36: "→", 40: "↑", 132: "⇆", 146: "N",
};
const RESULTS = new Set(["1-0", "0-1", "1/2-1/2", "½-½", "*"]);

/** Split a PGN file into headers + movetext. */
export function splitPgn(text) {
  const headers = {};
  const lines = text.replace(/\r/g, "").split("\n");
  let i = 0;
  for (; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;
    const m = line.match(/^\[(\w+)\s+"((?:[^"\\]|\\.)*)"\]$/);
    if (!m) break;
    headers[m[1]] = m[2].replace(/\\(.)/g, "$1");
  }
  return { headers, movetext: lines.slice(i).join("\n") };
}

function tokenize(movetext) {
  const re = /\{[^}]*\}|;[^\n]*|\(|\)|\$\d+|[^\s(){};]+/g;
  return movetext.match(re) || [];
}

function newNode(parent, fen, move) {
  return {
    parent, fen, children: [], comment: "", preComment: "", nags: [],
    san: move ? move.san : null, from: move?.from, to: move?.to,
    ply: parent ? parent.ply + 1 : 0,
  };
}

function appendText(a, b) { return a ? `${a} ${b}` : b; }

/**
 * Parse a full PGN string. Returns { headers, root, result, error }.
 * On an illegal move, parsing stops and `error` describes the problem;
 * the tree contains everything up to that point.
 */
export function parsePgn(text) {
  const { headers, movetext } = splitPgn(text);
  const startFen = headers.FEN || START_FEN;
  try { new Chess(startFen); } catch (e) {
    return { headers, root: newNode(null, START_FEN), result: "*", error: `Invalid FEN: ${startFen}` };
  }
  const root = newNode(null, startFen);
  let cur = root;
  let pendingPre = "";     // comment that precedes the next move (start of game / variation)
  let atLineStart = true;  // true right after start or "("
  const stack = [];
  let result = headers.Result || "*";
  let error = null;

  for (const raw of tokenize(movetext)) {
    if (raw.startsWith("{") || raw.startsWith(";")) {
      const body = raw.startsWith("{") ? raw.slice(1, -1).trim() : raw.slice(1).trim();
      if (!body) continue;
      if (atLineStart) pendingPre = appendText(pendingPre, body);
      else cur.comment = appendText(cur.comment, body);
      continue;
    }
    if (raw === "(") {
      if (!cur.parent) { error = "Variation opened before any move."; break; }
      stack.push(cur);
      cur = cur.parent;
      atLineStart = true;
      continue;
    }
    if (raw === ")") {
      if (!stack.length) { error = "Unbalanced ')' in movetext."; break; }
      cur = stack.pop();
      atLineStart = false;
      pendingPre = "";
      continue;
    }
    if (raw.startsWith("$")) { if (cur.parent) cur.nags.push(Number(raw.slice(1))); continue; }
    if (RESULTS.has(raw)) { if (!stack.length) result = raw === "½-½" ? "1/2-1/2" : raw; continue; }
    if (GLYPH_NAG[raw]) { if (cur.parent) cur.nags.push(GLYPH_NAG[raw]); continue; }

    // Strip move numbers like "12." "12..." (possibly glued to the move: "12.Nf3").
    let tok = raw.replace(/^\d+(\.+|…)/, "");
    if (!tok) continue;
    if (/^[!?]+$/.test(tok)) { if (SUFFIX_NAG[tok] && cur.parent) cur.nags.push(SUFFIX_NAG[tok]); continue; }

    const m = tok.match(/^(.*?)([!?]{1,2})?$/);
    let san = m[1].replace(/×/g, "x").replace(/0-0-0/g, "O-O-O").replace(/0-0/g, "O-O");
    const game = new Chess(cur.fen);
    let move;
    try { move = game.move(san); } catch { move = null; }
    if (!move) {
      const num = cur.fen.split(" ")[5];
      const side = cur.fen.split(" ")[1] === "w" ? `${num}.` : `${num}...`;
      error = `Illegal or unreadable move "${raw}" at ${side} (${stack.length ? "in a variation" : "main line"}).`;
      break;
    }
    const node = newNode(cur, game.fen(), move);
    if (m[2] && SUFFIX_NAG[m[2]]) node.nags.push(SUFFIX_NAG[m[2]]);
    if (pendingPre) { node.preComment = pendingPre; pendingPre = ""; }
    cur.children.push(node);
    cur = node;
    atLineStart = false;
  }
  if (pendingPre && !root.children.length) root.comment = pendingPre;
  if (!error) attachCommentLines(root);
  return { headers, root, result, error };
}

// A move sequence written inside a comment, e.g. "38...Qf7 39.Qd6+ Nc6 40.b×c6 b×c6".
const SAN_RE = String.raw`(?:O-O-O|O-O|0-0-0|0-0|[KQRBN]?[a-h]?[1-8]?[x×:]?[a-h][1-8](?:=?[QRBN])?)[+#]?[!?]*`;
const NUM_RE = String.raw`\d+\s*(?:\.\.\.|…|\.)\s*`;
const LINE_RE = new RegExp(String.raw`(?<![\w.])(${NUM_RE})${SAN_RE}(?:\s+(?:${NUM_RE})?${SAN_RE})*`, "g");

/**
 * Make move sequences that remain inside comments playable.
 *
 * The server already turns commentary lines into real variations where PGN
 * allows it. What is left is mostly a line after the game's final move (e.g.
 * why the opponent resigned), which PGN cannot attach as a variation. Such a
 * line is played from the commented position and stored in node.commentLines;
 * its nodes are reachable by clicking but are not part of the main line.
 */
function attachCommentLines(root) {
  const stack = [root];
  while (stack.length) {
    const node = stack.pop();
    stack.push(...node.children);
    if (!node.comment) continue;
    for (const m of node.comment.matchAll(LINE_RE)) {
      const line = playLine(node, m[0]);
      if (line) (node.commentLines ||= []).push(line);
    }
  }
}

function playLine(from, text) {
  const [, fullmove] = from.fen.split(" ").slice(4);
  const first = text.match(/^(\d+)\s*(\.\.\.|…|\.)/);
  const blackToMove = from.fen.split(" ")[1] === "b";
  // the line must start right here: same move number and side to move
  if (!first || Number(first[1]) !== Number(fullmove) || (first[2] !== ".") !== blackToMove) return null;
  const nodes = [];
  let cur = from;
  for (const raw of text.split(/\s+/)) {
    const tok = raw.replace(/^\d+(\.+|…)/, "");
    if (!tok) continue;
    const m = tok.match(/^(.*?)([!?]{1,2})?$/);
    const game = new Chess(cur.fen);
    let move;
    try { move = game.move(m[1].replace(/×/g, "x").replace(/0-0-0/g, "O-O-O").replace(/0-0/g, "O-O")); } catch { move = null; }
    if (!move) return null; // only fully legal lines become playable
    const node = newNode(cur, game.fen(), move);
    if (m[2] && SUFFIX_NAG[m[2]]) node.nags.push(SUFFIX_NAG[m[2]]);
    if (nodes.length) nodes[nodes.length - 1].children.push(node);
    nodes.push(node);
    cur = node;
  }
  return nodes.length ? nodes : null;
}

/** "12." for white-to-move parents, "12..." for black. */
export function moveNumberLabel(node) {
  const [, side, , , , full] = node.parent.fen.split(" ");
  return side === "w" ? `${full}.` : `${full}...`;
}

export function isWhiteMove(node) {
  return node.parent.fen.split(" ")[1] === "w";
}
