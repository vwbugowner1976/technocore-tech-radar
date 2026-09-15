# Job Progress Gate

`job_progress_gate.py` is a **read-only evidence gate** for the first manually approved Kibble claim trial.
It never sends a message, claims a job, executes job content, spends FLOP, touches a wallet, or changes TechnoScout autonomy.

## Why this gate exists

Job Scout can observe thousands of jobs, but a persisted `OPEN` state can become stale. Before a future manual claim trial we require three independent layers:

1. enough board-level observation to know the tracker is working,
2. a locally safe `FIT` candidate from a signed DID with useful lifecycle history,
3. a fresh live revalidation proving the exact same JOB is still OPEN.

`NEEDS_TOOL` and `NEEDS_COMPUTE` are intentionally excluded from the first claim trial. They require a separate Tool Planner / compute-spend boundary first.

## Default baseline thresholds

- observed jobs: 100
- OPEN jobs: 20
- closed/advanced jobs (`CLAIMED|DELIVERED|ATTESTED`): 50
- distinct signed issuers: 5

Candidate thresholds:

- lifecycle: `OPEN`
- signed issuer: yes
- class: `FIT` only
- relevance >= 50
- technical fit >= 60
- confidence >= 75

Issuer evidence thresholds:

- observed jobs >= 3
- closed jobs >= 2
- completed (`DELIVERED|ATTESTED`) >= 2
- attested >= 1
- completion rate >= 50%

These are conservative eligibility thresholds, not a proof of trust.

## States

- `COLLECTING`: not enough board-level evidence yet.
- `WAITING_FOR_SAFE_CANDIDATE`: enough board data, but no signed OPEN FIT job passes candidate + issuer thresholds.
- `NEEDS_LIVE_REVALIDATION`: local candidate exists; no claim trial is allowed until a fresh live read confirms it.
- `READY_FOR_MANUAL_CLAIM_TRIAL`: one exact JOB passed live OPEN revalidation. This is **not a claim**; it only means a separate human-approved trial may be designed.
- `NO_LIVE_OPEN_CANDIDATE`: eligible local candidates had already advanced/closed or mismatched when checked.
- `LIVE_CHECK_INCONCLUSIVE`: live reading was incomplete/truncated/unavailable; fail closed and retry later.

## Commands

Local evidence only:

```bash
.venv/bin/python job_progress_gate.py
```

Show issuer lifecycle reputation:

```bash
.venv/bin/python issuer_reputation.py --limit 20
```

Perform a bounded live OPEN revalidation (GET only):

```bash
.venv/bin/python job_progress_gate.py --live --candidate-limit 5
```

The live check starts immediately before the persisted JOB sequence, requires the same JOB sequence, issuer DID, and content hash, scans later lifecycle records in bounded pages, and fails closed on ambiguity.

## Important boundary

Even if the result is `READY_FOR_MANUAL_CLAIM_TRIAL`, do **not** add automatic CLAIM behavior to Job Scout. The next phase must be a separate controlled-trial path with explicit human approval, one exact job id, one-use permit semantics, and no FLOP spend or tool execution unless separately reviewed.
