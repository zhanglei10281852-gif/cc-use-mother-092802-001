"""HTTP 接口端到端验证 + SQLite 落盘重启验证（无第三方依赖）。"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mountain_response.http_api import create_server
from mountain_response.service import Service
from mountain_response.storage import Store
from mountain_response.timeutil import iso

T0 = datetime(2026, 9, 27, 4, 0, tzinfo=timezone.utc)
EV = "EV-HTTP-1"


class HttpSession:
    def __init__(self, server):
        self.server = server
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(self, method, path, body=None, expect=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status, payload = resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            status, payload = e.code, json.loads(e.read().decode("utf-8"))
        if expect is not None:
            assert status == expect, f"{method} {path}: 期望 {expect}，实际 {status}：{payload}"
        return status, payload


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server("127.0.0.1", 0, ":memory:")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.http = HttpSession(cls.server)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.svc.close()
        cls.server.server_close()

    def test_01_full_flow_over_http(self):
        h = self.http
        status, body = h.call("GET", "/healthz")
        self.assertEqual(body["status"], "ok")

        # 来源分级
        h.call("POST", "/v1/sources/trust",
               {"agency": "县应急局", "tier": "official"}, 201)
        h.call("POST", "/v1/facilities",
               {"event_key": EV, "facility": "W1", "location_code": "V1",
                "critical": True}, 201)

        report = {
            "event_key": EV, "kind": "people", "location_code": "V1",
            "agency": "县应急局", "external_id": "r1",
            "observed_at": iso(T0), "summary": "120人受困",
            "affected_people": 120, "severity": "critical"}
        s, first = h.call("POST", "/v1/reports", report, 201)
        self.assertFalse(first["duplicated"])
        s, again = h.call("POST", "/v1/reports", report, 201)
        self.assertTrue(again["duplicated"])
        self.assertEqual(first["report_id"], again["report_id"])

        # 队列可解释
        s, assess = h.call("GET", f"/v1/events/{EV}/assessment")
        item = assess["queue"][0]
        self.assertIn(item["level"], ("P1", "P2"))
        self.assertTrue(item["components"])

        # 道路封闭 + 重复封闭幂等
        road_req = {"road_code": "R4", "action": "road.close", "reason": "冰体掩埋"}
        s, c1 = h.call("POST", f"/v1/events/{EV}/roads", road_req, 200)
        self.assertFalse(c1["idempotent"])
        s, c2 = h.call("POST", f"/v1/events/{EV}/roads", road_req, 200)
        self.assertTrue(c2["idempotent"])

        # 调派 + 重复派遣 409
        disp = {"unit": "搜救一队", "target": "V1", "purpose": "人员转移"}
        h.call("POST", f"/v1/events/{EV}/dispatches", disp, 201)
        s, err = h.call("POST", f"/v1/events/{EV}/dispatches",
                        {"unit": "搜救二队", "target": "V1", "purpose": "人员转移"})
        self.assertEqual(s, 409)
        self.assertEqual(err["error"]["code"], "conflict")

        # 非法报文 400
        s, bad = h.call("POST", "/v1/reports", {"event_key": EV, "kind": "road"})
        self.assertEqual(s, 400)
        self.assertEqual(bad["error"]["code"], "invalid_request")

        # 决策链
        s, chain = h.call("GET", f"/v1/events/{EV}/chain")
        actions = [d["action"] for d in chain["timeline"]]
        self.assertIn("road.close", actions)
        self.assertIn("dispatch.create", actions)

        # 原始报文留痕（含重复）
        s, raw = h.call("GET", f"/v1/events/{EV}/raw")
        self.assertEqual(len(raw["messages"]), 2)

    def test_02_resolve_and_reopen_over_http(self):
        h = self.http
        # 尚有 P1/P2 → 解除被拒
        s, err = h.call("POST", f"/v1/events/{EV}/resolve", {})
        self.assertEqual(s, 400)
        # 官方更正为无人受困
        h.call("POST", "/v1/reports", {
            "event_key": EV, "kind": "people", "location_code": "V1",
            "agency": "县应急局", "external_id": "r2",
            "observed_at": iso(T0 + timedelta(hours=4)),
            "summary": "核实无人受困", "affected_people": 0,
            "severity": "info", "corrects": "r1"}, 201)
        # 道路恢复开放后才能解除
        h.call("POST", f"/v1/events/{EV}/roads",
               {"road_code": "R4", "action": "road.reopen", "reason": "清理完毕"}, 200)
        h.call("POST", "/v1/dispatches/update",
               {"event_key": EV, "dispatch_code": f"D-{EV}-001",
                "action": "dispatch.standdown", "reason": "结束"}, 200)
        s, resolved = h.call("POST", f"/v1/events/{EV}/resolve",
                             {"actor": "指挥员", "reason": "全部恢复"}, 201)
        import json as _json
        basis = resolved["decision"].get("basis") or _json.loads(
            resolved["decision"]["basis_json"])
        self.assertIn("final_snapshot", basis)

        # 解除后新消息自动重开
        s, ing = h.call("POST", "/v1/reports", {
            "event_key": EV, "kind": "road", "location_code": "V1",
            "agency": "县应急局", "external_id": "r3",
            "observed_at": iso(T0 + timedelta(hours=6)),
            "summary": "支沟再次滑塌", "road_code": "R4", "road_state": "closed"}, 201)
        self.assertTrue(ing["event_reopened"])
        s, chain = h.call("GET", f"/v1/events/{EV}/chain")
        actions = [d["action"] for d in chain["timeline"]]
        self.assertEqual(actions.count("event.resolve"), 1)
        self.assertEqual(actions.count("event.reopen"), 1)


class PersistenceTests(unittest.TestCase):
    def test_decisions_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "coord.db")
            svc = Service(Store(db))
            svc.set_source_trust("应急局", "official")
            svc.ingest({
                "event_key": "PERSIST", "kind": "people", "location_code": "V1",
                "agency": "应急局", "external_id": "x1",
                "observed_at": iso(T0), "summary": "30人",
                "affected_people": 30, "severity": "major"})
            svc.road_action({"event_key": "PERSIST", "road_code": "R9",
                             "action": "road.close", "reason": "测试"})
            svc.close()

            # 用全新进程式对象重新打开同一文件
            svc2 = Service(Store(db))
            self.assertEqual(svc2.event("PERSIST")["status"], "active")
            chain = svc2.chain("PERSIST")
            self.assertEqual(chain["timeline"][0]["action"], "road.close")
            self.assertEqual(chain["timeline"][0]["basis"]["totals"]["locations"], 1)
            self.assertEqual(len(svc2.reports("PERSIST")), 1)
            svc2.close()


if __name__ == "__main__":
    unittest.main()
