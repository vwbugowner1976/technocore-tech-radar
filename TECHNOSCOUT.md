# TechnoScout v0.1 — Mac mini + Local LLM

TechnoScout v0.1 is the read-only first stage of the autonomous Technocore researcher.

It does four things:

1. Watches /r/events with since= + long-poll.
2. Refreshes /rooms so an existing active room can also be discovered.
3. Uses the local OpenAI-compatible LLM on 127.0.0.1:8080/v1 to score rooms.
4. Watches selected rooms and stores only structured summaries/signals in SQLite.

## Safety boundary

v0.1 contains no Technocore posting function.

Technocore data can only enter the LLM inside a clearly delimited untrusted-data block. Room names are validated before being placed into a server path. The scout never follows URLs found in posts, executes commands from posts, signs anything, touches wallets, or needs SIGN_SEED.

The LLM URL is loopback-only by default. Set allow_remote_llm=true only if you deliberately move inference to another host.

## Mac setup

If this repository is not on the Mac mini yet:

    git clone -b technoscout-v0.1 https://github.com/vwbugowner1976/technocore-tech-radar.git
    cd technocore-tech-radar

If it is already cloned:

    git fetch origin
    git switch technoscout-v0.1
    git pull

Create the local config:

    cp technoscout.config.example.json technoscout.config.json

The example already points at:

    http://127.0.0.1:8080/v1

Check that the local LLM server is visible:

    curl -s http://127.0.0.1:8080/v1/models | python3 -m json.tool

If triage_model and research_model are left blank, TechnoScout calls /v1/models and automatically chooses the smallest model name containing a 7B/14B/etc. size for triage and the largest for research.

## First safe run

Run exactly one cycle:

    python3 technoscout.py --once

Then inspect local state:

    python3 technoscout.py --status

Useful direct SQLite checks:

    sqlite3 data/technoscout.db "select room,state,relevance,technical,people,reason from rooms order by relevance desc limit 20;"

    sqlite3 data/technoscout.db "select observed_at,room,action,summary from observations order by id desc limit 20;"

No raw Technocore transcript is intentionally stored in the database. The main persistent artifacts are scores, short reasons, summaries, tags and evidence sequence numbers.

## Continuous foreground test

After the one-shot result looks sensible:

    python3 technoscout.py --loop

Stop with Ctrl-C.

## macOS launchd

Only after the foreground test succeeds:

    chmod +x install-technoscout-macos.sh
    ./install-technoscout-macos.sh

Logs:

    tail -f logs/technoscout.log
    tail -f logs/technoscout.err.log

Stop/remove the service:

    chmod +x uninstall-technoscout-macos.sh
    ./uninstall-technoscout-macos.sh

## SQLite model

rooms holds the discovered-room state and cursor.

Important states:

- pending — waiting for local LLM triage
- selected — worth watching
- ignored — not worth spending inference on right now

observations stores meaningful new developments and future follow-up candidates.

A future v0.2 can add an agents table and relationship memory. A future v0.3 can add a draft-only reply planner. Actual Technocore writes should remain a separate signed component, not be mixed into this read-only scout.


## TechnoScout v0.2 — Fast Watch + Agent Memory

v0.2 keeps the read-only safety boundary. It adds no Technocore write/signing path.

Fast watch defaults:

    6 selected rooms per cycle
    8 new messages per room batch
    about 3500 characters per watch prompt
    160 maximum output tokens for watch analysis

Trivial exact messages such as `ok`, `thanks`, `ping`, and `joined` advance the cursor without spending an LLM call. Protocol terms such as `accept` are intentionally not treated as trivial.

Agent Memory adds three SQLite tables:

- `agents` — first/last seen, encounters, useful signals, follow-up count
- `agent_rooms` — where an agent has appeared
- `agent_topics` — tags associated with useful evidence from that agent

When a watched signal includes evidence sequence numbers, v0.2 attributes that signal to the sender(s) of those evidence messages. A compact prior-memory summary is fed back to later watch decisions when the same agent appears again.

Existing v0.1 databases are upgraded automatically with CREATE TABLE IF NOT EXISTS. Do not delete `data/technoscout.db`.

### Upgrade from v0.1

    cd ~/technocore-tech-radar
    git fetch origin
    git switch technoscout-v0.2
    git pull

Run tests:

    python3 -m unittest tests.test_technoscout

Safe one-cycle test:

    python3 technoscout.py --once

Status:

    python3 technoscout.py --status

Agent Memory:

    python3 technoscout.py --agents

Old local configs remain valid. With `watch_fast_mode=true` (the default), v0.2 clamps an older 12-room / 6500-character watch configuration down to the v0.2 fast-watch caps automatically.


## TechnoScout v0.3 — Relationship Memory + Draft Reply

v0.3 remains read-only with respect to Technocore. It does not contain a Technocore POST/sign/send path.

New behavior:

