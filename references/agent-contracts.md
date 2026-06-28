# Multi-Agent Contracts

Use this reference when the stock workflow is split across sub-agents. The main agent remains the orchestrator and final decision owner. Sub-agents produce JSON artifacts; they do not produce final buy/sell ratings.

## Invariants

- Contract version: `worth-buy-stocks.agent.v1`.
- Alpaca price/volume data remains the primary evidence chain.
- `scripts/indicators.py` or `pipeline.build_result()` is the only scoring authority.
- News and event risk can only cap or downgrade through `llm_context`.
- Account context can adjust practical action and exposure advice, but does not change `score.composite`.
- `status=unavailable` is a valid non-blocking artifact state for optional overlays.
- Core `bars` data is blocking. If bars for the target or required benchmarks are missing, return `无法评分`.
- Never include API keys, account identifiers, or raw credential-bearing command output.

## Orchestration

1. Main agent parses the request and normalizes ticker symbols. Always include `SPY` and `QQQ` for relative strength when live bars are fetched.
2. In parallel, delegate independent work where useful:
   - Market data agent -> `bars.json`. (当日 snapshot 由 `indicators.py` 自动并行拉取并写入 `supplemental`，无需 sub-agent 单独采集。)
   - News/event-risk agent -> `news_context.json`.
   - Account overlay agent -> `account_context.json`.
   - Finnhub context agent -> `finnhub_context.json`.
   - Chart agent -> chart text/image only when requested.
   - QA agent -> final response review only after script output exists.
3. Main agent validates each JSON artifact with `scripts/validate_agent_contract.py`.
4. Main agent runs `scripts/indicators.py`, passing validated artifact files:

```bash
python3 "$SKILL_DIR/scripts/indicators.py" \
  --symbols AAPL,SPY,QQQ \
  --feed iex \
  --adjustment split \
  --llm-context-file news_context.json \
  --account-context-file account_context.json \
  --finnhub-context-file finnhub_context.json
```

For a fully reproducible offline merge, use:

```bash
python3 "$SKILL_DIR/scripts/indicators.py" \
  --symbols AAPL,SPY,QQQ \
  --input bars.json \
  --account-context off \
  --finnhub-context off \
  --snapshot off \
  --llm-context-file news_context.json \
  --account-context-file account_context.json \
  --finnhub-context-file finnhub_context.json \
  --snapshot-context-file snapshot_context.json
```

5. Main agent writes the final answer from the `score` fields. If an optional artifact is missing or invalid, state that the overlay was unavailable and do not infer it.

## Artifact: news_context

Purpose: feed `score.llm_overlay`. This artifact is produced by a news/event-risk agent.

Accepted wrapper:

```json
{
  "contract_version": "worth-buy-stocks.agent.v1",
  "kind": "news_context",
  "status": "ok",
  "symbols": {
    "AAPL": {
      "as_of": "2026-06-27T00:00:00Z",
      "sources": [
        {
          "id": "s1",
          "title": "Example filing title",
          "published_at": "2026-06-26",
          "url": "https://example.com/source",
          "provider": "SEC"
        }
      ],
      "red_flags": [
        {
          "type": "dilution",
          "severity": "medium",
          "note": "Announced stock offering",
          "source_id": "s1"
        }
      ],
      "data_trust": "ok",
      "catalyst": "Optional event note"
    }
  }
}
```

Backward-compatible direct shape is also valid:

```json
{
  "AAPL": {
    "as_of": "2026-06-27T00:00:00Z",
    "sources": [],
    "red_flags": [],
    "data_trust": "ok"
  }
}
```

Rules:

- `severity` must be `high`, `medium`, or `low`.
- `data_trust` must be one of `ok`, `suspect`, `unverified`, `stale`, `bad`, `unknown` (if present).
- `high` and `medium` require verifiable sources before they can be used for downgrade.
- `low` and positive catalysts are explanation only and must not raise the rating.
- Social media rumors, analyst targets, and model memory are not valid downgrade sources.

Unavailable shape:

```json
{
  "contract_version": "worth-buy-stocks.agent.v1",
  "kind": "news_context",
  "status": "unavailable",
  "reason": "News search unavailable"
}
```

## Artifact: account_context

Purpose: feed `score.account_overlay`. This artifact is produced by a read-only account agent.

```json
{
  "contract_version": "worth-buy-stocks.agent.v1",
  "kind": "account_context",
  "status": "ok",
  "as_of": "2026-06-27T00:00:00Z",
  "account": {
    "equity": 100000,
    "cash": 20000,
    "long_market_value": 75000
  },
  "positions": [
    {
      "symbol": "AAPL",
      "qty": 5,
      "market_value": 1000,
      "avg_entry_price": 190,
      "current_price": 200
    }
  ]
}
```

Rules:

- Only call `alpaca account get` and `alpaca position list`.
- Do not include account IDs, API keys, profile names, or raw command output.
- Do not call any order endpoint.
- If account read fails, return `status=unavailable` with `reason`.

## Artifact: finnhub_context

Purpose: provide optional supplemental quote/news/profile/earnings context. This artifact is produced by a Finnhub context agent.

