# claude-infrastructure

Infrastructure for Claude Code: starts two Claude sessions in tmux automatically at boot (Armbian / Debian, systemd).

| tmux session | working directory          | Claude account                              |
|--------------|----------------------------|---------------------------------------------|
| `namesilo`   | `~/projects/NameSilo`      | primary (`~/.claude`)                       |
| `personal`   | `~/projects/millerrabin`   | secondary (`CLAUDE_CONFIG_DIR=~/.claude-2`) |

Both sessions start Claude with `--resume`, which opens the list of sessions for the directory.

## Files

- `bin/claude-tmux-start` creates any missing sessions (idempotent).
- `bin/cl1`, `bin/cl2` attach to `namesilo` / `personal` (creating the session if it does not exist).
- `systemd/claude-tmux.service` runs `claude-tmux-start` at boot as the user `millerrabin`.

## Installation

```bash
install -m 755 bin/* ~/.local/bin/
sudo ln -sfn ~/.local/bin/cl1 /usr/local/bin/cl1   # so that `ssh host cl1` works
sudo ln -sfn ~/.local/bin/cl2 /usr/local/bin/cl2
sudo cp systemd/claude-tmux.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now claude-tmux.service
```

Edit `User`, `Group`, `HOME` and the paths in `claude-tmux.service` to match your user.

## Usage

```bash
ssh -t user@host cl1        # session namesilo
ssh -t user@host cl2        # session personal
```

Detach without stopping Claude: `Ctrl+b`, then `d`.
Restart: `sudo systemctl restart claude-tmux` (closes both sessions and starts them again).
Create only the missing sessions: `claude-tmux-start`.
