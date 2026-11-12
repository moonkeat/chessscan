# ChessScan

Take photos of chess book pages, get a PGN and a playable board. Free by default. One page or many: a game that runs over several pages is stitched back together.

1. **Read**: a vision model transcribes each page into PGN: main line, comments, annotation symbols, and headers. It also reports the printed page number and whether the page continues a game from the previous page.
2. **Order and join**: the app tries ordering pages by printed page number, by move number, by upload order, and by which page joins on legally. It keeps the order with the most legal moves, so photos can be uploaded in any order. If a continuation page's move numbers were misread, the app shifts them to follow on from the previous page.
3. **Check**: python-chess replays every move, page by page. If a page introduces an illegal move (a misread `B`/`8`, `N`/`K`, …), only that page goes back to the model, along with the position where it starts (up to 2 retries). If problems remain, the next backend gets a turn.
4. **Tidy up**: move sequences written inside the commentary (e.g. `31...Qc8 32.Bh3 Nc4 …`) become real, clickable variations when every move is legal from the right position.
5. **Cross-check with the book's diagrams**: OpenCV finds each printed diagram, straightens it, and reads every square as empty, white or black. It doesn't identify piece types, which is much harder from a phone photo. Each diagram is matched to the move it shows. To catch a move that is *legal but wrong* (e.g. `15...h5` instead of `15...h6`), the app tries every legal alternative for earlier moves, replays the rest of the game, and suggests a correction only when one alternative fits all later diagrams clearly better. Random reading noise rarely fits that pattern, so false alarms are rare.
6. **Show**: the browser parses the PGN with chess.js into a move tree with variations and draws it with [Chessground](https://github.com/lichess-org/chessground). You get a clickable board, the move list, and an editable PGN box for manual fixes. At a move that has a book diagram, the photo of that diagram appears next to the board, with any differing squares circled. Moves the diagrams contradict are highlighted in red. A progress bar shows each step while the pages are processed, and the photos are cleared once the result is in.

## Backends

| Backend | Cost | Setup |
|---|---|---|
| `ollama` (default) | Free, runs locally | Install [Ollama](https://ollama.com), then `ollama pull qwen3-vl:8b-instruct` (~6 GB) |
| `gemini` (fallback) | Free tier, daily limits | Get a key at https://aistudio.google.com/apikey, set `GEMINI_API_KEY` |

Auto mode tries `ollama`, then `gemini`. It returns the first result where every move is legal, or otherwise the result with the fewest problems. A backend that isn't set up is skipped.

Settings (environment variables):

| Variable | Default |
|---|---|
| `EXTRACT_BACKENDS` | `ollama,gemini` |
| `OLLAMA_MODEL` | `qwen3-vl:8b-instruct` (use the instruct variant: plain `qwen3-vl:8b` is a thinking model, about 10x slower) |
| `OLLAMA_HOST` | `http://localhost:11434` |
| `OLLAMA_NUM_CTX` | `6144` (keeps most of the model on an 8 GB GPU: about 3x faster than 8192) |
| `GEMINI_MODEL` | `gemini-flash-latest` |

Note: on Gemini's free tier, Google may use the images you send to improve its products.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
ollama pull qwen3-vl:8b-instruct
$env:GEMINI_API_KEY="..."        # optional fallback
```

## Run

```powershell
.venv\Scripts\python server.py        # then open http://localhost:8000
```

Drop, choose, or paste (Ctrl+V) one or more page photos, pick a model (default: Auto), then click **Extract PGN**. If the pages contain more than one game, pick the game from the dropdown above the board. Tick *Translate commentary to English* if you want the comments translated.
Use ← / → / Home / End to step through the moves. Open `http://localhost:8000/#manual` to use the viewer on PGN you type or paste yourself.

Command-line only:

```powershell
.venv\Scripts\python extractor.py page1.jpg page2.jpg page3.jpg --translate --backends ollama,gemini > games.pgn
```

The web page shrinks photos to 2576 px on the long side before upload. The command line sends the file as it is, so shrink large phone photos first to keep the local model fast.

## Tests

```powershell
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest tests -q
```

About 60 tests, around 30 seconds, no model needed: recorded real model output is replayed from `tests/fixtures/`.

- `test_pgn_cleanup.py`: notation clean-up, PGN assembly, repairs, commentary lines → variations
- `test_pages.py`: page ordering (every upload order), stitching, move-number repair, misread-move repair, skipping incomplete games
- `test_diagrams.py`: diagram anchoring; legal-but-wrong moves caught with no false alarms
- `test_server.py`: job API, progress, error handling
- `test_ui.py`: the real page in headless Edge: board, progress, input locking, leave warning, resume, corrections, diagrams (skipped without Playwright)

## Files

- `extractor.py`: backends (Ollama / Gemini), PGN assembly, python-chess check, retry loop and fallback
- `diagrams.py`: diagram detection, grid fitting, square reading, and correction suggestions (OpenCV)
- `server.py`: stdlib HTTP server; serves `static/`, runs extraction as background jobs (`POST /api/extract`, `GET /api/jobs/<id>`) so the page can show progress
- `static/pgn.js`: PGN parser (variations, comments, NAGs) that checks moves with chess.js
- `static/app.js`: upload, board rendering, navigation, PGN editing and export

## Tips

- On an RTX 3070 (8 GB) a page takes about 25–30 s with `qwen3-vl:8b-instruct` (four pages plus checks: about 2 minutes).
- Only complete games are loaded: a game needs its first page (starting at move 1) and the page with its result. A game that runs onto a page you didn't photograph (like a new game starting at the bottom of the last page) is skipped, and the status line says why.
- A commentary line that comes after the game's last move (e.g. why the opponent resigned) stays as comment text. PGN can't attach a variation after the final move.
- Shoot the page flat and evenly lit. A local 8B model is more sensitive to blur and angle than cloud models.
- Any problem still left is listed above the move list. Fix it in the PGN box and click **Apply edits**.
