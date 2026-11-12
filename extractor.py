"""Extract chess games from photos of chess book pages and return validated PGN.

Each page is transcribed separately by a vision model. Pages are ordered by
their printed page number and games that run across pages are stitched
together. python-chess then checks every move page by page; if a page
introduces an illegal move, that page alone is sent back to the model (with
the position at the start of the page) so it can re-read it. If a backend
still fails, the next one is tried.

Backends (tried in order, see EXTRACT_BACKENDS):
  ollama  - local model via Ollama (free, offline)      [default first]
  gemini  - Google Gemini API free tier                  [default fallback]

Environment variables:
  EXTRACT_BACKENDS   comma-separated order        (default "ollama,gemini")
  OLLAMA_HOST        Ollama server URL             (default http://localhost:11434)
  OLLAMA_MODEL       vision model tag              (default qwen3-vl:8b-instruct)
  OLLAMA_NUM_CTX     context window                (default 8192)
  GEMINI_API_KEY     key from https://aistudio.google.com/apikey
  GEMINI_MODEL       model id                      (default gemini-flash-latest)

CLI usage:
    python extractor.py page1.jpg [page2.jpg ...] [--translate] [--backends ollama,gemini] > games.pgn
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request

import chess
import chess.pgn

logging.getLogger("chess.pgn").setLevel(logging.CRITICAL)  # errors are collected in game.errors

MAX_REPAIR_ROUNDS = 2
DEFAULT_BACKENDS = "ollama,gemini"

HEADER_KEYS = ["Event", "Site", "Date", "Round", "White", "Black", "Result", "ECO", "Opening"]

SCHEMA = {
    "type": "object",
    "properties": {
        "games": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "headers": {
                        "type": "object",
                        "properties": {k: {"type": "string"} for k in HEADER_KEYS},
                        "required": HEADER_KEYS,
                        "additionalProperties": False,
                    },
                    "continues_previous": {"type": "boolean"},
                    "fen": {"type": "string"},
                    "movetext": {"type": "string"},
                },
                "required": ["continues_previous", "headers", "fen", "movetext"],
                "additionalProperties": False,
            },
        },
        "page_number": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["page_number", "games", "notes"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You transcribe chess games from photos of chess book pages into PGN.

Reply with JSON only, in this shape:
{"page_number": "", "games": [{"continues_previous": false, "headers": {"Event": "", "Site": "", "Date": "", "Round": "", "White": "", "Black": "", "Result": "", "ECO": "", "Opening": ""}, "fen": "", "movetext": ""}], "notes": ""}

page_number: the page number printed on the page (e.g. "004"), or "" if none is visible.

Output one entry in `games` per game that appears on the page, in page order. A page may finish one game and start the next one: output both.

continues_previous:
- true for a game that was already in progress at the top of the page (the page starts with a later move such as "14...O-O-O", or with commentary before the first move). Its movetext starts exactly where the page starts: commentary before the first move goes first as a {comment}, then the moves with their real move numbers, e.g. "{...} 14... O-O-O 15. O-O h6". If the page holds only the end of the commentary and the result, the movetext is just "{...} 1-0". Use "?" for headers not printed on this page.
- false for a game that starts on this page (title, players, then move 1).

headers:
- Fill Event, Site, Date (YYYY.MM.DD, use ?? for unknown parts), Round, White, Black, ECO, Opening from the page.
- Write names and events in English / Latin script (e.g. 卡尔森 -> "Carlsen, Magnus"; 2015年欧洲队际锦标赛第九轮 -> Event "European Team Championship 2015", Round "9"). Use "?" when unknown.
- Result: "1-0", "0-1", "1/2-1/2", or "*" if the result is not shown on this page.

fen:
- Empty string, except when the book itself starts the game from a special position (a study or puzzle): then give that position's FEN.

movetext (standard PGN movetext, without headers):
- The main line is the game itself (usually printed in bold). Moves inside the commentary that begin from an earlier move number (e.g. "7...Qd7 8.Ngf3 e6 9.Ne5") are side variations. Put them in parentheses directly after the main-line move they replace, e.g. `7. Qb3 Qc8 (7... Qd7 8. Ngf3 e6) 8. Ngf3`.
- Use English SAN only: K Q R B N, captures with "x" (convert "×", ":" etc.), castling as O-O / O-O-O, promotions as e8=Q. Convert figurines and non-English piece letters. Convert long algebraic (Ng1-f3) to SAN (Nf3).
- Keep move annotations directly on the move: !, ?, !!, ??, !?, ?!. Convert evaluation symbols to NAGs after the move: = $10, += / ⩲ $14, =+ / ⩱ $15, ± $16, ∓ $17, +- $18, -+ $19, ∞ $13, N (novelty) $146.
- Put the book's prose commentary in {curly braces} right after the move it refers to. COMMENT_LANGUAGE_RULE Never use "{" or "}" inside a comment.
- Replace diagram references like "(图1)" / "(Diagram 1)" with a comment such as {Diagram 1}.
- End the movetext with the result token (same as the Result header).

Accuracy matters more than anything else: every move must be legal. Check moves against the printed diagrams when present. Watch for OCR confusions such as B/8, Q/O/0, N/K, l/1, b/6, c/e, g/9.

Use notes for anything uncertain (e.g. a move you could not read clearly) or an empty string."""

