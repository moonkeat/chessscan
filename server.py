"""Tiny web server: serves the UI from ./static and runs extraction jobs.

    python server.py            # http://localhost:8000
    python server.py --port 9000

API
  POST /api/extract   {"images": [data URL, ...], "translate": bool, "backends": "ollama,gemini"}
                      -> {"job": id}
  GET  /api/jobs/<id> -> {"status": "running"|"done"|"error", "progress": 0..1, "message": str,
                          "result": {...} | "error": str}
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import threading
import time
import traceback
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from extractor import BACKENDS, extract

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 100 * 1024 * 1024
MAX_PAGES = 30
JOB_TTL = 3600  # seconds a finished job is kept for polling
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
DATA_URL_RE = re.compile(r"^data:(image/[a-z+]+);base64,(.*)$", re.S)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()  # one extraction at a time: the local model uses the whole GPU


def _run_job(job_id: str, images, translate: bool, backends) -> None:
    def progress(frac: float, msg: str) -> None:
        with JOBS_LOCK:
            JOBS[job_id].update(progress=round(max(0.0, min(frac, 1.0)), 3), message=msg)

    try:
        with RUN_LOCK:
            progress(0.0, "Starting")
            result = extract(images, translate, backends, progress=progress)
        with JOBS_LOCK:
            JOBS[job_id].update(status="done", progress=1.0, message="Done", result=result, finished=time.time())
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        with JOBS_LOCK:
            JOBS[job_id].update(status="error", error=str(e), finished=time.time())


def _purge_old_jobs() -> None:
    now = time.time()
    with JOBS_LOCK:
        for jid in [j for j, v in JOBS.items() if v.get("finished") and now - v["finished"] > JOB_TTL]:
            del JOBS[jid]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt, *args):  # keep the console quiet while the page polls
        # args[0] is the request line for normal logs, but an HTTPStatus for log_error()
        if "/api/jobs/" not in str(args[0] if args else ""):
            super().log_message(fmt, *args)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{32})", self.path)
        if not m:
            return super().do_GET()
        with JOBS_LOCK:
            job = JOBS.get(m.group(1))
            payload = {k: v for k, v in job.items() if k != "finished"} if job else None
        if payload is None:
            return self._json(404, {"error": "Unknown job"})
        return self._json(200, payload)

    def do_POST(self):  # noqa: N802
        if self.path != "/api/extract":
            return self._json(404, {"error": "Not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._json(413, {"error": "Images missing or too large"})
        try:
            req = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return self._json(400, {"error": "Invalid JSON"})
        urls = req.get("images") or ([req["image"]] if req.get("image") else [])
        if not urls or len(urls) > MAX_PAGES:
            return self._json(400, {"error": f"Send between 1 and {MAX_PAGES} page images"})
        images = []
        for url in urls:
            m = DATA_URL_RE.match(url)
            if not m or m.group(1) not in ALLOWED_TYPES:
                return self._json(400, {"error": "Expected JPEG/PNG/WebP/GIF data URLs"})
            images.append((base64.b64decode(m.group(2)), m.group(1)))
        backends = req.get("backends") or None
        if backends and any(b.strip() not in BACKENDS for b in backends.split(",")):
            return self._json(400, {"error": f"Unknown backend in '{backends}'"})

        _purge_old_jobs()
        job_id = uuid.uuid4().hex
        with JOBS_LOCK:
            JOBS[job_id] = {"status": "running", "progress": 0.0,
                            "message": "Waiting for the previous job" if RUN_LOCK.locked() else "Starting"}
        threading.Thread(target=_run_job, args=(job_id, images, bool(req.get("translate")), backends),
                         daemon=True).start()
        return self._json(202, {"job": job_id})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Serving on http://localhost:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
