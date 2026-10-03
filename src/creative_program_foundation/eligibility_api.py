"""参赛资格与投稿治理模块的 HTTP/JSON 边界。

路由总览（X-Actor-Id 为内部账号 id 或参与者令牌 ``person:<id>`` / ``org:<id>``）：

- 建档：POST /eg/persons、/eg/orgs、/eg/relationships、/eg/subjects、
  /eg/subject-links、/eg/subject-merges、/eg/teams、/eg/team-member-changes
- 赛道窗口：POST /eg/tracks、/eg/windows、/eg/windows/freeze
- 投稿：POST /eg/submissions；POST /eg/submissions/<id>/correction|withdraw|transfer
- 评审：POST /eg/review-correction、/eg/review-decision；GET /eg/review-tasks
- 申诉：POST /eg/appeals、/eg/appeal-decision；GET /eg/appeals
- 查询：GET /eg/submissions、/eg/submissions/<id>、
  /eg/submissions/<id>/explain?at=<ISO>、/eg/conflicts?window_id=<id>
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import api as foundation_api
from .eligibility_service import EligibilityService
from .errors import DomainError, EligibilityConflictError, ValidationError
from .service import DomainService
from .storage import Database


def _result(receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def route_eligibility(service: EligibilityService, method: str, path: str,
                      body: dict[str, Any] | None,
                      headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]] | None:
    """命中治理路由时返回响应；未命中返回 None 以便回退到基础服务路由。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    segments = [s for s in parsed.path.split("/") if s]
    query = parse_qs(parsed.query)
    actor = headers.get("X-Actor-Id", "")

    try:
        if not segments or segments[0] != "eg":
            return None

        # ---------- 建档 ----------
        if method == "POST" and parsed.path == "/eg/persons":
            return _result(service.register_person(actor_id=actor, **body))
        if method == "POST" and parsed.path == "/eg/orgs":
            return _result(service.register_external_org(actor_id=actor, **body))
        if method == "POST" and parsed.path == "/eg/relationships":
            return _result(service.add_relationship(actor_id=actor, **body))
        if method == "POST" and parsed.path == "/eg/subjects":
            return _result(service.register_subject(actor_id=actor, **body))
        if method == "POST" and parsed.path == "/eg/subject-links":
            return _result(service.link_subject_party(actor_id=actor, **body))
        if method == "POST" and parsed.path == "/eg/subject-merges":
            return _result(service.merge_subjects(actor_id=actor, **body))
        if method == "POST" and parsed.path == "/eg/teams":
            return _result(service.register_team(actor_id=actor, **body))
        if method == "POST" and parsed.path == "/eg/team-member-changes":
            return _result(service.change_team_members(actor_id=actor, **body))

        # ---------- 赛道与窗口 ----------
        if method == "POST" and parsed.path == "/eg/tracks":
            return _result(service.register_track(actor_id=actor, **body))
        if method == "POST" and parsed.path == "/eg/windows":
            return _result(service.register_window(actor_id=actor, **body))
        if method == "POST" and parsed.path == "/eg/windows/freeze":
            return _result(service.freeze_window(actor_id=actor, **body))

        # ---------- 投稿 ----------
        if method == "POST" and parsed.path == "/eg/submissions":
            return _result(service.submit(actor_id=actor, **body))
        if method == "POST" and len(segments) == 4 and segments[1] == "submissions":
            sub_id, action = segments[2], segments[3]
            if action == "correction":
                return _result(service.correct(actor_id=actor, submission_id=sub_id, **body))
            if action == "withdraw":
                return _result(service.withdraw(actor_id=actor, submission_id=sub_id, **body))
            if action == "transfer":
                return _result(service.transfer(actor_id=actor, submission_id=sub_id, **body))
            raise ValidationError(f"未知投稿动作 {action}")

        # ---------- 评审 ----------
        if method == "POST" and parsed.path == "/eg/review-correction":
            return _result(service.request_correction(actor_id=actor, **body))
        if method == "POST" and parsed.path == "/eg/review-decision":
            return _result(service.decide(actor_id=actor, **body))
        if method == "GET" and parsed.path == "/eg/review-tasks":
            status = query.get("status", [None])[0]
            return 200, {"items": service.review_tasks(actor, status)}

        # ---------- 申诉 ----------
        if method == "POST" and parsed.path == "/eg/appeals":
            return _result(service.file_appeal(actor_id=actor, **body))
        if method == "POST" and parsed.path == "/eg/appeal-decision":
            return _result(service.decide_appeal(actor_id=actor, **body))
        if method == "GET" and parsed.path == "/eg/appeals":
            status = query.get("status", [None])[0]
            return 200, {"items": service.list_appeals(actor, status)}

        # ---------- 查询 ----------
        if method == "GET" and parsed.path == "/eg/submissions":
            return 200, service.list_submissions(
                actor, window_id=query.get("window_id", [None])[0],
                status_filter=query.get("status", [None])[0])
        if method == "GET" and len(segments) == 3 and segments[1] == "submissions":
            return 200, service.get_submission(actor, segments[2])
        if method == "GET" and len(segments) == 4 and segments[1] == "submissions" \
                and segments[3] == "explain":
            at = query.get("at", [""])[0]
            if not at:
                raise ValidationError("at 查询参数必填（ISO 8601 时间）")
            return 200, service.explain(actor, segments[2], at)
        if method == "GET" and parsed.path == "/eg/conflicts":
            window_id = query.get("window_id", [""])[0]
            if not window_id:
                raise ValidationError("window_id 不能为空")
            return 200, service.list_conflicts(actor, window_id)

        return 404, {"error": "route_not_found", "message": "治理接口不存在"}
    except EligibilityConflictError as exc:
        return exc.status, {"error": exc.code, "message": str(exc),
                            "conflicts": exc.conflicts}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except TypeError as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """先派发到治理路由，未命中再回退到基础服务路由。"""

    eligibility: EligibilityService
    foundation: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        header_map = {"X-Actor-Id": self.headers.get("X-Actor-Id", "")}
        response = route_eligibility(self.eligibility, self.command, self.path, body, header_map)
        if response is None:
            try:
                response = foundation_api.route(self.foundation, self.command, self.path, body,
                                                header_map)
            except DomainError as exc:
                response = exc.status, {"error": exc.code, "message": str(exc)}
            except (TypeError, ValueError) as exc:
                response = 400, {"error": "invalid_request", "message": str(exc)}
        self._write(response[0], response[1])

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


def main() -> int:
    parser = argparse.ArgumentParser(description="启动文化创意赛事参赛资格与投稿治理服务")
    parser.add_argument("--database", default="eligibility.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.foundation = DomainService(database)
    Handler.eligibility = EligibilityService(database)
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