# Repeating the key rules in the user turn noticeably helps small local models.
FIRST_PROMPT = (
    "Transcribe the chess game(s) on this page into PGN. Checklist:\n"
    "1. Main line: the bold moves, in order.\n"
    "2. Variations: every move sequence inside the commentary paragraphs (e.g. '7...Qd7 8.Ngf3 ...') "
    "goes in parentheses right after the main-line move it replaces.\n"
    "3. Comments: each commentary paragraph goes in {braces} after the move it follows.\n"
    "4. Result: only if printed on the page, otherwise '*'.\n"
    "5. Page start: if the page starts in the middle of a game, set continues_previous=true and start "
    "the movetext where the page starts. If a new game starts on the page, add it as another entry."
)


def system_prompt(translate: bool) -> str:
    rule = (
        "Translate the commentary into concise, natural English."
        if translate
        else "Keep the commentary in the book's original language, transcribed faithfully."
    )
    return SYSTEM_PROMPT.replace("COMMENT_LANGUAGE_RULE", rule)


class BackendUnavailable(Exception):
    """Backend is not configured or not reachable - skip to the next one."""


# --------------------------------------------------------------------------- HTTP

def _post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = 600) -> dict:
    req = urllib.request.Request(
        url, json.dumps(payload).encode("utf-8"),
        {"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {e.code} from {url.split('?')[0]}: {body}") from None


def _parse_json_text(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):  # tolerate fenced output from smaller models
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError("Model did not return JSON.")
    data = json.loads(text[start:end + 1])
    data.setdefault("notes", "")
    data.setdefault("games", [])
    data.setdefault("page_number", "")
    for g in data["games"]:
        g.setdefault("continues_previous", False)
        g.setdefault("fen", "")
        g.setdefault("movetext", "")
        hdr = g.setdefault("headers", {})
        for k in HEADER_KEYS:
            hdr.setdefault(k, "?")
    return data


# ----------------------------------------------------------------------- backends
# Each backend keeps its own conversation so a repair request can refer back to
# the image and to the model's previous answer.

class OllamaBackend:
    name = "ollama"

    def __init__(self):
        self.host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        # The instruct variant answers directly; "qwen3-vl:8b" is a thinking model (~10x slower).
        self.model = os.environ.get("OLLAMA_MODEL", "qwen3-vl:8b-instruct")
        # 8192 fits an 8 GB GPU better than larger contexts and is enough for a page + 2 repairs.
        self.num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
        self.messages: list = []
        self._check()

    def _check(self):
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=3) as resp:
                tags = json.loads(resp.read())
        except Exception:
            raise BackendUnavailable(f"Ollama is not running at {self.host}.") from None
        names = {m.get("name") for m in tags.get("models", [])} | {m.get("model") for m in tags.get("models", [])}
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        if wanted not in names:
            raise BackendUnavailable(f"Ollama model '{self.model}' is not installed (run: ollama pull {self.model}).")

    def _chat(self) -> dict:
        resp = _post_json(f"{self.host}/api/chat", {
            "model": self.model,
            "messages": self.messages,
            "format": SCHEMA,
            "stream": False,
            "keep_alive": "10m",
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
        }, timeout=1800)  # generous: thinking models on a partly CPU-offloaded GPU are slow
        text = resp["message"]["content"]
        self.messages.append({"role": "assistant", "content": text})
        return _parse_json_text(text)

    def start(self, system: str, image: bytes, mime: str, text: str) -> dict:
        self.messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": text, "images": [base64.b64encode(image).decode("ascii")]},
        ]
        return self._chat()

    def followup(self, text: str) -> dict:
        self.messages.append({"role": "user", "content": text})
        return self._chat()


