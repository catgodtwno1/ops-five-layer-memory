#!/usr/bin/env python3
"""
MiniMax API 並發壓測工具
測試目標：找出 429 的真正觸發閾值（瞬間並發 vs 持續頻率）

用法：
  python3 minimax-concurrency-test.py                    # 默認：M2.7-highspeed, 逐步升壓
  python3 minimax-concurrency-test.py --model MiniMax-M2.5
  python3 minimax-concurrency-test.py --concurrency 10   # 固定10並發
  python3 minimax-concurrency-test.py --all-models        # 測試所有模型
"""

import argparse, json, os, sys, time, threading, statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request, urllib.error

API_KEY = os.environ.get("MINIMAX_API_KEY", "")
BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1")

MODELS = [
    "MiniMax-M2.7-highspeed",
    "MiniMax-M2.7",
    "MiniMax-M2.5",
    "MiniMax-Text-01",
]

# 極小 prompt，最小化 token 消耗
TINY_PAYLOAD = {
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 5,
    "temperature": 0,
    "stream": False
}

def load_api_key():
    global API_KEY
    if API_KEY:
        return
    # 嘗試從 openclaw.json 讀取
    for p in [
        os.path.expanduser("~/.openclaw/openclaw.json"),
        os.path.expanduser("~/.config/openclaw/openclaw.json"),
    ]:
        if os.path.exists(p):
            try:
                with open(p) as f:
                    cfg = json.load(f)
                # 嘗試多種路徑
                for path in [
                    lambda c: c.get("env", {}).get("MINIMAX_API_KEY"),
                    lambda c: c.get("models", {}).get("providers", {}).get("minimax", {}).get("apiKey"),
                ]:
                    val = path(cfg)
                    if val and not val.startswith("$") and len(val) > 10:
                        API_KEY = val
                        return
            except Exception:
                pass
    # 嘗試從 models.json 讀取
    models_json = os.path.expanduser("~/.openclaw/models.json")
    if os.path.exists(models_json):
        try:
            with open(models_json) as f:
                mj = json.load(f)
            for provider in mj.get("providers", []):
                if "minimax" in provider.get("id", "").lower():
                    k = provider.get("apiKey", "")
                    if k and not k.startswith("$") and len(k) > 10:
                        API_KEY = k
                        return
        except Exception:
            pass

def call_api(model, timeout=15):
    """發送一次 API 請求，返回 (status_code, latency_ms, error_msg)"""
    payload = {**TINY_PAYLOAD, "model": model}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json"
        }
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            ms = int((time.monotonic() - t0) * 1000)
            return (resp.status, ms, "")
    except urllib.error.HTTPError as e:
        ms = int((time.monotonic() - t0) * 1000)
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        return (e.code, ms, body)
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000)
        return (0, ms, str(e)[:100])

def run_burst(model, concurrency, rounds=1):
    """
    發送 burst：同時發出 concurrency 個請求，重複 rounds 次
    返回 [(status, ms, err), ...]
    """
    results = []
    for r in range(rounds):
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(call_api, model) for _ in range(concurrency)]
            for f in as_completed(futures):
                results.append(f.result())
        if rounds > 1 and r < rounds - 1:
            time.sleep(0.5)  # burst 間隔 500ms
    return results

def run_sustained(model, rps, duration_sec):
    """
    持續壓力：每秒發 rps 個請求，持續 duration_sec 秒
    """
    results = []
    interval = 1.0 / rps if rps > 0 else 1.0
    end_time = time.monotonic() + duration_sec
    threads = []

    def worker():
        results.append(call_api(model))

    while time.monotonic() < end_time:
        t = threading.Thread(target=worker)
        t.start()
        threads.append(t)
        time.sleep(interval)

    for t in threads:
        t.join(timeout=20)
    return results

