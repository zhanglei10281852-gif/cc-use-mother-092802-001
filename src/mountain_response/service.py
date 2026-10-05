"""离线 HTTP 服务接口（仅标准库）。

启动：
    python -m mountain_response.service --host 127.0.0.1 --port 8080 --data data/events.jsonl

全部接口见 README。服务不访问任何外部网络，事件落盘为本地 JSONL。
"""

import argparse
import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs

from .clock import SystemClock
from .contracts import ImpactReport, ReportKind, SourceReference
from .core import CoordinationService, ServiceError
from .store import EventStore


def _parse_instant(text: str, field: str) -> datetime:
    try:
        instant = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise ServiceError(400, f"{field} 不是合法 ISO 时间: {text!r}")
    if instant.tzinfo is None:
        raise ServiceError(400, f"{field} 必须携带时区信息")
    return instant


def _build_report(body: dict) -> tuple[ImpactReport, list[dict]]:
    try:
        source = body["source"]
        report = ImpactReport(
            event_key=body["event_key"],
            kind=ReportKind(body["kind"]),
            location_code=body["location_code"],
            source=SourceReference(
                agency=source["agency"],
                external_id=source["external_id"],
                observed_at=_parse_instant(source["observed_at"], "source.observed_at"),
            ),
            summary=body.get("summary", ""),
            affected_people=int(body.get("affected_people", 0)),
        )
    except KeyError as exc:
        raise ServiceError(400, f"缺少必填字段: {exc}")
    except ValueError as exc:
        raise ServiceError(400, f"字段取值非法: {exc}")
    return report, list(body.get("claims", []))


class Api:
    """把 HTTP 请求映射到 CoordinationService。"""

    def __init__(self, service: CoordinationService):
        self.svc = service
        self.routes = [
            ("GET", re.compile(r"^/health$"), self.health),
            ("POST", re.compile(r"^/reports$"), self.post_report),
            ("GET", re.compile(r"^/reports/(?P<report_id>[^/]+)$"), self.get_report),
            ("GET", re.compile(r"^/locations/(?P<code>[^/]+)/claims$"), self.get_claims),
            ("GET", re.compile(r"^/locations/(?P<code>[^/]+)/judgment$"), self.get_judgment),
            ("POST", re.compile(r"^/claims/(?P<claim_id>[^/]+)/correct$"), self.post_correct),
            ("POST", re.compile(r"^/claims/(?P<claim_id>[^/]+)/revoke$"), self.post_revoke),
            ("PUT", re.compile(r"^/sources/(?P<agency>[^/]+)$"), self.put_source),
            ("GET", re.compile(r"^/queue$"), self.get_queue),
            ("POST", re.compile(r"^/dispatches$"), self.post_dispatch),
            ("POST", re.compile(r"^/roads/(?P<code>[^/]+)/(?P<action>close|temp-open|reopen)$"), self.post_road),
            ("POST", re.compile(r"^/incidents/(?P<event_key>[^/]+)/resolve$"), self.post_resolve),
            ("GET", re.compile(r"^/events$"), self.get_events),
            ("GET", re.compile(r"^/events/verify$"), self.get_events_verify),
            ("GET", re.compile(r"^/state$"), self.get_state),
        ]

    # -------------------------------------------------------------- 各端点

    def health(self, body, query):
        return 200, {"ok": True}

    def post_report(self, body, query):
        report, claims = _build_report(body)
        return 201, self.svc.receive_report(report, claims)

    def get_report(self, body, query, report_id):
        return 200, self.svc.get_report(report_id)

    def get_claims(self, body, query, code):
        return 200, {"location_code": code, "claims": self.svc.claims_for_location(code)}

    def get_judgment(self, body, query, code):
        return 200, self.svc.judgment_for_location(code)

    def post_correct(self, body, query, claim_id):
        observed = body.get("observed_at")
        return 201, self.svc.correct_claim(
            claim_id,
            new_value=body.get("new_value", ""),
            reason=body.get("reason", ""),
            agency=body.get("agency"),
            observed_at=_parse_instant(observed, "observed_at") if observed else None,
        )

    def post_revoke(self, body, query, claim_id):
        return 200, self.svc.revoke_claim(claim_id, reason=body.get("reason", ""))

    def put_source(self, body, query, agency):
        return 200, self.svc.set_source_reliability(
            agency, float(body.get("reliability", 0.5)), note=body.get("note", "")
        )

    def get_queue(self, body, query):
        return 200, {"queue": self.svc.queue()}

    def post_dispatch(self, body, query):
        need_id = body.get("need_id")
        if not need_id:
            raise ServiceError(400, "缺少必填字段: need_id")
        return 201, self.svc.order_dispatch(
            need_id,
            resources=list(body.get("resources", [])),
            note=body.get("note", ""),
            ordered_by=body.get("ordered_by", "值班员"),
        )

    def post_road(self, body, query, code, action):
        return 201, self.svc.record_road_event(
            action, code, note=body.get("note", ""), operator=body.get("operator", "值班员")
        )

    def post_resolve(self, body, query, event_key):
        return 200, self.svc.resolve_incident(event_key, note=body.get("note", ""))

    def get_events(self, body, query):
        since = query.get("since", [None])[0]
        event_type = query.get("type", [None])[0]
        return 200, {
            "events": self.svc.events(
                event_type=event_type,
                since=_parse_instant(since, "since") if since else None,
            )
        }

    def get_events_verify(self, body, query):
        return 200, self.svc.verify_chain()

    def get_state(self, body, query):
        as_of = query.get("as_of", [None])[0]
        return 200, self.svc.state(
            as_of=_parse_instant(as_of, "as_of") if as_of else None
        )

    # -------------------------------------------------------------- 路由

    def dispatch(self, method: str, path: str, body: dict, query: dict) -> tuple[int, dict]:
        for route_method, pattern, handler in self.routes:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match:
                params = {k: unquote(v) for k, v in match.groupdict().items()}
                return handler(body, query, **params)
        raise ServiceError(404, f"接口不存在: {method} {path}")


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self, method: str):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                self._respond(400, {"error": "请求体不是合法 JSON"})
                return
            try:
                status, payload = api.dispatch(method, parsed.path, body, query)
            except ServiceError as exc:
                self._respond(exc.status, {"error": exc.message})
                return
            except Exception as exc:  # noqa: BLE001 - 服务层兜底，避免连接悬挂
                self._respond(500, {"error": f"内部错误: {exc}"})
                return
            self._respond(status, payload)

        def _respond(self, status: int, payload: dict):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def log_message(self, fmt, *args):  # 保持安静，便于值班终端阅读
            pass

    return Handler


def build_service(data_path: str | None = None) -> CoordinationService:
    return CoordinationService(EventStore(data_path), SystemClock())


def main(argv=None):
    parser = argparse.ArgumentParser(description="山地灾害协同后端（离线运行）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", default=None, help="事件落盘路径（JSONL），缺省仅内存")
    args = parser.parse_args(argv)

    api = Api(build_service(args.data))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(api))
    print(f"灾害协同服务已启动: http://{args.host}:{args.port} （数据: {args.data or '仅内存'}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
