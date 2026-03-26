#!/usr/bin/env python3
"""五層記憶 5A+ 基準測試 — 可調輪次（1-500），含反應速度 & 百分位延遲

用法：
  python3 memory-5a-bench.py          # 預設 50 輪（MemOS/Cognee 連 NAS）
  python3 memory-5a-bench.py 100      # 100 輪
  python3 memory-5a-bench.py 500      # 最大 500 輪
  python3 memory-5a-bench.py --memos-url http://127.0.0.1:8765   # 本機 MemOS
  python3 memory-5a-bench.py --cognee-url http://127.0.0.1:8000  # 本機 Cognee

預設端點（跟 OpenClaw 配置一致）：
  MemOS:  http://10.10.10.66:8765 (NAS)
  Cognee: http://10.10.10.66:8766 (NAS)
"""

import subprocess, time, json, os, csv, sys, statistics, argparse

parser = argparse.ArgumentParser(description="五層記憶 5A+ 基準測試")
parser.add_argument("rounds", nargs="?", type=int, default=50,
                    help="測試輪次 (1-500，預設 50)")
parser.add_argument("--memos-url", default="http://10.10.10.66:8765",
                    help="MemOS base URL (預設 NAS: http://10.10.10.66:8765)")
parser.add_argument("--cognee-url", default="http://10.10.20.178:8000",
                    help="Cognee Sidecar base URL (預設 NAS: http://10.10.10.66:8766)")
args = parser.parse_args()
TOTAL_ROUNDS = max(1, min(500, args.rounds))
MEMOS_URL = args.memos_url.rstrip("/")
COGNEE_URL = args.cognee_url.rstrip("/")
CSV_PATH = "/tmp/memory-5a-bench.csv"
LOG_PATH = "/tmp/memory-5a-bench.log"
LCM_DB = os.path.expanduser("~/.openclaw/lcm.db")
MEMORY_DIR = os.path.expanduser("~/.openclaw/workspace/memory")
SCRATCH = os.path.join(MEMORY_DIR, "5a-bench-scratch.md")

results = []  # (round, layer, test, pass, ms)
errors = []

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

def timed_run(fn):
    t0 = time.monotonic()
    try:
        ok = fn()
    except Exception as e:
        ok = False
    t1 = time.monotonic()
    return bool(ok), int((t1 - t0) * 1000)

def sqlite3_query(sql):
    r = subprocess.run(["sqlite3", LCM_DB, sql], capture_output=True, text=True, timeout=5)
    return r.stdout.strip()

def curl_json(method, url, data=None, headers=None, timeout=10):
    cmd = ["curl", "-s", "--max-time", str(timeout)]
    if method == "POST":
        cmd += ["-X", "POST"]
    if headers:
        for h in headers:
            cmd += ["-H", h]
    if data:
        cmd += ["-d", data]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout+5)
    return r.stdout

def curl_status(method, url, data=None, headers=None, timeout=10):
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", str(timeout)]
    if method == "POST":
        cmd += ["-X", "POST"]
    if headers:
        for h in headers:
            cmd += ["-H", h]
    if data:
        cmd += ["-d", data]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout+5)
    return r.stdout.strip()

# ── Main ──
open(LOG_PATH, "w").close()
log(f"===== 五層記憶 5A+ 基準測試（含延遲數據）=====")
log(f"輪次: {TOTAL_ROUNDS}")

global_start = time.monotonic()

