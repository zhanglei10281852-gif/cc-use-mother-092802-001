"""离线 HTTP 接口（仅标准库 http.server，零第三方依赖）。

路由：
  POST /v1/reports                     接报（自动去重/更正/撤销）
  GET  /v1/events/{key}/assessment     当前判断 + 处置队列（可解释）
  POST /v1/events/{key}/roads          道路封闭/恢复/临时开放
  POST /v1/events/{key}/dispatches     调派
  POST /v1/dispatches/update           到达/转用/收队
  POST /v1/events/{key}/resolve        解除（?force=true 强制）
  POST /v1/events/{key}/reopen         手动重开
  GET  /v1/events/{key}/chain          决策事件链（含每步依据快照）
  GET  /v1/events/{key}/reports        所有原始说法（含矛盾/已撤销）
  GET  /v1/events/{key}/raw            每次到达的原文（含重复转发）
  POST /v1/facilities                  登记关键设施
  POST /v1/sources/trust               设定来源分级
  GET  /v1/events/{key}                事件状态
  GET  /healthz
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from . import operations
from .ingest import IngestError
from .service import Service
from .storage import Conflict, NotFound, Store
from .timeutil import iso, now_utc


class _HttpError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class _Handler(BaseHTTPRequestHandler):
    server_version = "DisasterCoord/1.0"

    def log_message(self, fmt, *args):  # 静默常规访问日志
        return

    # ---- 工具 ---------------------------------------------------------------

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False, default=iso).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str):
        self._json({"error": {"code": code, "message": message}}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise _HttpError(400, "bad_json", f"请求体不是合法 JSON: {exc}")
        if not isinstance(data, dict):
            raise _HttpError(400, "bad_request", "请求体必须是 JSON 对象")
        return data

    @property
    def svc(self) -> Service:
        return self.server.svc  # type: ignore[attr-defined]

    # ---- 路由 ---------------------------------------------------------------

    def do_GET(self):
        try:
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            query = parse_qs(urlsplit(self.path).query)
            if parts == ["healthz"]:
                return self._json({"status": "ok", "time": iso(now_utc())})
            if len(parts) == 4 and parts[:2] == ["v1", "events"] and parts[3] == "assessment":
                return self._json(self.svc.assessment(
                    parts[2], query.get("as_of", [None])[0]))
            if len(parts) == 4 and parts[:2] == ["v1", "events"] and parts[3] == "chain":
                return self._json(self.svc.chain(parts[2]))
            if len(parts) == 4 and parts[:2] == ["v1", "events"] and parts[3] == "reports":
                return self._json({"reports": self.svc.reports(parts[2])})
            if len(parts) == 4 and parts[:2] == ["v1", "events"] and parts[3] == "raw":
                return self._json({"messages": self.svc.raw_messages(parts[2])})
            if len(parts) == 3 and parts[:2] == ["v1", "events"]:
                return self._json(self.svc.event(parts[2]))
            raise _HttpError(404, "not_found", "未知路径")
        except _HttpError as e:
            return self._error(e.status, e.code, e.message)
        except KeyError as e:
            return self._error(404, "not_found", f"事件不存在: {e.args[0]}")
        except Exception as exc:  # 兜底，防止线程崩溃无响应
            return self._error(500, "internal", f"{type(exc).__name__}: {exc}")

    def do_POST(self):
        try:
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            query = parse_qs(urlsplit(self.path).query)
            body = self._body()

            if parts == ["v1", "reports"]:
                return self._json(self.svc.ingest(body), 201)
            if parts == ["v1", "facilities"]:
                row = self.svc.register_facility(
                    body["event_key"], body["facility"], body["location_code"],
                    critical=bool(body.get("critical", True)),
                    kind=body.get("kind", "water"))
                return self._json(row, 201)
            if parts == ["v1", "sources", "trust"]:
                row = self.svc.set_source_trust(
                    body["agency"], body["tier"], body.get("note"))
                return self._json(row, 201)
            if parts == ["v1", "dispatches", "update"]:
                return self._json(self.svc.dispatch_update(body))
            if len(parts) == 4 and parts[1] == "events" and parts[3] == "roads":
                body.setdefault("event_key", parts[2])
                return self._json(self.svc.road_action(body))
            if len(parts) == 4 and parts[1] == "events" and parts[3] == "dispatches":
                body.setdefault("event_key", parts[2])
                return self._json(self.svc.dispatch(body), 201)
            if len(parts) == 4 and parts[1] == "events" and parts[3] == "resolve":
                result = self.svc.resolve(
                    parts[2], actor=body.get("actor", "commander"),
                    reason=body.get("reason"), at=body.get("at"),
                    force=query.get("force", ["false"])[0].lower() == "true")
                return self._json(result, 200 if result["idempotent"] else 201)
            if len(parts) == 4 and parts[1] == "events" and parts[3] == "reopen":
                self.svc.store.ensure_event(parts[2])
                self.svc.store.reopen_event(parts[2], iso(now_utc()))
                d = self.svc.store.add_decision(
                    parts[2], "event", parts[2], "event.reopen",
                    body.get("actor", "commander"),
                    basis={"manual": True}, reason=body.get("reason"))
                return self._json({"decision": d}, 201)
            raise _HttpError(404, "not_found", "未知路径")
        except _HttpError as e:
            return self._error(e.status, e.code, e.message)
        except KeyError as e:
            return self._error(400, "bad_request", f"缺少字段: {e.args[0]}")
        except Conflict as e:
            return self._error(409, "conflict", str(e))
        except (IngestError, operations.OperationError, ValueError) as e:
            return self._error(400, "invalid_request", str(e))
        except NotFound as e:
            return self._error(404, "not_found", str(e))
        except Exception as exc:
            return self._error(500, "internal", f"{type(exc).__name__}: {exc}")


def create_server(host: str = "127.0.0.1", port: int = 8080,
                  db_path: str = ":memory:") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.svc = Service(Store(db_path))  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="灾害协同后端（离线）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="data/disaster.db",
                        help="SQLite 文件路径（默认 data/disaster.db，可 :memory:）")
    args = parser.parse_args(argv)

    server = create_server(args.host, args.port, args.db)
    print(f"灾害协同后端已启动: http://{args.host}:{args.port}  db={args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n关闭中…")
    finally:
        server.svc.close()
        server.server_close()


if __name__ == "__main__":
    main()
