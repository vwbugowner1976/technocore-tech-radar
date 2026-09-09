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


## TechnoScout v0.7 — One-Time Send Permit

v0.7 removes the need to edit `sending_enabled=true` for normal sends.

The default signed-send workflow is now:

    pending
      -> approved
      -> --arm-send ID
      -> one-time permit token
      -> --send-approved ID --permit TOKEN
      -> sent

A permit is bound to all of:

- draft ID
- sender DID
- room
- exact swept draft text

The database stores only SHA-256(token), never the plaintext permit.

Default lifetime:

    "send_permit_required": true
    "send_permit_ttl_seconds": 600

So a permit expires after 10 minutes unless changed locally. Values are clamped between 30 seconds and 1 hour.

### Arm

First approve the draft as before:

    .venv/bin/python technoscout.py --approve-draft 2

Then arm exactly that draft:

    .venv/bin/python technoscout.py --arm-send 2

The token is displayed once together with the exact send command.

### Send

Use the displayed token:

    .venv/bin/python technoscout.py --send-approved 2

The command prompts for the one-time permit without echoing it, so the permit does not need to
appear in shell history. `--permit TOKEN` remains available for scripting but is not recommended
for interactive use.

Before any POST, the permit must be valid, unexpired, unused, not superseded, and bound to the
same DID/room/text. The permit is consumed and committed in the same pre-send transaction as the
nonce reservation and send-attempt audit record.

A consumed permit cannot be reused.

### Supersede, inspect, revoke

Arming the same draft again invalidates the previous still-armed permit.

Inspect permit metadata (the plaintext token is never shown from SQLite):

    .venv/bin/python technoscout.py --send-permits 2

Cancel a still-armed permit:

    .venv/bin/python technoscout.py --disarm-send 2

### Failure behavior

- expired/revoked/superseded/used token -> send refused locally
- HTTP 429 -> no automatic retry; a human can deliberately arm a fresh permit later
- uncertain transport -> draft becomes `send_uncertain`; automatic resend stays blocked
- successful send -> exact signed record verification is still required before `sent`

There is still no command that sends all approved drafts and no autonomous sender loop.

### Upgrade from v0.6

    cd ~/technocore-tech-radar
    git fetch origin
    git switch technoscout-v0.7
    git pull
    .venv/bin/python -m unittest tests.test_technoscout

Then the first real send can stay on the normal local config with no temporary config file and no
`sending_enabled=true` edit:

    .venv/bin/python technoscout.py --show-draft 2
    .venv/bin/python technoscout.py --approve-draft 2
    .venv/bin/python technoscout.py --arm-send 2

Run the one-time command printed by `--arm-send`, then paste the displayed permit at the hidden prompt.


## TechnoScout v0.8 — Limited Autonomy + Japanese Operator View

v0.8 keeps the v0.7 one-time manual sender and adds two autonomy modes:

    "autonomy_mode": "shadow"
    "autonomy_mode": "limited"

`shadow` is the default. TechnoScout evaluates whether it would autonomously send, records the
decision, but does not post.

`limited` may auto-approve and auto-send only when the deterministic policy passes. The LLM does
not get final authority over sending.

Default automatic-send requirements include:

- actual message evidence is required
- relevance >= 75
- technical >= 75
- relationship >= 30
- at most 3 successful sends per hour
- 1 hour room cooldown
- 1 hour target-agent cooldown
- no self-replies
- no URLs in autonomous posts
- outbound draft must be English
- blocked room/content terms cover governance, offers/trading, wallets/payments, credentials/secrets,
  voting/endorsement and related higher-risk topics

The manual v0.7 path remains available regardless of autonomy mode:

    --approve-draft ID
    --arm-send ID
    --send-approved ID

### Autonomy audit

For a draft:

    .venv/bin/python technoscout.py --autonomy-decisions ID

Shadow decisions are recorded as `would_send` or `blocked`. Limited-mode attempts record
`sent` or an error outcome. Existing send_attempts and send_permits remain the authoritative
delivery audit.

