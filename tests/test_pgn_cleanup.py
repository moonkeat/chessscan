"""Notation clean-up and PGN assembly: pure functions, no model."""
import extractor as x


def legal(movetext, fen=""):
    pgn = x.build_pgn({"headers": {}, "fen": fen, "movetext": movetext})
    return x.validate_pgn(pgn)


def test_book_notation_is_converted_to_san():
    assert x.normalize_movetext("5. c×d4 0-0 6. ♘f3 0-0-0") == "5. cxd4 O-O 6. Nf3 O-O-O"
    assert x.normalize_movetext("15. Rx e5 Rx e5") == "15. Rxe5 Rxe5"


def test_evaluation_glyphs_become_nags():
    assert x.normalize_movetext("11. Bb5+- *") == "11. Bb5 $18 *"
    assert "$16" in x.normalize_movetext("6. Qd2+ e5 ± 7. Rd1")
    assert x.normalize_movetext("1/2-1/2") == "1/2-1/2"  # results are not glyphs


def test_diagram_references_become_comments():
    assert x.normalize_movetext("4. c3 (图1) 4... Nc6") == "4. c3 {Diagram 1} 4... Nc6"
    assert x.normalize_movetext("3. Bf4（图 9）") == "3. Bf4{Diagram 9}"


def test_comments_are_left_untouched():
    out = x.normalize_movetext("7. Qb3 {line: 7...Qd7 8.N×e5 0-0} 7... Qc8")
    assert "{line: 7...Qd7 8.N×e5 0-0}" in out


def test_headers_are_made_valid():
    pgn = x.build_pgn({"headers": {"Date": "??", "Result": "?"}, "fen": "", "movetext": "1. e4 *"})
    assert '[Date "????.??.??"]' in pgn and '[Result "*"]' in pgn
    pgn = x.build_pgn({"headers": {"Date": "2015.??"}, "fen": "", "movetext": "1. e4 *"})
    assert '[Date "2015.??.??"]' in pgn


def test_illegal_move_is_reported():
    problems = legal("1. d4 Nf6 2. Nb4 *")
    assert problems and "Nb4" in problems[0]
    assert legal("1. d4 Nf6 2. Bf4 d5 *") == []


def test_stray_parenthesis_is_repaired():
    data = {"games": [{"headers": {}, "fen": "", "movetext": "1. d4 Nf6 2. c4 (2... e6 3. Nc3 Bb4 *"}], "notes": ""}
    games, problems = x._evaluate(data)
    assert problems == []
    assert "(" not in games[0]["pgn"].split("\n\n")[1]


def test_commentary_line_becomes_a_variation():
    mt = ("1. d4 Nf6 2. Bf4 d5 3. e3 c5 4. c3 Nc6 5. Nd2 cxd4 6. exd4 Bf5 7. Qb3 Qc8 "
          "{avoid Qd7: 7...Qd7 8.Ngf3 e6 9.Ne5 N×e5? 10.d×e5 Ne4 11.Bb5+-, White wins} 8. Ngf3 *")
    games, problems = x._evaluate({"games": [{"headers": {}, "fen": "", "movetext": mt}], "notes": ""})
    pgn = games[0]["pgn"]
    assert problems == []
    assert "( 7... Qd7 8. Ngf3 e6 9. Ne5 Nxe5 $2 10. dxe5 Ne4 11. Bb5 $18" in pgn
    assert "avoid Qd7:" in pgn and "White wins" in pgn


def test_line_after_the_final_move_stays_a_comment():
    # PGN cannot attach a variation after the last move; the viewer makes it playable instead
    mt = "1. e4 e5 2. Nf3 Nc6 3. Bb5 {Black resigned, fearing 3...a6 4.Ba4 b5} 1-0"
    games, _ = x._evaluate({"games": [{"headers": {}, "fen": "", "movetext": mt}], "notes": ""})
    pgn = games[0]["pgn"]
    assert "(" not in pgn.split("\n\n")[1] and "3...a6 4.Ba4 b5" in pgn
    assert x._first_move_number("3. Bb5 {..} 1-0") == (3, False)


def test_line_that_replaces_an_earlier_move_becomes_a_variation():
    mt = "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# {or 3...g6 4.Qf3} 1-0"
    games, _ = x._evaluate({"games": [{"headers": {}, "fen": "", "movetext": mt}], "notes": ""})
    assert "Nf6 ( 3... g6 4. Qf3 ) 4. Qxf7#" in games[0]["pgn"]


def test_continuation_detection_uses_the_moves_not_the_flag():
    assert x._is_continuation({"continues_previous": False, "fen": "", "movetext": "24... Na5 25. Rxe5"})
    assert x._is_continuation({"continues_previous": False, "fen": "", "movetext": "{only commentary} 1-0"})
    assert not x._is_continuation({"continues_previous": True, "fen": "", "movetext": "1. d4 Nf6"})
