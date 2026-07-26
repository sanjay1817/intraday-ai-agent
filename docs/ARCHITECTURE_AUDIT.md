# Architecture Audit — AI Intraday Trading Agent

**Date:** 2026-07-13
**Scope:** Full repository (`app/`, `tests/`, `alembic/`, `docker/`, `docs/`, `scripts/`, `pyproject.toml`)
**Method:** Direct inspection of the dependency graph, config, and directory structure, plus four
targeted deep-dive reviews (brokers, indicators, research, cross-cutting) with every finding
verified against exact file:line citations.

---

## 0. Reality check — what actually exists

Scores below are meaningless without this context. Of the ~14 planned phases plus the Research
Platform, only **three subsystems have real business logic**:

| Subsystem | State |
|---|---|
| `app/brokers` (adapters, factory, ws client) | Fully implemented, tested |
| `app/indicators` (engine, registry, 6 indicator modules) | Fully implemented, tested |
| `app/research` (16 files, Quant Research Platform) | Fully implemented, **zero tests** |
| `app/domain`, `app/config`, `app/utils` | Real, working shared kernel/infra |
| `ai`, `ai_agents`, `analytics`, `backtest`, `control`, `execution`, `paper`, `platform`, `risk`, `strategy` | **DTOs/models only — zero business logic, zero services** |
| `app/api`, `app/core`, `app/database`, `app/models`, `app/prompts`, `app/repositories`, `app/scheduler`, `app/schemas`, `app/services`, `app/websocket`, `app/ai_agents/agents` | **Empty placeholder packages** (docstring only) |
| `alembic/`, `docker/`, `docs/`, `scripts/` (top-level) | **Completely empty** |

There is **no `main.py` / FastAPI entrypoint anywhere in the repo.** Nothing here can currently be
run as a service. Test suite: 76 tests, all passing, covering only `app/brokers` and
`app/indicators`.

---

## 1. Scores

| Dimension | Score /100 | One-line rationale |
|---|---|---|
| **Architecture** | **68** | Dependency-free domain core, zero circular imports, genuinely extensible indicator registry — but two real LSP violations and several bounded contexts coupled directly to each other's evolving stub types with no adapter. |
| **Security** | **61** | No live critical vulnerabilities (no eval/exec/pickle/hardcoded secrets/TLS bypass) — but WS credentials are embedded in a logged URL, broker errors can leak past the domain-exception boundary, and the entire auth layer implied by core deps doesn't exist yet. |
| **Performance** | **64** | What's built is efficient (correct async I/O, LRU caching, single-pass computation) — but caches are entry-count- not size-aware, indicators redundantly recompute shared sub-calculations, and grid search has no combinatorial cap. Never load-tested. |
| **Maintainability** | **63** | Documentation is a genuine, consistent, repo-wide strength — but duplicated logic recurs 3–6× in several subsystems, and the single most complex, numerically sensitive package (`research`) has zero regression tests. |
| **Scalability** | **55** | Async I/O is correct where implemented, but there's no persistence layer, no Redis, no worker/queue model, and the two caches that would matter under concurrent load are explicitly non-thread-safe despite docstrings recommending shared-singleton usage. |
| **Production Readiness** | **24** | No entrypoint, no DB, no auth, no Redis, no scheduler, no outward websocket layer; `alembic/docker/docs/scripts` are empty. This is quality foundational work, not a deployable service. |

---

## 2. Findings, prioritized by impact

### Critical

| # | Finding | Location | Failure scenario |
|---|---|---|---|
| C1 | No FastAPI entrypoint exists | repo-wide | Nothing here can be started as a service today. |
| C2 | No DB engine/session/migrations despite `sqlalchemy[asyncio]`, `alembic`, `asyncpg` being core deps | `app/database/__init__.py` (docstring only), `alembic/` (empty) | No persistence path exists for any of the 11 stub phases once they gain logic. |
| C3 | Zero automated tests across all 16 `app/research` files | `tests/` (no `tests/unit/research/`) | ~25 public functions with real numerical logic (cointegration + OLS hedge ratio, regime priority state machine, Monte Carlo bootstrap, walk-forward windowing, feature-transform dispatch) have no safety net; a future refactor can silently corrupt a research report. |
| C4 | `ExperimentTracker` backends silently change parameter types | `app/research/experiment_tracker.py:127-131`, `app/research/models.py:402` | MLflow stringifies every logged param (`9` → `"9"`); `InMemoryExperimentTracker` preserves the original type. `ExperimentRecord.parameters: dict[str, Any]` gives no schema protection — switching `tracking_backend` in config (no code change) silently breaks any downstream code comparing parameter values. |

### High

