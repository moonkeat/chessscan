"""Multi-page games, replaying real recorded model output (no model needed)."""
import io
import itertools
import json

import chess.pgn
import pytest

import extractor as x
from conftest import GAME1

PAGE_KEYS = ["p003", "p004", "p005", "p006"]


def mainline(pgn):
    return [n.san() for n in chess.pgn.read_game(io.StringIO(pgn)).mainline()]


def book_mainline():
    return [n.san() for n in chess.pgn.read_game(io.StringIO(GAME1)).mainline()]


@pytest.mark.parametrize("order", list(itertools.permutations(PAGE_KEYS)))
def test_any_upload_order_gives_the_book_order(model_pages, order):
    # page 006's printed number was misread as "004" by the model, so page numbers alone can't be trusted
    pages = [{"index": i, "data": x._parse_json_text(json.dumps(model_pages[k]))} for i, k in enumerate(order)]
    ordered, _ = x._order_pages(pages)
    assert [order[p["index"]] for p in ordered] == PAGE_KEYS


def test_four_pages_give_the_complete_game(replay, model_pages):
    result = replay([model_pages[k] for k in ("p003", "p005", "p004", "p006")],
                    top_text={"004": model_pages["p006_top_text"]})
    assert len(result["games"]) == 1, result["log"]
    game = result["games"][0]
    assert game["errors"] == []
    assert mainline(game["pgn"]) == book_mainline()
    assert game["headers"]["Result"] == "1-0"
    assert replay.backend.followups == []  # nothing needed re-reading


def test_incomplete_second_game_is_skipped(replay, model_pages):
    result = replay([model_pages[k] for k in PAGE_KEYS])
    assert [s["reason"] for s in result["skipped"]] == ["no result on these pages - add the page(s) where it ends"]


def test_misread_move_numbers_are_repaired(replay, model_pages):
    # the model once read page 005's "24...Na5" as "14...Na5" and renumbered the whole page
    p005 = json.loads(json.dumps(model_pages["p005"]))
    g = p005["games"][0]
    g["movetext"] = x._MOVE_NUM_RE.sub(lambda m: f"{int(m.group(1)) - 10}{m.group(2)}", g["movetext"])
    assert g["movetext"].startswith("14...")
    result = replay([model_pages["p003"], p005, model_pages["p004"], model_pages["p006"]])
    assert mainline(result["games"][0]["pgn"]) == book_mainline()


def test_commentary_above_the_next_game_closes_the_previous_one(replay, model_pages):
    result = replay([model_pages[k] for k in PAGE_KEYS], top_text={"004": model_pages["p006_top_text"]})
    pgn = result["games"][0]["pgn"]
    assert "38...Qf7 39.Qd6+" in pgn          # kept as a comment after the final move...
    assert pgn.rstrip().endswith("1-0")       # ...and before the result


def test_comment_lines_from_all_pages_become_variations(replay, model_pages):
    pgn = replay([model_pages[k] for k in PAGE_KEYS])["games"][0]["pgn"]
    for start in ("( 7... Qd7", "( 31... Qc8", "( 34... Nxa4", "( 36... Kd7"):
        assert start in pgn


def test_lone_mid_game_page_is_not_loaded(replay, model_pages):
    result = replay([model_pages["p005"]])
    assert result["games"] == []
    assert "first page is missing" in result["skipped"][0]["reason"]


def with_misread(model_pages, key, book, misread):
    pages = {k: json.loads(json.dumps(model_pages[k])) for k in PAGE_KEYS}
    pages[key]["games"][0]["movetext"] = pages[key]["games"][0]["movetext"].replace(book, misread)
    assert misread in pages[key]["games"][0]["movetext"]
    return [pages[k] for k in PAGE_KEYS]


def test_reported_rf1_misread_is_repaired_without_the_model(replay, model_pages):
    # from a real run: the model read "17.Rfe1" as "17.Rf1" and its retries did not fix it
    result = replay(with_misread(model_pages, "p004", "17. Rfe1", "17. Rf1"))
    game = result["games"][0]
    assert game["errors"] == [] and mainline(game["pgn"]) == book_mainline()
    assert game["corrections"] == [{"move_number": 17, "white": True, "read": "Rf1", "corrected": "Rfe1"}]
    assert replay.backend.followups == []                       # fixed in code, no model retry
    assert any("auto-corrected misread move 17. Rf1 -> Rfe1" in line for line in result["log"])


def test_misread_knight_move_is_repaired(replay, model_pages):
    result = replay(with_misread(model_pages, "p003", "9. Nh4", "9. Nb4"))
    game = result["games"][0]
    assert mainline(game["pgn"]) == book_mainline()
    assert [c["corrected"] for c in game["corrections"]] == ["Nh4"]


def test_every_automatic_correction_is_reported(replay, model_pages):
    # Two misreads can combine into a legal but wrong line (17.Rfc1 ... 20.Rc2 is legal), which
    # legality alone cannot detect - so each correction must be shown to the user to check.
    pages = with_misread(model_pages, "p004", "17. Rfe1", "17. Rf1")
    pages[1]["games"][0]["movetext"] = pages[1]["games"][0]["movetext"].replace("20. Rc1", "20. Rc2")
    game = replay(pages)["games"][0]
    assert game["errors"] == []
    assert game["corrections"] and all(c["read"] in ("Rf1", "Rc2") for c in game["corrections"])


def test_illegal_page_is_sent_back_with_its_starting_position(replay, model_pages):
    bad = json.loads(json.dumps(model_pages["p004"]))
    bad["games"][0]["movetext"] = bad["games"][0]["movetext"].replace("16. a4", "16. a5")  # a2 pawn can't reach a5
    replay([model_pages["p003"], bad, model_pages["p005"], model_pages["p006"]])
    assert replay.backend.followups, "the page with the illegal move should be re-asked"
    msg = replay.backend.followups[0]
    assert "illegal san: 'a5'" in msg and "The previous page ended with 14. Qc2" in msg