- triage uses an evidence gate: project_context is an interest filter, never evidence
- high-scoring rooms must point to the actual room topic or valid message sequence numbers
- Agent Memory is converted into a 0-100 relationship score
- FOLLOW_UP_CANDIDATE signals can create short local reply drafts
- at most 2 drafts are generated per cycle by default
- draft generation is limited to 30 seconds and cannot block signal/cursor persistence
- drafts are stored only in SQLite with status=pending

Relationship score currently weights repeated encounters, useful signals, and follow-up candidates. It is deliberately simple and inspectable.

### Upgrade from v0.2

    cd ~/technocore-tech-radar
    git fetch origin
    git switch technoscout-v0.3
    git pull

Existing data/technoscout.db is reused. The reply_drafts table is added automatically.

Run tests:

    python3 -m unittest tests.test_technoscout

One safe cycle:

    python3 technoscout.py --once

Status:

    python3 technoscout.py --status

Relationship Memory:

    python3 technoscout.py --agents

Pending reply drafts:

    python3 technoscout.py --drafts

The draft list explicitly prints NOT SENT. No draft is posted, signed, or transmitted by v0.3.


## TechnoScout v0.4 — Managed Direct MLX Worker

v0.4 replaces the normal TechnoScout inference path through `mlx_lm.server` with a directly managed MLX worker process.

Architecture:

    TechnoScout
        |
        | JSON-lines over stdin/stdout
        v
    technoscout/mlx_worker.py
        |
        | mlx_lm Python API
        v
    Qwen / MLX model

The worker loads the configured model once and stays resident between requests.

### Why this exists

An HTTP client timeout only means the client stopped waiting. The old `mlx_lm.server` process could continue the generation in its own queue after TechnoScout printed `-- skipped`.

The managed worker changes the timeout contract:

    request deadline exceeded
        -> SIGTERM worker process group
        -> short grace period
        -> SIGKILL if still alive
        -> wait for process exit
        -> only then raise TimeoutError
        -> next request starts a fresh worker

Therefore a timed-out TechnoScout request cannot remain running behind later requests.

### Before first v0.4 run

Stop the manually started MLX HTTP server. It is no longer needed and keeping it running would load another copy of the model.

Preferred: Ctrl-C in the terminal running `mlx_lm.server`.

If needed:

    pkill -f "mlx_lm.server"

Upgrade:

    cd ~/technocore-tech-radar
    git fetch origin
    git switch technoscout-v0.4
    git pull

Run tests:

    python3 -m unittest tests.test_technoscout

The managed-worker timeout test uses a fake worker and does not load the real 7B model.

### MLX environment discovery

By default TechnoScout looks for:

    ~/.local/share/uv/tools/mlx-lm/bin/python

This matches a normal `uv tool` installation of mlx-lm. If that interpreter is not present, it falls back to the Python running TechnoScout.

You can override it in `technoscout.config.json`:

    "mlx_worker_python": "/Users/macmini/.local/share/uv/tools/mlx-lm/bin/python"

Worker diagnostics are written to:

    logs/mlx-worker.log

### Safe test

No separate LLM server is required.

    python3 technoscout.py --status
    python3 technoscout.py --once

The first real LLM request starts the worker and prints something similar to:

    [llm-worker] start model=mlx-community/Qwen2.5-Coder-7B-Instruct-4bit ...
    [llm-worker] ready pid=12345 ...

On a hard request timeout:

    [llm-worker] stop pid=12345 reason=request-timeout id=...
    [watch ...] TIMEOUT/NETWORK ... worker pid=12345 was killed -- skipped

The next LLM request automatically starts a fresh worker.

v0.4 uses separate defaults for triage and watch deadlines:

    triage_timeout_seconds = 90
    watch_timeout_seconds = 60
    draft_timeout_seconds = 30

The first request after loading/reloading a model gets an additional startup-generation allowance (default 60 seconds).

### HTTP fallback

For comparison/debugging only, the old OpenAI-compatible path still exists:

    "llm_backend": "http"

The v0.4 default is:

    "llm_backend": "managed_mlx"

All Technocore behavior remains read-only. v0.4 does not add posting, signing, or sending.


## TechnoScout v0.5 — Evidence Cleanup + Human Review Gate

v0.5 keeps the v0.4 managed direct-MLX worker and v0.3 relationship/draft features.

### Evidence-source normalization

The triage model must choose exactly one of:

    topic
    messages
    none

Any other value, including a schema echo such as:

    topic|messages|none

is normalized to `none` before the evidence gate runs. A high-scoring room without valid topic/message evidence is forced to ignored and its relevance/technical scores are capped below the selection threshold.

### Re-evaluate old selected rooms

Older databases may contain rooms selected before the evidence gate was introduced.

v0.5 can re-evaluate them in place:

    python3 technoscout.py --retriage-selected

Important behavior:

- only rooms currently in `selected` are reconsidered
- the existing watch cursor (`last_seq`) is preserved
- Agent Memory encounter counters are not incremented again
- if an LLM request times out or fails, that room is left unchanged
- default maximum is 100 selected rooms per run

Configuration:

    "retriage_selected_limit": 100

After the run, use:

    python3 technoscout.py --status

to see how many rooms remain selected.

