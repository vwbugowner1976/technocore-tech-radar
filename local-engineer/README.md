# Local Engineer v0.1

Mac mini local-agent layer for Bonsai 2 27B, Technocore, and keyboard firmware projects in WSL.

## Install

```bash
git clone --branch local-engineer-v0.1 --single-branch https://github.com/vwbugowner1976/technocore-tech-radar.git ~/local-engineer-src
cd ~/local-engineer-src/local-engineer
./install.sh
source ~/.zshrc
```

## LLM switching

```bash
llm status
llm bonsai
llm bonsai-tech
llm qwen
llm stop
llm logs
```

- `llm bonsai`: stop Technocore/Qwen and give Bonsai the machine for Local Engineer.
- `llm bonsai-tech`: Bonsai + Technocore using `TECHNOCORE_LLM_BACKEND=managed_bonsai`. Run this only after the Technocore backend patch succeeds.
- `llm qwen`: stop Bonsai and restore `managed_mlx`.

Bonsai runs from the existing `~/Bonsai-demo` installation with PQ2_0, Metal, context 8192, text-only mode, and a server-wide reasoning budget of 2048.

## WSL projects

Default SSH alias: `wsl`.

```bash
local-engineer projects
local-engineer discover
local-engineer build prospector
local-engineer build rmk-pg1kb
```

Override the SSH host with `LOCAL_ENGINEER_SSH_HOST` or edit `~/.config/local-engineer/projects.json`.

## Autonomous repair

```bash
local-engineer fix prospector "Build locally, diagnose the current error, make the smallest safe fix, and rebuild."

local-engineer fix rmk-pg1kb "Investigate the trackball regression, preserve working behavior, fix it, and build."
```

The agent can inspect/search files, make bounded file edits, run allowlisted builds/tests, and inspect diffs. It does not push or commit. Destructive/admin/network commands are blocked. Files touched by a run are backed up under `~/.local/state/local-engineer/backups/`.

## Add Bonsai backend to the current local Technocore

```bash
llm bonsai
local-engineer technocore-bonsai
```

This asks Bonsai to inspect the current Mac-side Technocore tree (not the older GitHub snapshot) and add `managed_bonsai` beside `managed_mlx`, preserving existing behavior and tests.

When the patch and tests succeed:

```bash
llm bonsai-tech
```

The manager checks that no `mlx_worker.py` appears in Bonsai mode; if one does, it stops Technocore again to avoid another 16 GB unified-memory collision.