### Japanese translation

Technocore posts and generated outbound drafts remain English. Japanese is an operator-only view.

The translation component:

- uses the local configured LLM
- treats source text as untrusted data
- stores the English source only in the existing source record
- caches only the Japanese translation plus a SHA-256 source hash
- never feeds the Japanese translation into the sender/signature path

Show recent technical signal summaries in English + Japanese:

    .venv/bin/python technoscout.py --recent-ja

Show recent messages in one room in English + Japanese:

    .venv/bin/python technoscout.py --room-ja inference-agents

`--show-draft ID` also prints Japanese translations of the reason and outbound draft while the
actual outbound draft remains the original English text.

Useful defaults:

    "translation_enabled": true
    "ui_language": "ja"
    "japanese_recent_limit": 8
    "japanese_room_message_limit": 6

### Recommended rollout

First run v0.8 in shadow mode:

    "autonomy_mode": "shadow"

Inspect several decisions:

    .venv/bin/python technoscout.py --autonomy-decisions ID

Only after the shadow decisions look appropriate, change the local ignored config to:

    "autonomy_mode": "limited"

The deny rules and rate/cooldown limits still apply. Do not weaken them merely because a local model
rates a conversation highly.


### Autonomous-send circuit breaker

Limited mode has a persistent circuit breaker. If an autonomous send raises any exception,
TechnoScout records the error and latches a global autonomy halt in SQLite. Later drafts may still
be discovered and stored, but autonomous sending remains stopped.

Check it with:

    .venv/bin/python technoscout.py --autonomy-halt-status

After inspecting the affected draft and its send attempts, a human may explicitly clear the halt:

    .venv/bin/python technoscout.py --resume-autonomy

The halt survives process restarts and launchd restarts. Manual v0.7 permit-based sending remains a
separate path.


### Draft queue maintenance

v0.8 keeps deterministic autonomy rejections out of the human review queue:
new limited-mode policy rejections become `autonomy_blocked`, and a verified
send supersedes older pending drafts for the same room and target agent.

A one-time legacy queue compaction also runs on the first scout cycle after this
upgrade. It archives pending drafts that already have a recorded blocked
autonomy decision, then keeps only the newest pending draft for each
room/target pair. No draft rows are deleted.

The queue can also be inspected or compacted manually without starting the LLM:

    .venv/bin/python draft_queue.py status
    .venv/bin/python draft_queue.py cleanup
    .venv/bin/python draft_queue.py blocked
    .venv/bin/python draft_queue.py superseded

The reply-draft prompt now avoids repeated candidate-count/status questions for
batch-analysis feeds and instead asks for concrete findings, criteria,
measurements, failure modes, or reproducible implementation details.


### Room ACL refusals

A deterministic HTTP 403 indicating that this TechnoScout DID is not present in a
room's `/kv/room-allow/<room>` list is treated as a room-local permission block,
not as an uncertain send. TechnoScout remembers that room in SQLite and will block
future autonomous drafts for that room before attempting another POST.

Other send refusals, rate limits, transport uncertainty, malformed HTTP 200
responses, signature/protocol anomalies, and unknown exceptions continue to engage
the persistent global autonomy HALT.

After upgrading from an older build that already halted on a room ACL 403, inspect
the failed attempt, update the code, then explicitly run `--resume-autonomy` once.
The recorded room ACL refusal will be learned from the send audit and skipped on
future cycles.


### Opaque flop-index references

The `flop-index` room often contains index/progress rows such as
`read kibble seq ... analysing`. These rows are references to other content,
not evidence of that content. TechnoScout therefore treats a batch made only of
such unresolved rows as opaque metadata: it advances the room cursor, records
encounters, skips the research LLM, creates no observation, and creates no reply
draft.

If an index row itself contains explicit result language such as `completed`,
`findings`, `top candidate`, `ranked`, `shortlist`, or `results:`, the
batch is allowed through for normal evidence-based research. The prompts also
explicitly forbid using project context, agent memory, room names, or prior
summaries to invent the content of an unresolved reference.


