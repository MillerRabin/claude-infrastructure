# claude-infrastructure

Infrastructure for Claude Code on a headless machine (Armbian / Debian): helper commands that attach to Claude Code
running in tmux, and a Telegram front-end for the sessions.

| tmux session | working directory          | Claude account                              |
|--------------|----------------------------|---------------------------------------------|
| `namesilo`   | `~/projects/NameSilo`      | primary (`~/.claude`)                       |
| `personal`   | `~/projects/millerrabin`   | secondary (`CLAUDE_CONFIG_DIR=~/.claude-2`) |

Both start Claude with `--resume`, which opens the list of sessions for the directory.

## Files

- `bin/namesilo`, `bin/personal` attach to the tmux session with the same name and create it if it does not exist.

## Installation

```bash
install -m 755 bin/* ~/.local/bin/
sudo ln -sfn ~/.local/bin/namesilo /usr/local/bin/namesilo   # so that `ssh host namesilo` works
sudo ln -sfn ~/.local/bin/personal /usr/local/bin/personal
```

## Usage

```bash
ssh -t user@host namesilo   # session namesilo
ssh -t user@host personal   # session personal
```

Detach without stopping Claude: `Ctrl+b`, then `d`.
The sessions are not started automatically: they exist only while you use them.
The Telegram bot (below) does not need them.

## Telegram bot

[`telegram-bot/`](telegram-bot) is a Telegram front-end for both Claude accounts: one supergroup per account, one topic per
session, mirroring of new messages and the ability to continue a session from the phone. See
[`telegram-bot/README.md`](telegram-bot/README.md) (read the security section first).