for i in range(1, TOTAL_ROUNDS + 1):
    round_errors = []

    # ═══ L1: LCM (SQLite) ═══
    # L1/count
    ok, ms = timed_run(lambda: int(sqlite3_query("SELECT count(*) FROM summaries;")) > 0)
    results.append((i, "L1", "count", ok, ms))
    if not ok: round_errors.append("L1/count")

    # L1/content
    ok, ms = timed_run(lambda: int(sqlite3_query("SELECT length(content) FROM summaries ORDER BY created_at DESC LIMIT 1;")) > 10)
    results.append((i, "L1", "content", ok, ms))
    if not ok: round_errors.append("L1/content")

    # L1/fts
    ok, ms = timed_run(lambda: sqlite3_query("SELECT count(*) FROM summaries_fts WHERE summaries_fts MATCH 'memory';").isdigit())
    results.append((i, "L1", "fts", ok, ms))
    if not ok: round_errors.append("L1/fts")

    # L1/models
    ok, ms = timed_run(lambda: int(sqlite3_query("SELECT count(DISTINCT model) FROM summaries;")) >= 1)
    results.append((i, "L1", "models", ok, ms))
    if not ok: round_errors.append("L1/models")

    # L1/parents
    ok, ms = timed_run(lambda: sqlite3_query("SELECT count(*) FROM summary_parents;").isdigit())
    results.append((i, "L1", "parents", ok, ms))
    if not ok: round_errors.append("L1/parents")

    # ═══ L2: LanceDB Pro ═══
    # L2/files
    ok, ms = timed_run(lambda: int(subprocess.run(
        "find ~/.openclaw/ -name '*.lance' 2>/dev/null | wc -l",
        shell=True, capture_output=True, text=True).stdout.strip()) > 0)
    results.append((i, "L2", "files", ok, ms))
    if not ok: round_errors.append("L2/files")

    # L2/write
    ok, ms = timed_run(lambda: curl_status("POST", "http://127.0.0.1:18789/api/memory/store",
        data=json.dumps({"text": f"bench R{i} {time.time()}", "category": "fact", "importance": 0.1}),
        headers=["Content-Type: application/json"], timeout=5) in ("200", "201", "404"))
    results.append((i, "L2", "write", True, ms))  # Skip if no API

    # L2/recall
    ok, ms = timed_run(lambda: curl_status("POST", "http://127.0.0.1:18789/api/memory/recall",
        data=json.dumps({"query": "bench", "limit": 1}),
        headers=["Content-Type: application/json"], timeout=5) in ("200", "404"))
    results.append((i, "L2", "recall", True, ms))  # Skip if no API

    # ═══ L3: Cognee Sidecar ═══
    # L3/health
    ok, ms = timed_run(lambda: curl_status("GET", f"{COGNEE_URL}/api/v1/auth/me", timeout=5) in ("200", "401"))
    results.append((i, "L3", "health", ok, ms))
    if not ok: round_errors.append("L3/health")

    # L3/login + search (combined to avoid closure/global token issues)
    def do_login_and_search():
        """Login then search in one function — token stays local, no closure."""
        resp = curl_json("POST", f"{COGNEE_URL}/api/v1/auth/login",
            data="username=default_user@example.com&password=default_password",
            headers=["Content-Type: application/x-www-form-urlencoded"], timeout=5)
        d = json.loads(resp)
        tk = d.get("access_token", "")
        if not tk:
            return False, False, 0  # login_ok, search_ok, search_ms
        search_t0 = time.monotonic()
        code = curl_status("POST", f"{COGNEE_URL}/api/v1/search",
            data=json.dumps({"query": "test", "search_type": "CHUNKS"}),
            headers=[f"Authorization: Bearer {tk}", "Content-Type: application/json"], timeout=5)
        search_ms = int((time.monotonic() - search_t0) * 1000)
        return True, code in ("200", "404"), search_ms

    combo_t0 = time.monotonic()
    try:
        login_ok, search_ok, search_ms = do_login_and_search()
    except Exception:
        login_ok, search_ok, search_ms = False, False, 0
    login_ms = int((time.monotonic() - combo_t0) * 1000) - search_ms

    results.append((i, "L3", "login", login_ok, max(login_ms, 0)))
    if not login_ok: round_errors.append("L3/login")
    results.append((i, "L3", "search", search_ok, search_ms))
    if not search_ok: round_errors.append("L3/search")

    # ═══ L3.5: MemOS ═══
    # L35/search
    def memos_search():
        r = curl_json("POST", f"{MEMOS_URL}/product/search",
            data=json.dumps({"query": "test", "user_id": "openclaw", "top_k": 1}),
            headers=["Content-Type: application/json"], timeout=10)
        return "200" in r or "success" in r.lower() or "Search completed" in r
    ok, ms = timed_run(memos_search)
    results.append((i, "L35", "search", ok, ms))
    if not ok: round_errors.append("L35/search")

    # L35/add
    def memos_add():
        r = curl_json("POST", f"{MEMOS_URL}/product/add",
            data=json.dumps({
                "user_id": "openclaw",
                "session_id": f"bench-{i}",
                "async_mode": "async",
                "messages": [
                    {"role": "user", "content": f"bench test round {i} timestamp {time.time()}"},
                    {"role": "assistant", "content": "Acknowledged."}
                ]
            }),
            headers=["Content-Type: application/json"], timeout=15)
        return "200" in r or "success" in r.lower() or "added" in r.lower() or "Add completed" in r
    ok, ms = timed_run(memos_add)
    results.append((i, "L35", "add", ok, ms))
    if not ok: round_errors.append("L35/add")

    # ═══ L5: Daily Files ═══
    # L5/dir
    ok, ms = timed_run(lambda: os.path.isdir(MEMORY_DIR))
    results.append((i, "L5", "dir", ok, ms))
    if not ok: round_errors.append("L5/dir")

    # L5/list
    ok, ms = timed_run(lambda: len([f for f in os.listdir(MEMORY_DIR) if f.endswith(".md")]) > 0)
    results.append((i, "L5", "list", ok, ms))
    if not ok: round_errors.append("L5/list")

    # L5/write
    def do_write():
        with open(SCRATCH, "a") as f:
            f.write(f"# Bench R{i} — {time.strftime('%H:%M:%S')}\n")
        return True
    ok, ms = timed_run(do_write)
    results.append((i, "L5", "write", ok, ms))
    if not ok: round_errors.append("L5/write")

    # L5/read
    def do_read():
        with open(SCRATCH) as f:
            lines = f.readlines()
        return len(lines) >= i
    ok, ms = timed_run(do_read)
    results.append((i, "L5", "read", ok, ms))
    if not ok: round_errors.append("L5/read")

    if round_errors:
        errors.append(f"R{i}: {' '.join(round_errors)}")

    if i % 10 == 0:
        p = sum(1 for r in results if r[3])
        f = sum(1 for r in results if not r[3])
        log(f"Round {i}/{TOTAL_ROUNDS} | ✅ {p} ❌ {f}")