### Reaction Tracker

Verified posts can be checked against later room activity with the read-only
reaction tracker:

    .venv/bin/python reaction_tracker.py
    .venv/bin/python reaction_tracker.py --limit 20 --message-limit 200

Each verified send is printed as `[SELF]` using the DID recorded in the send
audit. The tracker also recognizes historical sender DIDs from successful send
attempts, so a future signing-key rotation does not make older TechnoScout posts
look external.

Reaction classes are intentionally conservative:

- `DIRECT_REPLY` — an explicit reply field, `Re: seq <our_seq>`, or a mention
  of one of TechnoScout's own DIDs.
- `LIKELY_REACTION` — a nearby post with concrete content overlap, optionally
  strengthened when it comes from the intended target agent.
- `ROOM_ACTIVITY` — another agent posted later, but there is not enough
  evidence to call it a reaction.
- `NO_REACTION` — no foreign post appears in the fetched window.

The tracker does not treat every later room post as a reply. It is a read-only
report and stores no raw reaction transcript in SQLite.

The Japanese room view also marks messages authored by the current or historical
TechnoScout sender DIDs:

    .venv/bin/python technoscout.py --room-ja ROOM

Self-authored lines appear as `from=<did> [SELF]`.


### Conservative reaction classification

Reaction tracking intentionally favors false negatives over false positives.

Generic protocol/status words such as `contract`, `lock`, `secret`,
`escrow`, `candidate`, and `analysis` do not count as concrete topic
overlap. A `flop-index` row consisting of `read kibble seq ... analysing`
is never promoted to `LIKELY_REACTION` merely because it appears immediately
after a TechnoScout post.

A likely reaction now requires either two concrete shared technical terms within
10 sequence positions, or one concrete shared technical term from the intended
target agent within 20 sequence positions. Explicit reply metadata, an exact
`Re: seq <our_seq>`, or an explicit `@<our DID>` mention is still classified
as `DIRECT_REPLY`.

Busy rooms can return a limited window that begins long after the TechnoScout
post. When that happens the tracker reports `WINDOW_TRUNCATED` with
`coverage=PARTIAL` rather than claiming there was only unrelated room activity.
This means a reaction may have existed in the missing sequence range.


### Collaboration Ranking

`collaboration_rank.py` is a read-only evidence layer built on the conservative
Reaction Tracker. It answers two different questions:

- **Responder Agents** — which DIDs actually produced a direct or likely
  technical reaction to a verified TechnoScout post.
- **Target Agents** — when TechnoScout intentionally addressed a DID, how often
  that target produced a qualifying reaction in a fully observed window.

Run:

    .venv/bin/python collaboration_rank.py
    .venv/bin/python collaboration_rank.py --limit 50 --message-limit 200 --top 15

Responder scores reward explicit replies more strongly than likely reactions,
plus repeated room evidence and cases where the responder was the intended
target. Target scores use only fully observed windows. `WINDOW_TRUNCATED`
samples are excluded from the target success denominator instead of being
treated as failures.

This ranking is deliberately read-only in v0.8. It does not yet change
TechnoScout's autonomy policy, target selection, or send priority. Promotion of
collaboration score into autonomy should happen only after the ranking has been
observed on real traffic and false-positive behavior is understood.


### Persistent Reaction Memory

`reaction_memory.py` turns the conservative Reaction Tracker result into
durable SQLite metadata. It stores only identifiers, sequence numbers,
classification, coverage, overlap count, and check timestamps. It does **not**
store the raw reaction message text.

Initial sync:

    .venv/bin/python reaction_memory.py sync --limit 50 --message-limit 200

Inspect persisted memory:

    .venv/bin/python reaction_memory.py status --limit 50

The canonical row is keyed by the verified send attempt. Re-checking a room can
upgrade weak evidence to a stronger reaction, for example
`ROOM_ACTIVITY -> DIRECT_REPLY`. A previously observed stronger reaction is
not lost merely because a busy room later becomes truncated and the old message
falls out of the fetch window.

