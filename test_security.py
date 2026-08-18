"""test_security.py — 本地接口安全回归测试（标准库 unittest，零依赖）。

覆盖 2026-08 安全扫描三个 medium 修复的回归：
  1. POST /api/subscription 的 source 白名单（存储型 XSS，kimi_board.py do_POST）
  2. 外部网站 Origin（kimi.com）不再获得跨源读授权（_origin_allowed）
  3. 仅回环对端可访问，GET /api/connect 已移除（_peer_ok / do_GET）

运行：python test_security.py
"""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

_TMP_HOME = tempfile.mkdtemp(prefix="kb-sec-test-")
os.environ["KIMI_CODE_HOME"] = _TMP_HOME

import kimi_board  # noqa: E402  (需先设好 KIMI_CODE_HOME 再导入)

_BASE = None


def _req(method, path, headers=None, body=None):
    """发请求，返回 (status, headers, body_text)；4xx/5xx 不抛异常。"""
    req = urllib.request.Request(_BASE + path, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


class SecurityRegression(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = kimi_board.QuietHTTPServer(("127.0.0.1", 0), kimi_board.Handler)
        global _BASE
        _BASE = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

    # ---- 修复 1：subscription source 白名单 ----

    def test_source_with_html_payload_rejected(self):
        st, _, body = _req(
            "POST",
            "/api/subscription?source=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"subscriptionBalance": {"amountUsedRatio": 0.2}}).encode(),
        )
        self.assertEqual(st, 400, body)
        self.assertNotIn("ok", body)

    def test_source_whitelist_value_accepted_and_stored(self):
        st, _, body = _req(
            "POST", "/api/subscription?source=extension",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"subscriptionBalance": {"amountUsedRatio": 0.2}}).encode(),
        )
        self.assertEqual(st, 200, body)
        self.assertEqual(json.loads(body), {"ok": True})
        st, _, body = _req("GET", "/api/settings")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["subscription"]["source"], "extension")

    def test_settings_template_escapes_source_label(self):
        # 纵深防御：设置页对未知 source 的兜底渲染必须经过 esc()
        self.assertIn("${esc(srcTxt)}", kimi_board.SETTINGS_HTML)

    # ---- 修复 2：外部网站 Origin 拒绝 ----

    def test_kimi_com_origin_rejected_without_acao(self):
        st, hdrs, _ = _req("GET", "/", headers={"Origin": "https://www.kimi.com"})
        self.assertEqual(st, 403)
        self.assertNotIn("Access-Control-Allow-Origin", hdrs)

    def test_arbitrary_origin_rejected(self):
        st, _, _ = _req("GET", "/api/stats", headers={"Origin": "https://evil.example"})
        self.assertEqual(st, 403)

    def test_webui_port_origin_still_readable(self):
        # 合法控制路径：扩展 content.js 所在的本地 WebUI 端口段仍可跨源读
        st, hdrs, _ = _req("GET", "/", headers={"Origin": "http://127.0.0.1:58627"})
        self.assertEqual(st, 200)
        self.assertEqual(hdrs.get("Access-Control-Allow-Origin"), "http://127.0.0.1:58627")

    # ---- 修复 3：对端回环校验 + 移除 GET /api/connect ----

    def test_peer_ok_accepts_loopback_only(self):
        for ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            h = kimi_board.Handler.__new__(kimi_board.Handler)
            h.client_address = (ip, 12345)
            self.assertTrue(h._peer_ok(), ip)
        h = kimi_board.Handler.__new__(kimi_board.Handler)
        h.client_address = ("203.0.113.9", 12345)
        self.assertFalse(h._peer_ok())

    def test_get_api_connect_removed(self):
        st, _, _ = _req("GET", "/api/connect")
        self.assertEqual(st, 404)

    def test_dashboard_loads_without_origin(self):
        # 合法控制路径：浏览器直接打开看板（顶层导航不带 Origin）
        st, _, body = _req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("kb-secret", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