class GeminiBackend:
    name = "gemini"

    def __init__(self):
        self.key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not self.key:
            raise BackendUnavailable("GEMINI_API_KEY is not set.")
        self.model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
        self.system = ""
        self.contents: list = []

    def _generate(self) -> dict:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        try:
            resp = _post_json(url, {
                "systemInstruction": {"parts": [{"text": self.system}]},
                "contents": self.contents,
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                    "responseJsonSchema": SCHEMA,
                },
            }, headers={"x-goog-api-key": self.key}, timeout=300)
        except RuntimeError as e:
            if "HTTP 429" in str(e):
                raise RuntimeError("Gemini free-tier quota exhausted - try again later.") from None
            raise
        candidates = resp.get("candidates") or []
        if not candidates:
            reason = resp.get("promptFeedback", {}).get("blockReason", "no candidates")
            raise RuntimeError(f"Gemini returned no answer ({reason}).")
        content = candidates[0].get("content", {})
        text = "".join(p.get("text", "") for p in content.get("parts", []) if not p.get("thought"))
        if not text:
            raise RuntimeError(f"Gemini returned an empty answer ({candidates[0].get('finishReason')}).")
        self.contents.append(content)  # keep the model turn as returned (incl. thought signatures)
        return _parse_json_text(text)

    def start(self, system: str, image: bytes, mime: str, text: str) -> dict:
        self.system = system
        self.contents = [{"role": "user", "parts": [
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode("ascii")}},
            {"text": text},
        ]}]
        return self._generate()

    def followup(self, text: str) -> dict:
        self.contents.append({"role": "user", "parts": [{"text": text}]})
        return self._generate()


BACKENDS = {"ollama": OllamaBackend, "gemini": GeminiBackend}


# --------------------------------------------------------------------- validation

_FIGURINES = str.maketrans("♔♕♖♗♘♚♛♜♝♞", "KQRBNKQRBN")
_GLYPH_NAGS = [("+-", 18), ("-+", 19), ("+/-", 16), ("-/+", 17), ("±", 16), ("∓", 17),
               ("+=", 14), ("=+", 15), ("⩲", 14), ("⩱", 15), ("∞", 13)]
_GLYPH_RE = re.compile(
    r"(" + "|".join(re.escape(g) for g, _ in _GLYPH_NAGS) + r")(?=[\s)]|$)"
)
_GLYPH_TO_NAG = dict(_GLYPH_NAGS)


_DIAGRAM_RE = re.compile(r"[（(]\s*(?:图|圖|Diagram|Diag\.?|Fig\.?)\s*(\d+)\s*[)）]", re.I)


def _normalize_moves(text: str) -> str:
    text = _DIAGRAM_RE.sub(r"{Diagram \1}", text)  # "(图1)" would otherwise parse as a bogus variation
    text = text.translate(_FIGURINES).replace("×", "x").replace("：", ":")
    text = re.sub(r"\b([KQRBN]?[a-h]?[1-8]?x)\s+([a-h][1-8])", r"\1\2", text)  # "Rx e5" -> "Rxe5"
    text = re.sub(r"\b0-0-0\b", "O-O-O", text)
    text = re.sub(r"(?<![-\d/])0-0(?![-\d])", "O-O", text)
    text = _GLYPH_RE.sub(lambda m: f" ${_GLYPH_TO_NAG[m.group(1)]}", text)  # "Bb5+-" -> "Bb5 $18"
    return text


def normalize_movetext(movetext: str) -> str:
    """Fix notation the model copied verbatim from the book; comments are left untouched."""
    parts = re.split(r"(\{[^}]*\})", movetext)
    return "".join(p if p.startswith("{") else _normalize_moves(p) for p in parts)


