#!/usr/bin/env python3
"""四机并发 MemOS + Hindsight 测试
测试两系统在高并发下的：成功率、P50/P95/P99 延迟

用法:
  python3 concurrent-memos-hindsight.py [rounds] [--memos-only|--hindsight-only]
  默认 20 轮
"""

import sys, json, time, urllib.request, urllib.error, statistics, random, string, os, socket

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 20
FILTER = sys.argv[2] if len(sys.argv) > 2 else "all"

MEMOS_URL = os.environ.get("MEMOS_URL", "http://127.0.0.1:8765")
HS_URL    = os.environ.get("HINDSIGHT_URL", "http://127.0.0.1:9077")

def rand_id():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

def api_call(method, url, data=None, headers=None, timeout=15):
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed = int((time.monotonic() - start) * 1000)
            return {"ok": True, "status": resp.status,
                    "body": resp.read().decode(), "ms": elapsed}
    except urllib.error.HTTPError as e:
        elapsed = int((time.monotonic() - start) * 1000)
        return {"ok": False, "error": f"HTTP {e.code}", "ms": elapsed,
                "body": e.read().decode()[:200] if e.fp else ""}
    except Exception as e:
        elapsed = int((time.monotonic() - start) * 1000)
        return {"ok": False, "error": str(e)[:100], "ms": elapsed}

# ── MemOS ──────────────────────────────────────────────────────────────────
def memos_add(tag, content):
    r = api_call("POST", f"{MEMOS_URL}/product/add",
                 {"messages":[{"role":"user","content":content}],
                  "async_mode":"sync","user_id":"concurrent_test"},
                 timeout=30)
    ok = r["ok"] and r.get("body","") and '"code":200' in r["body"]
    return {"op":"add", "ok": ok, "ms": r["ms"], "error": r.get("error")}

def memos_search(tag):
    r = api_call("POST", f"{MEMOS_URL}/product/search",
                 {"query": tag, "user_id": "concurrent_test",
                  "relativity": 0.3, "search_memory_type": "LongTermMemory", "top_k": 5},
                 timeout=60)
    ok = r["ok"] and r.get("body","")
    return {"op":"search", "ok": ok, "ms": r["ms"], "error": r.get("error")}

# ── Hindsight ──────────────────────────────────────────────────────────────
def hs_retain(tag, content):
    r = api_call("POST", f"{HS_URL}/mcp",
                 {"jsonrpc":"2.0","id":1,"method":"tools/call",
                  "params":{"name":"retain","arguments":{"content":content,
                                                        "metadata":{"source":"concurrent"}}}},
                 timeout=60)
    return {"op":"retain", "ok": r["ok"] and b"result" in r.get("body","").encode(), "ms": r["ms"], "error": r.get("error")}

def hs_recall(tag):
    r = api_call("POST", f"{HS_URL}/mcp",
                 {"jsonrpc":"2.0","id":1,"method":"tools/call",
                  "params":{"name":"recall","arguments":{"query":tag,"budget":"mid"}}},
                 timeout=60)
    body = r.get("body","")
    ok = r["ok"]
    if ok and body:
        for line in body.split("\n"):
            if line.startswith("data: "):
                try:
                    d = json.loads(line[6:])
                    if d.get("result"):
                        return {"op":"recall","ok":True,"ms":r["ms"]}
                except: pass
    return {"op":"recall","ok":False,"ms":r["ms"],"error":"no result"}

# ── 测试执行 ───────────────────────────────────────────────────────────────
machine_name = socket.gethostname()
results = {"machine": machine_name, "rounds": ROUNDS, "ops": []}

print(f"🚀 {machine_name} 开始 {ROUNDS} 轮并发测试")
print(f"   MemOS:     {MEMOS_URL}")
print(f"   Hindsight: {HS_URL}")
print()

all_ops = []
t0 = time.monotonic()

for i in range(1, ROUNDS + 1):
    tag = f"{machine_name}-r{i}-{rand_id()}"
    content = f"并发测试 {tag}"

    # MemOS
    if FILTER in ("all", "--memos-only"):
        r = memos_add(tag, content)
        all_ops.append(r)
        r = memos_search(tag)
        all_ops.append(r)

    # Hindsight
    if FILTER in ("all", "--hindsight-only"):
        r = hs_retain(tag, content)
        all_ops.append(r)
        r = hs_recall(tag)
        all_ops.append(r)

total_ms = int((time.monotonic() - t0) * 1000)

# ── 汇总 ───────────────────────────────────────────────────────────────────
summary = {"machine": machine_name, "rounds": ROUNDS,
           "total_ms": total_ms, "ops": {}}

for op in all_ops:
    name = op["op"]
    if name not in summary["ops"]:
        summary["ops"][name] = {"pass": 0, "fail": 0, "lats": []}
    if op["ok"]:
        summary["ops"][name]["pass"] += 1
    else:
        summary["ops"][name]["fail"] += 1
    summary["ops"][name]["lats"].append(op["ms"])

# 计算百分位
for op, data in summary["ops"].items():
    lats = sorted(data["lats"])
    n = len(lats)
    data["P50"] = lats[n//2] if n else 0
    data["P95"] = lats[int(n*0.95)] if n else 0
    data["P99"] = lats[int(n*0.99)] if n else 0
    data["max"] = max(lats) if lats else 0
    del data["lats"]

print(json.dumps(summary, ensure_ascii=False))
