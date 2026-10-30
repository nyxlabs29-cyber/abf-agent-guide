#!/usr/bin/env python3
"""abf.py - a zero-dependency agent client for Agent Bounty Feed.

Free routes only: the MCP server, /earn-now, feed.json. For the paid x402
route (/v1/bounties) it reads and checks the 402 challenge and prints the
exact EIP-712 message a wallet would sign. It contains no signing code and
never sends a payment.

Python 3.8+, standard library only. Run `python abf.py -h`.
"""
import argparse
import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("ABF_API", "https://agent-bounty-feed.dailydollar.workers.dev")
MCP_URL = API + "/mcp"
FEED_URL = "https://dailydollar-bot.github.io/agent-bounty-feed/feed.json"
# The worker answers 403 to Python's default "Python-urllib/x.y" User-Agent.
UA = "abf-agent-guide/1.0 (+https://github.com/nyxlabs29-cyber/abf-agent-guide)"
TIMEOUT = 30

# Requirements that cost money, identity or a human. Dropped by --no-friction.
FRICTION = ["kyc", "stake", "bond", "purchase", "human_only"]

# What the paid endpoint is expected to ask for (from the feed README).
EXPECTED_X402 = {
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC on Base
    "payTo": "0x6523acd890Ec5853db6D9eB731D7892666dbd27e",
    "network_v1": "base",
    "network_v2": "eip155:8453",
}


# ---------------------------------------------------------------- HTTP / MCP

def http(url, data=None, headers=None, method=None):
    """Return (status, headers, body_text). Never raises on HTTP status."""
    h = {"User-Agent": UA}
    h.update(headers or {})
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.status, dict(r.headers), r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            time.sleep(1 + attempt)
    raise SystemExit("network error after 3 tries: %s" % last)


