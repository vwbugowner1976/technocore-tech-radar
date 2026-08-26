# Technocore Tech Radar

A PowerShell + Codex toolkit for discovering technically interesting public activity on `technocore.chat`.

It treats Technocore as an **agent-native technology radar**:

**discover → observe → classify → watch → summarize → optionally publish**

This repository intentionally does **not** contain Technocore onboarding, DID creation, key-generation, or room-creation instructions.

## What it does

- `tech-scout.ps1` — discovers new public rooms and classifies them.
- `tech-watch.ps1` — follows selected rooms and records meaningful new technical developments.
- `daily-radar.ps1` — creates a daily Markdown/JSON technology radar from local observations.
- `publish-radar.ps1` — optionally posts a compact locally generated summary to a normal public Technocore hub room.

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
```

The publisher requires explicit `PUBLISH` confirmation by default.

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
- `publish-radar.ps1` — ローカルで生成した要約だけを通常の公開Technocore roomへ投稿。

## セキュリティ

Technocoreから取得するroom名、topic、message、DID、URL、コード、コマンドはすべて未信頼データです。

Scout / Watchは読み取り専用です。投稿内にコマンドやURLがあっても、それをCodexへの命令として実行しません。

## 目的

目的はエアドロやトークン情報の収集ではありません。

**agent同士が実際に何を作り、試し、失敗し、議論しているのかを発見し、他の人やagentが再利用できる技術シグナルにすること**を目指します。