def build_pgn(game: dict) -> str:
    headers = game["headers"]
    lines = []
    for key in HEADER_KEYS:
        value = (headers.get(key) or "?").replace("\\", "\\\\").replace('"', '\\"')
        if key in ("ECO", "Opening") and value in ("", "?"):
            continue
        if key == "Result" and value not in ("1-0", "0-1", "1/2-1/2"):
            value = "*"
        if key == "Date" and not re.fullmatch(r"[\d?]{4}\.[\d?]{2}\.[\d?]{2}", value):
            year = re.match(r"\d{4}", value)
            value = f"{year.group(0) if year else '????'}.??.??"
        lines.append(f'[{key} "{value}"]')
    fen = (game.get("fen") or "").strip()
    if fen:
        lines.append('[SetUp "1"]')
        lines.append(f'[FEN "{fen}"]')
    return "\n".join(lines) + "\n\n" + normalize_movetext(game["movetext"]).strip() + "\n"


def validate_pgn(pgn: str) -> list[str]:
    """Return a list of human-readable problems (empty list means fully legal)."""
    try:
        game = chess.pgn.read_game(io.StringIO(pgn))
    except Exception as exc:
        return [f"PGN could not be parsed: {exc}"]
    if game is None:
        return ["No game found in PGN."]
    problems = [str(err) for err in game.errors]
    if game.next() is None:
        problems.append("Main line contains no legal moves.")
    return problems


# A move sequence written inside commentary, e.g. "7...Qd7 8.Ngf3 e6 9.Ne5 N×e5? 10.d×e5 Ne4 11.Bb5+-".
_SAN = (r"(?:O-O-O|O-O|0-0-0|0-0|[KQRBN♔♕♖♗♘♚♛♜♝♞]?[a-h]?[1-8]?[x×:]?[a-h][1-8](?:=?[QRBN])?)"
        r"(?:\+-|-\+|\+/-|-/\+|[±∓⩲⩱∞]|[+#])?(?:[!?]{1,2})?(?:\+-|-\+|[±∓⩲⩱∞])?")
_MOVENUM = r"\d+\s*(?:\.\.\.|…|\.)\s*"
_SEQ_RE = re.compile(rf"(?<![\w.]){_MOVENUM}{_SAN}(?:\s+(?:{_MOVENUM})?{_SAN})*(?![\w])")
_SEQ_START_RE = re.compile(r"(\d+)\s*(\.\.\.|…|\.)")


def _play_sequence(start: chess.pgn.GameNode, seq_text: str) -> list | None:
    """Parse a commentary move sequence from `start`'s position; None unless every move is legal."""
    board = start.board()
    mini = chess.pgn.read_game(io.StringIO(
        f'[SetUp "1"]\n[FEN "{board.fen()}"]\n\n{_normalize_moves(seq_text)} *\n'))
    if mini is None or mini.errors:
        return None
    moves = [(n.move, set(n.nags)) for n in mini.mainline()]
    return moves or None


def _branch_candidates(node: chess.pgn.GameNode) -> list:
    """Positions a commentary line on `node` may branch from: the path back to the start, then a few moves ahead."""
    back, n = [], node
    while n is not None:
        back.append(n)
        n = n.parent
    ahead, n = [], node
    for _ in range(4):
        if not n.variations:
            break
        n = n.variations[0]
        ahead.append(n)
    return back + ahead


def promote_comment_variations(pgn: str) -> str:
    """Turn move sequences inside {comments} into real PGN variations."""
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None or game.errors:
        return pgn
    commented = [n for n in _walk(game) if n.comment and _SEQ_RE.search(n.comment)]
    changed = False
    for node in commented:
        text, kept, cursor = node.comment, [], 0
        last_var_node = None
        for m in _SEQ_RE.finditer(text):
            start = _SEQ_START_RE.match(m.group(0))
            number, black = int(start.group(1)), start.group(2) != "."
            grafted = None
            for cand in _branch_candidates(node):
                b = cand.board()
                if (cand.variations  # at the end of a line it would become the main line
                        and b.fullmove_number == number and b.turn == (chess.BLACK if black else chess.WHITE)):
                    moves = _play_sequence(cand, m.group(0))
                    if moves:
                        grafted = (cand, moves)
                        break
            if grafted is None:
                continue
            between = text[cursor:m.start()]
            if last_var_node is None:
                kept.append(between)
            else:
                last_var_node.comment = between.strip(" ，,;；。")
            cand, moves = grafted
            cur = cand
            for move, nags in moves:
                cur = cur.variation(move) if cur.has_variation(move) else cur.add_variation(move)
                cur.nags |= nags
            last_var_node, cursor, changed = cur, m.end(), True
        if last_var_node is None:
            continue
        tail = text[cursor:].strip(" ，,;；")
        if tail.strip("。. "):
            last_var_node.comment = tail
        node.comment = "".join(kept).strip()
    if not changed:
        return pgn
    exporter = chess.pgn.StringExporter(headers=True, variations=True, comments=True, columns=None)
    return game.accept(exporter) + "\n"


