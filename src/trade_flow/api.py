"""无第三方依赖的供应调度 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import contextlib
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .clock import SystemClock
from .errors import SupplyError, ValidationFailed
from .service import SupplyService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: SupplyService | None = None, *, database: str | Path | None = None) -> None:
        # 多席位并发由线程化 HTTP 服务承载：若所有请求共享单个 SQLite 连接，
        # 会触发“SQLite objects created in a thread can only be used in that
        # same thread”。生产路径为每个请求打开独立连接，由 WAL 与立即写
        # 事务在数据库层串行化；测试仍可直接注入单个 service。
        if service is None and database is None:
            raise ValueError("必须提供 service 或 database")
        self._service = service
        self._database = None if database is None else str(database)

    @contextlib.contextmanager
    def _service_for_request(self):
        if self._service is not None:
            yield self._service
            return
        connection = connect(self._database)
        try:
            yield SupplyService(connection, SystemClock())
        finally:
            connection.close()

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
        with self._service_for_request() as service:
            return self._handle(service, method, target, headers, body)

    def _handle(
        self,
        service: SupplyService,
        method: str,
        target: str,
        headers: Mapping[str, str] | None,
        body: bytes,
    ) -> Response:
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
                return Response(201, service.create_user(payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/quotes":
                return Response(201, service.record_quote(actor, payload))
            if method == "GET" and len(parts) == 3 and parts[:2] == ["quotes", "summary"]:
                return Response(200, service.price_summary(parts[2], int(query.get("sessions", ["20"])[0])))
            if method == "POST" and path == "/facilities":
                return Response(201, service.create_facility(actor, payload))
            if method == "POST" and path == "/routes":
                return Response(201, service.create_route(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "routes" and parts[2] == "outages":
                return Response(201, service.announce_outage(actor, parts[1], payload["starts_at"], payload.get("ends_at"), payload["capacity_percent"], payload["reason"]))
            if method == "POST" and path == "/inventory/lots":
                return Response(201, service.add_inventory_lot(actor, payload))
            if method == "GET" and path == "/inventory/summary":
                return Response(200, service.inventory_summary(query.get("facility_id", [""])[0], query.get("product", [""])[0]))
            if method == "POST" and path == "/nominations":
                return Response(201, service.submit_nomination(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "routes" and parts[2] == "allocate":
                return Response(200, service.allocate(actor, parts[1], payload["service_date"]))
            if method == "POST" and path == "/transfers":
                return Response(201, service.dispatch_transfer(actor, payload["transfer_id"], payload["nomination_id"], payload["lot_id"], int(payload["expected_revision"])))
            if method == "POST" and path == "/scenarios":
                return Response(201, service.create_scenario(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "scenarios" and parts[2] == "approve":
                return Response(200, service.approve_scenario(actor, parts[1], int(payload["expected_revision"])))
            if method == "POST" and len(parts) == 3 and parts[0] == "scenarios" and parts[2] == "run":
                return Response(200, service.run_scenario(actor, parts[1], payload["as_of_date"]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except SupplyError as exc:
            error: dict[str, Any] = {"code": exc.code, "message": str(exc)}
            if getattr(exc, "details", None):
                error["details"] = exc.details
            return Response(exc.status, {"error": error})
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
    # 先建立一次连接以初始化模式；每个请求随后使用自己的连接访问同一数据库。
    with contextlib.closing(connect(args.database)):
        pass
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(database=args.database)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
