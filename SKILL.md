---
name: ops-five-layer-memory
description: "Five-layer memory stack testing, benchmarking, and monitoring for OpenClaw. Use when: (1) running a full 5-layer health check or benchmark, (2) diagnosing which memory layer is failing, (3) setting up cron monitoring for memory health, (4) comparing memory performance across machines. Triggers on: 五层记忆, five-layer memory, memory benchmark, 记忆测试, L1-L5 check, memory health."
---

# Five-Layer Memory Stack Testing

Test, benchmark, and monitor all memory layers in OpenClaw.

## Layer Architecture (Updated 2026-03-29)

| Layer | Name | Backend | LLM Provider | What It Stores |
|-------|------|---------|--------------|----------------|
| L1 | LCM | SQLite (`~/.openclaw/lcm.db`) | Claude Sonnet-4-6 (Anthropic) | Conversation summaries (DAG) |
| L2 | LanceDB Pro | Lance files (`~/.openclaw/`) | Qwen2.5-32B-Instruct (SiliconFlow) | Semantic memory (vector + BM25 + reranker) |
| L3 | Hindsight | Docker + native PostgreSQL | **MiniMax-M2.7-highspeed** | Facts, entities, relationships (consolidation engine) |
| L3.5 | Cognee | Docker (cognee-fixed:v5) | **MiniMax-M2.7-highspeed** | Knowledge graph (entities + relationships via KG extraction) |
| L5 | Daily Files | Filesystem (`workspace/memory/`) | None | Raw daily notes |

### Architecture Change Log

- **2026-03-29**: MemOS removed from all 4 machines. Replaced by Hindsight (vectorize-io/hindsight). Cognee remains available but not actively tested in this skill.
- **2026-03-27**: Cognee moved to sidecar role; L3.5 MemOS was primary structured memory.
- **2026-03-26**: All layers migrated from MiniMax to SiliconFlow Qwen2.5-32B-Instruct.

### LLM Provider Strategy

Hindsight 和 Cognee 已從 SiliconFlow Qwen2.5-32B 切換至 **MiniMax M2.7-HS**（解決 TPM rate limit 問題）。

| Component | Model | Provider | Cost |
|-----------|-------|----------|------|
| L1 LCM (summary + expansion) | Claude Haiku-4-5 | Anthropic setup-token | 低 |
| L2 LanceDB Pro (LLM + reranker) | Qwen2.5-32B-Instruct + bge-reranker-v2-m3 | SiliconFlow | ~¥7-8/月 |
| L3 Hindsight (retain/recall/consolidation) | **MiniMax-M2.7-highspeed** | MiniMax api.minimaxi.com | Coding Plan 含 |
| L3.5 Cognee (knowledge graph) | **MiniMax-M2.7-highspeed** | MiniMax api.minimaxi.com | Coding Plan 含 |
| Embedding (L2 + L3 + Cognee shared) | BAAI/bge-m3 (1024 dims) | SiliconFlow | 免費 |
| Main session / subagent | Claude Opus-4-6 / MiniMax M2.7-HS | 各自 | 各自 |

### 2026-03-31 變更：Hindsight + Cognee LLM 切至 M2.7-HS

- **原因**：SiliconFlow Qwen2.5-32B 免費 tier TPM 限制太低，batch cognify/consolidation 頻繁 429
- **M2.7-HS 注意**：reasoning model，reasoning_tokens 消耗 max_tokens budget
  - Cognee KG extraction 需要 max_tokens≥8192（reasoning ~3000 + JSON output ~3000+）
  - Hindsight 無此問題（output 較短）
- **M2.1-HS 不可用**：thinking.enabled=false 無效，所有 tokens 被 `<think>` 吃掉
- **M2.5-HS 備選**：可用但不穩定（nondeterministic truncation）

## Hindsight Deployment (L3)

### Architecture

```
老大 (10.10.20.178)
├── Docker: hindsight-docker (:9077 → container :8888)
├── nginx proxy (:9078 → 127.0.0.1:9077) — LAN access
├── Native PostgreSQL (:5432, user=hindsight, db=hindsight)
└── Bank: openclaw (config overrides persisted via launchd)

老二/老三/老四 → http://10.10.20.178:9078 (hindsightApiUrl)
```