def analyze(results, label=""):
    """分析結果"""
    total = len(results)
    if total == 0:
        print(f"  {label}: 無結果")
        return 0

    ok = sum(1 for s, _, _ in results if s == 200)
    r429 = sum(1 for s, _, _ in results if s == 429)
    other = total - ok - r429
    latencies = [ms for s, ms, _ in results if s == 200]

    print(f"  {label}: {total} 請求 | ✅ {ok} | 🚫 429×{r429} | ❌ 其他×{other}", end="")
    if latencies:
        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) > 1 else latencies[0]
        avg = statistics.mean(latencies)
        print(f" | avg={avg:.0f}ms P50={p50:.0f}ms P95={p95:.0f}ms")
    else:
        print()

    if r429 > 0:
        first_429 = next((i for i, (s, _, _) in enumerate(results) if s == 429), -1)
        err_msg = next((e for s, _, e in results if s == 429), "")
        print(f"    首次 429 在第 {first_429 + 1} 個請求")
        if err_msg:
            print(f"    錯誤信息: {err_msg[:150]}")

    return r429

def test_escalation(model):
    """逐步升壓測試：1→2→5→10→20→50 並發"""
    print(f"\n{'='*60}")
    print(f"🔬 逐步升壓測試: {model}")
    print(f"{'='*60}")

    levels = [1, 2, 3, 5, 8, 10, 15, 20]
    for c in levels:
        print(f"\n--- 並發 {c} (burst×3) ---")
        results = run_burst(model, c, rounds=3)
        n429 = analyze(results, f"C={c}")
        if n429 > len(results) * 0.5:
            print(f"  ⚠️  超過 50% 被 429，停止升壓")
            break
        time.sleep(2)  # 升壓間等待

def test_sustained_rps(model):
    """持續 RPS 測試"""
    print(f"\n{'='*60}")
    print(f"📈 持續 RPS 測試: {model} (每級 10 秒)")
    print(f"{'='*60}")

    for rps in [1, 2, 5, 8, 10]:
        print(f"\n--- {rps} RPS × 10s ---")
        results = run_sustained(model, rps, 10)
        n429 = analyze(results, f"RPS={rps}")
        if n429 > len(results) * 0.3:
            print(f"  ⚠️  超過 30% 被 429，停止")
            break
        time.sleep(3)

def test_all_models():
    """對所有模型做基礎並發測試"""
    print(f"\n{'='*60}")
    print(f"🧪 全模型並發測試 (C=5, burst×2)")
    print(f"{'='*60}")

    for model in MODELS:
        print(f"\n--- {model} ---")
        # 先測單個確認可用
        status, ms, err = call_api(model)
        if status != 200:
            print(f"  ❌ 單請求失敗: HTTP {status} {err[:100]}")
            continue
        print(f"  ✅ 單請求 OK ({ms}ms)")
        time.sleep(1)

        results = run_burst(model, 5, rounds=2)
        analyze(results, model)
        time.sleep(3)

def main():
    parser = argparse.ArgumentParser(description="MiniMax API 並發壓測")
    parser.add_argument("--model", default="MiniMax-M2.7-highspeed")
    parser.add_argument("--concurrency", type=int, default=0, help="固定並發數 (0=逐步升壓)")
    parser.add_argument("--rps", action="store_true", help="持續 RPS 測試")
    parser.add_argument("--all-models", action="store_true", help="測試所有模型")
    parser.add_argument("--rounds", type=int, default=3, help="每級重複次數")
    args = parser.parse_args()

    load_api_key()
    if not API_KEY:
        print("❌ 找不到 MINIMAX_API_KEY，設定環境變量或確認 openclaw.json")
        sys.exit(1)

    print(f"🔑 API Key: {API_KEY[:8]}...{API_KEY[-4:]}")
    print(f"🌐 Base URL: {BASE_URL}")

    # 預熱
    print("\n⏳ 預熱...")
    status, ms, err = call_api(args.model)
    print(f"  預熱: HTTP {status} ({ms}ms)")
    if status != 200:
        print(f"  ❌ 預熱失敗: {err}")
        sys.exit(1)

    if args.all_models:
        test_all_models()
    elif args.rps:
        test_sustained_rps(args.model)
    elif args.concurrency > 0:
        print(f"\n--- 固定並發 {args.concurrency} × {args.rounds} rounds ---")
        results = run_burst(args.model, args.concurrency, args.rounds)
        analyze(results, f"C={args.concurrency}")
    else:
        test_escalation(args.model)

    print(f"\n{'='*60}")
    print("✅ 測試完成")

if __name__ == "__main__":
    main()
