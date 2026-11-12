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
    res["games"][0]["corrections"] = [{"move_number": 17, "white": True, "read": "Rf1", "corrected": "Rfe1"}]
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
    page.click(".check-row.corrected")
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