```json
{
  "contract_version": "worth-buy-stocks.agent.v1",
  "kind": "finnhub_context",
  "status": "ok",
  "provider": "finnhub",
  "as_of": "2026-06-27T00:00:00Z",
  "symbols": {
    "AAPL": {
      "status": "ok",
      "symbol": "AAPL",
      "quote": {"current_price": 200.0},
      "profile": {"name": "Apple Inc"},
      "news": [],
      "earnings": [],
      "data_flags": []
    }
  }
}
```

Rules:

- Quote/profile/news/earnings are supplemental only.
- Finnhub-derived news may create conservative `llm_context` downgrade candidates through the existing script mapping.
- Positive news is never used to upgrade.
- `status=unauthorized`, `status=rate_limited`, and `status=unavailable` are valid non-blocking states.

## Artifact: snapshot_context

Purpose: optional supplemental same-day snapshot (latest price, quote, trade). Produced automatically by `indicators.py` in non-offline mode via `alpaca data multi-snapshots`; an offline artifact is only needed for replay/tests. Snapshot is supplemental only and never feeds `score`.

```json
{
  "contract_version": "worth-buy-stocks.agent.v1",
  "kind": "snapshot_context",
  "status": "ok",
  "as_of": "2026-06-27T00:00:00Z",
  "feed": "iex",
  "symbols": {
    "AAPL": {
      "symbol": "AAPL",
      "daily_bar": {"open": 193.5, "high": 196.0, "low": 193.0, "close": 195.5, "volume": 12345678, "vwap": 194.8, "date": "2026-06-26"},
      "daily_change_pct": 1.03,
      "quote": {"bid": 195.4, "ask": 195.6, "bid_size": 100, "ask_size": 200, "quote_time": "2026-06-26T20:00:00Z"},
      "spread": 0.2,
      "spread_pct": 0.1026,
      "latest_trade": {"price": 195.5, "size": 50, "trade_time": "2026-06-26T19:30:00Z"},
      "minute_bar": {"open": 195.4, "high": 195.6, "low": 195.3, "close": 195.5, "time": "2026-06-26T20:30:00Z"}
    }
  }
}
```

Rules:

- Only `alpaca data multi-snapshots` is used; never place orders or print credentials.
- All fields except `symbol` are optional — a partial snapshot (e.g. `daily_bar` only) is valid.
- Snapshot writes to `result.supplemental.snapshots` (summary) and each `symbol.supplemental.snapshot` (detail).
- `status=unavailable` is a valid non-blocking state; scoring continues without it.

## Artifact: bars

Purpose: provide reproducible offline market data for `--input`.

```json
{
  "contract_version": "worth-buy-stocks.agent.v1",
  "kind": "bars",
  "status": "ok",
  "feed": "iex",
  "adjustment": "split",
  "bars": {
    "AAPL": [
      {"t": "2026-06-26T04:00:00Z", "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000000}
    ],
    "SPY": [],
    "QQQ": []
  }
}
```

Rules:

- Each bar must include `t`, `o`, `h`, `l`, `c`, and `v`.
- Symbols must be uppercase in final artifacts.
- For live scoring, include `SPY` and `QQQ` whenever possible.
- This is the only blocking artifact class.

## Artifact: result

Purpose: final machine-readable output from `scripts/indicators.py`.

Required shape:

```json
{
  "feed": "iex",
  "adjustment": "split",
  "symbols": {
    "AAPL": {
      "score": {
        "verdict": "是",
        "composite": 80.1,
        "blocking_reasons": [],
        "trade_plan": {},
        "account_overlay": {},
        "llm_overlay": null
      }
    }
  }
}
```

Rules:

- Main agent must not rewrite `score.verdict` or `score.composite`.
- If `score` is missing for the target, return `无法评分`.
- Final prose may summarize, but must preserve `blocking_reasons`, account overlay availability, and news overlay availability.

## Validation CLI

Use before merging sub-agent artifacts:

```bash
python3 "$SKILL_DIR/scripts/validate_agent_contract.py" --kind news_context news_context.json
python3 "$SKILL_DIR/scripts/validate_agent_contract.py" --kind account_context account_context.json
python3 "$SKILL_DIR/scripts/validate_agent_contract.py" --kind finnhub_context finnhub_context.json
python3 "$SKILL_DIR/scripts/validate_agent_contract.py" --kind bars bars.json
python3 "$SKILL_DIR/scripts/validate_agent_contract.py" --kind snapshot_context snapshot_context.json
python3 "$SKILL_DIR/scripts/validate_agent_contract.py" --kind result result.json
```

The CLI prints JSON. `status=ok` means the artifact is structurally safe to hand to the next stage. `status=error` means the main agent must discard that artifact or ask the relevant agent to regenerate it.

On `status=error`: the CLI exits with code 1 and prints a JSON body with `error_code` (`json_invalid` or `contract_invalid`) and `message`. The main agent should parse the exit code (or the JSON `status`) and, for optional overlays, mark that overlay as unavailable and continue; for the blocking `bars` artifact, abort with `无法评分`. `scripts/indicators.py` also enforces these checks in-process at load time — optional overlay failures are silently dropped with a stderr warning, `bars` failures are fatal, and a post-run `result` self-check warns on stderr without blocking output.
