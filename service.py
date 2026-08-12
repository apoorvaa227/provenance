"""The context layer as an HTTP service.

    GET  /health    200 once the catalog is loaded
    GET  /agents    the roster, and which classifier is actually running
    GET  /catalog   coverage summary, including where the catalog is thin
    POST /answer    one question envelope in, one verified answer out

Standard library only. The layer computes its figures from records, so the
service that fronts it needs a socket and a JSON encoder and nothing else —
a framework here would be a dependency to keep current for no behaviour.

Two things are deliberate.

**A question that raises still comes back well-formed.** One bad question
cannot be allowed to cost the other hundred and nine. The fallback is an
honest abstention carrying the error, which scores nothing on quality and
keeps availability intact — the same distinction the scorer draws.

**Readiness means the catalog is loaded, not that the process is up.** A
service that returns 200 on `/health` while answering every question from an
empty catalog is worse than one that is honestly down.

    python service.py
    curl localhost:8080/health
    curl -X POST localhost:8080/answer -H 'content-type: application/json' \\
      -d '{"question_id":"q1","prompt":"who owns PROD.SALES.FCT_ORDERS?"}'
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from contextlayer.agents import ROSTER, Ecosystem       # noqa: E402
from contextlayer.catalog import Catalog                # noqa: E402

CATALOG_PATH = os.environ.get("CATALOG_PATH", "data/catalog.json")
PORT = int(os.environ.get("PORT", "8080"))
USE_MODEL = os.environ.get("PROVENANCE_CLASSIFIER", "auto")

_state: dict = {"eco": None, "ready": False, "error": None, "classifier": None}
_lock = threading.Lock()


def load() -> None:
    """Read the catalog once and hold it. It does not change inside a run, and
    re-reading per question spends a latency budget for nothing."""
    try:
        catalog = Catalog(json.loads(
            Path(CATALOG_PATH).read_text(encoding="utf-8")))

        classifier = None
        if USE_MODEL in ("auto", "model"):
            try:
                from contextlayer.llm import ModelClassifier
                candidate = ModelClassifier()
                # Only adopt it if a provider was actually found. Otherwise the
                # roster would advertise a model-backed classifier that is
                # silently running regex underneath.
                if candidate.provider is not None:
                    classifier = candidate
            except Exception:                              # noqa: BLE001
                classifier = None
        if classifier is None and USE_MODEL == "model":
            raise RuntimeError(
                "PROVENANCE_CLASSIFIER=model was requested but no provider is "
                "available. Set a key, or use 'auto' to fall back to regex.")

        with _lock:
            _state["eco"] = Ecosystem(catalog, classifier=classifier)
            _state["classifier"] = (classifier.usage() if classifier
                                    else {"classifier": "regex"})
            _state["ready"] = True
        print(f"loaded {len(catalog.assets)} assets, "
              f"{sum(len(a['columns']) for a in catalog.assets)} columns from "
              f"{CATALOG_PATH}; classifier="
              f"{_state['classifier'].get('classifier')}", flush=True)
    except Exception as e:                                 # noqa: BLE001
        _state["error"] = f"{type(e).__name__}: {e}"
        print(f"failed to load: {_state['error']}", flush=True)
        traceback.print_exc()


def fallback(qid: str, reason: str) -> dict:
    return {"question_id": qid, "answer": "", "answer_value": None,
            "abstained": True, "refused": False, "reason": reason,
            "citations": [], "confidence": 0.0, "flags": ["service_error"],
            "agents": ["router"]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "provenance"

    def log_message(self, fmt, *args):       # quieter than the default log
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path == "/health":
            if _state["ready"]:
                return self._send(200, {"status": "ok"})
            return self._send(503, {"status": "loading",
                                    "error": _state["error"]})

        if path == "/agents":
            return self._send(200, {
                "agents": ROSTER,
                # Reported rather than asserted: if the model was requested
                # and is not reachable, this says regex, because the roster is
                # only useful if it describes what is running.
                "classifier": _state["classifier"],
                "notes": "Figures are computed from catalog records. No model "
                         "call sits in the path of a number; the model, where "
                         "present, classifies intent only.",
            })

        if path == "/catalog":
            eco = _state["eco"]
            if eco is None:
                return self._send(503, {"error": _state["error"]})
            cat = eco.cat
            return self._send(200, {
                "as_of": cat.as_of.isoformat(),
                "assets": len(cat.assets),
                "columns": sum(len(a["columns"]) for a in cat.assets),
                "glossary_terms": len(cat.terms),
                "coverage_gaps": {
                    "undocumented_assets":
                        sum(1 for a in cat.assets if not a["description"]),
                    "assets_without_an_owner":
                        sum(1 for a in cat.assets if not a["owner_id"]),
                    "restricted_assets":
                        sum(1 for a in cat.assets if cat.restricted(a)),
                },
                "synthetic": True,
            })

        return self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path != "/answer":
            return self._send(404, {"error": "not found"})
        qid = ""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            envelope = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            qid = envelope.get("question_id") or ""
            eco = _state["eco"]
            if eco is None:
                return self._send(200, fallback(
                    qid, f"catalog unavailable: {_state['error']}"))
            return self._send(200, eco.answer(envelope))
        except Exception as e:                             # noqa: BLE001
            traceback.print_exc()
            return self._send(200, fallback(
                qid, f"internal error: {type(e).__name__}: {e}"))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Server6(Server):
    """Dual-stack. A client that resolves localhost to ::1 first otherwise
    waits for the IPv6 connection to fail before retrying IPv4 — seconds per
    request, charged to a latency budget that gets measured."""
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()


def main() -> None:
    load()
    try:
        srv = Server6(("::", PORT), Handler)
    except OSError:
        srv = Server(("0.0.0.0", PORT), Handler)
    print(f"listening on :{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