def _walk(node: chess.pgn.GameNode):
    yield node
    for child in node.variations:
        yield from _walk(child)


def _unmatched_parens(movetext: str) -> list[int]:
    """Indexes of '(' / ')' (outside comments) that have no partner."""
    stack, stray, in_comment = [], [], False
    for i, ch in enumerate(movetext):
        if ch == "{":
            in_comment = True
        elif ch == "}":
            in_comment = False
        elif in_comment:
            continue
        elif ch == "(":
            stack.append(i)
        elif ch == ")":
            if stack:
                stack.pop()
            else:
                stray.append(i)
    return sorted(stray + stack)


def _auto_fix(game: dict) -> dict | None:
    """Cheap deterministic repairs tried before asking the model again.

    Small models sometimes copy a stray "(" from the page (e.g. from "(图1)"),
    which turns the rest of the game into a never-closed variation.
    """
    mt = normalize_movetext(game["movetext"])
    stray = _unmatched_parens(mt)
    if not stray:
        return None
    candidates = [[i] for i in stray] + [stray]
    for drop in candidates:
        fixed = "".join(ch for i, ch in enumerate(mt) if i not in drop)
        g = {**game, "movetext": fixed}
        if not validate_pgn(build_pgn(g)):
            return g
    return None


def _evaluate(data: dict) -> tuple[list, list]:
    games, problems = [], []
    for i, g in enumerate(data["games"]):
        pgn = build_pgn(g)
        errs = validate_pgn(pgn)
        if errs and (fixed := _auto_fix(g)):
            g, pgn, errs = fixed, build_pgn(fixed), []
        elif errs and _unmatched_parens(normalize_movetext(g["movetext"])):
            errs.append("Unbalanced parentheses: every '(' must close with ')'. Use parentheses only "
                        "for variations, never for diagram references.")
        if not errs:
            pgn = promote_comment_variations(pgn)
        games.append({**g, "pgn": pgn, "errors": errs})
        problems += [f"Game {i + 1}: {p}" for p in errs]
    if not games:
        problems.append("No game was found on the page.")
    return games, problems


_RESULT_TAIL_RE = re.compile(r"\s*(?:1-0|0-1|1/2-1/2|\*)\s*$")


def _page_sort_key(page: dict):
    m = re.search(r"\d+", page["data"].get("page_number") or "")
    return int(m.group(0)) if m else None


# A move number followed by a move, e.g. "24...Na5", "25.Rxe5", "31... Qc8".
_MOVE_NUM_RE = re.compile(r"(?<![\w.])(\d+)(\s*(?:\.\.\.|…|\.)\s*)(?=[KQRBNa-hO0♔♕♖♗♘♚♛♜♝♞])")
_COMMENT_RE = re.compile(r"\{[^}]*\}")


def _first_move_number(movetext: str, include_comments: bool = False):
    text = movetext if include_comments else _COMMENT_RE.sub(" ", movetext)
    m = _MOVE_NUM_RE.search(text)
    if not m:
        return None
    black_to_move = "..." in m.group(2) or "…" in m.group(2)
    return int(m.group(1)), black_to_move


def _is_continuation(game: dict) -> bool:
    """Does this fragment continue a game from an earlier page?

    Decided from the moves, not the model's `continues_previous` flag (small
    models get the flag wrong): anything that doesn't start at White's move 1
    from the initial position is a continuation.
    """
    if (game.get("fen") or "").strip():
        return False
    first = _first_move_number(game["movetext"])
    return first is None or first != (1, False)


def _move_order_key(page: dict):
    games = page["data"]["games"]
    if not games:
        return (2, 0)
    first = games[0]
    if not _is_continuation(first):
        return (0, 0)
    num = _first_move_number(first["movetext"], include_comments=True)
    return (1, num[0] if num else 10**6)


def _order_score(pages: list[dict]) -> tuple:
    """Lower is better: fewer problems, then more legal main-line moves."""
    games, problems = _evaluate(_stitch(pages))
    plies = 0
    for g in games:
        game = chess.pgn.read_game(io.StringIO(g["pgn"]))
        if game is not None:
            plies += sum(1 for _ in game.mainline_moves())
    return (len(problems), -plies)


