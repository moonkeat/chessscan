"""Job API: POST starts a background job, GET reports progress and the result."""
import base64
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import server

TINY_JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xd9").decode()


@pytest.fixture
def base_url(monkeypatch):
    def fake_extract(images, translate, backends, progress=None, check_diagrams=True):
        for i in range(3):
            progress(i / 3, f"Reading page {i + 1} of 3")
            time.sleep(0.2)
        if backends == "fail":
            raise RuntimeError("simulated failure")
        return {"games": [], "skipped": [], "log": ["ok"], "notes": "", "pages": len(images)}

    monkeypatch.setattr(server, "extract", fake_extract)
    monkeypatch.setitem(server.BACKENDS, "fail", object)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def poll(base_url, job):
    seen = []
    for _ in range(100):
        with urllib.request.urlopen(f"{base_url}/api/jobs/{job}") as r:
            j = json.loads(r.read())
        seen.append(j["message"])
        if j["status"] != "running":
            return j, seen
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_job_reports_progress_then_result(base_url):
    status, body = post(f"{base_url}/api/extract", {"images": [TINY_JPEG, TINY_JPEG]})
    assert status == 202
    job, seen = poll(base_url, body["job"])
    assert job["status"] == "done" and job["result"]["pages"] == 2
    assert any(m.startswith("Reading page") for m in seen)


def test_job_failure_is_reported(base_url):
    _, body = post(f"{base_url}/api/extract", {"images": [TINY_JPEG], "backends": "fail"})
    job, _ = poll(base_url, body["job"])
    assert job["status"] == "error" and "simulated failure" in job["error"]


@pytest.mark.parametrize("body, code", [
    ({}, 400),
    ({"images": ["not a data url"]}, 400),
    ({"images": [TINY_JPEG], "backends": "nope"}, 400),
])
def test_bad_requests_are_rejected(base_url, body, code):
    assert post(f"{base_url}/api/extract", body)[0] == code


def test_unknown_job_and_missing_file_do_not_crash(base_url):
    for path in ("/api/jobs/" + "0" * 32, "/missing.js"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base_url + path)
        assert e.value.code == 404
    with urllib.request.urlopen(base_url + "/") as r:  # server still alive after error logging
        assert r.status == 200
