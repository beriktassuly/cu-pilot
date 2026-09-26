"""Loopback-only demo control service backed by actual local program state."""

from __future__ import annotations

import argparse
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from examples.payouts.app import Application


def serve(directory: Path, port: int = 8787) -> None:
    token = secrets.token_hex(32)
    stop = threading.Event()
    lock = threading.Lock()
    worker = {"running": False, "error": None, "queue": None}
    last = {}
    drafts = {}

    def run_worker(queue):
        app = None
        try:
            app = Application(directory)
            for _ in range(18):
                if stop.is_set():
                    break
                result = app.step(queue)
                with lock:
                    last.clear()
                    last.update(result)
                if (
                    result.get("stopped")
                    or result.get("verification", {}).get("queue", {}).get("status") == 1
                ):
                    break
        except Exception as exc:
            with lock:
                worker["error"] = str(exc)
        finally:
            try:
                if app:
                    app.close()
            finally:
                with lock:
                    worker["running"] = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, value, status=200):
            payload = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def local_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def do_GET(self):
            if not self.local_host():
                self.reply({"error": "loopback Host required"}, 403)
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/":
                body = (Path(__file__).parents[2] / "apps/payout-demo/index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                    "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
                )
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/draft":
                try:
                    length = int(parse_qs(parsed.query).get("length", ["8"])[0])
                    if not 1 <= length <= 16:
                        raise ValueError("choose one to sixteen payments")
                    draft_id = secrets.token_hex(32)
                    # Only public recipient addresses survive; no recipient needs to sign.
                    payments = [
                        {"recipient": str(Keypair().pubkey()), "amount": str((i + 1) * 1000)}
                        for i in range(length)
                    ]
                    with lock:
                        if len(drafts) >= 64:
                            drafts.clear()
                        drafts[draft_id] = payments
                    self.reply({"payments": payments, "id": draft_id})
                except Exception as exc:
                    self.reply({"error": str(exc)}, 400)
                return
            if parsed.path != "/api/state":
                self.reply({"error": "not found"}, 404)
                return
            app = None
            try:
                app = Application(directory)
                # Collection fixtures stay in the journal, not in the interactive list.
                addresses = app.setting("interactive_queues") or []
                queues = [app.bridge.call("verify", queue=q) for q in addresses]
                history = []
                persisted_last = {}
                for row in app.store.db.execute(
                    "SELECT body,outcome,phase,signature FROM payout_steps "
                    "WHERE signature IS NOT NULL ORDER BY rowid DESC LIMIT 24"
                ):
                    body = json.loads(row["body"])
                    if body["queue"] not in addresses:
                        continue
                    outcome = json.loads(row["outcome"]) if row["outcome"] else {}
                    if not persisted_last:
                        persisted_last = {**body, **outcome, "signature": row["signature"]}
                    progress = outcome.get("verification", {}).get("queue", {})
                    history.append(
                        {
                            "id": body["id"],
                            "queue": body["queue"],
                            "chosen_count": body["chosen_count"],
                            "mode": body["mode"],
                            "phase": row["phase"],
                            "signature": row["signature"],
                            "success": outcome.get("success"),
                            "verification": {
                                "queue": {
                                    key: progress[key]
                                    for key in ("cursor", "length")
                                    if key in progress
                                }
                            },
                        }
                    )
                with lock:
                    value = {
                        "csrf_token": token,
                        "info": app.info,
                        "queues": queues,
                        "worker": dict(worker),
                        "last": dict(last) if last.get("chosen_count") else persisted_last,
                        "history": history,
                        "model_available": app.bundle is not None,
                    }
                self.reply(value)
            except Exception as exc:
                self.reply({"error": str(exc)}, 500)
            finally:
                if app:
                    app.close()

        def do_POST(self):
            if not self.local_host() or self.headers.get("X-Payout-Token") != token:
                self.reply({"error": "local control token required"}, 403)
                return
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
                self.reply({"error": "origin rejected"}, 403)
                return
            app = None
            step_active = False
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    raise ValueError("bounded JSON body required")
                body = json.loads(self.rfile.read(length))
                if self.path == "/api/start":
                    with lock:
                        if worker["running"]:
                            raise ValueError("worker already running")
                        worker.update(running=True, error=None, queue=body["queue"])
                        stop.clear()
                    threading.Thread(target=run_worker, args=(body["queue"],), daemon=True).start()
                    self.reply({"started": True})
                    return
                if self.path == "/api/stop":
                    stop.set()
                    self.reply({"stop_requested": True, "on_chain_pause_changed": False})
                    return
                if self.path == "/api/step":
                    with lock:
                        if worker["running"]:
                            raise ValueError("worker already running")
                        worker.update(running=True, error=None, queue=body["queue"])
                        stop.clear()
                    step_active = True
                    app = Application(directory)
                    result = app.step(body["queue"])
                    with lock:
                        last.clear()
                        last.update(result)
                    self.reply(result)
                    return
                app = Application(directory)
                if self.path == "/api/create":
                    payments = body["payments"]
                    if not isinstance(payments, list) or not 1 <= len(payments) <= 16:
                        raise ValueError("one to sixteen approved payments required")
                    for payment in payments:
                        if not Pubkey.from_string(payment["recipient"]).is_on_curve():
                            raise ValueError("recipient must be an on-curve wallet")
                        if (
                            not isinstance(payment["amount"], str)
                            or not payment["amount"].isdigit()
                            or not 0 < int(payment["amount"]) < 2**64
                        ):
                            raise ValueError("positive integer token amount required")
                    identifier = body["id"]
                    if identifier not in drafts:
                        raise ValueError("request a draft before approving")
                    result = app.create(
                        payments=payments,
                        identifier=identifier,
                        existing=int(body.get("existing", 0)),
                    )
                    addresses = app.setting("interactive_queues") or []
                    address = result["queue"]["address"]
                    if address not in addresses:
                        addresses.append(address)
                        app.set_setting("interactive_queues", addresses[-32:])
                elif self.path == "/api/pause":
                    if type(body.get("paused")) is not bool:
                        raise ValueError("paused must be a JSON boolean")
                    result = app.bridge.call("pause", queue=body["queue"], paused=body["paused"])
                elif self.path == "/api/ata":
                    result = app.bridge.call(
                        "create_ata", queue=body["queue"], index=int(body["index"])
                    )
                else:
                    self.reply({"error": "not found"}, 404)
                    return
                self.reply(result)
            except Exception as exc:
                if step_active:
                    with lock:
                        worker["error"] = str(exc)
                self.reply({"error": str(exc)}, 400)
            finally:
                try:
                    if app:
                        app.close()
                finally:
                    if step_active:
                        with lock:
                            worker["running"] = False

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Payout demo: http://127.0.0.1:{port} (real isolated local test runtime)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("artifacts/payouts"))
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    serve(args.directory, args.port)
