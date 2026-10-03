"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, PermissionDenied, ValidationError
from .governance import GovernanceService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None, governance: GovernanceService | None = None
          ) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        if governance is not None:
            return _route_governance(governance, method, parsed, body, headers)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _staff(headers: dict[str, str]) -> str:
    actor_id = headers.get("X-Actor-Id", "")
    if not actor_id:
        raise PermissionDenied("该接口需要 X-Actor-Id 员工身份")
    return actor_id


def _participant(governance: GovernanceService, headers: dict[str, str]) -> str:
    return governance.resolve_participant_token(headers.get("X-Participant-Token", ""))


def _receipt_status(receipt, created: int = 201) -> int:
    return 200 if getattr(receipt, "replayed", False) else created


def _route_governance(governance: GovernanceService, method: str, parsed, body: dict[str, Any],
                      headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
    path = parsed.path
    query = parse_qs(parsed.query)

    def q(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    # ---- 员工：赛事配置与主体登记 ----
    if method == "POST" and path == "/gov/tracks":
        r = governance.create_track(actor_id=_staff(headers), **body)
        return _receipt_status(r), r.__dict__
    if method == "POST" and path == "/gov/windows":
        r = governance.create_window(actor_id=_staff(headers), **body)
        return _receipt_status(r), r.__dict__
    if method == "POST" and path == "/gov/windows/freeze":
        r = governance.freeze_window(actor_id=_staff(headers), **body)
        return _receipt_status(r), r.__dict__
    if method == "POST" and path == "/gov/persons":
        r = governance.register_person(actor_id=_staff(headers), **body)
        return _receipt_status(r), r.__dict__
    if method == "POST" and path == "/gov/organizations":
        r = governance.register_organization(actor_id=_staff(headers), **body)
        return _receipt_status(r), r.__dict__
    if method == "POST" and path == "/gov/representations":
        r = governance.register_representation(actor_id=_staff(headers), **body)
        return _receipt_status(r), r.__dict__
    if method == "POST" and path == "/gov/representations/revoke":
        r = governance.revoke_representation(actor_id=_staff(headers), **body)
        return _receipt_status(r), r.__dict__
    if method == "POST" and path == "/gov/participant-tokens":
        result = governance.mint_participant_token(actor_id=_staff(headers), **body)
        return 201 if not result.get("replayed") else 200, result

    # ---- 员工：审核、冲突、申诉、占用、查询 ----
    if method == "GET" and path == "/gov/review-queue":
        return 200, {"items": governance.review_queue(_staff(headers))}
    if method == "POST" and path == "/gov/reviews":
        r = governance.decide_review(actor_id=_staff(headers), **body)
        return _receipt_status(r), r.__dict__
    if method == "GET" and path == "/gov/submissions":
        return 200, {"items": governance.list_submissions(_staff(headers), q("window_id"))}
    if method == "GET" and path == "/gov/conflicts":
        return 200, {"items": governance.list_conflicts(_staff(headers), q("window_id"))}
    if method == "GET" and path == "/gov/appeals":
        return 200, {"items": governance.list_appeals(_staff(headers))}
    if method == "POST" and path == "/gov/appeals/decide":
        r = governance.decide_appeal(actor_id=_staff(headers), **body)
        return _receipt_status(r), r.__dict__
    if method == "GET" and path == "/gov/occupancy":
        window_id = q("window_id")
        if not window_id:
            raise ValidationError("window_id 不能为空")
        return 200, {"items": governance.track_occupancy(_staff(headers), window_id)}

    # ---- 参赛人：团队与投稿（令牌） ----
    if method == "POST" and path == "/teams":
        person_id = _participant(governance, headers)
        r = governance.create_team(actor_id=person_id, **body)
        return _receipt_status(r), r.__dict__
    if method == "POST" and path == "/teams/beneficiary":
        person_id = _participant(governance, headers)
        r = governance.designate_team_beneficiary(actor_id=person_id, **body)
        return _receipt_status(r), r.__dict__
    if method == "POST" and path == "/teams/members":
        person_id = _participant(governance, headers)
        r = governance.change_team_member(caller_person_id=person_id, **body)
        return _receipt_status(r), r.__dict__
    if method == "POST" and path == "/submissions":
        person_id = _participant(governance, headers)
        r = governance.submit(caller_person_id=person_id, **body)
        return _receipt_status(r), r.__dict__
    if method == "POST" and path == "/submissions/correct":
        person_id = _participant(governance, headers)
        r = governance.correct(caller_person_id=person_id, **body)
        return _receipt_status(r), r.__dict__
    if method == "POST" and path == "/submissions/withdraw":
        person_id = _participant(governance, headers)
        r = governance.withdraw(caller_person_id=person_id, **body)
        return _receipt_status(r), r.__dict__
    if method == "POST" and path == "/submissions/switch-track":
        person_id = _participant(governance, headers)
        r = governance.switch_track(caller_person_id=person_id, **body)
        return _receipt_status(r), r.__dict__
    if method == "POST" and path == "/appeals":
        person_id = _participant(governance, headers)
        r = governance.file_appeal(caller_person_id=person_id, **body)
        return _receipt_status(r), r.__dict__
    if method == "GET" and path == "/me/submissions":
        person_id = _participant(governance, headers)
        return 200, {"items": governance.my_submissions(person_id)}

    # ---- 时间旅行：员工或参赛人皆可（各自只能看有权访问的作品） ----
    if path.startswith("/submissions/") and path.endswith("/timeline"):
        submission_id = path.split("/")[2]
        if headers.get("X-Participant-Token"):
            person_id = _participant(governance, headers)
            return 200, governance.timeline(submission_id=submission_id, person_id=person_id)
        return 200, governance.timeline(submission_id=submission_id, actor_id=_staff(headers))
    if path.startswith("/submissions/") and path.endswith("/explain"):
        submission_id = path.split("/")[2]
        at = q("at")
        if headers.get("X-Participant-Token"):
            person_id = _participant(governance, headers)
            return 200, governance.explain(submission_id=submission_id, at=at, person_id=person_id)
        return 200, governance.explain(submission_id=submission_id, at=at, actor_id=_staff(headers))

    return 404, {"error": "route_not_found", "message": "接口不存在"}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    governance: GovernanceService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", ""),
                                 "X-Participant-Token": self.headers.get("X-Participant-Token", "")},
                                self.governance)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def build_services(database: Database):
    """构造基础服务与治理服务。"""

    base = DomainService(database)
    governance = GovernanceService(database)
    return base, governance


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动白塔杯参赛资格与投稿治理服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service, Handler.governance = build_services(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
