"""Diagram cross-check: catch legal-but-wrong moves without false alarms."""
import io
from pathlib import Path

import chess.pgn
import pytest

import diagrams as d
from conftest import GAME1, ROOT


def check(pgn, grids):
    game = chess.pgn.read_game(io.StringIO(pgn))
    sans = [n.san() for n in game.mainline()]
    found = d.suggest_corrections(game.board(), sans, [d.grid_occupancy(g) for g in grids])
    return [(s["move_number"], s["white"], s["played"], s["suggested"]) for s in found]


def test_correct_game_raises_no_alarm(diagram_grids):
    assert check(GAME1, diagram_grids) == []


@pytest.mark.parametrize("planted, expected", [
    (("15. O-O h6", "15. O-O h5"), (15, False, "h5", "h6")),
    (("31. g3 Qc5", "31. h3 Qc5"), (31, True, "h3", "g3")),
    (("16. a4 Kb8", "16. a3 Kb8"), (16, True, "a3", "a4")),
])
def test_legal_misread_is_pinpointed(diagram_grids, planted, expected):
    assert check(GAME1.replace(*planted), diagram_grids) == [expected]


def test_each_diagram_is_anchored_to_its_move(diagram_grids):
    game = chess.pgn.read_game(io.StringIO(GAME1))
    boards, _ = d._replay(game.board(), [n.san() for n in game.mainline()])
    plies = [d.anchor(boards, d.grid_occupancy(g))[0] for g in diagram_grids]
    # after 4.c3, 11.Nf3, 19.Bf1, 23.Ne1, 26...Qxe5, 31.g3, 38.Ne8 (the book's diagrams 1, 2, 4-8)
    assert plies == [7, 21, 37, 45, 52, 61, 75]


PHOTOS = sorted(ROOT.glob("*.jpg"))


@pytest.mark.skipif(len(PHOTOS) < 4, reason="sample page photos are not in the repo (gitignored)")
def test_diagrams_are_found_on_the_photos():
    import cv2
    counts = sorted(len(d.find_boards(cv2.imread(str(p)))) for p in PHOTOS)
    assert sum(counts) >= 8
