"""无第三方依赖的供应调度 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import SupplyError, ValidationFailed
from .service import SupplyService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: SupplyService) -> None:
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
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/quotes":
                return Response(201, self.service.record_quote(actor, payload))
            if method == "GET" and len(parts) == 3 and parts[:2] == ["quotes", "summary"]:
                return Response(200, self.service.price_summary(parts[2], int(query.get("sessions", ["20"])[0])))
            if method == "POST" and path == "/facilities":
                return Response(201, self.service.create_facility(actor, payload))
            if method == "POST" and path == "/routes":
                return Response(201, self.service.create_route(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "routes" and parts[2] == "outages":
                return Response(201, self.service.announce_outage(actor, parts[1], payload["starts_at"], payload.get("ends_at"), payload["capacity_percent"], payload["reason"]))
            if method == "POST" and path == "/inventory/lots":
                return Response(201, self.service.add_inventory_lot(actor, payload))
            if method == "GET" and path == "/inventory/summary":
                return Response(200, self.service.inventory_summary(query.get("facility_id", [""])[0], query.get("product", [""])[0]))
            if method == "POST" and path == "/nominations":
                return Response(201, self.service.submit_nomination(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "routes" and parts[2] == "allocate":
                return Response(200, self.service.allocate(actor, parts[1], payload["service_date"]))
            if method == "POST" and path == "/transfers":
                return Response(201, self.service.dispatch_transfer(actor, payload["transfer_id"], payload["nomination_id"], payload["lot_id"], int(payload["expected_revision"])))
            if method == "POST" and path == "/scenarios":
                return Response(201, self.service.create_scenario(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "scenarios" and parts[2] == "approve":
                return Response(200, self.service.approve_scenario(actor, parts[1], int(payload["expected_revision"])))
            if method == "POST" and len(parts) == 3 and parts[0] == "scenarios" and parts[2] == "run":
                return Response(200, self.service.run_scenario(actor, parts[1], payload["as_of_date"]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except SupplyError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PowerDispatch/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
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
    parser = argparse.ArgumentParser(description="启动数字服务资源资产与流转分析服务")
    parser.add_argument("--database", type=Path, default=Path("trade_flow.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(SupplyService(connection))))
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
