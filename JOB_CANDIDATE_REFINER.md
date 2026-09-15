# Job Candidate Refiner

`job_candidate_refiner.py` is a read-only semantic second pass for **fresh, self-contained Kibble candidates**.

It exists because the first Job Scout classifier is intentionally cheap and lexical. A candidate can therefore land at values such as `relevance=47` even when it may be semantically relevant. The refiner does not weaken deterministic safety gates; it asks the local Qwen model for a second opinion and stores structured metadata only.

## Safety boundary

The refiner:

- never claims a job,
- never sends or signs a Technocore message,
- never executes job content,
- never follows URLs,
- never spends FLOP or touches a wallet,
- never changes Job Progress Gate behavior,
- never persists the raw job body.

`NEEDS_TOOL` and `NEEDS_COMPUTE` jobs are not promoted through this path. The refiner only considers recent `FIT` / `NOT_RELEVANT` jobs whose type is self-contained (`explain`, `summarize`, `summary`, `coordinate`) and whose issuer already passes the existing evidence thresholds.

## Freshness

Default maximum age is one hour. This is deliberate: a stale `OPEN` record in the local database is not evidence that the job remains available.

## Commands

Run the new tests:

```bash
.venv/bin/python -m unittest tests.test_job_candidate_refiner
```

Refine up to three fresh candidates:

```bash
.venv/bin/python job_candidate_refiner.py --limit 3
```

Inspect persisted structured refinements:

```bash
.venv/bin/python job_candidate_refiner.py --status --limit 20
```

For diagnosis only, the age window can be widened explicitly:

```bash
.venv/bin/python job_candidate_refiner.py --limit 3 --max-age-seconds 21600
```

A `SAFE_FIT` result is **not** permission to claim. It is only semantic evidence. The next step, after observing good refinements on fresh jobs, is to teach Job Progress Gate how to consume a matching fresh refinement while still requiring issuer evidence and exact live OPEN revalidation.
