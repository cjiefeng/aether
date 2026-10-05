# Milestone report

## M8: Catalysts + market structure (2026-10-05)

Dated catalysts with deterministic hit/slip resolution, FINRA short interest with a spike rule, and options analytics (skew, implied moves into catalysts, positioning, IV rank) on the ticker page and in the review pack. **$0 LLM**: nothing in M8 calls a model, and no options metric reaches targets or sizing.

### Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| A test event resolves a catalyst | ✅ | `tests/test_catalysts.py::test_test_event_resolves_catalyst_citing_it`: a synthetic ACME `roadmap_hit` event naming the seed keyword → `hit`, `resolved_by_event_id` = the event, cited in the note. The same file checks that quarantined, low-materiality, keyword-missing, other-company and too-early events don't resolve; slips by event and by window; earnings hit by an 8-K 2.02; a rescheduled earnings date → `cancelled`; lock-ups resolve by date; owner marks stick. |
| Short-interest spike rule fires on a fixture | ✅ | `tests/test_short_interest.py::test_spike_rule_fires_on_rise_once_and_is_idempotent` (+6 pp → one RISK event, T1, origin `finra`, idempotent on re-run) and `test_crossing_the_level_fires_but_staying_above_does_not`. The recorded real FINRA files (2026-08-31, 2026-09-15, filtered to the universe) parse and ingest in `test_recorded_files_*`. |
| Options metrics match hand-computed values on a recorded chain fixture | ✅ | `tests/test_options_analytics.py::test_skew_matches_hand_computation_on_recorded_chain`: on the real IONQ chain recorded 2026-10-04, the 30-day 25-delta skew is recomputed in the test from named strikes (put 39/40 and call 50/55 on 2026-10-30; put 38/40 and call 50/55 on 2026-11-06) with an independent Black-Scholes delta and interpolation; it matches to 2e-6. The straddle move is checked by hand from the 44-strike call and put. |
| Implied move uses the first expiry after a synthetic catalyst | ✅ | `test_implied_move_matches_straddle_by_hand_and_uses_first_expiry_after`: catalyst 2026-10-31 → 2026-11-06 (not 10-30 or 11-20); a catalyst on an expiry day takes the next one. `test_implied_move_needs_the_true_first_listed_expiry`: if the true first listed expiry wasn't fetched, the move is null with a reason. |
| A thin chain is flagged, not reported | ✅ | `test_thin_chain_is_flagged_not_reported`: ATM IV, term, skew and implied moves are all null, with `quality.thin` set. The ticker page shows "Thin chain: not reported." and no numbers (`tests/test_web_catalysts.py::test_ticker_page_panels`). |
| No options metric reaches `profile_targets` | ✅ | `test_no_options_metric_reaches_profile_targets`: publishing, writing wild options snapshots for every symbol, then publishing again gives byte-identical weights and input hashes. Statically, nothing under `portfolio/` reads options data. |
| Tests green, no network | ✅ | `make test`: 520 passed |
| ruff / mypy / pip-audit | ✅ | `make lint`: clean, `mypy --strict` on 116 files, no known vulnerabilities |
| `make secrets-scan` clean | ✅ | gitleaks: no leaks |

### What was built
- **Schema `0009_catalysts`** (hand-written, STRICT):
  - `catalysts` (natural `key` per origin: seed / earnings / lock-up; status and resolution CHECKs; fact and event links);
  - `short_interest` (WITHOUT ROWID) and `short_interest_files`;
  - `events` and `event_sources` rebuilt for the new `finra` origin.
  - **Migrations now run with foreign keys off**, followed by `PRAGMA foreign_key_check`. With them on, rebuilding `events` would have cascade-deleted its children. Tested on a populated DB: every child row survives.
- **Catalysts (`catalysts/`, `config/catalysts_seed.yaml`)**:
  - Seeds, each linked to a fact: IBM Kookaburra/Cockatoo/Starling (`ibm_roadmap_ftqc`); DARPA QBI Stage C for IONQ and QNT (new fact `darpa_qbi_stage_b_duration`, window from 2026-11-06 with no end); the QNT lock-up on 2026-11-30 (`qnt_lockup_expiry`).
  - Earnings dates and prospectus lock-ups are added automatically.
  - Deterministic resolution (see the README); the strict loader rejects unknown fact ids, symbols and keys, and the worker checks the seed file at startup.
  - The `mark_catalyst` CSRF'd command (hit / slipped / cancelled / reopen, optional event id and note).
  - Job every 30 minutes and after the EDGAR, earnings and classifier runs; a `job_runs` row only when something changed.
- **Short interest (`providers/finra.py`, `ingest/short_interest.py`)**:
  - FINRA bi-weekly files, 12-month backfill; settlement dates step back over weekends.
  - Percentage of shares outstanding from XBRL.
  - The `finra_short_interest_spike` rule (≥ 5 pp rise or crossing above 25%, materiality 3) and a matching open flag.
  - Daily 07:20 SGT. `make record-short-interest` records fixtures.
- **Options analytics (`options/analytics.py`)**:
  - 30-day 25-delta skew;
  - implied moves into catalysts (the chooser now also fetches the first expiry after each catalyst, and every listed expiry is recorded);
  - volume vs the 20-snapshot median;
  - IV rank and percentile over Aether's own 252 snapshots.
- **Dashboard**:
  - `/catalysts`: timeline chart, upcoming list with fact-status badges and a Mark form, hit/slip record; Catalysts in the nav.
  - Overview: catalysts and earnings for the next 12 months.
  - Ticker page: catalysts, the short-interest table and the options panel.
  - `/feed`: FINRA events.
- **Review pack**: catalysts for the next 90 days and the options panel per name. The Telegram text gets one line per name (IV30, rank or "building history", the next implied move) plus catalyst dates with an [unconfirmed] label for facts that aren't signed off. Still no holdings.

### Decisions (deviations from the spec / plan)
1. **Short % is of shares outstanding, not float** (plan decision 1). FINRA publishes no float, and no free T1 source does. The column is `pct_shares_out` and every label says so. QNT shows no percentage, because its XBRL share count isn't a single company-wide figure (dual class), and QTUM is an ETF.
2. **Spike thresholds** (plan decision 2): ≥ 5 pp rise or crossing above 25%, materiality 3, T1, direction −1. It fires on the crossing, not at every report above the level. Spikes found in backfilled files are dated by settlement date, so they never alert; live files are dated when Aether first sees them.
3. **New event origin `finra`** (plan decision 3), with the migration change above.
4. **QBI Stage C has no end date** (plan decision 4), so it never slips on the clock. Only an event or your mark resolves it.
5. **IBM roadmap catalysts sit on the context ticker `IBM`.** News isn't tagged with IBM, so for context tickers the keyword alone ties an event to the catalyst. For watchlist names the event must also be on that ticker, or on no ticker.
6. **qbi_stage_change resolves by direction**: +1 → hit (selected), −1 → slipped (not selected).
7. **Earnings whose date passes with no 8-K stay `upcoming`** and show "no result yet". They aren't marked slipped, because a late 8-K is common.
8. **Options stay on yfinance.** Tiger option quotes still need a paid permission (M5 finding).
9. **Implied moves use only catalysts with a known end date within `max_days`.** Year-long roadmap windows and open-ended QBI windows get none.

### Facts
- **Added `darpa_qbi_stage_b_duration`** (`verified_by_claude`): DARPA's Stage B page calls Stage B "yearlong" and gives no Stage C date. It's checked against darpa.mil and `FACTS.md` is regenerated.
- No other facts changed.

### Open questions
- **Stage C timing:** when DARPA will announce Stage C invitations (carried on the new fact).
- **QNT short % of shares:** needs a class-aware share count (Class A + B from the 10-Q cover page). Today it shows the share count and days to cover only.
- **Weekend snapshots:** the live check ran on a US Sunday. Most ATM straddles failed the quality gates (stale or one-sided quotes), so most implied moves were null with that reason; QNT's lock-up move (±34.1%) passed. Weekday 06:40 SGT snapshots should fill in more of them.
- **QTUM options:** its 30-day ATM IV is null ("beyond the last usable expiry (11 days)"): the QTUM chain is thin beyond the front month, as in M5.
- **FINRA terms:** the files are published for non-commercial use, and Aether keeps only the rows for its own six symbols.

### Live check (isolated compose project `aether-m8` on 127.0.0.1:8090, its own volume and image tag; Anthropic, Telegram, Tiger and Massive blanked; your SEC user agent passed so EDGAR could supply share counts; a throwaway password; torn down afterwards, and your `aether` stack kept running throughout)
- The worker migrated a fresh DB to `0009_catalysts`. EDGAR, earnings, prices, strategies, options and fx all came back `ok`.
- **Short interest:** 113 rows across 23 settlement dates (2025-10-15 → 2026-09-15). At 2026-09-15: IONQ 10.6%, QBTS 18.4%, RGTI 18.7%, INFQ 9.3% of shares outstanding; QNT and QTUM had no percentage. One backfilled spike: INFQ at 2026-06-30 (+6.3 pp, 10.0%), dated by settlement, so no alert.
- **Catalysts:** 11 rows: 6 seeds plus 5 earnings dates (IONQ 11-04, QBTS 11-05, QNT 11-09, RGTI 11-10, INFQ 11-12).
- **Options:** 30-day skew for IONQ −10.9, QBTS −6.8 and RGTI −8.3 points. QNT implied move into its lock-up: ±34.1% (2027-01-15 expiry). The other implied moves were null with reasons (see open questions).
- **Browser:**
  - `/catalysts` timeline and table rendered; a Mark (IBM Starling → cancelled) went through the command queue and the worker applied it (`resolution = owner`).
  - The QNT ticker page showed the catalysts, short interest and options panel.
  - `/feed?category=short_interest_spike` showed the FINRA event.
  - No console or CSP errors.
- The review pack built on the live data is plain text with no `$`, and every catalyst line names its ticker (fixed during the check: the two QBI lines looked identical).

### Owner checklist
- [ ] Review `config/catalysts_seed.yaml` (seeds, keywords, the 90-day lead/grace, materiality ≥ 3) and the `short_interest` section of `config/rubric.yaml` (5 pp / 25% / materiality 3).
- [ ] Review the new fact `darpa_qbi_stage_b_duration` on `/facts` and sign it off if you agree (edit `facts.yaml`).
- [ ] After merging: `./deploy.sh`. The worker migrates to `0009_catalysts` (migrations 0002–0009 if your stack is still on 0001). The 12-month FINRA backfill (~24 files of ~3 MB) runs about 5 minutes after start.
- [ ] Still open from earlier milestones: `DAILY_LLM_BUDGET_USD`, `MASSIVE_API_KEY`, the Telegram bot setup and the facts not yet signed off.

### How to verify
```bash
make test            # 520 passed, network blocked
make lint            # ruff, mypy --strict, |safe ban, broker + LLM import checks, pip-audit
make secrets-scan    # gitleaks: no leaks
./deploy.sh          # after merge; then open http://<lan-ip>:8080/catalysts and a ticker page
```

## M7: Classifier (2026-10-05)

Every news and research item is now classified as SIGNAL, NOISE or RISK. The pipeline (spec §5.2) is: deterministic rules first, then Claude Sonnet 5.5 with **no tools** and strict JSON validation, then trust-tier caps in code. Injection attempts are quarantined. The Feed page and `make eval` are new. EDGAR filings keep their M2 rules.

### Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| T3-only event can't exceed materiality 2 (unit test) | ✅ | `tests/test_caps.py::test_t3_only_event_cannot_exceed_two`: raw 5 → stored 2. Also tested: a single T2 → 3; a second independent T2 or a T1 source merged in later lifts the stored value back to raw; a syndicated copy doesn't. |
| 100% of adversarial cases flagged | ✅ | Acceptance run on the owner-labelled set (`evals/results/classify-v1-dd7bef0c.json`, 2026-10-05 13:10Z): **5/5 flagged, all 5 by the model itself** (the regex backstop caught 2 of them as well). Class unchanged in 5/5; materiality nudged +1 in 1/5 (2/5 in the earlier baseline; see open questions). Offline: `tests/test_eval.py`, `tests/test_classify_pipeline.py::test_model_flag_quarantines_and_excludes_from_alerts` and `test_regex_backstop_quarantines_when_the_model_misses_it`. |
| ≥85% class agreement | ✅ 93.4% | Same run, **owner-labelled** (you reviewed all 61 rows and kept every label): 57/61 agree (precision/recall: SIGNAL 83%/94%, NOISE 98%/93%), category agreement 88.5%, materiality MAE 0.21. The earlier baseline on the same labels and prompt scored 95.1%; one item (g038) flipped between runs, so expect a point or two of run-to-run variation. |
| ≥95% RISK recall | ❌ not measurable yet | The golden set has **0 RISK rows**. None of the 116 real RSS items ingested (live, recorded and older feed pages) was RISK news for the watchlist. Dilution, insider and compliance risk come through EDGAR and the deterministic rules. Every RISK category is listed as under-sampled (`evals/README.md`); the research backfill on first deploy should supply real ones. |
| Golden set from real ingested items, owner labels | ✅ | `evals/classifier_golden.jsonl`: 61 rows, each the title, excerpt and URL exactly as Aether's RSS ingest stored them. 16 SIGNAL and 45 NOISE, 42 T1 and 19 T2. Labels proposed by Claude Code; all 61 reviewed by you (`labeled_by: owner`, no changes). |
| Tests green, no network | ✅ | `make test`: 477 passed. The classifier talks to the real SDK over an in-process fake transport. |
| ruff / mypy / pip-audit | ✅ | `make lint`: clean, `mypy --strict` on 106 files, no known vulnerabilities |
| `make secrets-scan` clean | ✅ | gitleaks: no leaks |

