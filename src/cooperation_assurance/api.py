"""无第三方依赖的 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import ServiceError, ValidationFailed
from .service import AssuranceService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

    def __init__(self, service: AssuranceService) -> None:
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

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            if method == "POST" and path == "/users":
                result = self.service.create_user(payload["user_id"], payload["display_name"], payload["role"])
                return Response(201, result)
            if method == "POST" and path == "/cooperation_programs":
                result = self.service.register_program(
                    self._actor(normalized_headers), payload["program_id"], payload["program_name"], payload["lead_organization"]
                )
                return Response(201, result)
            if method == "POST" and path == "/evidence_revisions":
                result = self.service.register_evidence_revision(
                    self._actor(normalized_headers), payload["evidence_revision_id"], payload["program_id"],
                    payload["version"], payload["content_sha256"],
                )
                return Response(201, result)
            if method == "POST" and path == "/protocols":
                return Response(201, self.service.publish_protocol(self._actor(normalized_headers), payload))
            if method == "POST" and path == "/batches":
                result = self.service.create_batch(
                    self._actor(normalized_headers), payload["batch_id"], payload["protocol_id"],
                    int(payload["protocol_version"]), payload["evidence_revision_id"],
                )
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "start":
                result = self.service.start_batch(
                    self._actor(normalized_headers), parts[1], int(payload["expected_revision"])
                )
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "observations":
                key = normalized_headers.get("idempotency-key", "").strip()
                if not key:
                    raise ValidationFailed("缺少 Idempotency-Key")
                result = self.service.import_observations(
                    self._actor(normalized_headers), parts[1], key, payload.get("observations", [])
                )
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "seal":
                result = self.service.seal_batch(
                    self._actor(normalized_headers), parts[1], int(payload["expected_revision"])
                )
                return Response(200, result)
            if method == "GET" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "report":
                return Response(200, self.service.report(self._actor(normalized_headers), parts[1]))
            if method == "POST" and path == "/exclusions":
                result = self.service.request_exclusion(
                    self._actor(normalized_headers), int(payload["observation_id"]), payload["reason"]
                )
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "exclusions" and parts[2] == "review":
                result = self.service.review_exclusion(
                    self._actor(normalized_headers), int(parts[1]), bool(payload["approve"]), payload.get("note", "")
                )
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "exclusions" and parts[2] == "revoke":
                result = self.service.revoke_exclusion(
                    self._actor(normalized_headers), int(parts[1]), payload["reason"]
                )
                return Response(200, result)
            if method == "POST" and path == "/jobs/claim":
                result = self.service.claim_job(payload["worker_id"], int(payload.get("lease_seconds", 60)))
                return Response(200, {"job": result})
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "complete":
                result = self.service.complete_job(
                    payload["worker_id"], int(parts[1]), self._actor(normalized_headers)
                )
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "fail":
                result = self.service.fail_job(
                    payload["worker_id"], int(parts[1]), payload["error"], int(payload.get("retry_seconds", 0))
                )
                return Response(200, result)
            if method == "POST" and path == "/decisions":
                result = self.service.decide(
                    self._actor(normalized_headers), payload["batch_id"], int(payload["analysis_id"]),
                    payload["decision"], payload["reason"],
                )
                return Response(201, result)
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RobotTrials/1"

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
    parser = argparse.ArgumentParser(description="启动合作项目状态与结项评估准入 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("cooperation_assurance.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    application = JsonApplication(AssuranceService(connection))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
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
