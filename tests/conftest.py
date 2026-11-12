import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

import extractor  # noqa: E402

# Game 1 of the sample pages (Carlsen - Wojtaszek, 2015), main line as printed in the book.
GAME1 = ("1. d4 Nf6 2. Bf4 d5 3. e3 c5 4. c3 Nc6 5. Nd2 cxd4 6. exd4 Bf5 7. Qb3 Qc8 8. Ngf3 e6 9. Nh4 Be4 "
         "10. Nxe4 Nxe4 11. Nf3 Bd6 12. Bxd6 Nxd6 13. Bd3 Qc7 14. Qc2 O-O-O 15. O-O h6 16. a4 Kb8 17. Rfe1 Rc8 "
         "18. Qd1 Rhe8 19. Bf1 Re7 20. Rc1 Rd8 21. Re2 Ne4 22. b4 Ng5 23. Ne1 e5 24. b5 Na5 25. Rxe5 Rxe5 "
         "26. dxe5 Qxe5 27. c4 Ne6 28. Nf3 Qf4 29. cxd5 Rxd5 30. Qxd5 Qxc1 31. g3 Qc5 32. Qd7 Qf8 33. Ne5 Nc5 "
         "34. Qd5 Kc7 35. Nxf7 Nxa4 36. Qe5+ Kb6 37. Nd6 Nc5 38. Ne8 1-0")


@pytest.fixture(scope="session")
def model_pages():
    """Real qwen3-vl output for pages 003-006 (book commentary replaced by placeholders)."""
    return json.loads((FIXTURES / "model_pages.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def diagram_grids():
    """Occupancy readings ('.', 'W', 'B' per square) of game 1's diagrams from the sample photos."""
    return json.loads((FIXTURES / "diagram_readings.json").read_text())["game1_diagram_grids"]


class ReplayBackend:
    """Stands in for a vision model: returns recorded page outputs, in order."""

    name = "replay"
    queue: list = []
    top_text: dict = {}
    followups: list = []

    def __init__(self):
        self.page = None

    def start(self, system, image, mime, text):
        self.page = ReplayBackend.queue.pop(0)
        return extractor._parse_json_text(json.dumps(self.page))

    def followup(self, text):
        ReplayBackend.followups.append(text)
        return extractor._parse_json_text(json.dumps(self.page))

    def ask_text(self, question, top_fraction=1.0):
        return ReplayBackend.top_text.get(self.page.get("page_number"), "")


@pytest.fixture
def replay(monkeypatch):
    """Run extractor.extract() against recorded pages: replay(pages, top_text={}) -> result."""
    monkeypatch.setitem(extractor.BACKENDS, "replay", ReplayBackend)

    def run(pages, top_text=None, check_diagrams=False):
        ReplayBackend.queue = [json.loads(json.dumps(p)) for p in pages]
        ReplayBackend.top_text = top_text or {}
        ReplayBackend.followups = []
        images = [(b"", "image/jpeg")] * len(pages)
        return extractor.extract(images, backends="replay", check_diagrams=check_diagrams)

    run.backend = ReplayBackend
    return run
