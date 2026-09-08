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