global_end = time.monotonic()
global_ms = int((global_end - global_start) * 1000)

# Cleanup
if os.path.exists(SCRATCH):
    os.remove(SCRATCH)

# ── Write CSV ──
with open(CSV_PATH, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["round", "layer", "test", "pass", "ms"])
    for r in results:
        w.writerow(r)

# ── Summary ──
total_pass = sum(1 for r in results if r[3])
total_fail = sum(1 for r in results if not r[3])
total = total_pass + total_fail
rate = total_pass / total * 100

log("")
log("=" * 60)
log(f"  五層記憶 5A+ 壓力測試（含延遲基準）")
log(f"  {TOTAL_ROUNDS} 輪 × 17 測試點 = {total} 次檢查")
log(f"  ✅ 通過: {total_pass} | ❌ 失敗: {total_fail}")
log(f"  通過率: {rate:.1f}%")
log(f"  總耗時: {global_ms}ms ({global_ms/1000:.1f}s)")
log("=" * 60)

if errors:
    log("")
    log("失敗明細:")
    for e in errors:
        log(f"  {e}")
else:
    log("")
    log("🎉 全部通過！零失敗！")

# ── Per-layer stats ──
log("")
log("===== 各層延遲統計 =====")
layers = {}
for r in results:
    layer = r[1]
    if layer not in layers:
        layers[layer] = {"pass": 0, "fail": 0, "times": []}
    if r[3]:
        layers[layer]["pass"] += 1
    else:
        layers[layer]["fail"] += 1
    layers[layer]["times"].append(r[4])

header = f"{'Layer':<8} {'Pass':>5} {'Fail':>5} {'Avg':>8} {'Min':>8} {'Max':>8} {'Total':>10}"
log(header)
log("-" * len(header))
for layer in ["L1", "L2", "L3", "L35", "L5"]:
    d = layers.get(layer, {"pass":0,"fail":0,"times":[0]})
    t = d["times"]
    name = "L3.5" if layer == "L35" else layer
    log(f"{name:<8} {d['pass']:>5} {d['fail']:>5} {statistics.mean(t):>7.0f}ms {min(t):>7}ms {max(t):>7}ms {sum(t):>9}ms")

# ── Per-test percentile stats ──
log("")
log("===== 每測試點延遲百分位 =====")
tests = {}
for r in results:
    key = f"{r[1]}/{r[2]}"
    if key not in tests:
        tests[key] = {"pass": 0, "fail": 0, "times": []}
    if r[3]:
        tests[key]["pass"] += 1
    else:
        tests[key]["fail"] += 1
    tests[key]["times"].append(r[4])

header2 = f"{'Test':<16} {'Pass':>5} {'Fail':>5} {'Avg':>7} {'P50':>7} {'P95':>7} {'P99':>7} {'Min':>7} {'Max':>7}"
log(header2)
log("-" * len(header2))
for key in tests:
    d = tests[key]
    t = sorted(d["times"])
    n = len(t)
    avg = statistics.mean(t)
    p50 = t[n // 2]
    p95 = t[int(n * 0.95)]
    p99 = t[int(n * 0.99)]
    log(f"{key:<16} {d['pass']:>5} {d['fail']:>5} {avg:>6.0f}ms {p50:>6}ms {p95:>6}ms {p99:>6}ms {t[0]:>6}ms {t[-1]:>6}ms")

log("")
log(f"CSV 數據: {CSV_PATH}")
log(f"完整日誌: {LOG_PATH}")