| # | Finding | Location | Failure scenario |
|---|---|---|---|
| H1 | WS credentials embedded in URL, reachable by broad exception logging | `app/brokers/zerodha.py:530-534`, `app/brokers/ws_client.py:116-121` | `api_key`/`access_token` are query-string params in the Kite WS URL; `_run()` logs `repr(exc)` on any connect failure. Query-string secrets are a well-known leak vector (proxy logs, connection-debug logs) independent of whether this specific exception embeds the URL. |
| H2 | Broker adapters leak raw `KeyError`/`ValueError` instead of domain errors | `app/brokers/angel_one.py:396-403` (+ equivalents in upstox.py/zerodha.py) | A malformed/error response body causes a raw Python exception, not `BrokerAPIError` — confirmed by `tests/unit/brokers/test_angel_one.py:369-389`, which asserts the raw `KeyError` propagates. Any caller written against the documented `except BrokerError` contract misses it and crashes. |
| H3 | LSP violation: `tradingsymbol` means a different thing per adapter | `app/brokers/zerodha.py:481-488`, `angel_one.py:26-31`, `upstox.py:176-187` | `BrokerInterface` takes a plain `tradingsymbol: str`, but Angel One/Zerodha actually need a broker-specific numeric token and Upstox needs `f"{exchange}|{tradingsymbol}"`. Adapters aren't actually swappable behind the shared interface without also swapping what the caller passes. |
| H4 | No auth/JWT despite `pyjwt[crypto]`/`passlib[bcrypt]` being core deps | repo-wide (grep confirms zero real usage) | Any future API layer has no access-control model to build on yet. |
| H5 | No Redis client despite `redis[hiredis]` being a core dep | `app/control/models.py:13,50` (docstring mentions only) | The Control Center's planned session store and any shared cache have no multi-process path; `app.utils.cache.LRUCache` is explicitly single-process only. |
| H6 | `.env.example` has drifted ~30 vars ahead of `settings.py`'s actual ~6 fields | `.env.example` vs `app/config/settings.py:85-102` | `.env.example:2` claims the two are kept in sync — they aren't. `model_config` sets `extra="ignore"` (`settings.py:93`), so an operator setting `POSTGRES_PASSWORD`/`REDIS_HOST` per the example gets silently swallowed with no error. |
| H7 | `IndicatorEngine`'s cache is non-thread-safe; package import forces `pandas_ta` for schema-only consumers | `app/indicators/engine.py:50-57`, `app/utils/cache.py:16-22`, `app/indicators/__init__.py:53-66` | The engine's own docstring recommends a singleton-across-requests usage pattern that would race under a sync route/thread-pool. Currently latent — `IndicatorEngine` is only instantiated in tests today, not from any route. Separately, any consumer wanting only `IndicatorResult` (e.g. `app/research`, `app/strategy/dto.py`) unavoidably imports every indicator module and `pandas_ta` at package-import time. |
| H8 | Unbounded grid search — no combinatorial cap | `app/research/hyperparameter_optimizer.py:101-132` | `max_trials` is deliberately not applied to `GRID_SEARCH`; 5 parameters × 100 steps each fully materializes 10^10 trials with no guard, risking memory exhaustion or an indefinite hang. |
| H9 | Sync MLflow/Optuna calls would block the event loop if ever invoked from a request handler | `app/research/experiment_tracker.py:146-183`, `app/research/hyperparameter_optimizer.py:158-188` | The whole `research` package is synchronous by design (correct for offline batch use), but there's no async adapter boundary — the first caller to invoke these from inside an `async def` endpoint freezes all concurrent request handling for the run's duration. Latent: nothing currently calls `research` from an API layer, because no API layer exists yet. |

### Medium (grouped)

- **Duplicate code**, recurring 3–6× per pattern rather than factored into a shared helper:
  - `_as_order_rejection` parsing (~75 lines ×3) — `angel_one.py:610-634`, `upstox.py:467-498`, `zerodha.py:431-454`.
  - `itertuples`-to-Pydantic boilerplate (~60 lines ×6, unlike the properly-factored single-value path) — `momentum.py:68-77,113-117`, `trend.py:35-45,73-83,138-149`, `volatility.py:74-85`.
  - Optional-dependency `try/except ImportError` guards (×6-7, drifting wording) — `experiment_tracker.py:135-142`, `hyperparameter_optimizer.py:161-167`, `statistical_analysis.py:79-85,139-144,197-204`, `explainability.py:63-69,127-133`.
  - Directional-consistency validators reimplemented independently ×3 — `strategy/models.py:59-89`, `ai/decision_models.py:59-100`, `paper/dto.py:102-125`.
  - Order-type vocabulary restated in two separate `StrEnum`s — `paper/dto.py:25-39`, `execution/dto.py:27-48`.