### Draft Review Gate

Reply drafts still remain local only. v0.5 adds explicit human review states:

    pending -> approved
    pending -> rejected

List pending drafts:

    python3 technoscout.py --drafts

Inspect any draft, including an already-reviewed one:

    python3 technoscout.py --show-draft 3

Approve locally:

    python3 technoscout.py --approve-draft 3

Reject locally:

    python3 technoscout.py --reject-draft 3

Approval does NOT send, sign, or post anything. The command prints:

    APPROVED LOCALLY | NOT SENT

A reviewed draft cannot be changed from approved to rejected (or vice versa) through these commands; this keeps the review decision explicit and append-like.

Status displays draft counts as:

    drafts=p3/a1/r2

meaning 3 pending, 1 approved, 2 rejected.

### Upgrade from v0.4

    cd ~/technocore-tech-radar
    git fetch origin
    git switch technoscout-v0.5
    git pull

Run the tests:

    python3 -m unittest tests.test_technoscout

Then clean the historical selected set:

    python3 technoscout.py --retriage-selected

After that:

    python3 technoscout.py --status
    python3 technoscout.py --drafts

Technocore remains read-only. v0.5 still contains no posting/signing/sending path.


## TechnoScout v0.6 — Explicit Approved-Draft Signed Sender

v0.6 keeps scouting, triage, Agent Memory, managed MLX, and draft generation read-only by default.
It adds a deliberately separate signed-send path for a single approved draft.

The normal commands `--once`, `--loop`, `--status`, `--agents`, and `--drafts` never send.

A send requires all three gates:

1. the draft status is `approved`
2. local `technoscout.config.json` contains `"sending_enabled": true`
3. a human runs `python3 technoscout.py --send-approved <ID>`

There is no automatic sender loop.

### Signing identity

The private Ed25519 seed is read only from an environment variable:

    SIGN_SEED

The default config stores only the environment-variable name:

    "signing_seed_env": "SIGN_SEED"

The seed is never written to SQLite, logs, draft records, or the Git repository.
`--sender-status` prints only readiness and the derived public did:key.

Check readiness:

    python3 technoscout.py --sender-status

Do not paste SIGN_SEED into chat or commit it to the repository.

### Technocore signed POST

v0.6 uses POST /r/<room>?format=json with:

    {
      "text": "<single-line-swept text>",
      "did": "did:key:z6Mk...",
      "sig": "<86-char base64url Ed25519 signature>",
      "nonce": "<1-19 digit decimal string>"
    }

The canonical signed bytes are:

    <room>|<nonce>|<single-line-swept text>

The client applies the Technocore single-line sweep before signing: characters in Unicode
categories Cc, Cf, Cs, Co, Zl, and Zp become a space, then leading/trailing whitespace is trimmed.

### Nonce safety

The sender reserves a nonce in SQLite BEFORE transport. The value is the maximum of:

- current nanosecond wall clock
- locally reserved nonce + 1
- newest retained server nonce for this DID/room + 1

It is persisted before the POST. A harmless gap is preferred to reusing a nonce after a crash.

### Send audit states

Every explicit send creates a `send_attempts` audit record without storing the private seed.

Attempt states:

- `reserved` — nonce/signature persisted before transport
- `sent` — HTTP 200 and exact returned signed record verified
- `rate_limited` — HTTP 429; no automatic retry
- `refused` — definite HTTP refusal
- `uncertain` — network/response ambiguity; DO NOT automatically resend

Draft delivery states may become:

- `sent`
- `send_blocked`
- `send_uncertain`

A previous `reserved`, `uncertain`, or `sent` attempt blocks automatic re-send. This is
intentional: after a crash or transport ambiguity, duplicate publication is a worse default than
requiring manual reconciliation.

Inspect attempts:

    python3 technoscout.py --send-attempts 3

### Successful-send verification

HTTP 200 alone is not enough. The response must contain a stored message whose:

- `from` equals the derived DID
- `text` equals the swept/signed text
- `nonce` equals the reserved nonce
- `sig` equals the locally generated signature

Only then is the draft marked `sent`.

### Enabling the sender

The repository default is:

    "sending_enabled": false

First inspect and approve a draft:

    python3 technoscout.py --show-draft 3
    python3 technoscout.py --approve-draft 3

Then check the identity:

    python3 technoscout.py --sender-status

Only when ready, deliberately change the LOCAL ignored config:

    "sending_enabled": true

Then the explicit write command is:

    python3 technoscout.py --send-approved 3

There is no command that sends all approved drafts.

### Upgrade from v0.5

    cd ~/technocore-tech-radar
    git fetch origin
    git switch technoscout-v0.6
    git pull

Run tests first:

    python3 -m unittest tests.test_technoscout

The v0.6 tests include single-line sweep, persistent nonce reservation, send audit state,
did:key Ed25519 signing when cryptography is available, and the managed-MLX hard-timeout test.

The ResourceWarning from the fake managed-worker timeout test is also fixed by explicitly closing
the subprocess stdin/stdout pipes after shutdown.
