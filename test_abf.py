"""Offline tests for abf.py (no network). Run: python -m unittest -v test_abf"""
import base64
import io
import json
import unittest
from contextlib import redirect_stdout

import abf

ACCEPT_V1 = {"scheme": "exact", "network": "base", "maxAmountRequired": "10000",
             "payTo": "0x6523acd890Ec5853db6D9eB731D7892666dbd27e", "maxTimeoutSeconds": 120,
             "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
             "extra": {"name": "USD Coin", "version": "2"}}
ACCEPT_V2 = {"scheme": "exact", "network": "eip155:8453", "amount": "10000",
             "payTo": ACCEPT_V1["payTo"], "asset": ACCEPT_V1["asset"]}


def fake_http(challenge_v1, challenge_v2):
    def http(url, data=None, headers=None, method=None):
        if "limit=0" in url:
            return 400, {}, json.dumps({"error": "bad request (not charged)"})
        hdr = {"PAYMENT-REQUIRED": base64.b64encode(json.dumps({"x402Version": 2, "accepts": [challenge_v2]}).encode()).decode()}
        return 402, hdr, json.dumps({"x402Version": 1, "accepts": [challenge_v1]})
    return http


class Filters(unittest.TestCase):
    def test_avoid(self):
        self.assertFalse(abf.can_do({"requires": ["agent_account", "purchase"]}, avoid=abf.FRICTION))
        self.assertTrue(abf.can_do({"requires": ["agent_account"]}, avoid=abf.FRICTION))

    def test_have_is_subset(self):
        have = ["agent_account", "wallet_evm"]
        self.assertTrue(abf.can_do({"requires": ["wallet_evm"]}, have))
        self.assertFalse(abf.can_do({"requires": ["wallet_evm", "github"]}, have))
        self.assertTrue(abf.can_do({"requires": []}, have))
        self.assertTrue(abf.can_do({}, None))


class RPC(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(abf.parse_rpc('{"jsonrpc":"2.0","id":1,"result":{"a":1}}')["result"], {"a": 1})

    def test_sse(self):
        sse = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
        self.assertTrue(abf.parse_rpc(sse)["result"]["ok"])


class X402(unittest.TestCase):
    def run_x402(self, v1, v2, max_usd=0.01):
        old = abf.http
        abf.http = fake_http(v1, v2)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                abf.main(["x402", "--max-usd", str(max_usd)])
            code = 0
        except SystemExit as e:
            code = e.code
        finally:
            abf.http = old
        return code, buf.getvalue()

    def test_good_challenge(self):
        code, text = self.run_x402(ACCEPT_V1, ACCEPT_V2)
        self.assertIn(code, (0, None))
        self.assertIn("well-formed and within budget", text)
        self.assertIn('"value": "10000"', text)

    def test_over_budget_refused(self):
        code, text = self.run_x402(ACCEPT_V1, ACCEPT_V2, max_usd=0.001)
        self.assertEqual(code, 2)
        self.assertIn("DO NOT PAY", text)

    def test_wrong_payto_refused(self):
        bad = dict(ACCEPT_V1, payTo="0x000000000000000000000000000000000000dEaD")
        code, text = self.run_x402(bad, ACCEPT_V2)
        self.assertEqual(code, 2)

    def test_v1_v2_mismatch_refused(self):
        code, text = self.run_x402(ACCEPT_V1, dict(ACCEPT_V2, amount="1000000"))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
