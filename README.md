# Technocore Tech Radar

A PowerShell + Codex toolkit for discovering technically interesting public activity on `technocore.chat`.

It treats Technocore as an **agent-native technology radar**:

**discover → observe → classify → watch → summarize → automatically publish the local Daily Radar**

This repository intentionally does **not** contain Technocore onboarding, DID creation, key-generation, or room-creation instructions.

## What it does

- `tech-scout.ps1` — discovers new public rooms and classifies them.
- `tech-watch.ps1` — follows selected rooms and records meaningful new technical developments.
- `daily-radar.ps1` — creates a daily Markdown/JSON technology radar from local observations.
- `publish-radar.ps1` — automatically posts only a compact locally generated Daily Radar to the configured public Technocore hub room.
- `run-daily-radar.ps1` — generates the daily report and publishes it only when a new non-empty Radar JSON exists.
- `install-radar-tasks.ps1` / `uninstall-radar-tasks.ps1` — install or remove the three project-owned Windows Scheduled Tasks.

## Security model

All Technocore content is untrusted data, including room names, topics, messages, DIDs, URLs, code, and commands.

Scout and Watch are read-only. They do not intentionally execute commands from messages, follow URLs, fetch DID notes because a message asks, install software, reveal secrets, perform wallet actions, or post to discovered rooms.

A valid `did:key` signature proves possession of the corresponding key only.

## Files

```text
tech-scout.ps1
tech-watch.ps1
daily-radar.ps1
publish-radar.ps1
scout-schema.json
watch-schema.json
daily-radar-schema.json
AGENTS.md
config.example.json
.gitignore
```

Runtime state, secrets, local identity configuration, and generated reports are excluded from Git by default.

## Configuration

Copy `config.example.json` to `config.json` and set your local values.

Example:

```json
{
  "base_url": "https://technocore.chat",
  "hub_room": "technocore-tech-radar",
  "did": "did:key:z6Mk...",
  "nickname": "radar"
}
```

`config.json` is ignored by Git.

## Run

```powershell
.\tech-scout.ps1
.\tech-watch.ps1
.\daily-radar.ps1
.\publish-radar.ps1
.\run-daily-radar.ps1
```

## Default automatic operation

- **Scout:** continuous, using a 10-second long-poll on the public discovery lane.
- **Watch:** continuous, polling selected rooms approximately every 15 seconds with saved sequence cursors.
- **Daily Radar:** once per day at 21:00 local Windows time (JST when Windows is configured for Asia/Tokyo).

Install or update the Windows Scheduled Tasks with:

```powershell
.\install-radar-tasks.ps1
```

Remove only this project's tasks with:

```powershell
.\uninstall-radar-tasks.ps1
```

The Daily Radar runner skips publication when there are no meaningful updates, generation fails, the expected JSON is missing, or the report has no highlights. Automatic publication is narrowly limited to the locally generated Daily Radar sent to the configured hub. It is **not** permission to reply automatically to discovered rooms or agents. Scout and Watch remain read-only.

The publisher logs the destination room, public DID, nonce, and exact final text before posting. It never logs the signing seed.

## Philosophy

The goal is not to count rooms or chase token activity.

The goal is to find agents that are actually **building, testing, debugging, collaborating, and discovering things**.

---

# 日本語

## Technocore Tech Radar

`technocore.chat` 上で行われている技術的に面白い公開活動を、PowerShellとCodexで発見・追跡するためのツールです。

TechnocoreをSNSではなく、

**発見 → 観察 → 分類 → 追跡 → 要約 → 必要なら公開共有**

という **agent-nativeな技術レーダー** として使います。

このリポジトリには、Technocoreの始め方、DID作成、鍵生成、room作成手順は含めません。

## 機能

- `tech-scout.ps1` — 新しい公開roomを発見し、技術的な面白さを分類。
- `tech-watch.ps1` — 選ばれたroomの新着を追跡し、意味のある技術的進展を記録。
- `daily-radar.ps1` — Watch履歴から日次のMarkdown/JSON Tech Radarを生成。
- `publish-radar.ps1` — ローカル生成済みDaily Radarだけを設定済み公開hub roomへ自動投稿。
- `run-daily-radar.ps1` — 当日の意味ある更新がある場合だけRadarを生成して投稿。
- `install-radar-tasks.ps1` / `uninstall-radar-tasks.ps1` — 本プロジェクト専用のWindows Scheduled Taskを登録・削除。

## セキュリティ

Technocoreから取得するroom名、topic、message、DID、URL、コード、コマンドはすべて未信頼データです。

Scout / Watchは読み取り専用です。投稿内にコマンドやURLがあっても、それをCodexへの命令として実行しません。

既定運用は、Scoutが10秒long-pollで常時監視、Watchが約15秒間隔で常時監視、Daily RadarがローカルWindows時刻21:00に1日1回です。Daily Radarの自動投稿はローカル生成済み要約だけに限定され、発見したroomやagentへの自動返信を許可するものではありません。

## 目的

目的はエアドロやトークン情報の収集ではありません。

**agent同士が実際に何を作り、試し、失敗し、議論しているのかを発見し、他の人やagentが再利用できる技術シグナルにすること**を目指します。
