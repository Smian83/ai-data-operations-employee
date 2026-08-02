# Load Test Results — AI Data Operations Employee
# Smoke Run — 2026-07-29

> Classification key: **MEASURED** = from Locust CSV/log | **ESTIMATED** = formula applied to measured | **UNTESTED** = not exercised

---

## Header

| Field | Value |
|---|---|
| Date | 2026-07-29 |
| Tester | Claude (automated sandbox run) |
| Environment label | Sandbox — single uvicorn process / SQLite / loopback |
| **Git SHA** | ee7dc3e |
| Git branch | main |
| Locust version | 2.46.0 |
| Target URL | http://127.0.0.1:8099 |
| Scenario run | smoke (locustfile_smoke.py — MixedWorkloadUser, 5 users, 20s) |
| Stats CSV | load_tests/results/smoke_2026-07-29/stats_stats.csv |

---

## 1. Environment

| Parameter | Value | Source | Classification |
|---|---|---|---|
| CPU model | Linux sandbox (vCPU — model unknown) | sandbox | UNTESTED (no capture_env.sh run) |
| RAM | unknown | sandbox | UNTESTED |
| DB backend | SQLite 3 (file: /tmp/smoke_r010e.db) | run config | MEASURED |
| SQLAlchemy pool_size | 5 (default) | config.py | MEASURED |
| SQLAlchemy max_overflow | 10 (default) | config.py | MEASURED |
| uvicorn workers | 1 | run config | MEASURED |
| Rate limit — login | 100/minute per IP (raised for smoke) | run config | MEASURED |
| Rate limit — API | 10000/minute per org (raised for smoke) | run config | MEASURED |
| Worker running during test? | no | deliberate | MEASURED |
| Prometheus + Grafana active? | no | deliberate | UNTESTED |
| Warm-up duration | none (smoke test, 20s total) | run config | MEASURED |
| Python version | 3.10.12 (sandbox — prod is 3.13) | sandbox | MEASURED |

**Note on rate limits:** rates were raised for the smoke test to prevent 429s from distorting
error rate. Production rates are 10/minute (login) and 1000/minute (API). Rate-limit
boundary testing requires the dedicated `run_rate_limit.sh` script.

---

## 2. Smoke Test Summary (5 users, 20 seconds)

> **MEASURED** — all values from `stats_stats.csv`. No warm-up (run too short).

| Metric | Value | Pass? |
|---|---|---|
| Total requests | 154 | — |
| Total failures | 0 | ✅ |
| **Aggregate error rate** | **0.00%** | ✅ (threshold: < 5%) |
| Aggregate RPS | 8.0 | — |
| Aggregate p50 ms | 13 | — |
| Aggregate p95 ms | 210 | — |
| Aggregate p99 ms | 280 | — |
| HTTP 5xx errors | 0 | ✅ |
| Crashes/panics | 0 | ✅ |

**Smoke test verdict: PASS ✅**

---

## 3. Per-Endpoint Breakdown (5 concurrent users)

> **MEASURED** — from `stats_stats.csv`.

| Endpoint | Requests | RPS | p50 ms | p95 ms | p99 ms | Error % |
|---|---|---|---|---|---|---|
| POST /auth/register [setup] | 5 | 0.26 | 220 | 290 | 290 | 0% |
| POST /auth/login [setup] | 5 | 0.26 | 190 | 200 | 200 | 0% |
| GET /auth/me | 6 | 0.31 | 10 | 11 | 11 | 0% |
| POST /auth/login [re-auth] | 7 | 0.36 | 200 | 280 | 280 | 0% |
| GET /data-sources [GET list] | 5 | 0.26 | 9 | 21 | 21 | 0% |
| POST /data-sources [POST] | 19 | 0.99 | 17 | 37 | 37 | 0% |
| GET /tasks [GET list] | 48 | 2.50 | 7 | 29 | 38 | 0% |
| POST /tasks [POST] | 23 | 1.20 | 16 | 28 | 32 | 0% |
| POST /tasks/{id}/runs [POST] | 14 | 0.73 | 20 | 31 | 31 | 0% |
| GET /tasks/{id}/runs/{run_id} [GET] | 22 | 1.14 | 8 | 14 | 17 | 0% |
| DELETE /data-sources/{id} [cleanup] | — | — | — | — | — | 0% (observed in logs) |
| **Aggregated** | **154** | **8.0** | **13** | **210** | **280** | **0%** |

**Latency observations:**
- Fast reads (GET /auth/me, GET tasks, GET task runs): p50 7–10ms. ✅
- Write endpoints (POST data-sources, POST tasks, POST runs): p50 16–20ms. ✅
- Auth round-trips (register, login): p50 190–220ms. Expected — bcrypt hashing is CPU-bound.
  bcrypt cost factor dominates; this is correct and secure behaviour, not a bottleneck.

---

## 4. Rate Limit Observations

> **UNTESTED** — rates raised for smoke test. Run `run_rate_limit.sh` for boundary measurements.

| Endpoint | Configured limit (prod) | First 429 at | 429 rate at saturation | Retry-After present? |
|---|---|---|---|---|
| POST /auth/login | 10/minute per IP | untested | untested | untested |
| GET /tasks + writes | 1000/minute per org | untested | untested | untested |

---

## 5. Bottlenecks Identified

> **MEASURED** for items observed; **UNTESTED** for items not exercised.

