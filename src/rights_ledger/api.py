"""无第三方依赖的权益额度账本 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import LedgerError, ValidationFailed
from .service import RightsLedgerService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: RightsLedgerService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(payload["user_id"], payload["display_name"], payload["role"], payload.get("household_id")))
            if method == "POST" and path == "/households":
                return Response(201, self.service.register_household(actor, payload["household_id"], payload["name"], payload["village"]))
            if method == "POST" and path == "/plots":
                return Response(201, self.service.register_plot(actor, payload["plot_id"], payload["village"], payload["category"], payload["area_mu"]))
            if method == "POST" and path == "/rules":
                return Response(201, self.service.create_rule(actor, payload))
            if method == "POST" and path == "/grants":
                return Response(201, self.service.grant_entitlement(actor, payload))
            if method == "POST" and path == "/applications":
                return Response(201, self.service.submit_application(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "exceptions" and parts[2] == "review":
                return Response(200, self.service.review_exception(actor, parts[1], payload["decision"], payload["reason"]))
            if method == "GET" and path == "/exceptions":
                return Response(200, self.service.list_exceptions(actor))
            if method == "POST" and len(parts) == 3 and parts[0] == "applications" and parts[2] == "confirm":
                return Response(200, self.service.confirm_allocation(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "applications" and parts[2] == "cancel":
                return Response(200, self.service.cancel_application(actor, parts[1], payload["reason"]))
            if method == "POST" and path == "/deliveries":
                return Response(201, self.service.record_delivery(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "reservations" and parts[2] == "exit":
                return Response(200, self.service.exit_reservation(actor, parts[1], payload["reason"], failed=bool(payload.get("failed", False))))
            if method == "POST" and len(parts) == 3 and parts[0] == "settlements" and parts[1] == "years":
                return Response(200, self.service.settle_year(actor, int(parts[2])))
            if method == "GET" and len(parts) == 3 and parts[0] == "households" and parts[2] == "summary":
                return Response(200, self.service.household_summary(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "households" and parts[2] == "entries":
                return Response(200, self.service.household_entries(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "explain" and parts[1] == "entries":
                return Response(200, self.service.explain_entry(actor, int(parts[2])))
            if method == "GET" and len(parts) == 3 and parts[0] == "explain" and parts[1] == "exceptions":
                return Response(200, self.service.explain_exception(actor, parts[2]))
            if method == "GET" and len(parts) == 2 and parts[0] == "reservations":
                return Response(200, self.service.reservation(actor, parts[1]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except LedgerError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    dispatch_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "RightsLedger/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            with dispatch_lock:
                response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动家庭土地权益额度账本服务")
    parser.add_argument("--database", type=Path, default=Path("rights_ledger.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(RightsLedgerService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
