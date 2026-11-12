"""Find chess diagrams in page photos and read each square as empty / white / black.

Used to cross-check a transcription: the diagram's occupancy map is compared
with every position in the game. A perfect match confirms the moves up to that
point; a near match points to a misread move (or a misread diagram square).

This deliberately does not identify piece types. Occupancy and colour are much
easier to get right from a phone photo, and are enough to catch a move that
went to the wrong square.

Pipeline per page:
  1. find_boards     - large square-ish outlines -> perspective-corrected crops
  2. fit_grid        - locate the real 8x8 grid by correlating with a checkerboard
                       (the detected outline is often off by part of a square)
  3. read_squares    - per-square features -> '.', 'W', 'B'
"""

from __future__ import annotations

import cv2
import numpy as np

WARP = 560          # size of the straightened crop (board + margin)
PAD = 0.06          # extra margin around the detected outline, as a fraction of its size


# ------------------------------------------------------------------ 1. find boards

def _order_corners(pts: np.ndarray) -> np.ndarray:
    pts = pts.reshape(4, 2).astype("float32")
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], dtype="float32")


def find_boards(img: np.ndarray) -> list[np.ndarray]:
    """Return corner quads (tl, tr, br, bl) of diagram-sized squares, top to bottom."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    found = []
    for block, c in ((31, 10), (51, 8), (21, 12)):  # a few binarisations: lighting varies
        th = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, block, c)
        th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(th, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if not (0.12 * min(w, h)) ** 2 < area < (0.7 * min(w, h)) ** 2:
                continue
            approx = cv2.approxPolyDP(cnt, 0.03 * cv2.arcLength(cnt, True), True)
            if len(approx) != 4 or not cv2.isContourConvex(approx):
                continue
            _, _, bw, bh = cv2.boundingRect(approx)
            if not 0.75 < bw / bh < 1.33:
                continue
            found.append((area, _order_corners(approx)))
    found.sort(key=lambda b: -b[0])
    kept: list[np.ndarray] = []
    for _, quad in found:  # one quad per diagram: drop overlapping (inner frame, duplicates)
        side = np.sqrt(cv2.contourArea(quad))
        if all(np.linalg.norm(quad.mean(0) - k.mean(0)) > 0.4 * side for k in kept):
            kept.append(quad)
    return sorted(kept, key=lambda q: q.mean(0)[1])


def warp_board(img: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """Perspective-correct a board with a margin around it (the outline may be off)."""
    c = quad.mean(0)
    src = c + (quad - c) * (1 + 2 * PAD)
    dst = np.array([[0, 0], [WARP - 1, 0], [WARP - 1, WARP - 1], [0, WARP - 1]], dtype="float32")
    return cv2.warpPerspective(img, cv2.getPerspectiveTransform(src.astype("float32"), dst), (WARP, WARP),
                               borderMode=cv2.BORDER_REPLICATE)


# ------------------------------------------------------------------ 2. fit grid

def normalize(img: np.ndarray) -> np.ndarray:
    """Grayscale with uneven lighting removed: paper / light squares ~ 1.0."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    k = int(WARP / 8 * 1.6) | 1
    bg = cv2.morphologyEx(g, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    bg = cv2.GaussianBlur(bg, (k, k), 0)
    return np.clip(g / np.maximum(bg, 1), 0, 1.3)


def _square_waves(length: int, offsets: np.ndarray, sizes: np.ndarray) -> tuple[np.ndarray, list]:
    """Matrix of +-1 square waves (one column per (offset, size)), zero outside the 8 cells."""
    x = np.arange(length, dtype=np.float32)[:, None]
    params = [(o, s) for s in sizes for o in offsets]
    o = np.array([p[0] for p in params], dtype=np.float32)[None, :]
    s = np.array([p[1] for p in params], dtype=np.float32)[None, :]
    idx = np.floor((x - o) / s)
    wave = np.where(idx % 2 == 0, 1.0, -1.0).astype(np.float32)
    wave[(idx < 0) | (idx > 7)] = 0.0
    return wave, params


def fit_grid(norm: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return x and y boundaries (9 each) of the 8x8 squares in a normalized crop.

    The board's light/dark pattern is matched against shifted, scaled
    checkerboards: score(x-params, y-params) = WAVEy^T . S . WAVEx.
    Then each rank / file is nudged separately to follow slight page curvature.
    """
    n = norm.shape[0]
    # light (+1) vs dark (-1), with pieces mostly suppressed by a median filter
    med = cv2.medianBlur((np.clip(norm, 0, 1) * 255).astype(np.uint8), 15).astype(np.float32) / 255
    s = np.where(med > np.median(med), 1.0, -1.0).astype(np.float32)

    board = n / (1 + 2 * PAD)
    sizes = np.linspace(0.90, 1.10, 21) * board / 8
    offsets = np.arange(0, n * 0.16, 2, dtype=np.float32)
    wx, px = _square_waves(n, offsets, sizes)
    wy, py = _square_waves(n, offsets, sizes)
    score = wy.T @ (s @ wx)                       # (len(py), len(px))
    # a1 is dark: the top-left square (a8) is light, i.e. +1 * +1
    iy, ix = np.unravel_index(np.argmax(score), score.shape)
    (oy, sy), (ox, sx) = py[iy], px[ix]

    xs = ox + sx * np.arange(9)
    ys = oy + sy * np.arange(9)
    return _refine(s, xs, ys), _refine(s.T, ys, xs)


def _refine(s: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Per rank, shift the x-boundaries a little to best match that rank's stripe; average the result.

    Returns refined x boundaries (curvature is small, so one shift per board edge is enough:
    we fit a straight line through the per-rank best shifts and apply the mean).
    """
    size = (xs[-1] - xs[0]) / 8
    shifts = np.arange(-size * 0.25, size * 0.25 + 1, 1)
    best = []
    for r in range(8):
        y0, y1 = int(ys[r] + size * 0.2), int(ys[r + 1] - size * 0.2)
        if y1 <= y0:
            continue
        row = s[max(y0, 0):max(y1, 1)].mean(0)
        sign = 1 if r % 2 == 0 else -1
        scores = []
        for d in shifts:
            b = xs + d
            val = 0.0
            for f in range(8):
                a0, a1 = int(b[f] + size * 0.2), int(b[f + 1] - size * 0.2)
                if 0 <= a0 < a1 <= len(row):
                    val += sign * (1 if f % 2 == 0 else -1) * row[a0:a1].mean()
            scores.append(val)
        best.append(shifts[int(np.argmax(scores))])
    return xs + (float(np.median(best)) if best else 0.0)


# ------------------------------------------------------------------ 3. read squares

def read_squares(img: np.ndarray, quad: np.ndarray | None = None) -> list[str]:
    """Return 8 strings (rank 8 first) of '.', 'W', 'B' for one diagram."""
    crop = warp_board(img, quad) if quad is not None else img
    norm = normalize(crop)
    xs, ys = fit_grid(norm)
    feats = {}
    for r in range(8):
        for f in range(8):
            x0, x1, y0, y1 = xs[f], xs[f + 1], ys[r], ys[r + 1]
            mx, my = (x1 - x0) * 0.2, (y1 - y0) * 0.2
            c = norm[int(y0 + my):int(y1 - my), int(x0 + mx):int(x1 - mx)]
            if c.size == 0:
                c = np.ones((4, 4), np.float32)
            edges = cv2.Canny((np.clip(c, 0, 1) * 255).astype(np.uint8), 50, 130).mean() / 255
            feats[(r, f)] = (c, float(edges))

    light = [(r, f) for r in range(8) for f in range(8) if (r + f) % 2 == 0]
    dark = [(r, f) for r in range(8) for f in range(8) if (r + f) % 2 == 1]
    level = {}
    for name, cells in (("L", light), ("D", dark)):
        meds = sorted(float(np.median(feats[k][0])) for k in cells)
        level[name] = meds[len(meds) * 3 // 4] if name == "L" else meds[len(meds) // 2]
    edge_floor = np.percentile([feats[k][1] for k in feats], 30)

    grid = []
    for r in range(8):
        row = ""
        for f in range(8):
            c, edges = feats[(r, f)]
            sq = level["L" if (r + f) % 2 == 0 else "D"]
            occupied = edges > max(0.04, edge_floor * 2.5)
            if not occupied:
                row += "."
                continue
            solid = float((c < min(0.3, sq * 0.55)).mean())      # black fill
            bright = float((c > sq + 0.15).mean()) if sq < 0.8 else 0.0  # white fill on dark squares
            row += "B" if solid > 0.18 and bright < 0.15 else "W"
        grid.append(row)
    return grid


def read_page(img: np.ndarray) -> list[list[str]]:
    """Occupancy grids of all diagrams on a page photo, top to bottom."""
    return [read_squares(img, q) for q in find_boards(img)]


# ------------------------------------------------------------------ 4. check a game

MAX_ANCHOR_DIFF = 12   # a diagram further than this (in occupied squares) from every position is not from this game


def grid_occupancy(grid: list[str]) -> frozenset:
    import chess
    return frozenset(chess.square(f, 7 - r) for r in range(8) for f in range(8) if grid[r][f] != ".")


def _board_occupancy(board) -> frozenset:
    return frozenset(board.piece_map())


def _replay(start, sans: list[str]):
    """Boards after each ply (index 0 = start) and whether every move was legal."""
    b = start.copy()
    out = [b.copy()]
    for san in sans:
        try:
            b.push_san(san)
        except ValueError:
            return out, False
        out.append(b.copy())
    return out, True


def anchor(boards: list, occ: frozenset) -> tuple[int, int]:
    """(ply, number of squares that differ) of the position closest to a diagram reading."""
    dists = [len(_board_occupancy(b) ^ occ) for b in boards]
    ply = int(np.argmin(dists))
    return ply, dists[ply]


def suggest_corrections(start, sans: list[str], readings: list[frozenset]) -> list[dict]:
    """Find legal-but-wrong moves that the diagrams contradict.

    For each move before a diagram, try every legal alternative, replay the rest
    of the game (which must stay legal) and measure how well the positions now
    match ALL the diagrams from that move on. Reading noise is random; a real
    misread leaves a consistent footprint that one alternative explains.
    """
    boards, _ = _replay(start, sans)
    anchored = sorted((anchor(boards, occ)[0], occ) for occ in readings)
    anchored = [(p, o) for p, o in anchored if p > 0]
    if not anchored:
        return []

    window = 2  # a misread move can shift which ply a diagram matches best

    def total_diff(bs, from_ply):
        total = 0
        for p, o in anchored:
            if p + window <= from_ply:
                continue
            plies = range(max(p - window, 0), min(p + window, len(bs) - 1) + 1)
            total += min(len(_board_occupancy(bs[t]) ^ o) for t in plies)
        return total

    out = []
    last_ply = min(anchored[-1][0] + window, len(sans))
    for q in range(last_ply):  # the move that leads from ply q to ply q+1
        base = total_diff(boards, q)
        best = None
        for alt in boards[q].legal_moves:
            alt_san = boards[q].san(alt)
            if alt_san == sans[q]:
                continue
            trial = sans[:q] + [alt_san] + sans[q + 1:]
            bs, legal = _replay(start, trial)
            if not legal:
                continue
            gain = base - total_diff(bs, q)
            if gain >= 2 and (best is None or gain > best[0]):
                best = (gain, alt_san)
        if best:
            number = boards[q].fullmove_number
            white = boards[q].turn
            out.append({"ply": q + 1, "move_number": number, "white": bool(white),
                        "played": sans[q], "suggested": best[1], "gain": best[0]})
    # keep the strongest suggestion per diagram stretch (neighbouring plies explain the same squares)
    out.sort(key=lambda s: -s["gain"])
    kept = []
    for s in out:
        if all(abs(s["ply"] - k["ply"]) > 1 for k in kept):
            kept.append(s)
    return sorted(kept, key=lambda s: s["ply"])