### Docker Run Command

```bash
docker run -d --name hindsight-docker \
  -p 9077:8888 -p 9999:9999 \
  -e HINDSIGHT_API_HOST=0.0.0.0 \
  -e HINDSIGHT_API_PORT=8888 \
  -e HINDSIGHT_API_DATABASE_URL=postgresql://hindsight:hindsight@host.docker.internal:5432/hindsight \
  -e HINDSIGHT_API_DB_POOL_MAX_SIZE=50 \
  -e HINDSIGHT_API_LLM_PROVIDER=openai \
  -e HINDSIGHT_API_LLM_MODEL=MiniMax-M2.7-highspeed \
  -e HINDSIGHT_API_LLM_API_KEY=<MINIMAX_API_KEY> \
  -e HINDSIGHT_API_LLM_BASE_URL=https://api.minimaxi.com/v1 \
  -e HINDSIGHT_API_EMBEDDINGS_PROVIDER=openai \
  -e HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL=BAAI/bge-m3 \
  -e HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL=https://api.siliconflow.cn/v1 \
  -e HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY=<SILICONFLOW_KEY> \
  -e HINDSIGHT_API_RERANKER_PROVIDER=litellm \
  -e HINDSIGHT_API_RERANKER_LITELLM_MODEL=BAAI/bge-reranker-v2-m3 \
  -e HINDSIGHT_API_RERANKER_LITELLM_API_BASE=https://api.siliconflow.cn/v1 \
  -e HINDSIGHT_API_RERANKER_LITELLM_API_KEY=<SILICONFLOW_KEY> \
  --restart unless-stopped \
  ghcr.io/vectorize-io/hindsight:latest
```

**Important env var names**: `EMBEDDINGS` (with S), not `EMBEDDING`.

### Bank Config Optimizations