def _greedy_order(pages: list[dict], preferred: list[dict]) -> list[dict]:
    """Repeatedly append the page that keeps the stitched games most legal.

    A page that legally continues the current game beats one that starts a new
    game; remaining ties go to the page that comes first in `preferred`.
    """
    rank = {id(p): i for i, p in enumerate(preferred)}

    def starts_new_game(p: dict) -> bool:
        games = p["data"]["games"]
        return not games or not _is_continuation(games[0])

    def key(p: dict):
        problems, neg_plies = _order_score(order + [p])
        return (problems, starts_new_game(p), neg_plies, rank[id(p)])

    order, remaining = [], list(pages)
    while remaining:
        nxt = min(remaining, key=key)
        order.append(nxt)
        remaining.remove(nxt)
    return order


def _order_pages(pages: list[dict]) -> tuple[list[dict], str]:
    """Pick the page order whose stitched games are most legal.

    Candidates: printed page numbers (the model can misread or invent them),
    move numbers (a game's later pages start at later moves), upload order.
    """
    keys = [_page_sort_key(p) for p in pages]
    # Printed page numbers (even incomplete or duplicated ones), then upload order.
    by_page = sorted(pages, key=lambda p: (_page_sort_key(p) is None, _page_sort_key(p) or 0, p["index"]))
    candidates = [("move legality", _greedy_order(pages, by_page))]  # first: wins ties
    if all(k is not None for k in keys) and len(set(keys)) == len(keys):
        candidates.append(("page numbers", by_page))
    candidates.append(("move numbers", sorted(pages, key=_move_order_key)))
    candidates.append(("upload order", list(pages)))
    best_name, best_order, best_score = None, None, None
    for name, order in candidates:
        score = _order_score(order)
        if best_score is None or score < best_score:
            best_name, best_order, best_score = name, order, score
    return best_order, best_name