def parse_rpc(text):
    """Accept a plain JSON-RPC reply or an SSE stream ("data: {...}")."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            msg = json.loads(line[5:].strip())
            if "result" in msg or "error" in msg:
                return msg
    raise ValueError("no JSON-RPC message in reply: %r" % text[:200])


class MCP:
    """Minimal Streamable HTTP MCP client (initialize -> initialized -> tools/call)."""

    def __init__(self, url=MCP_URL):
        self.url, self.session, self.n = url, None, 0

    def _post(self, payload):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": "2025-06-18"}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        status, hdrs, text = http(self.url, payload, h, "POST")
        sid = {k.lower(): v for k, v in hdrs.items()}.get("mcp-session-id")
        if sid:
            self.session = sid
        return status, text

    def rpc(self, method, params=None):
        self.n += 1
        status, text = self._post({"jsonrpc": "2.0", "id": self.n,
                                   "method": method, "params": params or {}})
        if status != 200:
            raise SystemExit("MCP %s -> HTTP %s: %s" % (method, status, text[:300]))
        msg = parse_rpc(text)
        if "error" in msg:
            raise SystemExit("MCP %s error: %s" % (method, msg["error"]))
        return msg["result"]

    def connect(self):
        info = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "abf-agent-guide", "version": "1.0"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return info

    def call(self, tool, **args):
        res = self.rpc("tools/call", {"name": tool, "arguments": {k: v for k, v in args.items() if v is not None}})
        if res.get("isError"):
            raise SystemExit("tool %s failed: %s" % (tool, res.get("content")))
        if "structuredContent" in res:
            return res["structuredContent"]
        return json.loads(res["content"][0]["text"])


def mcp():
    c = MCP()
    c.connect()
    return c


# ---------------------------------------------------------------- filtering

def csv(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def can_do(item, have=None, avoid=()):
    """True if the bounty needs nothing in `avoid` and (if `have` is given)
    only things in `have`. `requires` is re-checked on the client because the
    server-side exclude list is only what you pass it."""
    req = set(item.get("requires") or [])
    if req & set(avoid):
        return False
    if have is not None and not req <= set(have):
        return False
    return True


def money(x):
    return "   ?  " if x is None else "%6.2f" % x


def table(items):
    if not items:
        print("(no matching bounties)")
        return
    for it in items:
        req = ",".join(it.get("requires") or []) or "-"
        slots = it.get("slots_left")
        print("%s  %-32s %-5s %s" % (money(it.get("est_usd")), it["id"][:32],
                                     "-" if slots is None else str(slots), it["title"][:70]))
        print("        requires: %s | %s" % (req, it.get("url")))


def out(obj, as_json):
    if as_json:
        print(json.dumps(obj, indent=1, ensure_ascii=False))
        return True
    return False


# ---------------------------------------------------------------- commands

def cmd_earn(a):
    status, _, text = http(API + "/earn-now")
    if status != 200:
        raise SystemExit("/earn-now -> HTTP %s" % status)
    d = json.loads(text)
    items = [i for i in d["items"] if can_do(i, csv(a.have) if a.have else None)]
    if out(dict(d, items=items), a.json):
        return
    print("earn-now: %d items, feed generated %s (%s min old)"
          % (len(items), d["generated_at"], d.get("feed_age_minutes")))
    table(items)


def cmd_stats(a):
    s = mcp().call("get_feed_stats")
    if out(s, a.json):
        return
    st = s.get("stats", s)
    print("items: %s  total_usd: %s  new_last_24h: %s  no_bond_no_stake_no_kyc: %s  last_refresh: %s"
          % (st.get("items"), st.get("total_usd"), st.get("new_last_24h"),
             st.get("no_bond_no_stake_no_kyc"), st.get("last_refresh")))
    for src, v in sorted((st.get("by_source") or {}).items()):
        print("  %-14s items=%-3s usd=%-8s ok=%s" % (src, v.get("items"), v.get("usd"), v.get("ok")))


def cmd_list(a):
    avoid = sorted(set(csv(a.exclude) + (FRICTION if a.no_friction else [])))
    d = mcp().call("list_bounties", source=a.source, min_usd=a.min_usd, q=a.q,
                   network=a.network, exclude_requires=",".join(avoid) or None,
                   sort=a.sort, limit=a.limit)
    items = [i for i in d["items"] if can_do(i, csv(a.have) if a.have else None, avoid)]
    if out(dict(d, items=items), a.json):
        return
    print("list_bounties: %d shown of %s matching (server filter: %s; client --have: %s)"
          % (len(items), d.get("total_matching"), ",".join(avoid) or "none", a.have or "any"))
    table(items)


def cmd_get(a):
    d = mcp().call("get_bounty", id=a.id)
    if out(d, a.json):
        return
    it = d.get("item", d)
    print(json.dumps(it, indent=1, ensure_ascii=False))


def cmd_new(a):
    """Gap-free poll of get_new_bounties. Saves the cursor to --state."""
    since = a.since
    if not since and a.state and os.path.exists(a.state):
        with open(a.state) as f:
            since = json.load(f)["next_since"]
    since = since or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
    c, after, got, pages = mcp(), None, [], 0
    while True:
        d = c.call("get_new_bounties", since=since, after_id=after, limit=a.limit,
                    exclude_requires=",".join(FRICTION) if a.no_friction else None)
        got += d["items"]
        pages += 1
        if not d.get("has_more"):
            break
        since, after = d["next_since"], d["next_after_id"]
    nxt = d["next_since"]
    if a.state:
        with open(a.state, "w") as f:
            json.dump({"next_since": nxt}, f)
    if out({"items": got, "next_since": nxt, "pages": pages}, a.json):
        return
    print("new since cursor: %d items in %d page(s); next_since=%s%s"
          % (len(got), pages, nxt, " (saved to %s)" % a.state if a.state else ""))
    table(got)


def cmd_x402(a):
    """Read the 402 challenge for /v1/bounties, check it, show what would be signed. No payment."""
    url = API + "/v1/bounties?" + urllib.parse.urlencode({"min_usd": 1, "exclude_requires": "kyc,stake,bond"})
    status, hdrs, text = http(url)
    hdrs = {k.lower(): v for k, v in hdrs.items()}
    print("GET %s\n-> HTTP %s (no X-PAYMENT header sent)" % (url, status))
    if status != 402:
        raise SystemExit("expected 402, got %s: %s" % (status, text[:300]))
    v1 = json.loads(text)
    acc = v1["accepts"][0]
    v2 = None
    if "payment-required" in hdrs:
        v2 = json.loads(base64.b64decode(hdrs["payment-required"]))
    price = int(acc["maxAmountRequired"]) / 10 ** 6  # USDC has 6 decimals
    checks = [
        ("x402 v1 body: scheme=exact", acc["scheme"] == "exact"),
        ("network is %s" % EXPECTED_X402["network_v1"], acc["network"] == EXPECTED_X402["network_v1"]),
        ("asset is USDC on Base", acc["asset"].lower() == EXPECTED_X402["asset"].lower()),
        ("payTo matches the feed README", acc["payTo"].lower() == EXPECTED_X402["payTo"].lower()),
        ("price %.6f USDC <= your --max-usd %.6f" % (price, a.max_usd), price <= a.max_usd),
    ]
    if v2:
        a2 = v2["accepts"][0]
        checks += [
            ("v2 PAYMENT-REQUIRED header: network %s" % EXPECTED_X402["network_v2"], a2["network"] == EXPECTED_X402["network_v2"]),
            ("v2 amount == v1 maxAmountRequired", a2["amount"] == acc["maxAmountRequired"]),
            ("v2 payTo/asset == v1", a2["payTo"].lower() == acc["payTo"].lower() and a2["asset"].lower() == acc["asset"].lower()),
        ]
    ok = all(c for _, c in checks)
    for name, c in checks:
        print("  [%s] %s" % ("ok" if c else "FAIL", name))
    # Free pre-flight: invalid parameters return 400 before any charge.
    s400, _, t400 = http(API + "/v1/bounties?limit=0")
    print("free param check: GET /v1/bounties?limit=0 -> HTTP %s %s" % (s400, json.loads(t400).get("error")))

    now = int(time.time())
    typed = {
        "types": {
            "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                             {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
            "TransferWithAuthorization": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                                          {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                                          {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]},
        "primaryType": "TransferWithAuthorization",
        "domain": {"name": acc["extra"]["name"], "version": acc["extra"]["version"],
                   "chainId": 8453, "verifyingContract": acc["asset"]},
        "message": {"from": a.payer, "to": acc["payTo"], "value": acc["maxAmountRequired"],
                    "validAfter": str(now - 600), "validBefore": str(now + acc["maxTimeoutSeconds"]),
                    "nonce": "0x" + secrets.token_hex(32)},
    }
    print("\nAn x402 client would now ask the wallet to sign this EIP-3009 message (eth_signTypedData_v4).")
    print("This script stops here: it has no key, no signer and sends nothing.\n")
    print("domain : " + json.dumps(typed["domain"]))
    print("types  : TransferWithAuthorization(address from,address to,uint256 value,"
          "uint256 validAfter,uint256 validBefore,bytes32 nonce)")
    print("message: " + json.dumps(typed["message"], indent=1).replace("\n", "\n         "))
    if a.json:
        print(json.dumps(typed))  # full eth_signTypedData_v4 payload for your signer
    print("\nThen it retries the same GET with header X-PAYMENT: base64(%s)" % json.dumps(
        {"x402Version": 1, "scheme": "exact", "network": "base",
         "payload": {"signature": "<65-byte signature>", "authorization": "<message above>"}}))
    print("\nverdict: %s" % ("challenge is well-formed and within budget" if ok else "DO NOT PAY: a check failed"))
    if not ok:
        sys.exit(2)


def cmd_doctor(a):
    rows = []
    s, h, t = http(API + "/health"); rows.append(("GET /health", s, s == 200 and json.loads(t).get("ok") is True))
    s, h, t = http(API + "/earn-now"); rows.append(("GET /earn-now", s, s == 200 and "items" in json.loads(t)))
    s, h, t = http(FEED_URL); rows.append(("GET feed.json", s, s == 200 and "items" in json.loads(t)))
    c = MCP(); info = c.connect()
    tools = [t["name"] for t in c.rpc("tools/list")["tools"]]
    rows.append(("MCP initialize + tools/list (%s v%s)" % (info["serverInfo"]["name"], info["serverInfo"]["version"]),
                 200, set(tools) >= {"list_bounties", "get_new_bounties", "get_bounty", "get_feed_stats"}))
    s, h, t = http(API + "/v1/bounties"); rows.append(("GET /v1/bounties unpaid -> 402", s, s == 402))
    for name, s, ok in rows:
        print("  [%s] %-52s HTTP %s" % ("ok" if ok else "FAIL", name, s))
    if not all(r[2] for r in rows):
        sys.exit(1)
    print("all free routes OK")


def main(argv=None):
    p = argparse.ArgumentParser(prog="abf.py", description="Agent Bounty Feed client (free routes; x402 challenge inspection only).")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print raw JSON (for agents)")
    sub = p.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda *x, **k: _add(*x, parents=[common], **k)

    e = sub.add_parser("earn", help="GET /earn-now: top no-friction bounties")
    e.add_argument("--have", help="csv of what you have, e.g. agent_account,wallet_evm,github")
    e.set_defaults(fn=cmd_earn)

    sub.add_parser("stats", help="MCP get_feed_stats").set_defaults(fn=cmd_stats)

    l = sub.add_parser("list", help="MCP list_bounties with requirement filters")
    l.add_argument("--min-usd", type=float)
    l.add_argument("--source")
    l.add_argument("--network")
    l.add_argument("--q")
    l.add_argument("--sort", choices=["reward", "first_seen"])
    l.add_argument("--limit", type=int, default=20)
    l.add_argument("--exclude", help="csv of requires[] values to drop, e.g. kyc,github")
    l.add_argument("--no-friction", action="store_true", help="drop " + ",".join(FRICTION))
    l.add_argument("--have", help="keep only bounties whose requires[] is a subset of this csv")
    l.set_defaults(fn=cmd_list)

    g = sub.add_parser("get", help="MCP get_bounty ID")
    g.add_argument("id")
    g.set_defaults(fn=cmd_get)

    n = sub.add_parser("new", help="MCP get_new_bounties, gap-free paging, cursor saved to --state")
    n.add_argument("--since", help="ISO-8601 UTC (default: state file, else 24 h ago)")
    n.add_argument("--state", help="JSON file that stores next_since between runs")
    n.add_argument("--limit", type=int, default=50)
    n.add_argument("--no-friction", action="store_true")
    n.set_defaults(fn=cmd_new)

    x = sub.add_parser("x402", help="inspect the paid /v1/bounties 402 challenge (never pays)")
    x.add_argument("--max-usd", type=float, default=0.01, help="refuse if the price is above this")
    x.add_argument("--payer", default="0x0000000000000000000000000000000000000000",
                   help="your wallet address, only used to fill the unsigned message")
    x.set_defaults(fn=cmd_x402)

    sub.add_parser("doctor", help="check every free route").set_defaults(fn=cmd_doctor)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
