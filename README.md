# claude-infrastructure

Infrastructure for Claude Code: starts two Claude sessions in tmux automatically at boot (Armbian / Debian, systemd).

| tmux session | working directory          | Claude account                              |
|--------------|----------------------------|---------------------------------------------|
| `namesilo`   | `~/projects/NameSilo`      | primary (`~/.claude`)                       |
| `personal`   | `~/projects/millerrabin`   | secondary (`CLAUDE_CONFIG_DIR=~/.claude-2`) |

Both sessions start Claude with `--resume`, which opens the list of sessions for the directory.

## Files

- `bin/claude-tmux-start` creates any missing sessions (idempotent).
- `bin/namesilo`, `bin/personal` attach to the tmux sessions with the same name (creating the session if it does not exist).
- `systemd/claude-tmux.service` runs `claude-tmux-start` at boot as the user `millerrabin`.

## Installation

```bash
install -m 755 bin/* ~/.local/bin/
sudo ln -sfn ~/.local/bin/namesilo /usr/local/bin/namesilo   # so that `ssh host namesilo` works
sudo ln -sfn ~/.local/bin/personal /usr/local/bin/personal
sudo cp systemd/claude-tmux.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now claude-tmux.service
```

Edit `User`, `Group`, `HOME` and the paths in `claude-tmux.service` to match your user.

## Usage

```bash
ssh -t user@host namesilo   # session namesilo
ssh -t user@host personal   # session personal
```

Detach without stopping Claude: `Ctrl+b`, then `d`.
Restart: `sudo systemctl restart claude-tmux` (closes both sessions and starts them again).
Create only the missing sessions: `claude-tmux-start`.
