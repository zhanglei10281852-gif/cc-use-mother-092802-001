"""HTTP 接口：真实起本地服务（127.0.0.1，随机端口），离线可用。"""

import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from urllib.parse import quote

from helpers import make_service
from mountain_response.service import Api, make_handler


class ServiceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        svc, _ = make_service()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Api(svc)))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    def test_full_lifecycle_over_http(self):
        report_body = {
            "event_key": "ev-http",
            "kind": "road",
            "location_code": "R-9",
            "source": {"agency": "巡查组", "external_id": "h-1",
                       "observed_at": "2026-10-05T04:00:00+00:00"},
            "summary": "冰崩掩埋路段",
            "affected_people": 6,
            "claims": [{"aspect": "road_status", "value": "blocked"}],
        }
        status, created = self.call("POST", "/reports", report_body)
        self.assertEqual(status, 201)
        self.assertFalse(created["deduplicated"])

        # 重复报送 → 幂等
        status, dup = self.call("POST", "/reports", report_body)
        self.assertTrue(dup["deduplicated"])

        # 队列出现该需求
        status, queue = self.call("GET", "/queue")
        item = next(i for i in queue["queue"] if i["location_code"] == "R-9")
        self.assertIn("explanation", item)

        # 派遣 → 幂等
        status, d1 = self.call("POST", "/dispatches",
                               {"need_id": item["need_id"], "resources": ["救援队A"]})
        self.assertEqual(status, 201)
        _, d2 = self.call("POST", "/dispatches", {"need_id": item["need_id"]})
        self.assertTrue(d2["deduplicated"])

        # 道路封闭 → 事件链 → 校验
        status, _ = self.call("POST", f"/roads/{quote('R-9')}/close", {"note": "封闭"})
        self.assertEqual(status, 201)
        _, chain = self.call("GET", "/events/verify")
        self.assertTrue(chain["ok"])

        # 历史回放接口可用
        _, state = self.call("GET", "/state?as_of=2026-10-05T04:00:00%2B00:00")
        self.assertTrue(state["is_historical"])

        # 解除
        status, resolved = self.call("POST", "/incidents/ev-http/resolve", {"note": "完毕"})
        self.assertTrue(resolved["resolved"])

    def test_error_mapping(self):
        status, body = self.call("GET", f"/reports/{quote('rpt-不存在')}")
        self.assertEqual(status, 404)
        self.assertIn("error", body)
        status, body = self.call("POST", "/reports", {"kind": "road"})
        self.assertEqual(status, 400)
        status, body = self.call("GET", "/no-such-route")
        self.assertEqual(status, 404)

    def test_chinese_path_params(self):
        status, body = self.call("PUT", f"/sources/{quote('边境巡查组')}",
                                 {"reliability": 0.7, "note": "调整"})
        self.assertEqual(status, 200)
        self.assertEqual(body["agency"], "边境巡查组")


if __name__ == "__main__":
    unittest.main()