- **Coupling without an adapter** between bounded contexts that are documented as independent: `risk/dto.py:21` → `app.ai.decision_models.TradingDecision`; `research/models.py:21` & `hyperparameter_optimizer.py:26-27` → `app.analytics.dto.OptimizationTrialResult`. A reshape on either side breaks the dependent silently (no tests catch it).
- **Cache size-unawareness**: `IndicatorEngine`'s `LRUCache(256)` (`engine.py`) and `DatasetManager`'s `LRUCache(32)` (`dataset_manager.py:46-49`) bound entry *count*, not memory — a handful of wide feature matrices or large historical DataFrames can dominate resident memory with no ceiling.
- **Redundant recomputation**: requesting `ATR`+`ADX`+`SUPERTREND` (or `RSI`+`STOCH_RSI`) together each independently recomputes shared sub-calculations — no intermediate-result sharing within one `calculate()` batch (`engine.py:62-95`).
- Blind positional column renaming on `pandas-ta` output at 6 sites, pinned to a pre-release version range (`pandas-ta>=0.4.71b0,<0.5`) with no runtime shape guard.
- WS reconnect loop (`ws_client.py:113-123`) catches bare `Exception` around message dispatch too, so a latent tick-parsing bug is logged and retried forever instead of surfacing distinctly.
- `WebSocketError` (`app/domain/exceptions/broker.py:66-69`) is dead code — no reconnect-exhaustion path exists to ever raise it.
- Missing edge-case tests: retry-exhaustion, concurrent `start_websocket`/`stop_websocket`, and real multi-attempt WS reconnection (brokers); `InsufficientDataError`, single-row/short DataFrames (indicators).
- Production-readiness gaps below Critical/High: no scheduler despite `apscheduler` core dep; no outward-facing websocket server (distinct from the broker's outbound WS client); `alembic/docker/docs/scripts` all empty.

### Low

- Duplicate timestamp parsers (`angel_one.py:500-509`, `upstox.py:357-366`).
- Lossy sum-of-row-hashes cache fingerprint (`engine.py:129-141`) — commutative, theoretically collision-prone.
- `clean_float` doesn't handle `pd.NA`/nullable dtypes (theoretical — none in use yet).
- Trivial param-model repetition (`RSIParams`, `ATRParams` each restate `gt=0`).
- `.env.example` secret placeholders are currently inert (no matching settings field), but are a landmine if a future `SecretKey` field defaults to the same placeholder string.

### Explicitly not a problem (verified, don't touch)

- `app.domain` is a genuinely dependency-free core; **no circular imports anywhere in the repo**.
- Retry/backoff/timeout handling in brokers is correctly centralized in `base.py`/`app/utils/retry.py` — not duplicated per adapter.
- `httpx.AsyncClient` lifecycle (created once, closed once) is correct; no blocking calls found inside any `async def` in `app/brokers`.
- The indicator registry is genuine, working Open/Closed: adding an indicator requires zero edits to `engine.py`/`base.py`.
- Documentation quality is a real, repo-wide strength — consistently explains *why*, not just *what*.
- No hardcoded secrets, `eval`/`exec`/`pickle.load`, `subprocess`, or TLS-verification bypass anywhere in `app/`.
- 76/76 existing tests pass; what's tested (broker happy-path + one-retry, indicator core computation) is tested reasonably well.

---

## 3. Recommended priority order

1. **C4** (`ExperimentTracker` param-type LSP violation) — small, contained fix; prevents a silent correctness bug that's live today.
2. **H1** (credentials in WS URL) — security-sensitive, contained to `zerodha.py`/`ws_client.py`.
3. **H2** (raw exceptions leaking past broker boundary) — directly contradicts an existing, tested contract.
4. **C3** (research test coverage) — the largest single risk to future velocity; the codebase's most complex logic has no safety net.
5. **H6** (`.env.example` drift) — cheap to fix, actively misleading to anyone standing this up.
6. **H3** (broker `tradingsymbol` LSP violation) — needs a design decision (typed per-broker instrument identifier) before more broker-dependent code is built on top.
7. **H8/H9** (research: grid-search cap, sync/async boundary) — cheap guards now, expensive to retrofit once something calls into this package from a live service.
8. Everything else in Medium/Low — address opportunistically, none are blocking.
9. **C1/C2/H4/H5** (entrypoint, DB, auth, Redis) — these aren't bugs to "fix," they're the next phases of work already on your roadmap (Phases 4–14, foundation Sections 2–20). Sequencing them is a scope decision, not an audit finding.

No existing working code needs a rewrite. Every Critical/High item above is a targeted, localized fix within the file(s) cited — the architecture that exists is sound enough to build on.
