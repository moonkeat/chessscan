# ChessScan

Chess book photos → validated PGN → playable board. See README.md for the pipeline.

## After every code change

Run the test suite and make sure it passes before reporting the change as done:

```
.venv\Scripts\python -m pytest tests -q
```

- The tests never call a model: `tests/fixtures/model_pages.json` replays real recorded qwen3-vl
  output, and `diagram_readings.json` holds real diagram readings. They run in ~30 s.
- `tests/test_ui.py` drives the real page in headless Edge (Playwright) with the job API mocked;
  it is skipped if Playwright is missing.
- When fixing a bug, add a test that reproduces it first (see the `17.Rf1` case in `tests/test_pages.py`).
- Fixtures must not contain the book's commentary text (copyright): keep the moves, replace prose
  with placeholders like `{note 7...Qd7 8.Ngf3 end}`.

## What matters

- Correct moves matter most. Player names / header metadata errors are acceptable.
- Never let a guess pass silently: automatic move corrections go into `game["corrections"]` and are
  shown in the UI for the user to check.
- The local model (`qwen3-vl:8b-instruct` via Ollama, 4608 context, photos capped at 1200x1600) is the default; Gemini free tier
  is the fallback. No paid APIs.