def _merge_headers(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for k, v in extra.items():
        if k == "Result":
            if v in ("1-0", "0-1", "1/2-1/2"):
                merged[k] = v
        elif merged.get(k) in (None, "", "?", "??") and v not in ("", "?", "??"):
            merged[k] = v
    return merged


def _expected_next(game: dict):
    """(move number, black_to_move) of the next move after `game`'s main line, or None."""
    pgn = build_pgn({**game, "movetext": _RESULT_TAIL_RE.sub("", game["movetext"]) + " *"})
    parsed = chess.pgn.read_game(io.StringIO(pgn))
    if parsed is None or parsed.errors:
        return None
    board = parsed.end().board()
    return board.fullmove_number, board.turn == chess.BLACK


def _fix_numbering(prev: dict, movetext: str) -> str:
    """Shift a continuation page's move numbers (comments included) to follow on from `prev`.

    Small models sometimes misread the first move number (e.g. 24 as 14) and then
    renumber the whole page, including lines in the commentary, to match.
    """
    expected, first = _expected_next(prev), _first_move_number(movetext)
    if not expected or not first or first[1] != expected[1] or first[0] == expected[0]:
        return movetext
    delta = expected[0] - first[0]
    return _MOVE_NUM_RE.sub(lambda m: f"{max(1, int(m.group(1)) + delta)}{m.group(2)}", movetext)


def _stitch(pages: list[dict]) -> dict:
    """Join per-page fragments into whole games."""
    games, notes = [], []
    for p in pages:
        data = p["data"]
        if data.get("notes"):
            notes.append(f"p.{data.get('page_number') or p['index'] + 1}: {data['notes']}")
        for j, g in enumerate(data["games"]):
            if j == 0 and _is_continuation(g) and games:
                prev = games[-1]
                mt = _fix_numbering(prev, g["movetext"].strip())
                prev["movetext"] = _RESULT_TAIL_RE.sub("", prev["movetext"]) + " " + mt
                prev["headers"] = _merge_headers(prev["headers"], g["headers"])
            else:
                games.append({"headers": dict(g["headers"]), "fen": g.get("fen", ""),
                              "movetext": g["movetext"].strip()})
    return {"games": games, "notes": "\n".join(notes)}


def _position_before(pages: list[dict]) -> str:
    """Describe where the last stitched game stands, for a repair prompt about the next page."""
    stitched = _stitch(pages)
    if not stitched["games"]:
        return ""
    g = stitched["games"][-1]
    pgn = build_pgn({**g, "movetext": _RESULT_TAIL_RE.sub("", g["movetext"]) + " *"})
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None or game.errors:
        return ""
    last = game.end()
    if last.move is None:
        return ""
    board = last.board()
    if board.turn == chess.BLACK:
        label, side = f"{board.fullmove_number}. {last.san()}", "Black"
    else:
        label, side = f"{board.fullmove_number - 1}... {last.san()}", "White"
    return (f"The previous page ended with {label}. If this page continues that game, the next move is "
            f"{side}'s move {board.fullmove_number} from this position: {board.fen()}\n")


def _run_backend(cls, system: str, images: list[tuple[bytes, str]], progress=None) -> dict:
    report = progress or (lambda frac, msg: None)
    n = len(images)
    # Pass 1: transcribe every page in its own conversation.
    pages = []
    for idx, (img, mime) in enumerate(images):
        report(idx / (n + 1), f"Reading page {idx + 1} of {n} ({cls.name})")
        backend = cls()
        pages.append({"index": idx, "backend": backend, "rounds": 0,
                      "data": backend.start(system, img, mime, FIRST_PROMPT)})
    pages, order_method = _order_pages(pages)
    calls = len(pages)

    # Pass 2: validate page by page; send problems back to the page that introduced them.
    prev_problems: set[str] = set()
    for k in range(len(pages)):
        while True:
            _, problems = _evaluate(_stitch(pages[:k + 1]))
            new = [p for p in problems if p not in prev_problems]
            if not new or pages[k]["rounds"] >= MAX_REPAIR_ROUNDS:
                prev_problems = set(problems)
                break
            pages[k]["rounds"] += 1
            calls += 1
            label = pages[k]["data"].get("page_number") or f"#{pages[k]['index'] + 1}"
            report(n / (n + 1), f"Re-reading page {label} to fix an illegal move (try {pages[k]['rounds']})")
            pages[k]["data"] = pages[k]["backend"].followup(
                "A chess engine validated your transcription and found these problems:\n- "
                + "\n- ".join(new) + "\n\n" + _position_before(pages[:k])
                + "Look at the image again carefully around those moves (and the moves just "
                "before them - an earlier misread often causes a later illegal move). "
                "Return the complete corrected JSON for this page."
            )

    stitched = _stitch(pages)
    games, problems = _evaluate(stitched)
    page_order = [p["data"].get("page_number") or f"#{p['index'] + 1}" for p in pages]
    return {"games": games, "notes": stitched["notes"], "attempts": calls, "pages": page_order,
            "order_method": order_method, "backend": cls.name, "problem_count": len(problems)}


def _model_label(name: str) -> str:
    if name == "ollama":
        return os.environ.get("OLLAMA_MODEL", "qwen3-vl:8b-instruct")
    if name == "gemini":
        return os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    return ""


def extract(images: list[tuple[bytes, str]], translate: bool = False, backends: str | None = None,
            progress=None, check_diagrams: bool = True) -> dict:
    """Extract games from one or more page images given as (bytes, mime) pairs.

    Tries each backend in order; returns the first fully legal result, else the best one.
    `progress(fraction, message)` is called as work advances (fraction 0..1).
    """
    report = progress or (lambda frac, msg: None)
    result = _extract_games(images, translate, backends,
                            lambda frac, msg: report(frac * (0.9 if check_diagrams else 1.0), msg))
    if check_diagrams:
        report(0.9, "Checking moves against the book's diagrams")
        try:
            _attach_diagram_checks(images, result)
        except Exception as e:  # noqa: BLE001 - the check is an extra; never lose the transcription over it
            result["log"].append(f"diagram check skipped: {e}")
    report(1.0, "Done")
    return result


def _extract_games(images, translate, backends, report) -> dict:
    order = [b.strip() for b in (backends or os.environ.get("EXTRACT_BACKENDS", DEFAULT_BACKENDS)).split(",") if b.strip()]
    system = system_prompt(translate)
    log: list[str] = []
    best = None
    for name in order:
        cls = BACKENDS.get(name)
        if cls is None:
            log.append(f"{name}: unknown backend")
            continue
        try:
            result = _run_backend(cls, system, images, report)
        except BackendUnavailable as e:
            log.append(f"{name}: skipped - {e}")
            continue
        except Exception as e:  # noqa: BLE001 - network/quota/parse errors: try the next backend
            log.append(f"{name}: failed - {e}")
            continue
        label = f"{name} ({_model_label(name)})"
        pages = (f"{len(images)} pages ordered by {result['order_method']}: "
                 f"{', '.join(result['pages'])}") if len(images) > 1 else "1 page"
        if result["problem_count"] == 0:
            log.append(f"{label}: OK - {pages}, {result['attempts']} model call(s)")
            result["log"] = log
            return result
        log.append(f"{label}: {result['problem_count']} problem(s) remain - {pages}, {result['attempts']} model calls")
        if best is None or result["problem_count"] < best["problem_count"]:
            best = result
    if best is None:
        raise RuntimeError("No backend could process the images:\n" + "\n".join(log))
    best["log"] = log
    return best


def _attach_diagram_checks(images: list[tuple[bytes, str]], result: dict) -> None:
    """Find the book's diagrams, anchor each to a move, and suggest fixes for legal-but-wrong moves.

    Adds to each game: "diagrams" [{image, ply, diff, squares}] and "suggestions".
    """
    import cv2
    import numpy as np

    import diagrams as dg

    found = []  # (crop data URL, occupancy)
    for img_bytes, _ in images:
        img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        for quad in dg.find_boards(img):
            grid = dg.read_squares(img, quad)
            crop = cv2.resize(dg.warp_board(img, quad), (320, 320), interpolation=cv2.INTER_AREA)
            ok, jpg = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
            url = "data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode("ascii") if ok else ""
            found.append((url, dg.grid_occupancy(grid)))

    games = []
    for g in result["games"]:
        g["diagrams"], g["suggestions"] = [], []
        parsed = chess.pgn.read_game(io.StringIO(g["pgn"]))
        if parsed is None or g["errors"]:
            games.append(None)  # only legal games can be checked
            continue
        sans = [n.san() for n in parsed.mainline()]
        boards, _ = dg._replay(parsed.board(), sans)
        games.append((parsed.board(), sans, boards))

    # each diagram belongs to the game (and ply) it matches best
    per_game: dict[int, list] = {}
    for url, occ in found:
        best = None
        for gi, info in enumerate(games):
            if info is None:
                continue
            ply, diff = dg.anchor(info[2], occ)
            if best is None or diff < best[2]:
                best = (gi, ply, diff)
        if best and best[2] <= dg.MAX_ANCHOR_DIFF:
            gi, ply, diff = best
            board = games[gi][2][ply]
            squares = sorted(chess.square_name(s) for s in dg._board_occupancy(board) ^ occ)
            result["games"][gi]["diagrams"].append({"image": url, "ply": ply, "diff": diff, "squares": squares})
            per_game.setdefault(gi, []).append(occ)

    checked = 0
    for gi, readings in per_game.items():
        start, sans, _ = games[gi]
        result["games"][gi]["diagrams"].sort(key=lambda d: d["ply"])
        result["games"][gi]["suggestions"] = dg.suggest_corrections(start, sans, readings)
        checked += len(readings)
    n_sug = sum(len(g.get("suggestions", [])) for g in result["games"])
    result["log"].append(f"diagram check: {len(found)} diagram(s) found, {checked} matched to moves, "
                         f"{n_sug} suspicious move(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("images", nargs="+", help="page photos, in any order (sorted by printed page number)")
    parser.add_argument("--translate", action="store_true", help="translate commentary into English")
    parser.add_argument("--backends", help=f"backend order (default: {DEFAULT_BACKENDS})")
    args = parser.parse_args()

    images = []
    for path in args.images:
        with open(path, "rb") as f:
            images.append((f.read(), mimetypes.guess_type(path)[0] or "image/jpeg"))
    result = extract(images, args.translate, args.backends)

    for g in result["games"]:
        sys.stdout.write(g["pgn"] + "\n")
        for e in g["errors"]:
            print(f"WARNING: {e}", file=sys.stderr)
    for g in result["games"]:
        for sug in g.get("suggestions", []):
            num = f"{sug['move_number']}{'.' if sug['white'] else '...'}"
            print(f"CHECK: diagram suggests {num} {sug['suggested']} instead of {num} {sug['played']}", file=sys.stderr)
    for line in result["log"]:
        print(f"[{line}]", file=sys.stderr)
    if result["notes"]:
        print(f"NOTES: {result['notes']}", file=sys.stderr)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
