"""The real page in headless Edge/Chromium, with the job API mocked. Skipped without Playwright."""
import base64
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

import server

sync_api = pytest.importorskip("playwright.sync_api")

def _photo() -> str:
    import cv2
    import numpy as np
    ok, png = cv2.imencode(".png", np.full((60, 40, 3), 230, np.uint8))  # a small blank "page"
    return "data:image/png;base64," + base64.b64encode(png.tobytes()).decode()


PHOTO = _photo()
JOB = "d" * 32


@pytest.fixture(scope="module")
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="msedge")
        except Exception:
            try:
                b = p.chromium.launch()
            except Exception as e:  # no browser available
                pytest.skip(f"no browser for Playwright: {e}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def result(model_pages):
    """A real pipeline result for the sample pages (no model: recorded output), plus one fake diagram."""
    import extractor
    from conftest import ReplayBackend
    extractor.BACKENDS["replay"] = ReplayBackend
    ReplayBackend.queue = [json.loads(json.dumps(model_pages[k])) for k in ("p003", "p004", "p005", "p006")]
    ReplayBackend.top_text = {"004": model_pages["p006_top_text"]}
    res = extractor.extract([(b"", "image/jpeg")] * 4, backends="replay", check_diagrams=False)
    res["games"][0]["diagrams"] = [{"image": PHOTO, "ply": 7, "diff": 0, "squares": []}]
    res["games"][0]["suggestions"] = []
    res["games"][0]["corrections"] = [{"move_number": 17, "white": True, "read": "Rf1", "corrected": "Rfe1",
                                       "alternatives": ["Rfc1", "Rfd1"]}]
    return res


def open_page(browser, base_url, job_bodies):
    """Open the app with POST /api/extract and GET /api/jobs/<id> answered from job_bodies (last one repeats)."""
    page = browser.new_page(viewport={"width": 1300, "height": 950})
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.route("**/api/extract", lambda r: r.fulfill(status=202, content_type="application/json",
                                                     body=json.dumps({"job": JOB})))
    calls = {"n": 0}

    def job(route):
        body = job_bodies[min(calls["n"], len(job_bodies) - 1)]
        calls["n"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/jobs/*", job)
    page.goto(base_url + "/")
    return page


def add_photo(page):
    page.evaluate("""async (url) => {
        const blob = await (await fetch(url)).blob();
        const dt = new DataTransfer(); dt.items.add(new File([blob], 'p.png', {type: 'image/png'}));
        document.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt}));
    }""", PHOTO)
    page.wait_for_selector(".thumb")


RUNNING = {"status": "running", "progress": 0.4, "message": "Reading page 2 of 4 (ollama)"}


def test_full_flow(browser, base_url, result):
    page = open_page(browser, base_url, [RUNNING, RUNNING, {"status": "done", "progress": 1, "message": "Done",
                                                           "result": result}])
    assert not page.is_visible("#viewer") and not page.is_visible("#diagramBox")
    add_photo(page)
    page.click("#extract")
    page.wait_for_selector("#progress:not([hidden])")
    page.wait_for_function("document.querySelector('#progressMsg').textContent.startsWith('Reading page')")
    for sel in ("#extract", "#file", "#backend", "#translate"):  # everything locked while busy
        assert page.is_disabled(sel), sel
    page.wait_for_selector("#viewer:not([hidden])", timeout=15000)
    assert page.locator(".thumb").count() == 0 and page.is_hidden("#progress")
    assert not page.is_disabled("#backend")
    assert page.locator("cg-board piece").count() == 32
    # main line grid + comment blocks + variations
    assert page.locator(".movelist > .ml-comment").count() > 5
    assert page.locator(".movelist > .ml-var").count() >= 4
    # an automatic correction is shown for the user to check, and jumps to the move
    assert "Auto-corrected 17. Rf1 → 17. Rfe1" in page.inner_text(".check-row.corrected")
    page.click(".check-link")
    assert page.inner_text(".mv.active") == "Rfe1"
    assert page.locator(".mv.corrected").count() == 1
    # the book diagram appears at its move (after 4.c3, ply 7), and only there
    page.locator(".movelist .mv").nth(6).click()
    assert page.is_visible("#diagramBox") and page.locator("cg-auto-shapes *").count() == 0
    page.keyboard.press("ArrowRight")
    assert not page.is_visible("#diagramBox")
    # the line after the final move (38...Qf7 ... 42.Qxc7#) is playable
    line = page.locator(".ml-line .mv")
    assert line.count() == 8  # 38...Qf7 39.Qd6+ Nc6 40.bxc6 bxc6 41.Qd8+ Qc7 42.Qxc7#
    line.last.click()
    assert page.inner_text(".mv.active").startswith("Qxc7#")
    assert page.errors == []
    page.close()


def test_warns_before_leaving_while_processing(browser, base_url):
    page = open_page(browser, base_url, [RUNNING])
    add_photo(page)
    page.click("#extract")
    page.wait_for_selector("#progress:not([hidden])")
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.type), d.dismiss()))  # dismiss = "stay on page"
    with pytest.raises(sync_api.TimeoutError):
        page.reload(timeout=3000)  # the reload is cancelled, so it never completes
    assert dialogs == ["beforeunload"]
    assert page.is_visible("#progress")  # still processing
    page.close()


def test_no_warning_when_idle(browser, base_url):
    page = open_page(browser, base_url, [RUNNING])
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.type), d.dismiss()))
    page.reload()
    assert dialogs == []
    page.close()


def test_reload_resumes_the_running_job(browser, base_url, result):
    page = open_page(browser, base_url, [{"status": "done", "progress": 1, "message": "Done", "result": result}])
    page.evaluate(f"localStorage.setItem('chessscan.job', '{JOB}')")
    page.reload()
    page.wait_for_selector("#viewer:not([hidden])", timeout=15000)
    assert page.evaluate("localStorage.getItem('chessscan.job')") is None
    assert page.errors == []
    page.close()


DONE = lambda result: [{"status": "done", "progress": 1, "message": "Done", "result": result}]  # noqa: E731


def load_game(browser, base_url, result):
    page = open_page(browser, base_url, DONE(json.loads(json.dumps(result))))
    add_photo(page)
    page.click("#extract")
    page.wait_for_selector("#viewer:not([hidden])", timeout=15000)
    return page


def test_wrong_auto_correction_can_be_swapped_for_an_alternative(browser, base_url, result):
    page = load_game(browser, base_url, result)
    page.click(".chip.alt >> text=Rfd1")
    assert page.inner_text(".mv.active") == "Rfd1"
    assert page.locator(".check-row.corrected").count() == 0      # the user decided: warning gone
    assert "17. Rfd1" in page.input_value("#pgn") and "Rfe1" not in page.input_value("#pgn")
    assert "Qd1" in page.inner_text("#warnings")                   # 18.Qd1 no longer fits: flagged, not hidden
    assert page.errors == []
    page.close()


def test_alternative_that_breaks_later_moves_is_flagged(browser, base_url, result):
    page = load_game(browser, base_url, result)
    page.click(".chip.alt >> text=Rfc1")                           # then 20.Rc1 is impossible
    assert "Illegal or unreadable move" in page.inner_text("#warnings")
    assert page.inner_text(".mv.active") == "Rfc1"
    page.close()


def test_any_move_can_be_replaced_on_the_board(browser, base_url, result):
    page = load_game(browser, base_url, result)
    page.locator('.movelist > .ml-move:text-is("a4")').click()     # 16. a4
    assert page.inner_text(".mv.active") == "a4"
    page.click("#editMove")
    assert page.is_visible("#editBar") and "16. a4" in page.inner_text("#editBar")
    box = page.locator("cg-board").bounding_box()
    sq = box["width"] / 8
    center = lambda f, r: (box["x"] + (f + .5) * sq, box["y"] + (7 - r + .5) * sq)  # noqa: E731
    page.mouse.click(*center(0, 1))                                # a2
    page.mouse.click(*center(0, 2))                                # a3
    page.wait_for_function("document.querySelector('.mv.active')?.textContent === 'a3'")
    assert page.is_hidden("#editBar")
    assert "16. a3" in page.input_value("#pgn")
    # Esc leaves edit mode without changing anything
    page.click("#editMove")
    page.keyboard.press("Escape")
    assert page.is_hidden("#editBar") and page.inner_text(".mv.active") == "a3"
    assert page.errors == []
    page.close()