These must be applied after every container restart (PATCH API doesn't persist in Docker):

```bash
curl -X PATCH http://127.0.0.1:9078/v1/default/banks/openclaw/config \
  -H "Content-Type: application/json" \
  -d '{"updates":{
    "retain_extraction_mode": "detailed",
    "consolidation_llm_batch_size": 4,
    "consolidation_source_facts_max_tokens_per_observation": 2000
  }}'
```

A launchd plist (`ai.openclaw.hindsight-bank-init`) auto-injects these after boot.

### OpenClaw Plugin Config (openclaw.json)

```json
{
  "plugins": {
    "slots": { "memory": "memory-lancedb-pro" },
    "entries": {
      "hindsight-openclaw": {
        "enabled": true,
        "config": {
          "hindsightApiUrl": "http://10.10.20.178:9078",
          "recallTypes": ["world", "experience", "observation"],
          "recallBudget": "mid",
          "recallMaxTokens": 1024,
          "dynamicBankId": false,
          "retainEveryNTurns": 2,
          "retainOverlapTurns": 1,
          "autoRecall": true,
          "autoRetain": true
        }
      }
    }
  }
}
```

**Note**: `hindsight-openclaw` plugin manifest must NOT have `"kind": "memory"` — this was removed on all 4 machines to allow it to coexist with memory-lancedb-pro as a sidecar.

### MCP Tool Parameters (Correct)

| Tool | Key Params |
|------|-----------|
| retain | content, context, timestamp, tags, metadata, document_id, bank_id |
| recall | query, max_tokens, budget, types, tags, bank_id |
| list_memories | type, q, limit, offset, bank_id |

**Endpoint**: POST `/mcp` (SSE). REST `/v1/...` endpoints return 405 for retain.

### nginx Proxy Config

Location: `/opt/homebrew/etc/nginx/servers/hindsight-proxy.conf`

```nginx
upstream hindsight_backend {
    server 127.0.0.1:9077;
    keepalive 64;
}
server {
    listen 9078;
    location / {
        proxy_pass http://hindsight_backend;
        proxy_read_timeout 300s;
        proxy_buffering off;
    }
}
```

## Performance Benchmarks

### Quality (10 business scenarios, 2026-03-29)

| Method | Score | Notes |
|--------|-------|-------|
| recall (world+experience) | **10/10 = 100%** | After bank config optimization |
| recall (+observation) | 10/10 = 100% | All type combos work |
| list_memories (keyword) | 2/10 = 20% | Expected — consolidation rewrites raw text |

### Latency — Single Machine (localhost)

| Concurrency | retain P50 | recall P50 | list P50 |
|:-----------:|:----------:|:----------:|:--------:|
| C=1 | 8ms | 1.3s | 9ms |
| C=8 | 15ms | 2.5s | 19ms |
| C=32 | 41ms | 2.7s | 39ms |
| C=96 | 129ms | 8.9s | - |

### Concurrency Limits

| Scenario | Stable Limit | Notes |
|----------|:------------:|-------|
| Single machine | **C=96** (100%) | C=128 drops to ~95% |
| 4 machines simultaneous | **C=32×4=128** (100%) | C=48×4=192 drops to ~80% |
| Bottleneck | recall (LLM reranking) | retain/list stay sub-100ms at high C |
| Real-world usage | ~4-12 QPS | 10x headroom from limit |

### Four-Machine Simultaneous (C=32×4=128, 2026-03-29)

All 100% pass, zero failures:
- Scott1 (localhost): retain P50=41ms, recall P50=2.7s
- Scott2 (LAN): retain P50=226ms, recall P50=4.0s
- Scott3 (LAN): retain P50=271ms, recall P50=4.1s
- Scott4 (LAN): retain P50=2062ms, recall P50=3.3s

## Known Issues

### macOS Tahoe Python LAN Socket Bug (老二)

Homebrew Python 3.14 (adhoc-signed) can get silently blocked from LAN connections by macOS Tahoe's `networkserviceproxy`. Symptoms: `Errno 65 No route to host` for LAN IPs only, `curl`/`nc`/Node.js unaffected.

**Root cause**: Previous `codesign --force` operation cached a rejection in `networkserviceproxy` memory.

**Fix**:
```bash
sudo killall networkserviceproxy nesessionmanager symptomsd mDNSResponder
```
Processes auto-restart via launchd with clean state. Does NOT require Python reinstall.

### Hindsight Docker PATCH Config Not Persistent

Bank config overrides applied via PATCH API are lost on container restart. Use the launchd plist (`ai.openclaw.hindsight-bank-init`) for auto-injection.

### Embedding Dimension Mismatch (Historical)

Earlier Hindsight Docker used default 384-dim embeddings. Current Docker is configured with `BAAI/bge-m3` (1024-dim). If migrating from old data, truncate the DB first.

## Deprecated Components

### MemOS (Removed 2026-03-29)

MemOS was removed from all 4 machines due to persistent issues:
- API key injection failures in Docker env vars
- Neo4j dedup O(n²) causing 30s+ add latency
- WorkingMemory routing bugs (search returning 0 results)
- Slow writes (~8s at C=1 with LLM extraction)

Docker containers `memos-api`, `memos-neo4j`, `memos-qdrant` have been stopped and removed. The `memos-openclaw` plugin entry has been deleted from all machines' openclaw.json.

### Cognee (L3.5 — Active Sidecar)

Cognee 作為知識圖譜索引器活躍運行，配置已更新：
- LLM: MiniMax M2.7-HS (max_tokens=8192)
- Embedding: SiliconFlow BAAI/bge-m3
- Docker image: `cognee-fixed:v5`（含 message role order + embedding dimensions 兩個 patch）
- Port: `0.0.0.0:8000`（LAN 可達，四台機器共用）
- Dataset: `openclaw-main-v7`
- searchType: `CHUNKS`（避免 GRAPH_COMPLETION timeout）

詳見 `ops-cognee-rollout` skill。

### NAS 備份架構

NAS (10.10.10.66) 部署了完整 Docker stack 作為冷備：
- oc-neo4j：3,671 Memory nodes（legacy MemOS 數據）
- oc-postgres：Hindsight DB，1,712+ memories
- oc-cognee：M2.7-HS，含已匯入的 3,486 條 legacy Qdrant 數據
- oc-hindsight：M2.7-HS

本機（老大）為主力，NAS 為災備。不支持雙連接（每個插件只能指一個 baseUrl）。
