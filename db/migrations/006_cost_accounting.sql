-- 006_cost_accounting.sql — what each memo actually consumed (MEMO-22).
--
-- Applied by db/migrate.sh inside a transaction it already opened, so the two
-- rules 001_init.sql states hold here too: no BEGIN/COMMIT, and nothing illegal
-- inside a transaction block.
--
-- **Actual spend on this project is zero and these columns will read zero or
-- NULL on every row a local run produces.** That is the point rather than a
-- shortcoming. The question this schema has to be able to answer is "what would
-- this cost per 1000 memos on a hosted provider?", and answering it needs the
-- *inputs* to a bill — audio minutes, token counts — not a bill. Those inputs
-- are measurable for free, and they are what ai/memo_ai/rates.py multiplies by a
-- rate nobody has been charged.
--
-- Half of what MEMO-22 asks to persist was already here. 001_init.sql has
-- `duration_ms`, `stt_provider`, `stt_model` and `cost_micro_usd`, and MEMO-16
-- commits all four alongside the transcript. What is missing is everything about
-- the *second* stage — enrichment produced a title and a summary since MEMO-21
-- and left no record of what it spent doing so — plus the one measurement that
-- makes the local numbers meaningful: how long inference actually took.
--
-- Every column added here is nullable with no default, which is the same choice
-- `duration_ms` made and for the same reason. NULL means "nobody measured this",
-- and zero means "measured, and it was zero". A text memo never reaches a
-- transcriber, so its `stt_ms` is NULL rather than 0; a memo enriched by
-- `ENRICH_PROVIDER=none` has NULL token counts rather than 0. Defaulting these
-- to 0 would put a measurement that was never taken into an average, and an
-- average is exactly what this table is now expected to produce.

-- How long transcription itself ran, in milliseconds of wall clock.
--
-- **Inference only.** memo_ai/stt/local.py times the decode and not
-- `_ready_model()`, so the first voice memo after a boot does not record its
-- model load here. That is deliberate and it is what makes this column usable as
-- a per-memo rate: the load is a one-off cost of the *process*, and folding it
-- into the first memo would put a 1.65 GB model fetch into the numerator of a
-- "seconds of inference per minute of audio" figure and make one row the median
-- on a small sample.
--
-- NULL from the `fake` provider, which runs no model at all — the same honesty
-- `stt_model` keeps there, and for the reason memo_ai/stt/fake.py states at that
-- line: a canned sentence must not contribute a timing to a table that prices
-- transcription.
--
-- integer, like `duration_ms`, and 2.1 billion milliseconds is 24 days.
ALTER TABLE memos ADD COLUMN stt_ms integer;

-- Which enricher produced this memo's title and summary, and with which model.
--
-- The exact counterpart of `stt_provider` / `stt_model`, and added for the
-- argument those two make in memo_ai/stt/base.py: what the *configuration* says
-- and what actually ran are different questions, and only the row can answer the
-- second. `ENRICH_PROVIDER=local` with a missing weight file publishes a memo
-- with a heuristic title and no enrichment, and a row that recorded the
-- configured name would claim a model ran on it.
--
-- `enrich_model` is the GGUF's filename rather than its full path — the path is
-- a fact about one image's layout, and putting `/opt/models/llm/` in front of
-- every row would only make the column harder to GROUP BY.
--
-- Both stay NULL under `ENRICH_PROVIDER=none`, which is the accurate description
-- of a memo nothing enriched.
ALTER TABLE memos ADD COLUMN enrich_provider text;
ALTER TABLE memos ADD COLUMN enrich_model text;

-- What the enrichment pass fed the model and what it got back, in tokens.
--
-- These are the two numbers a hosted enrichment provider bills on, and they are
-- billed at *different* rates — output typically four to five times input — so
-- one combined total would not be enough to price anything. ai/memo_ai/rates.py
-- keeps them apart for that reason.
--
-- **Accumulated across attempts rather than overwritten**, which is the one
-- place this table's write rules differ from `cost_micro_usd`'s. A job reaped in
-- the gap between MEMO-16's two commit points re-runs enrichment, and on a hosted
-- provider both runs are on the invoice. memo_ai/memos.py's `_accumulated` is the
-- expression; transcription needs no equivalent because the two-commit design is
-- what stops it running twice at all.
--
-- integer rather than bigint: this is one memo's usage, and the context window
-- the local model is loaded with is 12,288 tokens. A hosted model with a million
-- of them is still four orders of magnitude short of overflowing this, even
-- summed over the handful of attempts a memo can make.
ALTER TABLE memos ADD COLUMN enrich_input_tokens integer;
ALTER TABLE memos ADD COLUMN enrich_output_tokens integer;

-- How long that generation ran, in milliseconds of wall clock.
--
-- Measured inside memo_ai/enrich/local.py around the generation, so — like
-- `stt_ms` — it excludes the lazy model load that the first enriched memo after a
-- boot pays for. On this stack that load is a fraction of a second on a warm page
-- cache, because the weights are baked into the image rather than downloaded, but
-- excluding it keeps the two timing columns measuring the same kind of thing.
--
-- **Assigned rather than accumulated, unlike the token counts two lines up**, and
-- the asymmetry is the point rather than an oversight. Tokens are spend and spend
-- adds up; this is a latency, and a memo enriched twice has two latencies rather
-- than one that is twice as long. Summing it would quietly corrupt the only
-- column that answers "how slow is this model on real memos".
ALTER TABLE memos ADD COLUMN enrich_ms integer;

-- No index on any of the above, deliberately.
--
-- Every query in ai/memo_ai/costs.py is an aggregate over the whole table — a
-- SUM, a COUNT, a percentile — which is a sequential scan whatever indexes exist,
-- and it runs when a person asks rather than on a request path. An index here
-- would cost every INSERT and every one of the worker's two commits, to speed up
-- a report nobody runs in a loop. 001_init.sql indexes only what the claim, the
-- list and the search actually use, and that rule holds.