### What was built
- **Schema `0008_classify`** (hand-written, STRICT):
  - `event_tickers.direction` (per-ticker direction; rebuilt keeping WITHOUT ROWID);
  - `classify_state` (retry / batched / failed / done, attempts, batch and custom ids);
  - `eval_runs` (one row per eval result).
- **Rubric (`config/rubric.yaml` → `classifier:`)**:
  - the spec §5.1 categories with class, a typical materiality range and a definition, and 1–5 materiality anchors;
  - headline rules (analyst rating, listicle), an empty `noise_domains` list and the injection backstop regexes.
  - Strict pydantic: unknown keys are rejected; the category/class pairs must equal the spec mapping, which is also the DB CHECK; regexes must compile.
- **Classifier (`classify/`)**:
  - `prompt.py`: system prompt = rubric + S1 notice. User message = tickers, aliases, source, tier, date and the wrapped title and excerpt; nothing else. A fixed JSON schema. `prompt_version` hashes the template, the schema and the rubric section, so any edit is a new version.
  - `llm.py`: schema validation plus the checks above (category in class, verbatim evidence quote, exact ticker coverage, refusal and truncation). Invalid answers are rejected, never repaired.
  - `caps.py`: the S1 caps, re-applied whenever a source merges (`news_events._recount`).
  - `rules.py`: `classify_news` for non-T1 headlines.
  - `pipeline.py`:
    - rules → sync calls under the soft budget (max 60 per run; stops on a budget refusal; 3 API errors in a row fail the job);
    - more than 25 waiting → one Message Batch, with a poller to ingest the results;
    - one short write per result: classification, directions, caps, quarantine.
- **LLM wrapper**: `output_format` (structured outputs). `fallbacks: "default"` is sent only to models that accept it. `submit_batch` checks tools per purpose.
- **Jobs**: `classify` runs every 10 minutes and right after RSS, a research sweep or a backfill poll. `classify_batch_poll` runs while a batch is open. Neither writes a `job_runs` row when idle.
- **Eval (`classify/eval.py`, `make eval`)**:
  - runs the production pipeline on the golden set plus 5 synthetic adversarial cases, against a throwaway DB;
  - reports per-class precision/recall, the confusion matrix, category agreement, materiality MAE, under-sampled categories and acceptance;
  - writes `evals/results/<version>.json` (committed), which the worker loads into `eval_runs`.
  - `scripts/golden_candidates.py` / `make golden-candidates DB=…` exports real events from a DB copy, read-only.
- **Dashboard**:
  - `/feed` (filters, NOISE hidden by default, quarantine warning, "capped from N", directions, sources with tiers, failed list, counts, latest eval for this prompt);
  - class badges on `/news` and on the ticker news card;
  - a Feed nav link.
- **Config**:
  - `CLASSIFIER_MODEL` defaults to `claude-sonnet-5-5`;
  - `DAILY_LLM_BUDGET_USD` defaults to $5 (Settings, compose, `.env.example`);
  - a `classify:` section in `llm.yaml`.

### Decisions (deviations from the spec / plan)
1. **Your calls (2026-10-05):**
   - classifier Sonnet 5.5 (not the cheap tier);
   - soft budget $5;
   - the backlog goes through Message Batches outside the daily budget;
   - one live baseline eval.
2. **The golden set is news-only.** EDGAR items are classified by deterministic rules (unit-tested since M2), so they aren't part of the LLM eval. That's why RISK recall is unmeasurable for now rather than padded with rule hits.
3. **`listicle_or_momentum` also covers "no new company fact" items**: conference participation, earnings-date notices, blogs, appointments that aren't departures. The spec has no "other" category, and these are a large share of IR feeds. It's written into the category definition, so it's part of the prompt version. Change it if you prefer a different home for them.
4. **Headline rules skip T1 sources.** A company's own release always goes to the model, even if its headline says "upgrades".
5. **Injection flag = model OR regex backstop.** The acceptance counts both. The report also gives the model-only rate (5/5 in the baseline).
6. **Adversarial "unchanged" compares class and post-cap materiality** with the clean run of the same item. Quarantined items are excluded downstream regardless.
7. **Rule hits store the headline as the evidence quote**, with direction 0 and confidence 0.9 (headline rules) or 1.0 (noise domains).
8. **Golden ids aren't DB ids.** Each row keeps the `event_id` from the scratch ingest DB it was exported from, plus the canonical URL. Your live DB had no events yet: it's still on schema 0001, see the owner checklist.
9. **Older feed pages (`?paged=N`) were used once** to find enough real items, through Aether's own RSS client (robots.txt respected). The scheduled ingest is unchanged.

### Facts
- No facts changed or added.

### Open questions
- **Adversarial materiality drift:** the model flagged every injection but still raised materiality by 1 in some cases (2/5 in the baseline, 1/5 in the acceptance run; class always unchanged). The quarantine makes this harmless downstream. If you want it fixed in the prompt, that's a new prompt version and another eval run (about $0.20).
- **Class disagreements in the acceptance run** (label → model):
  - g026 WISeSat business combination: NOISE → SIGNAL m_and_a.
  - g036 QC Design 10× logical-error claim: NOISE synthetic_benchmark → SIGNAL logical_qubit_milestone.
  - g038 IonQ/FIU Superion 256 deployment: NOISE partnership_no_value → SIGNAL contract_with_value (agreed in the baseline run).
  - g044 Infleqtion/Japan Moonshot "Shunkai" operational: SIGNAL roadmap_hit → NOISE partnership_no_value.
  - Three more agree on class but differ on category (g001, g004, g043).
- **Eval results are keyed by prompt version,** so the acceptance run replaced the baseline file `classify-v1-dd7bef0c.json` (the baseline is still in git history). The worker loads each run into `eval_runs` by version and timestamp.
- **RISK coverage:** as above. After the backfill, run `make golden-candidates` against a copy of the live DB and add real RISK items.
- **`noise_domains` is empty.** Listing outlets as noise would be your opinion call, so I left it out.

### Live check (isolated compose project `aether-m7` on 127.0.0.1:8090, its own volume and image tag; Anthropic, Telegram, Tiger, SEC and Massive blanked; a throwaway password; torn down afterwards)
- The worker migrated a fresh DB to `0008_classify` and loaded 1 eval result into `eval_runs`. `news_rss` came back `ok` with 32 items.
- `classify` came back `ok`, warning "ANTHROPIC_API_KEY is not set; 32 items wait for the classifier", which is the intended behaviour without a key.
- Browser, logged in:
  - `/feed` rendered the counts (waiting 32) and the eval line (then provisional, 95% agreement, RISK n/a, 5/5 adversarial, below the bar because of RISK);
  - `/news` showed "pending" badges;
  - no console or CSP errors.
- **Your `aether` stack:** its app and worker containers were stopped at 12:57:53Z (a `docker stop`/kill, not an exit on their own), while the isolated stack was coming up. I didn't send that stop: the isolated project shares no containers, volumes or network with yours. After you said it was fine, I restarted the same containers (`docker start`; no rebuild or deploy), and both report healthy. It's still running the M0 image (schema `0001_baseline`), so deploying will apply migrations 0002–0008 in one go.

### Owner checklist
- [x] **Review the golden set:** done (all 61 rows `labeled_by: owner`), and the acceptance `make eval` has run ($0.21).
- [ ] **Set `DAILY_LLM_BUDGET_USD=5`** in `.env`. Your `.env` still says its own value; the new default applies only when the variable is empty.
- [ ] Review the `classifier:` section of `config/rubric.yaml` (materiality ranges, the broadened `listicle_or_momentum`, headline rules, injection patterns) and the `classify:` section of `config/llm.yaml`.
- [ ] Optional: list noise-only domains in `noise_domains`.
- [ ] After merging, run `./deploy.sh`. With `ANTHROPIC_API_KEY` set, the M6 research backfill submits, and its results will be classified through one Message Batch (with Sonnet 5.5, roughly $1–3 for ~1,000 items).
- [ ] Still open from earlier milestones: `MASSIVE_API_KEY`, the Telegram bot setup and the facts not yet signed off.

### How to verify
```bash
make test            # 477 passed, network blocked
make lint            # ruff, mypy --strict, |safe ban, broker + LLM import checks, pip-audit
make secrets-scan    # gitleaks: no leaks
make eval            # live (costs ~$0.20): golden set + adversarial, writes evals/results/
./deploy.sh          # after merge; then open http://<lan-ip>:8080/feed
```

## M6: News & research ingest (2026-10-05)

Phase 2 starts. It adds RSS news, Claude web-search research runs (a twice-daily sweep plus a one-time 12-month backfill through the Message Batches API) and the one LLM wrapper with the soft budget guard. Everything lands as **unclassified, untrusted** events; classification is M7.

### Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| 3 syndicated copies → 1 event with independent count 1 | ✅ | `tests/test_news_dedupe.py::test_three_syndicated_copies_are_one_event_with_independent_count_one`: the same headline and body on two sites plus a wire copy → 1 event, 3 `event_sources` rows (2 marked syndicated), independent count 1. `test_independent_outlets_count_separately`: three different outlets → 3, a same-domain copy adds nothing. |
| Budget breach stops calls | ✅ | `tests/test_llm_client.py::test_budget_breach_stops_calls`: spent + worst case > `DAILY_LLM_BUDGET_USD` → `BudgetExceeded`, **zero HTTP requests**, a `budget_refused` row. `test_budget_counts_worst_case_estimate` (refused even when spend alone fits); `tests/test_research.py::test_sweep_stops_on_budget_and_keeps_partial` (the sweep stops at the next name and keeps what it got). |
| Excerpts ≤ 600 chars | ✅ | 500 enforced in code (`clip_excerpt`), DB CHECK at 600 on `events` and now `event_sources`. `test_excerpt_capped` (an 8,000-char body), `tests/test_rss.py::test_recorded_feeds_ingest` (every excerpt from five real recorded feeds). Live: longest excerpt 499. |
| Tests green, no network | ✅ | `make test`: 429 passed. The real `anthropic` SDK runs against an in-process `httpx2.MockTransport`; RSS replays five feeds recorded 2026-10-05. |
| ruff / mypy / pip-audit | ✅ | `make lint`: clean, `mypy --strict` on 98 files, `check_llm_imports: ok`, no known vulnerabilities |
| `make secrets-scan` clean | ✅ | gitleaks: no leaks. The test key is a synthetic non-key string. |

### What was built
- **Schema `0007_news`** (hand-written, STRICT):
  - New `feed_state` (conditional-GET state per feed) and `research_runs` (kind, ticker, window, status, model, batch/custom id, results, new events, cost, audit payload).
  - `event_sources` rebuilt (STRICT, WITHOUT ROWID kept) with title, date, excerpt, title/excerpt simhash, `syndicated` and origin. EDGAR rows survive (tested).
  - `llm_calls` rebuilt with status (`ok/error/budget_refused`), `batch`, cache-write tokens, request id, research run and error.
  - The `llm_budget` alert kind; indexes on `events(simhash)` and `(source_domain, published_at)`.
- **LLM wrapper (`llm/client.py`, `llm/pricing.py`, `config/llm.yaml`)**:
  - The only `anthropic` importer (`scripts/check_llm_imports.py` in `make lint`).
  - Calls are refused for an unpriced model, or when tools are set outside `research*` purposes (and research may use only web search).
  - The budget guard sums today's (SGT) synchronous `llm_calls` plus a worst-case estimate.
  - The system prompt is cached. `fallbacks: "default"` handles refusals on synchronous calls, and a fallback is priced per iteration model.
  - One `llm_calls` row per attempt, written after the response. The key is scrubbed from errors; the SDK and HTTP loggers stay at WARNING.
  - An 80% alert, once per SGT day, comes from the alerts job.
- **RSS (`providers/rss.py`, `ingest/news_rss.py`, job `news_rss` hourly)**:
  - stdlib XML with DTDs rejected; RSS 2.0 and Atom.
  - https only, a 5 MB cap, robots.txt, conditional GET.
  - IR feeds are pinned to their ticker. Press feeds keep alias, ticker or theme-keyword items only.
- **Shared writer (`ingest/news_events.py`)**:
  - canonical URLs; 64-bit simhash on the normalized headline (publisher suffix stripped); a 7-day merge window at ≤3 bits;
  - syndication detected by wire/mirror domain or the same excerpt;
  - independent count = distinct registrable domains among non-syndicated sources;
  - best tier wins, and the earliest report dates the story.
