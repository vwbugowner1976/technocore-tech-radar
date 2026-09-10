# TechnoScout Job Scout Shadow

Job Scout is the first step from a conversational TechnoScout toward a work-performing agent.
The initial phase is deliberately observation-only.

## What it does now

- Reads `/r/kibble` with GET requests only.
- Parses strict one-line Kibble v1 records.
- Recognizes `JOB`, `CLAIM`, `RESULT`, `DELIVER`, `ATTEST`, and `WITNESS`.
- Tracks job lifecycle as `OPEN -> CLAIMED -> DELIVERED -> ATTESTED`.
- Classifies jobs as:
  - `FIT`
  - `NEEDS_TOOL`
  - `NEEDS_COMPUTE`
  - `TOO_EXPENSIVE`
  - `UNSAFE`
  - `LOW_CONFIDENCE`
  - `NOT_RELEVANT`
  - `SKIP_CLOSED`
- Stores only structured metadata and a SHA-256 content hash in SQLite. Raw job title/body text is not persisted.
- Uses a deterministic lightweight policy for the daemon phase, so it does not start a second MLX 7B worker alongside the main TechnoScout process.

The parser accepts both `RESULT` and `DELIVER` because both forms have been observed in Kibble-compatible tooling. This is a room convention, not a core Technocore server primitive, so the parser is intentionally strict and versioned.

## What it cannot do

Job Scout Shadow does **not**:

- post `CLAIM`
- post `RESULT` or `DELIVER`
- post `ATTEST` or `WITNESS`
- sign or submit messages
- spend FLOP or any token
- use a wallet
- use faucet/reward flows
- execute a job
- run shell commands from job text
- open job-provided URLs
- access credentials or secrets

Those remain separate future phases with their own gates.

## Safety policy

Unsigned/anonymous issuers are recorded as unsafe for future work selection. Jobs that mention credential/secret handling, wallets, transfers, payments, escrow, faucet/airdrop/rewards, staking, bridging, or HTLC actions are deterministically blocked before any model evaluation.

External-data or execution-oriented work such as repository research, builds, tests, code review, debugging, browsing, downloads, or current-data queries is labeled `NEEDS_TOOL`; Job Scout does not perform the tool action.

The first shadow daemon uses a deterministic evaluator. This is intentional: it keeps the work-discovery lane separate from conversational autonomy and avoids competing with the existing managed MLX worker on a 16 GB Mac mini.

## Persistence

Job memory is stored in the existing TechnoScout SQLite database in the `job_shadow_candidates` table. The table contains identifiers, issuer DID, lifecycle, classification, fit/confidence scores, effort, capability labels, reason/summary metadata, evaluation counters, and a content hash. It does not retain the raw job body.

The cursor is stored as `job_shadow_cursor:<room>` in `meta`. If the per-cycle evaluation cap is reached, the cursor stops before the first deferred job so a later cycle can evaluate it instead of silently losing it.

## Manual inspection

```bash
cd ~/technocore-tech-radar
.venv/bin/python job_shadow.py status --limit 20
```

A one-shot read-only scan can be run with:

```bash
.venv/bin/python job_shadow_runner.py --verbose
```

On the very first run, Job Scout reads the latest bounded Kibble window. Later runs continue from the persisted sequence cursor.

## launchd

Install the separate one-shot launchd service with:

```bash
cd ~/technocore-tech-radar
bash scripts/install_job_shadow_launchd.sh
```

The label is:

```text
com.vwbugowner.technoscout-jobshadow
```

It runs once per minute and exits. Logs are written to:

```text
logs/job-shadow.log
logs/job-shadow-error.log
```

This service is intentionally separate from `com.vwbugowner.technoscout`.

## Planned promotion path

The intended progression is:

```text
Job Shadow observation
  -> job history / issuer history
  -> job-fit progress gate
  -> controlled manual CLAIM trial
  -> sandboxed tool execution
  -> reviewed RESULT/DELIVER
  -> ATTEST tracking / Job Success Memory
  -> bounded job autonomy
  -> FLOP compute backend with explicit spend budget
```

No stage should automatically unlock the next one. A gate may report that there is enough evidence to design a controlled trial; it is not authorization to enable spending, claiming, or job execution.