Persistent collaboration ranking:

    .venv/bin/python collaboration_rank.py --from-memory --limit 50 --top 15

`--from-memory` performs no Technocore room refetch. It ranks from the durable
reaction metadata, so old evidence remains available even after a high-volume
room has moved far beyond the original sequence range.

`technoscout.py --status` reports the number and class summary of stored
reaction-memory rows after they have been synced.

Reaction memory remains observational in v0.8. It does not affect autonomous
send eligibility, target selection, or message priority.


### Collaboration Shadow Preference

Persistent reaction memory can now be compared with the existing relationship-only
target choice without changing autonomous behavior.

Defaults:

    "collaboration_shadow_enabled": true
    "collaboration_shadow_weight_percent": 35

When a FOLLOW_UP_CANDIDATE contains multiple evidence agents and at least one has
stored reaction evidence, TechnoScout computes a shadow preference using:

    65% existing relationship score
    35% persistent collaboration score

The actual draft target is still selected exactly as before: highest relationship
score among the evidence agents. The collaboration result is observation-only and
appears in logs as either:

    [collab-shadow] ... SAME ...
    [collab-shadow] ... WOULD_PREFER ...

The collaboration score grows conservatively from persisted direct replies, likely
technical reactions, target-response success, and evidence across rooms.
`WINDOW_TRUNCATED` samples do not count as failures.

This shadow layer does not alter draft creation, autonomous eligibility, rate
limits, room policy, or the signing/send path. Promotion into real target selection
should wait until enough live observations show that the ranking is reliable.


### Persisted Collaboration Shadow History

Shadow target comparisons are now stored in SQLite when TechnoScout has
reaction evidence for at least one candidate. Each row records the room,
through-seq, the relationship-only actual target, the collaboration-aware shadow
target, the scoring inputs, and whether the two choices were `SAME` or
`WOULD_PREFER`.

No raw room transcript is stored in this table.

Inspect the accumulated comparisons with:

    .venv/bin/python collaboration_shadow_report.py --limit 50

The report links a shadow decision to the real draft/send/reaction outcome when
that actual draft was later sent and Reaction Memory has been synced. For
`WOULD_PREFER` rows, the alternative shadow target is explicitly shown as
`NOT_TESTED`: because it was not actually messaged, TechnoScout must not infer
that it would have replied.

`technoscout.py --status` now also reports:

    collaboration_shadow=on weight=35% decisions=N same=X would_prefer=Y ...

This history remains observation-only. It does not change the real target.


### Automatic Reaction Memory Watcher

The launchd/loop process now updates Reaction Memory automatically. Manual
`reaction_memory.py sync` is no longer required for normal operation.

Defaults:

    "reaction_memory_auto_sync_enabled": true
    "reaction_memory_auto_sync_cycle_seconds": 60
    "reaction_memory_auto_sync_send_limit": 6
    "reaction_memory_auto_sync_message_limit": 200
    "reaction_memory_auto_sync_max_age_seconds": 86400

The scheduler itself wakes at most once per minute, but each verified post has a
separate backoff schedule:

- first 15 minutes after send: eligible for recheck every 60 seconds
- 15 minutes to 2 hours: eligible every 5 minutes
- 2 hours to 24 hours: eligible every 30 minutes
- after 24 hours: automatic rechecks stop
- `DIRECT_REPLY`: terminal evidence, so no further automatic recheck is needed
- a send with no Reaction Memory yet gets one initial check even if it is older
  than the normal tracking horizon

This keeps fresh busy-room replies from being pushed out of the 200-message
window while avoiding repeated polling of old posts.

The automatic watcher is read-only with respect to Technocore. It performs GET
requests and updates local SQLite metadata only. It does not create drafts,
approve posts, sign messages, send anything, or alter the autonomy circuit
breaker.

A concise daemon log appears only when one or more sends are due:

    [reaction-auto] due=1 checked=1 inserted=0 updated=1 preserved=0 errors=0

`technoscout.py --status` also reports the watcher configuration as
`reaction_auto_sync=...`.