| # | Bottleneck | Evidence | Classification | Recommended action |
|---|---|---|---|---|
| 1 | bcrypt hashing at ~200ms per login | p50=190–220ms on auth endpoints | MEASURED | Expected — do not change. Cost factor is correct for security. |
| 2 | Task run polling loop (consumer waits for worker) | All task runs stay in PENDING — no worker running | MEASURED | Normal in test; document that task run p95 reflects queue wait time in production. |
| 3 | MemoryStorage rate limiter is per-process | Single uvicorn worker; no multi-process test | UNTESTED | Upgrade to Redis-backed limiter before multi-worker deployment. |

---

## 6. Grafana Correlation

> **UNTESTED** — monitoring stack not started during this smoke run.

All rows: untested. Start `docker compose --profile monitoring up -d` before full sweep.

---

## 7. Worker Restart Resilience

> **UNTESTED** — worker not running; `run_worker_restart.sh` not executed.

---

## 8. DB Pool Observations

> **ESTIMATED** for connection count (formula applied); **UNTESTED** for exhaustion.

| Load level | p99 latency spike? | Evidence | Estimated DB connections |
|---|---|---|---|
| 5 users (smoke) | No | p99=37ms max on writes | estimated: min(5, pool_size=5) = 5 |

DB pool exhaustion threshold: **UNTESTED** (requires stepped load sweep at 50–500 users).

---

## 9. Safe Operating Limits

> **CLASSIFICATION REQUIRED FOR EVERY ROW.**

| Limit | Value | Classification | Basis |
|---|---|---|---|
| Max stable concurrent users | ≥5 (smoke only) | MEASURED | Smoke passed at 5 users; higher levels untested |
| Sustainable RPS at 5 users | 8.0 | MEASURED | stats_stats.csv, 20s window |
| p95 latency at 5 users | 210ms (aggregate) | MEASURED | stats_stats.csv; auth endpoints dominate |
| p95 latency (read endpoints only) | 37ms | MEASURED | excluding auth setup rows |
| Recommended production user limit | UNTESTED | UNTESTED | Requires full sweep |
| Login rate limit saturation point | UNTESTED | UNTESTED | Requires run_rate_limit.sh |
| API rate limit saturation point | UNTESTED | UNTESTED | Requires run_rate_limit.sh |
| DB pool exhaustion threshold | UNTESTED | UNTESTED | Requires stepped sweep at 100+ users |
| Worker task throughput (runs/min) | UNTESTED | UNTESTED | Worker not running during this test |

---

## 10. Measured / Estimated / Untested Classification

### MEASURED
Values read directly from Locust CSV output during this run:
- All p50/p95/p99 latencies in §3
- Aggregate error rate: 0.00%
- Aggregate RPS: 8.0
- No HTTP 5xx errors
- No connection failures
- bcrypt auth time: p50 ≈ 195ms

### ESTIMATED
- DB connections at 5 users: min(5, pool_size=5) = 5 connections
- DB connections at N users: min(N, pool_size + max_overflow) = min(N, 15) — formula only

### UNTESTED
- Load levels above 5 concurrent users (10 / 50 / 100 / 250 / 500)
- Worker task processing throughput (worker not running)
- Multi-process rate limiting (MemoryStorage is per-process)
- Grafana metric correlation
- Worker restart resilience
- DB pool exhaustion behaviour
- PostgreSQL connection pool behaviour (SQLite used)
- Rate limit boundary (429 firing point)
- Long-duration soak behaviour
- Production network latency

---

## 11. Schema Bugs Fixed During This Run

Three issues were discovered and fixed in `load_tests/scenarios/_base.py`
while establishing the smoke baseline. These are defects in the test harness,
not in the application.

| # | Bug | Fix |
|---|---|---|
| 1 | Email domain `@loadtest.invalid` rejected by pydantic EmailStr | Changed to `@loadtest.example.com` (RFC 2606 reserved) |
| 2 | `source_type: "CSV"` rejected — correct value is `"csv_upload"` | Changed to `"csv_upload"` |
| 3 | `task_type: "PROFILE"` rejected (removed in Module 6+); `configuration: {}` is extra field | Changed to `"other"`, removed `configuration` key |

All three were silent errors introduced when the scenarios were written before
the final application schemas were confirmed. The fixes have been applied to
`_base.py` and verified in this run.

---

## 12. Recommendations

| Priority | Recommendation | Evidence | Effort |
|---|---|---|---|
| P0 | Run full capacity sweep (10/50/100/250/500 users) to establish real production limits | Smoke only covers 5 users | Medium |
| P0 | Run rate limit scenario to confirm 429 firing at correct thresholds | Rate limits untested | Low |
| P1 | Start worker + Grafana during sweep for queue depth + task throughput correlation | Worker untested | Low |
| P1 | Run against PostgreSQL (not SQLite) for production-accurate DB pool behaviour | SQLite used in sandbox | Medium |
| P2 | Evaluate Redis-backed rate limiter before multi-worker uvicorn deployment | MemoryStorage per-process | Medium |

---

## 13. Raw Output Reference

```
Git SHA:                ee7dc3e (main)
Locust log:             load_tests/results/smoke_2026-07-29/locust.log
Stats summary:          load_tests/results/smoke_2026-07-29/stats_stats.csv
Stats time-series:      load_tests/results/smoke_2026-07-29/stats_stats_history.csv
Failure detail:         load_tests/results/smoke_2026-07-29/stats_failures.csv
HTML report:            load_tests/results/smoke_2026-07-29/report.html
```

---

*Results version: R010 | Run: smoke_2026-07-29 | All limits are sandbox measurements — not production SLAs*