- **Research (`research/runner.py`)**:
  - Prompts hold the ticker, the company name and the window only; the system prompt carries the S1 notice.
  - **Events come only from `web_search_result` blocks.** The excerpt is a verbatim `cited_text`. The date comes from `page_age`, else the retrieval time flagged "found". Results dated outside the window are dropped. The model's text is kept in `research_runs.payload` for audit only.
  - **Sweep** 08:00/20:00 SGT, plus **Run research sweep now** (CSRF'd `research_sweep` command).
  - **Backfill**: one batch, submitted once ~2 min after start, polled every 15 min, idempotent. Failed or stale submissions are retried. It's excluded from the daily budget.
- **Dashboard**:
  - `/news`: items with tier badges, independent/syndicated counts, ticker/origin filters, LLM spend vs budget, backfill status, feed health, sweep history.
  - A "Recent news" card on the QTUM and pure-play ticker pages, and a News nav link.
  - No inline script or style. `DAILY_LLM_BUDGET_USD` is passed to `app` for display only.
- **Config**:
  - `sources.yaml`: company domains as T1 (ionq, rigetti, quantinuum, dwavequantum, dwavesys, infleqtion), five feeds, syndicators, theme keywords.
  - `watchlist.yaml`: one company-name alias per name.
  - `RESEARCH_MODEL` and `RESEARCH_BACKFILL` go to the worker only.

### Decisions (deviations from the spec / plan)
1. **Your calls (2026-10-05):**
   - the official `anthropic` SDK (1.11.0) is added;
   - research uses `claude-opus-5-5`;
   - **the backfill runs automatically with no cap**, so it sits outside the daily soft budget and only the Console limit caps it;
   - RSS keeps watchlist + theme items only.
2. **Opus sweep cost.** Two sweeps a day × 6 names come to roughly $1.5–2/day, which leaves about $1 of the $3 soft budget for M7 classification. M7 will need a higher budget or a cheaper research model; I'll raise it in the M7 plan.
3. **Batch spend isn't counted in "today"** for the soft budget, so the backfill can't block that day's sweeps. It's shown separately on `/news`.
4. **robots.txt unreachable → the feed is still read.** Rigetti's and Quantinuum's IR hosts time out on robots.txt but serve their feeds; an explicit `Disallow` is always honoured. A published feed is an invitation to subscribe; it isn't scraping.
5. **No feedparser and no Public Suffix List.** The stdlib parser rejects DTDs, as the ECB parser does. Registrable domains use a small multi-part suffix list.
6. **Direct web search (`allowed_callers: ["direct"]`)**, not dynamic filtering, so the raw result blocks come back to build events from.
7. **Worst-case budget estimate:** prompt chars ÷ 3 at the cache-write rate, plus 3k + 6k input tokens per allowed search, plus `max_tokens` output, plus every search. It's deliberately pessimistic, so the guard refuses early rather than late.
8. **Date-only `page_age` values are stored at 12:00 UTC.** Undated results get the retrieval time and a "found" badge.

### Facts
- No facts changed. No new facts were seeded: news items are evidence, not facts.

### Open questions
- **Bot-walled feeds:** `investors.ionq.com`, `ir.dwavesys.com` and `hpcwire.com/feed/` answer 403 to non-browser clients. IonQ and D-Wave are covered by EDGAR 8-Ks and research only. If you know other official feeds for them, add them to `sources.yaml`.
- **The T2 list is short** (three outlets). Research is allow-listed to T1+T2, so the backfill only finds what those domains published. Widening the allow-list is your call.
- **`page_age` precision** varies (sometimes relative, sometimes just a month). The reaction engine (M9) should treat research-dated items with care.
- **Research and backfill weren't exercised live**: there was no API key in the test stack. The first deploy with a key will run the backfill; check `/news` → Backfill afterwards.

### Live check (isolated compose project `aether-m6` on 127.0.0.1:8090, its own volume and image tag; Anthropic, Telegram, Tiger and SEC blanked; your stack wasn't touched)
- Migration `0007_news` applied on a fresh DB. `news_rss` came back `ok` with 31 rows. Rigetti, Quantinuum and Infleqtion kept 10/10 each, Quantum Computing Report 1/10 (a D-Wave item, tagged QBTS) and The Quantum Insider 0/10 (none of its current items named a watchlist company or theme keyword).
- All items were T1/T2 with no syndicated duplicates in this sample; the longest excerpt was 499 chars.
- Worker log: "research disabled: ANTHROPIC_API_KEY is not set". No backfill was submitted.
- Browser, logged in: `/news` rendered spend ($0.00 of $3.00), feed health and items. **Run research sweep now** → command `done` with "ANTHROPIC_API_KEY is not set". No console or CSP errors. `curl /news` without a session → 303.
- The stack and its volume were removed afterwards.

### Owner checklist
- [ ] **Before deploying, decide about the backfill.** With `ANTHROPIC_API_KEY` in `.env`, the worker submits the 12-month backfill (~$10–15 with Opus, no cap) about 2 minutes after start. Set `RESEARCH_BACKFILL=false` if you want to hold it.
- [ ] Make sure the **Console workspace spend limit** is set (S3). It's the only hard cap on the backfill.
- [ ] Review `config/llm.yaml` (prices, effort `low`, 5 searches per call, the 3-day sweep window, 12 months) and `config/sources.yaml` (T1 company domains, feeds, syndicators, theme keywords).
- [ ] Consider raising `DAILY_LLM_BUDGET_USD` before M7 (decision 2).
- [ ] After merging: `./deploy.sh`. The worker migrates to `0007_news`; RSS fills `/news` within a minute.
- [ ] Still open: `MASSIVE_API_KEY`, the Telegram bot setup, and the three facts not yet signed off.

### How to verify
```bash
make test            # 429 passed, network blocked
make lint            # ruff, mypy --strict, |safe ban, broker + LLM import checks, pip-audit
make secrets-scan    # gitleaks: no leaks
./deploy.sh          # after merge; then open http://<lan-ip>:8080/news
```

## M5: Password, holdings & rebalance (2026-10-05)

Phase 1b is complete. $0 LLM: every number is computed in code, and nothing calls a model. Built against the amended spec (PR #10: family-fund sleeve mandate, monthly targets, research overlay, options snapshots, review pack).

### Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Unauthenticated request to any data route → `/login` | ✅ | `tests/test_auth.py::test_unauthenticated_data_routes_redirect_to_login`: 10 pages and JSON APIs → 303 to `/login?next=…`; htmx requests get 401 + `HX-Redirect`. Commands without a session are refused too. Live: `curl /holdings` → 303. |
| 6th failed login in 15 min → 429 | ✅ | `test_sixth_failed_login_is_429_and_password_never_logged`: 5 × 401, then 429 even with the right password. Another IP is unaffected. The log has the IP and never the password. |
| Missing hash/secret → app won't start | ✅ | `test_missing_or_malformed_secrets_app_wont_start` (6 cases: missing, short secret, plaintext, weak scrypt cost, bad base64), `test_web_main_exits_without_password` (exit 1, uvicorn never runs). Live: the image without the env vars logs "refusing to start" and exits. |
| Holdings edit goes through `commands`; the authorizer still denies direct writes | ✅ | `tests/test_holdings.py::test_holdings_edit_goes_through_commands` (POST → `commands` row → worker → `holdings` + `holdings_history` with the command id), `test_command_engine_cannot_write_holdings` (INSERT/UPDATE/DELETE on holdings, history and settings: "not authorized"), `test_worker_handlers_apply_update_and_replan` (through the real scheduler wiring). |
| Same inputs → identical plan | ✅ | `tests/test_rebalance.py::test_same_inputs_give_identical_plan_and_hash` (canonical JSON and input hash equal; one cent of cash changes the hash). |
| Targets change only on a monthly publish or **Publish targets now** | ✅ | `tests/test_overlay_publish.py::test_targets_change_only_on_publish`: new daily backtests refresh the plan, not the targets; the off-cycle command publishes and cites the event. |
| Going-concern 10-Q or 8-K 3.01 on a held name → weight 0 at the next publish, cited by event ID; freed weight within the sleeve up to caps, remainder to QTUM | ✅ | `test_hard_rule_on_held_name_zeroes_it_at_next_publish[*]` (going concern, 3.01 deficiency, delisted common stock): nothing moves before the publish, then DEMO = 0, the chain cites the event, and the other pure-plays get the freed weight. `test_remainder_beyond_caps_goes_to_qtum`. |
| Materiality-5 RISK event → one off-cycle review alert, no automatic target change | ✅ | `test_materiality_5_event_sends_one_off_cycle_alert_and_changes_nothing` (3 alert runs → 1 alert; targets identical; materiality 3 and quarantined events don't alert). |
| Each profile's QTUM weight equals its fixed value | ✅ | `tests/test_portfolio_job.py::test_each_profile_uses_its_fixed_qtum_weight` (75 / 45 / 15%). |
| No suggested trade below the minimum | ✅ | `test_no_trade_below_minimum` (and every trade ≥ $100 across several cash levels). |
| Review pack Telegram text: plain, ≤4096 chars, no share counts, dollar values or account number; once per month | ✅ | `tests/test_review_pack.py`: one pack and one `review_pack:YYYY-MM` alert across the 1st, the retry on the 2nd and a rerun; the text has no `$`, share counts, cash or position values; overflow ends "… more lines on /review". |
| Options snapshot stores a row from a recorded fixture and nulls a thin chain with a reason | ✅ | `tests/test_fx_options.py` on a real IONQ chain recorded 2026-10-04 (`make record-options`): 30/60/90-day ATM IV inside the bracketing expiries, no extrapolation, and a thin chain → nulls + "thin chain: …". |
| Holdings never in a prompt or `llm_calls` | ✅ | `test_holdings_never_reach_llm_calls`: `llm_calls` stays empty, and the portfolio modules import no LLM client. `drift_summary` (the only thing that may ever reach synthesis, M10) carries percentages only: `test_drift_output_has_percentages_only`. |
| Tiger: sync replaces only universe symbols and leaves cash; failed sync keeps the snapshot with a stale banner; missing credentials disable the module; lint fails on a planted `place_order` / stray import | ✅ | `tests/test_tiger.py` (12 tests). **Synthetic SDK-shaped fixtures, not recorded responses**: there are no Tiger credentials to record with. Real SDK construction is exercised offline (`test_connect_builds_sdk_client_without_network`). |
| Tests green, no network | ✅ | `make test`: 357 passed |
| ruff / mypy / pip-audit | ✅ | `make lint`: clean, `mypy --strict` on 88 files, `check_broker_readonly: ok`, no known vulnerabilities |
| `make secrets-scan` clean | ✅ | gitleaks: no leaks |

### What was built
- **Login (`security/auth.py`)**:
  - scrypt hash `scrypt:n:r:p:salt:hash` (no `$`, so compose never interpolates it); `make hash-password` prints it and a session secret.
  - Signed 30-day HttpOnly, SameSite=Strict cookie carrying a password fingerprint, so a new password logs everyone out.
  - `AuthMiddleware` (headers → CSRF → auth → routes); the login form posts via htmx, so CSRF stays header-only.
  - In-memory failed-login limiter per IP; `/logout`.
- **Schema `0006_holdings`** (hand-written, STRICT):
  - `holdings` (`$CASH` row), `holdings_history`, `portfolio_settings`;
  - `profile_targets` (base/published weights, adjustment chain, trigger, trigger event, input hash);
  - `rebalance_plans`, `fx_rates`, `options_snapshots`, `review_packs`;
  - `alerts` rebuilt in batch mode for two new kinds (`off_cycle_review`, `review_pack`), keeping its rows and STRICT (tested).
- **Holdings (`portfolio/holdings.py`)**:
  - pydantic `HoldingsUpdate`/`SettingsUpdate`, validated in the app and again in the worker;
  - universe-only (QTUM + pure-plays + cash), up to 6 dp shares;
  - tiger mode applies cash only;
  - one-time `positions.yaml` import.
- **Fixed-QTUM profiles (M4 change)**: `qtum_weight` 75/45/15%, caps 10/20/35%, vol/DD limits `null` (shown, not enforced). 12 candidates + 3 benchmarks per run. `ALGO_VERSION` bumped, so the first run after deploy is a new backtest.
- **Monthly publishing (`portfolio/publish.py`)**:
  - base weights (the selected sleeve method) → overlay → `profile_targets`;
  - bootstrap publish when nothing exists;
  - the daily plan runs against the latest published targets;
  - `publish_targets` command (CSRF) for off-cycle publishes, citing an event.
- **Research overlay layer 1 (`portfolio/overlay.py`)**:
  - going concern (latest 10-K/10-Q);
  - 8-K 3.01 **deficiency** notices (180 days);
  - **delisted common stock**: a Form 25/25-NSE/15-12B/15-12G covering the common stock **and** no close for 10 QTUM sessions.

  Freed weight goes to the other pure-plays pro rata within caps, then QTUM. Each name's chain is stored and shown, linked to the filing. `cleared_accessions` lets you clear a reviewed filing.
- **EDGAR ingest**:
  - now fetches and parses 8-K Item 3.01 bodies (`extract_listing_notice`: deficiency / transfer / ambiguous / unclear) and Form 25/15 documents (`extract_delisted_class`: security title, covers common stock?);
  - new rubric rules: Form 25/15 → `delisting_or_compliance` at materiality 3 (alert only), and 8-K 5.01 at 4 (off-cycle alert only).
- **Rebalance plan (`portfolio/rebalance.py`)**:
  - no-trade band (3 pp or 25% of target; trades ≥ $100), sells first, then buys most-underweight first;
  - each buy is capped by cash after the 10 bps cost;
  - whole or fractional shares, new-cash-only mode, and a "needs cash" flag;
  - `drift_summary`/`drift_lines` for M10.
- **Off-cycle alert** (`alerts/candidates.py`): one alert per material (≥4) non-quarantined event on a pure-play.
- **Review pack (`review/pack.py`)**:
  - the 1st at 10:30 SGT, with a retry on the 2nd;
  - targets + chains, plan, drift, USD/SGD value, open flags, earnings and lock-ups;
  - a holdings-free Telegram text through the outbox;
  - `/review` page.
- **USD/SGD (`providers/fx.py`, `ingest/fx.py`)**: yfinance `SGD=X`, ECB reference-rate cross fallback (DTD-rejecting stdlib XML). Daily 06:50.
- **Options snapshot (`providers/options.py`, `options/`)**:
  - yfinance chains, choosing the nearest expiry plus the ones bracketing 30/60/90 days;
  - ATM IV with quality gates (`config/options.yaml`), total-variance interpolation and no extrapolation;
  - put/call volume and OI ratios. Daily 06:40.
- **Tiger (`providers/tiger.py`, `portfolio/tiger_sync.py`)**: read-only facade holding only the bound `get_positions`, fail-closed config, scrubbed errors, masked account, daily 07:05 job and `sync_holdings` command. `scripts/check_broker_readonly.py` is in `make lint`.
- **Dashboard**:
  - `/login`, `/holdings` (settings, holdings form, published targets with chains, off-cycle events + **Publish targets now**, plan with SGD value), `/review`, the Overview drift card, and Holdings, Review and Log out in the nav;
  - htmx `responseHandling` now swaps 4xx/5xx bodies, so error messages (rate limit, invalid input, wrong password) show.

  There's still no inline script or style.

### Decisions (deviations from the spec / plan)
1. **Layer-1 rules tightened after the live check** (your decision, 2026-10-05). Read literally (any Form 25/25-NSE/15-12B, any 8-K 3.01 or 5.01), the rules would have zeroed **QBTS, INFQ and IONQ**, and every one was a false positive:
   - IONQ's 25-NSE (2026-09-30) and QBTS's 25-NSE (2025-11-19) delist **warrants**.
   - QBTS's 8-K 3.01 (2026-07-14) and Churchill X's (INFQ, 2026-02-03) announce **voluntary exchange transfers**, and their common-stock Form 25s belong to those transfers.
   - INFQ's 8-K 5.01 is its own de-SPAC close.

   Now 3.01 needs deficiency language with no transfer language, delisting needs the common stock **and** a halt in trading, and 5.01 only alerts. These six real filings are a regression fixture.
2. **`profile_targets.as_of` is the publish date (SGT)**, with `prices_as_of` for the backtest session. A second publish on the same day replaces the row.
3. **Bootstrap**: with no published targets at all, the daily job publishes once (trigger `monthly`), so the page isn't empty for a month.
4. **The review pack is monthly only.** "Publish targets now" updates targets and the plan but sends no pack.
5. **The Telegram pack shows target weights and chains, not current weights or drift**, per spec ("target weights, flags, dates and the number of suggested trades only").
6. **Options from yfinance only**: Tiger sells API option quotes separately (no free delayed options endpoint in the SDK). Skew, implied moves and IV rank wait for M8.
7. **Holdings are limited to the strategy universe** and cost basis is per share (your answer). In tiger mode, manual saves change only the cash.
8. **The login limiter is in memory** (the app can't write SQLite), so an app restart resets it.
9. **The ECB fallback is a EUR cross** (SGD/EUR ÷ USD/EUR) and exists only on ECB business days.
10. **Delisting/deregistration forms alert at materiality 3**, below the off-cycle threshold, because most are warrant or transfer filings.
11. **`tigeropen` 3.8.0 added** (approved). It pulls in pandas (already present), protobuf, stomp.py, delorean, getmac and others; pip-audit is clean. It's imported lazily, only in `providers/tiger.py`, with dynamic-domain lookup off and props/token files in `/tmp`.

### Facts
- **You signed off five facts** (included in this PR as you asked): `ionq_revenue_fy2025_guidance_2026`, `qnt_ipo`, `darpa_qbi_stage_b`, `ibm_roadmap_ftqc`, `pqc_deadlines`. `FACTS.md` is regenerated, and the tests now check that exactly these are signed off and that the rest are labelled UNCONFIRMED in prompts.
- **Still `verified_by_claude`** (each has an open question): `ionq_acquire_skywater`, `qnt_lockup_expiry`, `infq_listing`.
- **Observed, not added as facts**: the IONQ and QBTS warrant delistings and the QBTS NYSE→Nasdaq transfer (filings above).

### Open questions
- **Tiger API**:
  - No read-only key scope exists (the `TIGERMCP_READONLY` flag is for Tiger's MCP server), so the key can trade.
  - The SDK sends a MAC-address `device_id`; in Docker that's the container's.
  - Holdings sync is untested against a real account.
- **Tiger option quotes**: confirm whether your API account has US options permission (check `get_quote_permission` once credentials exist) before M8 considers it.
- **3.01 text classification is regex-based.** An "unclear" or "ambiguous" notice never zeroes a name; it only alerts. Review those by hand.
- **The `qnt_lockup_expiry` first sale day** and the other carried-over questions are unchanged.

### Live check (isolated compose project `aether-m5` on port 8090, its own volume and image tag, Telegram and Tiger blanked; your stack wasn't touched)
- Migration `0006_holdings` applied on a fresh DB. The prices, EDGAR backfill (2,382 rows), dividends, strategies (one run, 15 metric rows), `fx` (21 USD/SGD rows, yfinance), `options` (6 snapshots) and `rebalance` jobs all came back `ok`.
- **Layer 1 on real EDGAR data: no name was zeroed.** The ingest parsed all six listing filings:
  - IONQ and QBTS 25-NSE: warrants (`covers_common: false`);
  - QBTS and INFQ 8-K 3.01: `transfer`;
  - QBTS and INFQ Form 25: common stock, but still trading.
- **First publish (bootstrap)**: safe = QTUM 75% + inverse-volatility sleeve (each pure-play 4.7–5.4%); medium = QTUM 45% + min-variance (IONQ 20%, RGTI 18.5%, QNT 16.5%); aggressive = QTUM 15% + min-variance (IONQ 35%, QNT 25.3%, RGTI 24.7%). The QTUM weights are exactly the configured values.
- **Options**: 30/60/90-day ATM IV for the five pure-plays (e.g. IONQ 73.8% / 75.0% / 71.5%, INFQ 85.5% / 86.3% / 84.4%). QTUM's 30/60/90-day IVs are null with "beyond the last usable expiry (12 days); not extrapolated": its 2026-11-20 ATM contracts failed the gates.
- **Browser flow, logged in**:
  - wrong password → "Wrong password." (401);
  - right password → redirect to `/holdings`;
  - synthetic holdings saved → command done → plan with sells before buys, cash after trades $124.61, S$ value at USD/SGD 1.2791;
  - Overview drift card;
  - **Publish targets now** → three `off_cycle` rows.
- A review pack built in the test stack: Telegram text of 632 chars with target weights, risk flags and dates only; the `/review` page rendered; one `review_pack:2026-10` alert (dashboard only, Telegram blanked).
- `curl /holdings` without a session → 303 to `/login`. The image started without a password hash logged "refusing to start" and exited.
- **No CSP errors.** The only console errors were the deliberate wrong-password 401. The stack and its volume were removed afterwards.

### Owner checklist
- [ ] `make hash-password`, then put `AETHER_DASHBOARD_PASSWORD_HASH` and `AETHER_SESSION_SECRET` in `.env` **before deploying**. The dashboard won't start without them.
- [ ] Review `config/strategies.yaml`:
  - fixed QTUM weights 75/45/15% and caps 10/20/35%;
  - no-trade band 3 pp / 25% / $100;
  - off-cycle threshold 4;
  - overlay `compliance_notice_days: 180` and `delisted_stale_sessions: 10`.
- [ ] Review `config/options.yaml` (OI ≥ 50, spread ≤ 50%, 30/60/90 days, 120 days / 8 expiries).
- [ ] Review decision 1 (tightened layer 1). If you'd rather have the literal rules, say so and I'll switch back.
- [ ] Optional Tiger setup: create a **dedicated** API key, set `TIGER_ID`, `TIGER_PRIVATE_KEY` and `TIGER_ACCOUNT` in `.env`, then pick "Tiger" as the holdings source and press **Sync from Tiger**.
- [ ] After merging: `./deploy.sh`. The worker migrates to `0006_holdings`, re-runs the backtest with the fixed QTUM weights, publishes the first targets about 5 minutes after start, and builds the first review pack on 2026-11-01.
- [ ] Still open from earlier milestones: `MASSIVE_API_KEY`, the Telegram bot setup, and the three facts not yet signed off.

### How to verify
```bash
make test            # 357 passed, network blocked
make lint            # ruff, mypy --strict, |safe ban, broker read-only check, pip-audit
make secrets-scan    # gitleaks: no leaks
./deploy.sh          # after merge; then log in at http://<lan-ip>:8080/holdings and /review
```

## Roadmap change: family-fund sleeve mandate, research overlay, options analytics (2026-10-04, owner-approved)

Docs only: no code or config changes. The new values below land in `config/strategies.yaml` with M5. Spec: §1 (items 7–9), §1.1, §1.3, **new §1.4**, §3, S8, §4, §6.2, §6.4, §6.5, §6.6, **new §6.6.1, §6.8, §6.9**, §7, §8, §9, §10, §11 (M5, M8–M12).

**Mandate (new §1.4).** The portfolio features serve one family-fund sleeve:
- 10–15% of the owner's total portfolio, with no cash or T-bill position (the owner manages the rest)
- US-listed stocks only, listed companies only
- SGD base currency (shown for reporting only)
- 12-year+ horizon; a 100% drawdown is accepted
- no options held
- the owner reviews monthly and makes the call

**What changes**
1. **Fixed QTUM weight per profile** (§6.5). Risk appetite is the size of the QTUM core, set by the owner: safe 75%, medium 45%, aggressive 15%. Per-name caps are 10% / 20% / 35%. The backtest picks only the sleeve method. Volatility and drawdown limits are shown, not enforced.
2. **Monthly targets** (§6.6). Targets are published on the 1st. The daily caps (1 pp / 3 pp) and the −3σ shock override are dropped. Material events send an off-cycle review alert, and targets change only when the owner presses **Publish targets now**. The no-trade band ($100, 3 pp / 25%) stays.
3. **Research overlay** (§6.6.1), deterministic, no LLM:
   - **Layer 1 (M5):** filing hard rules. Going concern, an 8-K 3.01 notice or an acquisition sets the weight to 0.
   - **Layer 1 (M9):** a fully diluted share increase over 20% YoY or a cash runway under 12 months halves the weight.
   - **Layer 2 (M10):** stance multipliers ×1.25 / 1.0 / 0.5 / 0, clamped to [0.75, 1.25] until the ticker's track record beats the baselines.
   - **Layer 3 (M10):** tracks the adjusted portfolio against the base portfolio, so the dashboard can say if the research isn't adding value.
   - Freed weight goes to the other pure-plays first, then QTUM. Every adjustment cites its evidence.
4. **Options analytics** (§6.8), research only:
   - **M5:** a daily snapshot job, so IV history starts building.
   - **M8:** term structure, skew, implied moves into catalysts, positioning and IV rank.
   - **M9:** implied vs realized move.
   - Options feed reports and synthesis, never sizing or trades. This replaces the old "optional IV".
5. **Monthly review pack** (§6.9). Sent on the 1st at 10:30 SGT, after the universe review: targets, adjustment chains, plan, flags and catalysts, with later milestones adding options, stances and universe proposals. The Telegram version carries no share counts or dollar values, because holdings never leave the machine.
6. **Smaller changes:** SGD view (USD/SGD rate, reporting only); track-record horizons extended to 24 and 36 months; K8s manifests optional in M11.

**Open questions (to verify at implementation)**
- Whether Tiger option quotes need a paid market-data subscription, and whether QNT and INFQ have listed options liquid enough to report.
- A free source for a daily USD/SGD reference rate.
- All new numbers are initial values for review: QTUM weights, caps, rule thresholds (20% dilution, 12-month runway), stance multipliers (ACCUMULATE ×1.0 is the conservative alternative), the earned-trust clamp and the options quality gates.

**Owner checklist**
- [ ] Review §1.4, §6.5 (profile table), §6.6.1 and §6.8.
- [ ] If any M5 work has started against the old §6.6 (daily caps, shock override, QTUM grids), realign it with this amendment before the M5 PR.

## Roadmap change: monthly universe review (2026-10-04, owner-approved)

There's a new **M12, Phase 4 "Discovery"**, after hardening. Spec: §1.1, §1.2, §3, **new §6.7**, §7, §8, §9, §10, §11.

- **When:** on the 1st of each month at 10:00 SGT.
- **What it does:**
  - Finds candidates deterministically, for free: QTUM holdings, SEC full-text search, and the current pure-plays.
  - Researches them in depth on the **strongest Opus model** (`claude-opus-5-5`) with web search.
  - Checks eligibility in code.
- **What you get:** a Telegram message listing each ticker to **add / remove / watch**, with what the company does and the reasons, backed by sources.
- **Proposals only:** Aether never edits `watchlist.yaml`. You apply a change through a PR.
- **Pure-play test** (`config/universe.yaml`, initial values for your review):
  1. US-listed with an SEC CIK
  2. quantum is the principal business, per a T1 filing excerpt
  3. market cap ≥ $500M and median dollar volume ≥ $5M
  4. ≥60 trading sessions; anything newer is "watch"
- **Removal:** a company is proposed for removal if it's delisted or acquired, its business moves away from quantum, or it fails the size/liquidity test in 3 consecutive reviews.
- **Cost:** each run has its own cap, `UNIVERSE_REVIEW_BUDGET_USD` (default $10), separate from the daily budget. Expect about $3–8 a month.

**Open question:** the original five pure-plays came from the brief, not from these criteria. The first review will test them against the same rules, so it may propose removing one.

## M4: Backtest lab + model strategies (2026-10-04)

Phase 1b, part 1. $0 LLM: every number is computed in code from stored prices.

### Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Metrics match hand-computed values on a synthetic series | ✅ | `tests/test_portfolio_metrics.py`: total return, CAGR, volatility, downside deviation, Sharpe, Sortino, max DD + duration (recovered and unrecovered), VaR95/CVaR95, Calmar, beta/alpha, tracking error / IR, up/down capture and calendar months, all checked against plain-Python arithmetic. Zero-variance inputs give None, not an error. |
| Look-ahead test (perturbing day *t* never changes weights before *t+1*) | ✅ | `tests/test_strategies_backtest.py::test_no_look_ahead[*]`, for all 4 families: day-*k* closes ×1.5 → every weight vector held on sessions ≤ *k* is bit-identical, and so are the backtest's returns before *k*. |
| Dividends on a synthetic series raise total return by the expected amount | ✅ | `tests/test_total_return.py`: $1 on a flat $50 series = exactly +2%; a weekend ex-date applies on the next session. `tests/test_portfolio_job.py::test_dividends_raise_total_return_by_expected_amount`: a $0.75 QTUM dividend inside the out-of-sample period multiplies QTUM's total return by (c+0.75)/c (rel. 1e-12). |
| Same input hash → byte-identical output | ✅ | `test_same_inputs_give_byte_identical_output`: two separate databases with the same synthetic inputs give identical `strategy_runs`, `strategy_metrics`, `strategy_weights` and `strategy_curves` rows. A rerun on unchanged inputs writes nothing; a changed price or config gives a new hash and a new run. |
| A candidate breaking a profile limit is never selected | ✅ | `tests/test_select.py::test_candidate_breaking_a_limit_is_never_selected` (the rule-breaker has the best CVaR and is still skipped). `test_profile_limit_breaking_candidates_never_selected` runs it end to end. |
| "No qualifying strategy" renders | ✅ | `tests/test_web_strategies.py::test_no_qualifying_strategy_renders`: the page shows it with the reason, and the curves API returns no strategy for that profile. |
| numpy as a direct dependency, no scipy | ✅ | `pyproject.toml` (`numpy>=2.0`; it was already in the lock via pandas). Min-variance is projected gradient in numpy. |
| Tests green, no network | ✅ | `make test`: 268 passed |
| ruff / mypy / pip-audit | ✅ | `make lint`: clean, `mypy --strict` on 71 files, no known vulnerabilities |
| `make secrets-scan` clean | ✅ | gitleaks: no leaks |

### What was built
- **Schema** (`0005_portfolio`, hand-written, every table STRICT):
  - `dividends` (WITHOUT ROWID; PK symbol + ex_date; `Micros` amount > 0; USD only; provider CHECK).
  - `strategy_runs` (UNIQUE as_of + 32-byte input hash; config and summary JSON).
  - `strategy_metrics` (candidate or benchmark, with a CHECK tying `kind` to profile/family/QTUM weight).
  - `strategy_weights` (WITHOUT ROWID, FK to its metrics row, weight in [0, 1]).
  - `strategy_curves` (see decision 1). Child rows cascade on delete.
- **Providers**: `providers/dividends.py`, yfinance `Ticker.dividends` + Massive `GET /stocks/v1/dividends` (checked against Massive's docs 2026-10-04: free Stocks Basic tier, 2 years of history).
  - Both give split-adjusted cash per share; Massive's `split_adjusted_cash_amount` is preferred.
  - Massive reuses the price client's Bearer auth, throttle and host pinning, and has a 20-page cap.
- **`ingest/dividends.py`** (job `dividends`): QTUM, the pure-plays, QQQ and SOXX, 730 days. Network first, then one `write_tx`.
- **`portfolio/`** (pure numpy except `job.py` and `view.py`):
  - `total_return.py`: TR from split-adjusted closes + dividends.
  - `metrics.py`: the §6.5 list, with definitions in the docstring.
  - `strategies.py`: 4 families, water-filling caps, an exact capped-simplex projection, and accelerated projected-gradient min-variance.
  - `backtest.py`: walk-forward, monthly rebalance, 10 bps cost.
  - `select.py`: limits relative to QTUM's out-of-sample result, ranking, tie-breaks.
  - `job.py`: inputs → hash → compute → one `write_tx`; canonical JSON.
  - `view.py`: read-only page queries.
- **Config**: `config/strategies.yaml` (numbers only, `extra=forbid`, validated so every grid value is ≥ the profile's minimum QTUM weight). Values are the spec's initial ones plus the QTUM grids: safe [80, 90]%, medium [50, 65, 80]%, aggressive [0, 25, 50]%. That makes 32 candidates + 3 benchmarks per run.
- **Jobs**: `portfolio` cron at 07:10 SGT (dividends, then strategies; the backtest still runs if the dividend fetch fails). A `recompute_strategies` dashboard command (CSRF, rate limit) shares its lock.
- **Dashboard**: `/strategies` (nav link) and `/api/strategies/curves?profile=`. The page has:
  - the verbatim banner, plus "not financial advice" and the banner text in the footer
  - computed history caveats ("QNT has 84 sessions of history …") and the out-of-sample window
  - the rf = 0 note
  - per profile: the model strategy, current target weights, an out-of-sample summary, equity and drawdown charts vs QTUM/QQQ, and a candidates table with pass/fail badges
  - the full 24-column metrics table for every candidate and benchmark, a stale banner and a **Recompute** button

  There's still no inline script or style.

### Decisions (deviations from the spec / plan)
1. **New `strategy_curves` table** (not in §7). It holds equity curves only for the recommended strategies and QTUM/QQQ/SOXX, pruned to the latest 30 runs. Metrics and weights are kept for every run.
2. **Sleeve weight the per-name caps can't place goes to QTUM.** Example: safe, top-3 momentum, 5% cap → 15% sleeve, QTUM 85%. This follows "a safer profile means more QTUM" with no cash sleeve.
3. **The initial allocation is charged the 10 bps cost**, like any rebalance. Benchmarks carry no cost.
4. **Dividends cover only the backtest universe and benchmarks.** An empty dividend result is normal, so the Massive fallback triggers only on an error (prices fall back on empty too).
5. **Metric conventions:**
   - historical VaR/CVaR are positive daily losses (5th percentile, linear interpolation)
   - downside deviation = RMS of min(r, 0)
   - capture uses arithmetic means on QQQ up/down days
   - max-DD duration runs from peak to recovery, or to the end of the period
   - "average turnover" is Σ|Δw| per monthly rebalance, excluding the initial allocation
   - months are calendar months, partial ones included
6. **Momentum for a newly listed name** uses the returns it has (≥60), not a full 126 sessions.
7. **Min-variance uses Nesterov-accelerated projected gradient** (still projected gradient, numpy only), with an early stop at 1e-12. The first plain-gradient version with a bisection projection took minutes per run.
8. **Missing bars are forward-filled** (a 0 return that day; the move lands on the next bar). Sessions follow QTUM's calendar.
9. **The startup catch-up runs 5 minutes after boot**, so the fresh-deploy prices backfill lands first. The live run showed the two racing (they finished in the same second).
10. **`as_of` is the last QTUM session.** Targets are the weights for the next session.

### Facts
- No facts changed. All 8 are still `verified_by_claude`, awaiting your sign-off from M3.
- No new facts were seeded. The backtest uses only stored prices and dividends.

### Open questions
- **Dividend history is unverified against issuer data.** The live run stored 8 dividends each for QTUM, QQQ and SOXX over about 21 months (yfinance). Pure-plays had none. If you want them checked, compare against Defiance's/Invesco's/iShares' distribution pages.
- **The Massive dividend fallback hasn't been exercised live** (still no `MASSIVE_API_KEY`).
- **Profile limits and grids are initial values** (decision 6 of the roadmap change). Live, safe passes 4 of 8 candidates, medium 5 of 12 and aggressive 12 of 12. Every q=0.80 safe candidate fails both the volatility and drawdown limits.

### Live check (isolated compose project `aether-m4` on port 8090, separate image tag, torn down afterwards; your stack wasn't touched)
- Migration `0005_portfolio` applied on a fresh DB. Prices (5,743 rows, yfinance) and dividends (24 rows) came in, then `strategies` wrote one run (35 metric rows) in about 1 s.
- **Out-of-sample window: 2025-04-01 to 2026-10-02 (379 sessions).** The caveats list INFQ (159 sessions) and QNT (84 sessions).
- **QTUM out-of-sample:** total return +112.8%, volatility 31.9%, max DD 21.5%.
- **Selections:**
  - safe: QTUM core + equal-weight sleeve, QTUM 90%
  - medium: equal-weight sleeve, QTUM 80%
  - aggressive: inverse-volatility sleeve, QTUM 50%

  These come from a short two-year sample; see the banner.
- **Cap checks:** safe min-var weights keep every pure-play ≤ 5% with QTUM at 80%. Medium momentum puts 15% each in 3 names and 55% in QTUM.
- `/strategies` rendered in a browser with charts and the 24-column table, and **no console or CSP errors**.
- **Recompute** → command `done`, dividends re-fetched, `strategies` ok with 0 rows (identical inputs, no new run).

### Owner checklist
- [ ] Review `config/strategies.yaml`:
  - minimum QTUM weight, per-name caps
  - volatility limits (1.15× / 1.6× QTUM) and drawdown limits (+5 / +15 pp)
  - ranking metrics and the QTUM grids
  - cost (10 bps) and windows (120 / 60 / 126)
- [ ] Review the decisions above, especially #2 (overflow to QTUM) and #3 (initial cost).
- [ ] After merging: `./deploy.sh`. The worker migrates to `0005_portfolio`, and the first backtest appears on `/strategies` about 5 minutes after start (or press **Recompute**).
- [ ] Still open from earlier milestones: `MASSIVE_API_KEY`, the Telegram bot setup, and FACTS.md sign-off.

### How to verify
```bash
make test            # 268 passed, network blocked
make lint            # ruff, mypy --strict, |safe ban, pip-audit
make secrets-scan    # gitleaks: no leaks
./deploy.sh          # after merge; then open http://<lan-ip>:8080/strategies
```

## Roadmap change: optional Tiger Brokers API, read-only (2026-10-04, owner-approved)

**Tiger Brokers OpenAPI** is now an optional provider, used read-only. Spec: §1.3, §2.2 S4/S6/**new S8**, §4, §7, §8, §9, §10, §11 (M5 and M8 rows).

- **M5: holdings sync.** It's opt-in (`holdings_source: tiger`). It imports positions in the strategy universe only (QTUM + pure-plays). Sleeve cash stays manual. If a sync fails, the last snapshot is kept with a stale banner. Manual entry remains the default.
- **M8: implied volatility from Tiger option chains** if configured, otherwise from yfinance.
- **Unchanged:** the intelligence phase's inputs. Tiger has no news, fundamentals or short-interest data (`get_short_interest` is marked "Currently Unavailable" in its docs).
- **Safety (S8):**
  - The key can place orders, so only `providers/tiger.py` may import `tigeropen`, and only through an allow-list of read calls.
  - A lint check bans order methods and stray imports.
  - Credentials go to the worker only.
  - Positions and the account number never reach prompts or logs.
  - If Tiger offers a read-only key scope, M5 will use it.
- **Dependency:** `tigeropen` (Apache-2.0) is approved. The API is free with a funded account; real-time quotes are a paid add-on and aren't needed.


## Roadmap change: Portfolio phase added (2026-10-04, owner-approved)

Two new milestones go **straight after M3**, before the intelligence phase. Both cost $0 in LLM spend. Spec: §1.1, §1.3, §2.2 S2, §6.5, §6.6, §7, §8, §9, §11 of `AETHER_BUILD_PROMPT.md`.

- **M4: Backtest lab + model strategies.** Walk-forward backtests of rule-based QTUM-core + pure-play strategies; full risk/return metric set (CAGR, vol, downside deviation, max DD, VaR/CVaR95, Sharpe, Sortino, Calmar, alpha/beta vs QQQ and QTUM, capture, turnover); one recommended model strategy per profile (safe / medium / aggressive); Strategies page.
- **M5: Password, holdings & rebalance.** Site-wide password login; Holdings page (saved via the command queue); deterministic rebalance plan toward the selected profile, with daily caps and a no-trade band so targets stay stable unless a materiality-≥4 event or a market-wide shock overrides them.

**Owner decisions**
1. The pages present **model strategies**, with the "not financial advice" footer.
2. **No cash or T-bill sleeve.** A safer profile means more QTUM. Banner: "Backtest for reference only. Historical returns are not future gains."
3. **Backtests use dividend-adjusted (total-return) prices.** This revisits M1 decision 2 for backtests only; `prices_daily` stays split-adjusted.
4. **No IP allow-list; add a password page** instead. This amends S2 ("no login").
5. **numpy becomes a declared dependency.** No scipy.
6. **Profile limits and stability settings** are the proposed defaults in §6.5 and §6.6, kept in `config/strategies.yaml` for review.

**Renumbering.** Earlier entries below keep their original numbers.

| Old | New | Milestone |
|---|---|---|
| — | **M4** | Backtest lab + model strategies |
| — | **M5** | Password, holdings & rebalance |
| M4 | M6 | News & research ingest |
| M5 | M7 | Classifier |
| M6 | M8 | Catalysts + market structure |
| M7 | M9 | Scorecards, reactions, theme (positions drift moved to M5) |
| M8 | M10 | Conclusions, track record, brief |
| M9 | M11 | Escalation, ops & deploy |

**Old numbers in code and config.** The comments and docs that used the old numbers (`Makefile`, README, `config/rubric.yaml`, `config/sources.yaml`, `classify/`, `risk/flags.py`, `db/models.py`, `jobs.py`, `market.py`, `ticker.html`) were updated in this change. Only comments and text changed.

**Known limits, shown in the UI:**
- Only about 2 years of price history (the Massive free-tier limit). QNT and INFQ have much less.
- The universe is small and concentrated.
- The risk-free rate is 0.
- The password travels over plain HTTP on the LAN.

## M3: Alerts → MVP done (2026-10-04)

Phase 1 (risk watcher MVP, $0 LLM) is complete once you've signed off FACTS.md (checklist below).

### Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Synthetic S-3 fixture → one Telegram message (mocked transport), no duplicate on re-run | ✅ | `tests/test_alerts.py::test_synthetic_s3_sends_one_message_and_no_duplicate_on_rerun`: an ACME S-3 goes through the real M2 path (`classify_filing` → `write_event`). Result: one `sendMessage` to the owner chat (plain text, no link preview), three re-runs, still one message and one `alerts` row (`sent`). |
| Failing-job alert fires | ✅ | `test_failing_job_alert_fires_once_then_recovers`: `prices` failing for 26h alerts once, `edgar` failing for 3h doesn't, and the next ok run sends one "recovered". `test_failing_job_detected_from_real_run_job` drives it through `run_job`. |
| Update from another user ID → dropped, no reply | ✅ | `test_update_from_other_user_is_dropped_without_reply`: only `deleteWebhook` + `getUpdates` are called. The log has the sender ID and chat type but not the text. |
| Group-chat update from the owner → dropped + `leaveChat` | ✅ | `test_group_update_from_owner_is_dropped_and_bot_leaves`. Also covered: being added to a supergroup (`my_chat_member`) and a channel post both trigger `leaveChat`, while a "kicked" update doesn't. |
| Missing `TELEGRAM_ALLOWED_USER_ID` → module disabled | ✅ | `test_token_without_valid_owner_id_disables_module`: missing, empty, `@owner`, negative, non-numeric and `0` all fail closed with an error log. `test_disabled_module_keeps_alerts_on_dashboard`: no Telegram call, and alerts are `dashboard_only`. `test_scheduler_registers_alert_jobs`: no inbound polling when disabled. |
| `getChat` returning a group → no messages sent | ✅ | `test_getchat_not_owner_private_chat_sends_nothing`: a group, or a private chat with another ID, means zero `sendMessage`. The pending alert is marked `failed` with the reason, and later alerts go to the dashboard. |
| Tests green, no network | ✅ | `make test`: 216 passed |
| ruff / mypy / pip-audit | ✅ | `make lint`: clean, `mypy --strict` on 60 files, no known vulnerabilities |
| `make secrets-scan` clean | ✅ | gitleaks: no leaks. Test tokens are synthetic (`test-token-not-real`; the redaction test builds a token-shaped string at runtime). |

### What was built
- **Schema** (`0004_alerts`, hand-written):
  - `alerts` is rebuilt as an outbox. New columns: `status` (`pending/sent/failed/expired/dashboard_only`), `text`, `created_at`, `attempts`, `last_error`. New CHECKs on kind, status, text length (≤4096) and attempts, plus indexes on (status, id) and created_at. It's STRICT, and the FK to `events` is kept. Nothing wrote `alerts` before, and the upgrade refuses to run if the table has rows.
  - `facts.open_question` is added and synced from `facts.yaml`.
- **`alerts/candidates.py`** (reads only): RISK events, insider clusters, lock-up and earnings T−7/T−1 reminders (the tightest reminder that applies, so a late first run doesn't send both), and job failing/recovered. Each has a stable dedupe key. Message text is built from DB fields only.
- **`alerts/dispatch.py`** (job `alerts`, every 10 min):
  - It collects candidates, then inserts new ones in one short `write_tx` (`ON CONFLICT DO NOTHING`).
  - It sends pending rows outside any transaction, about 1/s, up to 20 per run, and records each outcome in its own `write_tx`.
  - Rows expire after 48h and are `failed` after 5 attempts. A transient Telegram outage leaves rows pending.
- **`alerts/telegram.py`**: the Bot API client (checked against Bot API 10.3).
  - `TelegramConfig.from_settings` fails closed.
  - `send_message` has no chat parameter, so it can only reach the configured chat.
  - `verify()` runs `deleteWebhook` → `getChat` (private, id == owner), cached for 6h. A mismatch blocks sending for the process.
  - Long polling uses an in-memory offset. 429 `retry_after` is honoured once.
  - **Token hygiene**: the httpx/httpcore loggers are at WARNING, and errors are re-raised as `TelegramError` with the token scrubbed and `from None`. A test checks that the token reaches neither `job_runs.error` nor the logs.
- **`alerts/telegram_guard.py`**: `is_owner` (plain `message`/`edited_message` only; `from.id`, chat type and chat id must all match; bool IDs are rejected), `chats_to_leave`, and a rate-limited `DropLog` that never logs text.
- **Jobs**:
  - `alerts` every 10 min, first run 2 min after start.
  - `telegram_in` long poll (20s), only when Telegram is configured. It writes a `job_runs` row only when updates arrive, and failures are recorded at most every 10 min.
  - A `test_alert` dashboard command (CSRF, rate limit) that shares a lock with the job.
- **Dashboard**:
  - `/alerts`: delivery status with the reason, the last 100 alerts, and **Send test alert**.
  - `/facts`: status badges and counts, `extlink` source links, notes, open questions and sign-off instructions.
  - A "Recent alerts" card on the Overview and nav links. There's still no inline script or style.
- **Config**: `config/alerts.yaml` (numbers only, `extra=forbid`).

### Decisions (deviations from the spec / plan)
1. **The alert threshold is RISK materiality ≥ 3.** Single Form 4 sales (2), resale prospectuses (2) and 8-K 5.02 (2) don't alert on their own; insider clusters cover selling. Going concern (5), 424B primaries (4), 3.01 (4), S-3/S-1/3.02/NT (3) do.
2. **A 3-day event lookback.** Without it, the first deploy would send the whole 2025–26 backfill.
3. **Flags vs events.** ATM, shelf and going-concern flags don't alert separately, because their filings already alert as events. Lock-ups alert as T−7/T−1 reminders rather than at the 60-day flag.
4. **`TELEGRAM_CHAT_ID` is optional** and defaults to the user ID. When set, it must equal the user ID; a private chat's ID is the user's ID.
5. **One `alerts` job every 10 min** covers the spec's hourly job-health check.
6. **"Failing > 24h" means** the first failure since the last ok run is ≥24h old and no ok run has happened since. Jobs that silently stop running aren't covered; the dashboard's stale banners cover those.
7. **If `getChat` fails verification**, alerts already queued for Telegram are marked `failed` with the reason (visible on `/alerts`), and later ones are created `dashboard_only`.
8. **The test-alert button is on `/alerts`**, not Health as the plan said.
9. **No new dependencies.**

### Facts
- No facts changed in M3. All 8 are still `verified_by_claude` from M2, and the Facts page now shows them for your review.
- Sign-off is yours (spec §2.3). I haven't flipped any status.

### Open questions (carried over from M2)
- INFQ earn-out shares (S-4/proxy), the SkyWater closing date and consideration, and the first day QNT lock-up shares can be sold (2026-11-30 or 12-01).
- **Earnings reminders depend on the yfinance calendar** (unofficial). The message says to confirm on the company's IR site.

### Live check (isolated compose project `aether-m3` on port 8090, torn down afterwards; your stack wasn't touched)
- Migration `0004_alerts` applied on a fresh DB. The EDGAR backfill (270 events) and earnings calendar ran, then the first `alerts` run: `ok`, provider `dashboard`, warning `TELEGRAM_BOT_TOKEN not set`.
- **It created no alerts, which is correct.** The newest event is from 2026-09-22 (outside the 3-day lookback), the earnings dates (IONQ 11-04, QBTS 11-05, QNT 11-09, RGTI 11-10, INFQ 11-12) are more than 7 days away, and the QNT lock-up (11-30) is 57 days away.
- **Read-only dry run** with a 30-day lookback and a 40-day reminder window: it produced 8 candidates (RGTI/QNT/QBTS 8-K Item 3.02 dilution, plus earnings reminders), and their text read correctly.
- **Send test alert** → command `done` → a `test` row with `dashboard_only`. Its wording now says Telegram is off when it is.
- `/facts`, `/alerts` and the Overview render with **no console or CSP errors**.
- **Not exercised live: real Telegram delivery**, because there's no bot token yet. That's the first item below.

### Owner checklist
- [ ] Create the bot with **@BotFather** (`/newbot`), then `/setjoingroups` → **Disable** and `/setprivacy` → **Enable**.
- [ ] Add `TELEGRAM_BOT_TOKEN` to `.env` (mode 0600). Message the bot once, read your numeric `message.from.id` from `getUpdates` (see README), and set `TELEGRAM_ALLOWED_USER_ID` (and optionally `TELEGRAM_CHAT_ID`, the same number).
- [ ] After merging: `./deploy.sh`. Then **Send test alert** on `/alerts`; the delivery card should say "Telegram".
- [ ] Review `config/alerts.yaml` (threshold 3, 3-day lookback, T−7/T−1, 24h job failure, 48h expiry).
- [ ] **Review FACTS.md on `/facts` and sign off.** Set `status: signed_off` in `config/facts.yaml` for each fact you accept, run `make facts`, then commit via a PR. Look especially at the `qnt_ipo` discrepancy (Honeywell 47.8% vs the seed's 49.1%) and the three open questions.

### How to verify
```bash
make test            # 216 passed, network blocked
make lint            # ruff, mypy --strict, |safe ban, pip-audit
make secrets-scan    # gitleaks: no leaks
./deploy.sh          # after merge; then open http://<lan-ip>:8080/alerts and /facts
```

## M2: SEC EDGAR + deterministic risk (2026-10-04)

### Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Fixture tests flag known S-3/424B/Form 4 for ≥2 tickers | ✅ | `tests/test_ingest_edgar.py::test_ingest_flags_dilution_insiders_and_qnt_lockup` replays recorded SEC responses. `dilution`: QBTS S-3ASR + 424B7, IONQ 424B5, QNT 424B4. `insider_selling`: real Form 4 sales for RGTI and IONQ. The 10b5-1-only sale gets the lower materiality, and a tax-withholding Form 4 (code F) produces no event. |
| QNT lock-up date extracted from a recorded prospectus fixture | ✅ | `tests/test_edgar_parsers.py::test_qnt_lockup_from_recorded_424b4`: the recorded 424B4 ("Prospectus dated June 3, 2026", "180 days after the date of this prospectus") gives **2026-11-30**, with early release possible. The ingest test checks the `lockups` row and that the 60-day open flag fires on 2026-10-04 but not on 2026-09-01. |
| Verify every `FACTS.md` seed against its source | ✅ | All 8 facts are now `verified_by_claude`, each with a primary source. Corrections and discrepancies are listed under "Facts" below. |
| Idempotent re-runs | ✅ | `test_ingest_is_idempotent`: a second run leaves row counts in all six tables unchanged, and parsed documents aren't fetched again. |
| Tests green, no network | ✅ | `make test`: 183 passed |
| ruff / mypy / pip-audit | ✅ | `make lint`: clean, `mypy --strict` on 53 files, no known vulnerabilities |
| `make secrets-scan` clean | ✅ | gitleaks: no leaks. The recorded fixtures don't contain the SEC User-Agent (checked with grep). |

### What was built
- **Schema** (`0003_edgar`, hand-written, every table STRICT):
  - `filings`, `insider_txns`, `fundamentals_q` (WITHOUT ROWID; PK includes `period_days` so quarter, FY and instant values can share a period end), `capital_structure`, `lockups` and `earnings_calendar` (WITHOUT ROWID).
  - The event tables `events`, `event_sources`, `event_tickers` (WITHOUT ROWID) and `event_classifications`. The last has CHECKs on class, category (all §5.1 categories), materiality 1–5, direction and confidence, a provenance CHECK (`rule_id` or model + prompt_version), and the §7 partial index `materiality WHERE class != 'NOISE'`.
  - `alerts.event_id` now has a foreign key to `events.id`, added through a batch rebuild that keeps STRICT.
- **`providers/edgar.py`**: the SEC client.
  - Refuses to start without a contact User-Agent, and sends it only to `www.sec.gov` / `data.sec.gov` over https.
  - Builds URLs from CIK + accession only and validates primary-document names (no traversal).
  - Throttles to 5 req/s and retries 429/5xx with backoff. A 404 fails immediately.
- **`edgar/`** (pure parsers):
  - `submissions.py`: `filings.recent` plus older `files[]` pages.
  - `form4.py`: stdlib XML, rejects any DTD. Reads the code, A/D, shares (whole shares, half-even rounding), price, the `aff10b5One` flag or a 10b5-1 footnote (a negated footnote doesn't count), and joint filers.
  - `text.py`: HTML → text, plus the lock-up, going-concern and ATM extractors. Each stores a ≤600-char excerpt and returns nothing rather than guess.
  - `xbrl.py`: companyfacts → quarter/FY/instant facts (no YTD values) and warrant/convertible rows.
- **`ingest/edgar.py`** (job `edgar`):
  - Fetches submissions since 2025-01-01, then documents only for Form 4, 424B4/B1, 424B5/B2 and 10-K/10-Q (at most 400 per run, newest first), then companyfacts when a new periodic report lands.
  - Writes everything in one `write_tx`.
  - **Form-based events fire on first sight**, so a failed document download never hides a dilution filing. Parsing adds the Form 4 sales, the going-concern finding and the lock-up excerpt later.
- **`classify/rules.py` + `config/rubric.yaml`**: form, 8-K item (2.02, 3.01, 3.02, 5.02), Form 4 and going-concern rules. The YAML holds numbers and identifiers only (`extra=forbid`, and a form may appear in only one rule). Titles and rationales are built from filing metadata.
- **`risk/flags.py`**: open flags for a lock-up within 60 days, an insider cluster (≥3 distinct insiders by CIK in a 30-day window, with the 10b5-1 count), an ATM ≤365 days old, a shelf ≤3 years old, and going concern in the latest 10-K/10-Q.
- **`ingest/earnings_calendar.py`** (job `earnings_calendar`, 07:15 SGT): reported dates from 8-K Item 2.02 filings and scheduled dates from the yfinance calendar. Future scheduled rows are replaced on every run.
- **Jobs**:
  - `edgar` runs every 30 min from 21:00 to 05:00 SGT on US weekdays, and at 09:00 and 17:00 SGT, with a startup catch-up.
  - A `refresh_edgar` dashboard command (CSRF, rate limit, a lock shared with the cron job, added to the command allow-list).
- **Dashboard**:
  - Overview: an "Open risk flags" card and a "RISK filings, last 30 days" card.
  - Ticker page (pure-plays): flags, lock-ups with the prospectus excerpt, earnings dates, classified SEC events, a shares-outstanding chart (`/api/dilution/<sym>`), the capital-structure table, Form 4 transactions (10b5-1 badge) and the filings list with rule badges.
  - All EDGAR links go through `extlink`. There's still no inline script or style.
- **Tooling**: `make record-cassette … UA=… GZIP=1` writes `.json.gz` cassettes (the QNT 424B4 is 4.5 MB of HTML, 410 KB gzipped). `tests/cassettes.py` loads either format.

### Decisions (deviations from the spec)
1. **The event tables are created in M2**, with EDGAR as the first event origin, instead of in M4/M5. M4 and M5 will extend these tables rather than create them.
2. **There's a new `lockups` table** (not in §7) so each extracted date carries its excerpt and accession. M6 catalysts will link to it.
3. **`fundamentals_q` stores either `value_micros` or `value_int`** (a CHECK enforces exactly one), and `period_days` is part of the PK.
4. **An 8-K with several ruled items gets one classification**: the highest materiality wins, and the other matches are named in the rationale. Item 5.02 covers both departures and appointments; the rule follows the spec (`exec_departure`) at materiality 2.
5. **Going concern is deliberately conservative.** Only an unhedged "substantial doubt … going concern" sentence counts. Risk-factor boilerplate ("could raise substantial doubt") never does.
6. **Insider share counts are rounded to whole shares** (half-even), because §7 calls for INTEGER share counts.
7. **The EDGAR schedule ignores NYSE holidays** until M7 brings an exchange calendar. On a holiday it just makes a few extra polls.
8. **Rubric materialities are initial values for your review.** Changing `rubric.yaml` doesn't reclassify filings that were already classified, except when their document is parsed again.
9. **No new dependencies.** XML and HTML parsing use the stdlib, and upcoming earnings dates come from yfinance, which was already a dependency.

### Facts (all 8 checked against primary sources; none signed off)
- `ionq_revenue_fy2025_guidance_2026` ✅ $130.0M FY2025 (8-K Ex. 99.1, 2026-02-25); FY26 guidance $280–290M, midpoint $285M (8-K Ex. 99.1, 2026-08-05).
- `ionq_acquire_skywater` ✅ Merger agreement dated 2026-01-25 (8-K Item 1.01). Closing is stated in IonQ's Q2 release.
- `qnt_ipo` **corrected**: the 424B4 dated 2026-06-03 shows Nasdaq "QNT", 28.0M Class A shares at $60.00, and Honeywell Entities at **47.8%** of voting power (47.0% with the option exercised).
  - **Discrepancy**: the seed said ≈49.1%.
  - The ~$14.3B "top of range" market cap predates pricing and isn't in the 424B4, so it was dropped.
- `qnt_lockup_expiry` **resolved**: 180 days after 2026-06-03 = **2026-11-30**. The underwriters can release shares early.
- `infq_listing` ✅ De-SPAC with Churchill Capital Corp X, consummated 2026-02-13 (closing 8-K filed 2026-02-17). Trades on the NYSE, with warrants `INFQ WS` at $11.50. Registration-rights holders have a 180-day transfer restriction with a $12.00 VWAP early release.
- `darpa_qbi_stage_b` ✅ All 11 names match darpa.mil ("as of Nov. 6, 2025").
- `ibm_roadmap_ftqc` ✅ Matches the IBM blog post (2025-06-10).
- `pqc_deadlines` **refined**: NIST IR 8547 is still an initial public draft. Its "deprecated after 2030" applies only to 112-bit strength, and everything is "disallowed after 2035". The EU roadmap v1.1 sets high-risk use cases at end-2030 and medium-risk at end-2035 (checked in the roadmap PDF).
- **New `open_question` field** in `facts.yaml`. FACTS.md lists the open questions.

### Open questions
- **INFQ earn-out shares**: the closing 8-K doesn't mention any. Read the S-4 / proxy.
- **SkyWater closing date and consideration**: check IonQ's closing 8-K (Item 2.01).
- **The exact first day QNT lock-up shares can be sold** (2026-11-30 or 12-01), and any announced early release.
- **QNT and INFQ don't tag `dei:EntityCommonStockSharesOutstanding` in companyfacts** (seen for QNT), so their shares-outstanding chart uses balance-sheet and weighted-average concepts where tagged. Fully diluted counts are M7.
- **Company IR domains (T1) are not configured yet.** They arrive with the IR RSS feeds in M4.
- **Capital-structure extraction from filing text is limited to ATM amounts.** Convertibles, warrants and earn-outs beyond XBRL tags need the M5+ LLM extraction (T1, cross-checked against XBRL), as the spec plans.

### Live check (isolated compose project `aether-m2` on port 8090; your stack wasn't touched)
- **First EDGAR backfill**: 984 filings since 2025-01-01 across the five pure-plays (IONQ 284, QBTS 307, INFQ 172, RGTI 170, QNT 51). This **matches an independent count** from the raw submissions JSON. 354 documents were parsed with 364 SEC requests in about 90 s, with no parse errors.
- **Results**:
  - 627 Form 4 transactions, 452 XBRL facts, 254 rule events: 151 insider_selling, 45 dilution, 29 earnings_release, 26 exec_departure, 3 delisting_or_compliance.
  - The QNT lock-up is 2026-11-30, and its 60-day flag is open.
  - ATM programs found, each with a stated size: IONQ $500M (2025-02-27), QBTS $150M (2025-01-10) and $400M (2025-06-10), RGTI $350M (2025-05-30).
- **Two fixes came out of the live run** (both with tests):
  - The going-concern extractor had flagged QBTS's FY2024 10-K. That 10-K only *refers back to* earlier disclosures ("we disclosed that there was substantial doubt …"). Retrospective and "alleviated" statements no longer count; the re-run has no going-concern flags.
  - ATM detection now requires a stated aggregate amount near the at-the-market wording. Base-prospectus boilerplate had produced two ATM rows with no amount.
- **Rules added after reviewing the pages**:
  - 8-K Item 3.02 (unregistered equity sale) → `dilution` (materiality 3). QNT's 2026-09-08 8-K (Items 1.01/3.02) was unflagged before this.
  - Titles no longer repeat the form ("QNT 424B4", not "QNT 424B4 (424B4)").
- **Pages**: Overview, `/t/QNT` and `/t/RGTI` render (lock-up card, flags, Form 4 table, shares-outstanding chart, filings) with **no console or CSP errors**.
- **Known first-deploy gap**: the `earnings_calendar` startup run can finish before the first EDGAR backfill, so past (8-K 2.02) dates appear after the next 07:15 run.

### Owner checklist
- [ ] `SEC_USER_AGENT` is now set in `.env`. **`chmod 600 .env`**: it's currently 0644, and `./deploy.sh` refuses to run until it's fixed.
- [ ] Review `config/rubric.yaml`: the materiality per form/item, and the thresholds (60-day lock-up window, 3 insiders / 30 days, ATM 365 days, shelf 3 years).
- [ ] Review FACTS.md, especially the `qnt_ipo` discrepancy (Honeywell 47.8% vs the seed's 49.1%). Sign-off happens at the end of M3.
- [ ] After merging, run `./deploy.sh`. The worker migrates to `0003_edgar` and backfills EDGAR (a few minutes on the first run).

### How to verify
```bash
make test            # 183 passed, network blocked
make lint            # ruff, mypy --strict, |safe ban, pip-audit
make secrets-scan    # gitleaks: no leaks
./deploy.sh          # after merge; then open http://<lan-ip>:8080/t/QNT
```

## M1: Market data (2026-10-04)

### Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Idempotent re-runs | ✅ | `tests/test_ingest_prices.py::test_backfill_then_rerun_is_idempotent` (backfill, then incremental, then full refresh: same row count), `test_revised_bars_update_in_place`, `tests/test_qtum_holdings.py::test_ingest_is_idempotent_and_replaces_snapshot`. Live: a full 2-year refresh of 5,754 rows left the count at 5,754. |
| Simulated yfinance failure → fallback serves, `provider` recorded | ✅ (fallback is **Massive**, not Stooq; see decision 1) | `test_simulated_yfinance_failure_fallback_serves_and_provider_recorded`: every row has `provider='massive'`, and `job_runs.provider='massive'`. `tests/test_providers.py` covers the failover rules (trip after 3, probe after 24 h, a failed probe restarts the timer, an empty result is not a failure, both-fail errors). **Not run live**: there's no `MASSIVE_API_KEY` yet. |
| Charts render | ✅ | Checked live in a browser against an isolated stack: the Overview comparison chart (all five ranges, tooltips), the ticker page (QNT close + volume), dark and light themes, and **no console / CSP errors**. Tests check that no page has an inline `<script>` or `style=`. |
| Tests green, no network | ✅ | `make test`: 130 passed |
| ruff / mypy / pip-audit | ✅ | `make lint`: clean; `mypy --strict` on 39 files; no known vulnerabilities (yfinance's dependency tree included) |
| `make secrets-scan` clean | ✅ | gitleaks: no leaks (vendored `echarts.min.js` included) |

### What was built
- **Schema** (`0002_market_data`):
  - `prices_daily`: PK(symbol, d), FK to `tickers`, CHECKs on provider, OHLC > 0 and h ≥ l, volume ≥ 0, and the date format.
  - `qtum_holdings`: PK(snapshot_date, holding_symbol).
  - Both are STRICT and WITHOUT ROWID; the tests now check WITHOUT ROWID too.
- **Providers** (`providers/prices.py`):
  - `PriceProvider` protocol. `YFinanceProvider` uses `auto_adjust=False` and rounds to 6 dp to drop float32 noise. `MassiveProvider` uses Bearer auth, a 12.5 s throttle (the free tier allows 5 calls/min), follows `next_url` only on `api.massive.com`, and maps errors to `ProviderError`.
  - `FailoverPriceProvider` handles switching (see README).
- **Ingest:**
  - `ingest/prices.py`: 2-year backfill, a 10-day revision overlap, a Sunday full refresh, bar validation, fetch-then-one-`write_tx`, and partial failures recorded in `job_runs.error` while the run stays `ok`.
  - `ingest/qtum_holdings.py`: a fail-closed robots.txt gate, a stdlib HTML parse, and sanity checks (header, as-of date, weights summing to 95–105%, no duplicates). Each snapshot date is replaced wholesale.
- **Jobs:**
  - `prices` runs at 06:30 SGT and `qtum_holdings` at 06:35, each with a startup catch-up if its last ok run is more than 24 h old.
  - `refresh_prices` is a dashboard command (CSRF + rate limit) that shares a lock with the cron job.
  - `run_job` moved to `runs.py`, and `JobResult.warning` was added.
- **Dashboard:**
  - Overview: banners, a log-scale comparison chart, a ticker table, the watchlist's weight in QTUM, and the refresh button.
  - `/t/<symbol>`, plus the `/api/prices/overview?range=` and `/api/prices/<symbol>` JSON endpoints.
  - ECharts 6.1.0 is vendored (sha256 in `VENDORED.txt`). `static/charts.js` reads `data-*` attributes and uses canvas `richText` tooltips, so no HTML is injected.
- **Live check** (isolated compose project on port 8090; your M0 stack was not touched):
  - yfinance backfilled all 13 tickers (5,754 rows); QNT starts 2026-06-04 and INFQ 2026-02-17.
  - The holdings snapshot loaded 90 rows (as of 2026-10-05, summing to 99.95%), with all 5 pure-plays present, combined at 4.64% of QTUM.
  - The IONQ 2026-10-02 OHLC matched an independent source.

### Decisions (deviations from the spec)
1. **The fallback is Massive's free tier, not Stooq** (your choice during planning). On 2026-10-04, Stooq's CSV endpoint returned a JavaScript proof-of-work bot challenge, and `stooq.com/robots.txt` disallows `*`. Using it would mean getting past bot detection. `api.nasdaq.com/robots.txt` also disallows everything. Massive "Stocks Basic" is $0, 5 calls/min, 2 years of history, end-of-day, individual use.
2. **Split-adjusted, not dividend-adjusted, prices from both providers**, so a failover can't mix conventions. Total-return effects (QTUM/QQQ/SOXX dividends) are left out for now. Revisit in M7 if the event-reaction maths needs them.
3. **The backfill is exactly 730 days**, matching the Massive free tier's 2-year limit.
4. **Failover state is in memory.** A worker restart acts as a probe of yfinance. An empty yfinance result tries Massive without counting as a failure.
5. **The holdings page is parsed with the stdlib `html.parser`**, so no new dependency. Defiance's `robots.txt` is malformed; read literally (as Python's `robotparser` does) it allows everything. Its evident intent is to block `/wp-content/uploads/funddocs/`, which Aether never fetches.
6. **Staleness uses a 4-calendar-day tolerance per symbol and 30 h for the prices job.** It stands in for an NYSE holiday calendar until M7 adds `exchange_calendars`.
7. **The Overview chart uses a log scale**, because the pure-play basket rose about 17× over two years, which flattens the other lines on a linear axis.
8. **`/` is now the Overview.** Health lives at `/health` only.
9. **yfinance stays the primary source (owner decision, 2026-10-04).** Yahoo's `robots.txt` (`query1.finance.yahoo.com`) is `Disallow: /`. The owner confirmed this is personal-use API access by a single self-hosted instance, not crawling (spec S6).

### Facts
- No seed facts were verified or changed (verification is M2).
- Observed (not added as facts): Yahoo serves daily prices for `QNT` from 2026-06-04 and `INFQ` from 2026-02-17, and Defiance lists both in QTUM. Their listing route, date and CIKs remain M2 EDGAR items under `qnt_ipo` / `infq_listing`.

### Open questions
- **The Massive failover hasn't been exercised live** until you add `MASSIVE_API_KEY`.
- **Volume differs slightly between vendors** (IONQ 2026-10-02: 19.04M on Yahoo vs 19.09M on another source). It's informational now and matters for abnormal volume in M7, so the provider is kept per row.

### Owner checklist
- [ ] Sign up for **Massive Stocks Basic** (free) at massive.com, then add `MASSIVE_API_KEY=` to `.env` (still mode 0600). It's passed to the worker only.
- [ ] After merging: `./deploy.sh`. The worker migrates to `0002_market_data`, backfills 2 years of prices and takes the first holdings snapshot within about a minute.
- [ ] Review decision 2 (no dividend adjustment).

### How to verify
```bash
make test            # 130 passed, network blocked
make lint            # ruff, mypy --strict, |safe ban, pip-audit
make secrets-scan    # gitleaks: no leaks
./deploy.sh          # after merge; then open http://<lan-ip>:8080/
```

## M0: Scaffold + security baseline (2026-10-04)

### Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Health page reports SQLite version + WAL | ✅ | `tests/test_web_security.py::test_health_page_reports_sqlite_version_and_wal`, `test_healthz_json`. Live: `make smoke` → `{"ok":true,"sqlite_version":"3.46.1","journal_mode":"wal","schema_revision":"0001_baseline","file_mode":"0o600"}` |
| ~~Request from an IP outside `AETHER_ALLOWED_CIDRS` → 403~~ | Removed | The owner dropped the CIDR allow-list after M0 review (see "M0 amendments"). |
| Command POST without a CSRF token → 403 | ✅ | `test_command_without_csrf_token_is_403`, plus missing-cookie, forged-token and cross-site `Origin`/`Sec-Fetch-Site` cases. Live: `curl -X POST …/commands/ping` → `403 CSRF check failed`. With a token → `202`, and the worker marked it `done` |
| `\|safe` lint check works | ✅ | `test_lint_flags_planted_violation` (5 planted variants incl. `Markup(` and `markupsafe.Markup`). `make lint` runs `scripts/check_no_safe.py` |
| Writer + 2 readers concurrency test, no `SQLITE_BUSY` | ✅ | `tests/test_concurrency.py`: 3 s of batched `BEGIN IMMEDIATE` inserts with 2 `mode=ro` readers. No errors; readers see monotonic counts and never a partial batch |
| Tests green, no network | ✅ | `make test` → 79 passed, 0 warnings (pytest-socket `--disable-socket`; `test_network_is_blocked`) |
| ruff / mypy clean | ✅ | `make lint`: ruff, ruff format, `mypy --strict` (31 files), `check_no_safe`, `pip-audit --require-hashes` → no known vulnerabilities |
| `make secrets-scan` clean | ✅ | gitleaks v8.30.1 over git history and working tree: no leaks |

### What was built
- **Toolchain, all in Docker.** Python 3.12.15 and uv 0.12.23 (pinned images), with a committed `uv.lock`. The `dev` compose service (profile `tools`) runs every make target, and the venv lives in a named volume. Nothing is installed on the Mac.
- **docker-compose.**
  - `worker` (single writer: migrate → sync config → APScheduler) and `app` (dashboard).
  - Both run non-root (uid 10001) with a read-only root FS, `cap_drop: ALL` and `no-new-privileges`.
  - They share the named volume `aether-data` at `/data`.
  - Ports are `8080:8082` (host 8080 → app on 8082), per owner direction.
  - Healthchecks: the worker checks the schema is at head and a recent heartbeat exists; the app checks `/healthz`. The app waits for a healthy worker.
  - Started and updated with `./deploy.sh` (see amendments).
- **SQLite (`db/`).**
  - Pragmas are set on every connection (WAL, `busy_timeout=5000`, FKs on, `synchronous=NORMAL`, `temp_store=MEMORY`, `cache_size=-20000`).
  - Every rw transaction is `BEGIN IMMEDIATE`, and `write_tx()` retries busy lock acquisition.
  - The ro engine uses a `mode=ro` URI plus `query_only`.
  - The DB file is 0600 in a 0700 directory.
  - `dialect.py` provides `upsert` (sqlite/postgres/mysql) and `json_extract`. `types.py` provides `Micros` (INTEGER ↔ Decimal, floats rejected), UTC ISO timestamps and simhash signed/unsigned helpers.
- **Alembic baseline `0001_baseline`.** STRICT tables `tickers`, `facts`, `commands`, `job_runs`, `llm_calls` and `alerts`, with CHECK enums and `json_valid` CHECKs. Tests verify that every table is STRICT and that the models match the migrations.
- **Config.** pydantic-settings reads env only (no env_file). Typed YAML loaders use `extra="forbid"` for `watchlist.yaml` (identifiers only) and `sources.yaml` (trust tiers; T3 is the default).
- **Facts registry.** `config/facts.yaml` holds the 8 seed facts from §2.3, all `unverified`. `FACTS.md` is generated from it, and a test checks the two stay in sync. `render_for_prompt` labels anything not `signed_off` as UNCONFIRMED. The worker upserts facts into the `facts` table at startup.
- **S2.**
  - No IP allow-list (removed after review, see amendments). uvicorn runs with `proxy_headers=False`.
  - CSRF: a signed double-submit token (HttpOnly, SameSite=Strict cookie plus the `X-CSRF-Token` header, which HTMX sends via `hx-headers`) and an Origin/Sec-Fetch-Site check.
  - Commands are rate-limited to 10/hour, counted from the `commands` table, so the limit survives restarts.
  - CSP has no inline script or style. Also set: `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `nosniff`, COOP and Permissions-Policy. These apply to error responses too.
- **S4.** `.env`, `config/positions.yaml` and `data/` are in both `.gitignore` and `.dockerignore`. Other S4 pieces:
  - `.env.example` lists the keys only.
  - `deploy.sh` refuses a `.env` that isn't 0600.
  - Secrets are interpolated only into the `worker` service.
  - The gitleaks pre-commit hook (`.githooks/pre-commit`, Docker-based, fails closed) is enabled via `core.hooksPath`.
  - `make secrets-scan` is in place.
- **S5.** `security/sanitize.py` (markdown-it with raw HTML disabled → nh3 strict allow-list, http/https-only links with `rel="noopener noreferrer nofollow"`) is the only module allowed to construct `Markup`. The `md` and `extlink` Jinja filters are registered, and `scripts/check_no_safe.py` enforces the ban.
- **S1 groundwork.** `security/untrusted.py::wrap_untrusted` adds `<untrusted_document id=…>` delimiters, neutralises delimiter-injection, and defines the system-prompt notice.
- **Test tooling.** pytest-socket, a temp-file DB with real migrations and pragmas, and respx cassette replay (`tests/cassettes.py`) with a synthetic `ACME` fixture. `scripts/record_cassette.py` records manually, keeps no request headers and allow-lists response headers.
- **Ops.** Online backup via `sqlite3.backup()` to `/data/backups/aether-YYYYMMDD.db` (0600, 14 days kept). A nightly 04:00 SGT job does backup → prune `job_runs` > 90 days → `PRAGMA optimize` → `wal_checkpoint(TRUNCATE)`. `make backup` was verified live.
- **Dashboard.** A health page (SQLite version, journal mode, schema revision, file mode, last run per job, ping button) with dark/light themes and the "not financial advice" footer.

### Decisions (deviations from the spec, approved in the M0 plan)
1. **The app's one write path.** "App only reads" conflicts with the dashboard filling the `commands` table. Resolution:
   - `enqueue_command` runs on a separate `mode=rw` (no-create) engine with an **SQLite authorizer that denies everything except `INSERT INTO commands`**. UPDATE, DELETE, DDL, PRAGMA, ATTACH and writes to other tables are all tested as denied.
   - A test asserts `aether/web` never uses the writer engine.
2. **Port binding is `8080:8082` on all host interfaces**, per owner direction (rather than `${AETHER_LAN_IP}:8000`).
3. **No CIDR allow-list** (owner decision after review; this deviates from spec S2). Verified live: Docker Desktop delivered every request as `192.168.65.1`, the VM gateway, so the allow-list couldn't tell clients apart on this host anyway. LAN-only access now depends on the network boundary and host firewall. Documented in the README.
4. **Schema is built per milestone.** M0 ships the infra tables only. Each milestone adds its own tables in a new hand-written migration, with §7 as the target.
5. **Hand-written migrations and STRICT-safe types only.** SQLAlchemy's String/Boolean/Float/DateTime aren't valid in STRICT tables.
6. **The `facts` table stores `source_urls` as a JSON array**, not the outline's single `source_url`, because several seed facts cite two sources.
7. **APScheduler 3.11** (stable), not 4.x.
8. **CSRF is header-only** (`X-CSRF-Token`). Plain non-HTMX form POSTs aren't supported, which is stricter.
9. **`pip-audit` runs via `uvx` against `uv export` with hashes** (`uv pip audit` doesn't exist). It needs network, so it belongs to `make lint` and is never a test.
10. **Backups use Python's `sqlite3.backup()`** (same online API as the CLI's `.backup`), because the slim image has no `sqlite3` binary.
11. **htmx 2.0.4 is vendored** (sha256 recorded in `web/static/VENDORED.txt`), with `includeIndicatorStyles`, `allowEval` and `allowScriptTags` turned off for the CSP.
12. **Job-run hygiene.** `process_commands` polls every 30 s but records a `job_runs` row only when there's work. The heartbeat runs every 10 min, and job runs are pruned after 90 days.
13. **`httpx2` is a dev dependency** (owner decision). Starlette 1.7's TestClient now uses it natively, so the deprecation-warning filter is gone. `httpx` stays the runtime HTTP client, and respx cassettes mock `httpx`.
14. **`./deploy.sh` replaces `make up`** (owner decision). It fast-forwards to `origin/main` and runs `docker compose up -d --build --wait`.

### Facts
- No facts were verified in M0; per spec, verification is M2. All 8 seeds are `unverified`.
- Source URLs are copied from the brief with only an `https://` scheme added. No hosts or paths were changed.
- `qnt_lockup_expiry` has no source. It's listed under Open questions, to be derived from the final 424B4 in M2.

### Open questions
- **QNT lock-up expiry.** Unknown until the 424B4 is parsed (M2).
- **QNT / INFQ.** Ticker existence, listing route and CIKs all need EDGAR verification in M2. Until then the watchlist CIKs are blank.
- **T1 IR domains and T2 RSS feed URLs** aren't configured yet (M2/M4). `sources.yaml` has `sec.gov` (T1) and the three T2 press domains named in the brief.
- **Model IDs and the web-search tool version** will be looked up from the Anthropic docs when the LLM wrapper is built (M4). `CLASSIFIER_MODEL` and `SYNTH_MODEL` are declared but unused.

### Owner checklist
- [ ] **Anthropic Console:** create a dedicated **workspace** for Aether, create an API key in it, and set a **monthly spend limit**. This is the real cost cap (S3).
- [ ] Create **separate keys** for dev and for the long-running app. Put the app key in `.env` as `ANTHROPIC_API_KEY` (not needed until M4).
- [ ] Set **`SEC_USER_AGENT`** in `.env` as `"Your Name your-email@example.com"`. SEC requires it, and it's sent only to sec.gov (needed from M2).
- [ ] `cp .env.example .env && chmod 600 .env`. `./deploy.sh` refuses anything else.
- [ ] **Never port-forward 8080** on your router, and never put it behind a public tunnel. Check that the macOS firewall is on.
- [ ] With no IP allow-list, anyone who can reach port 8080 can view the dashboard: make sure you trust everything on your LAN.
- [ ] Deploy and update with `./deploy.sh` (needs a clean checkout of `main` and SSH access to the GitHub remote).
- [ ] Copy `/data/backups` off-host occasionally if you want real disaster recovery: `docker compose cp worker:/data/backups ./backups-copy`.
- [ ] Review the decisions above, especially #1 (the command-engine write exception) and #4 (per-milestone schema).

### How to verify
```bash
make test            # 79 passed, network blocked
make lint            # ruff, mypy --strict, |safe ban, pip-audit
make secrets-scan    # gitleaks: no leaks
./deploy.sh          # pull main, build, start, wait healthy, check /healthz
```

### M0 amendments (after owner review, 2026-10-04)
- **Removed the CIDR allow-list.** That means `AETHER_ALLOWED_CIDRS`, `AETHER_TRUSTED_PROXY`, `security/network.py`, the X-Forwarded-For handling and their tests are all gone. The dashboard has no IP-based access control now. It relies on the LAN boundary and host firewall, plus CSRF and the rate limit for commands. This is a deliberate deviation from spec S2 and from the original M0 acceptance criterion.
- **Added `httpx2`** as a dev dependency and removed the pytest warning filter. The test run is now warning-free.
- **Added `./deploy.sh`** in place of `make up`/`make build`/`make check-env`. It checks `.env` is 0600, refuses to run with uncommitted tracked changes, fast-forwards to `origin/main`, then runs `docker compose up -d --build --remove-orphans --wait` and checks `/healthz`.
- **Added GitHub Actions CI** (`.github/workflows/ci.yml`), which runs on pull requests and on pushes to `main`. It has four jobs: test, lint, secrets-scan and a runtime image build. Actions are pinned to commit SHAs, and permissions are `contents: read`. From now on, changes land through PRs only (see CLAUDE.md "Git workflow").
