# Telegram front-end for Claude Code

A small bot (Python standard library only, no dependencies) that shows your Claude Code sessions in Telegram and lets
you continue them from the phone.

- Every Claude **account** is a Telegram **supergroup with Topics**: `namesilo` and `personal`.
- Every Claude **session** is a **topic** of its group. Recent sessions (default: active in the last 7 days) get a topic
  automatically; older ones with `/open <id>`.
- **Mirroring:** every new message of every session is posted into its topic: your prompts (`👤`), Claude's answers (`🤖`)
  and tool calls (`🔧`, grouped into one collapsible block). This includes what you type in the tmux session on the machine itself. History is not replayed.
- **Writing:** a message in a topic continues that session with `claude -p --resume <id>`. A message in the group's
  General topic starts a **new** session (and creates its topic); a message in a topic you created by hand starts a new
  session bound to that topic.
- Only the Telegram user ids in `allowed_user_ids` can do anything. Everybody else is ignored silently.

> **Where it runs.** On `deploy.int-t.com` under the dedicated unprivileged user `claude` (no sudo, no `docker` group; an ACL
> denies it `/home/ci`, the unit adds `NoNewPrivileges`, `InaccessiblePaths` and memory/task limits). It used to run on an
> Orange Pi. The unit in this repository is the deploy version. The VPN side (tunnels, split DNS for the FSK domains) is
> described in the `infrastructure` repository (`vpn/README.md`).

## Security: read this first

`default_mode = "full"` runs Claude with `--dangerously-skip-permissions`: **a message from an allowed Telegram account
can run any command as the bot's Linux user** (here `claude`: it can use the VPN tunnels and the servers behind them, and read
everything in its own home, including the SSH keys and tokens copied there).

- `allowed_user_ids` must contain only your own account. With an empty list the bot is in **setup mode** and executes nothing.
- Keep the token in `~/.config/claude-tg/token` (mode 600). Anyone with the token can read the messages the bot sees.
- Use `/mode safe` in a topic (or `default_mode = "safe"`) to restrict Claude to the read-only tools `Read`, `Glob`, `Grep`.
- The systemd unit sets `NoNewPrivileges=yes`, so Claude cannot use `sudo`.
- Do not use the same session in Telegram and in tmux at the same time: both would append to the same file.
- Deleting a topic in Telegram only removes its messages there. The session files stay on the machine, `/resync` recreates the topic.
- Enable two-step verification on the Telegram account that is allowed to control the bot.

## Setup

1. In Telegram, talk to **@BotFather**: `/newbot`, copy the token.
2. Create **two supergroups**, open each group's settings, enable **Topics**, add the bot as **administrator** with the
   right to **manage topics** (an admin bot also receives all messages).
3. On the machine, store the token (it is never committed):

   ```bash
   install -d -m 700 ~/.config/claude-tg
   read -rsp "token: " T; echo; (umask 077; printf '%s\n' "$T" > ~/.config/claude-tg/token); unset T
   ```
4. Install and start in **setup mode** (`allowed_user_ids = []`):

   ```bash
   install -d ~/claude-tg-bot && install -m 755 bot.py ~/claude-tg-bot/
   install -m 600 config.example.toml ~/.config/claude-tg/config.toml   # then empty the [[groups]] for setup
   sudo cp claude-tg-bot.service /etc/systemd/system/ && sudo systemctl daemon-reload
   sudo systemctl enable --now claude-tg-bot
   ```
5. Send `/whoami` in a chat with the bot and in each group (General topic). It answers with your **user id** and the
   **chat id** of the group. Put them into `~/.config/claude-tg/config.toml` (`allowed_user_ids`, `chat_id` of each group).
6. `sudo systemctl restart claude-tg-bot`. Topics for recent sessions appear within seconds.

## Commands

| Where | Command | Effect |
|---|---|---|
| topic | any text | continue the session |
| a topic you created yourself | any text | starts a **new** session bound to that topic (then it is an ordinary topic) |
| topic | `/mode full\|safe` | full access / read-only (per topic) |
| topic | `/stop` | kill the running `claude` for this session |
| topic | `/tools quote\|spoiler\|plain\|off` | how tool calls (`🔧`) are shown in this topic: `quote` = collapsible block (default), `spoiler` = hidden until tapped, `plain` = ordinary lines, `off` = not shown |
| topic | `/model [name\|default]` | model of this topic (`sonnet`, `opus`, `opusplan`, `fable`, `haiku` or a full model id); without an argument shows the current one; `default` returns to the account's default. Passed as `--model` on every run. Claude's own `/model` is `//model` |
| General | `/model [name\|default]` | model of **new** sessions of the group (existing topics keep theirs) |
| topic | `/usage`, `/context`, `/cost`, `/model`, `/compact`, any other `/command` | forwarded to Claude (`claude -p --resume`) and the output is posted at once. `/status` is not available in headless mode |
| topic | `//help` | sends `/help` to Claude even when the bot has a command with that name (the bot's own: `/help /mode /stop /sessions /open /resync /whoami`) |
| General | any text | start a new session and create its topic |
| General | `/sessions` | list recent sessions (● has a topic, ○ has not) |
| General | `/open <id>` | create a topic for an older session (first characters of the id are enough) |
| General | `/resync` | check every known topic and recreate the ones deleted in Telegram (old messages of a deleted topic cannot be recovered; the Claude session is intact) |
| anywhere | `/help`, `/whoami` | help, ids |

## How it works

- Sessions are the `*.jsonl` files in `<config_dir>/projects/<project>/` (the same files `claude --resume` shows).
  The bot tails them and posts new lines; it also renames a topic when Claude generates a session title.
- **Formatting:** Claude writes Markdown, Telegram does not render it, so the bot converts it to Telegram's HTML subset:
  `**bold**`, `*italic*`, `~~strike~~`, `` `code` ``, fenced code blocks (with language), `# headings` (bold), lists (`•`),
  `> quotes`, links (only `http(s)`, `tg`, `mailto`) and tables (monospace, Telegram has no tables). All text is escaped.
  Telegram does not allow code inside bold/italic, so `**`x`**` becomes plain monospace `x`. If Telegram rejects a message's
  formatting, it is sent again as plain text.
- **Attachments:** a text file (or a text file sent as a document) is placed into the prompt together with its caption, up to
  `max_inline_bytes` (150 KB by default). Larger files, binary files and photos are saved to `uploads_dir` (mode 600, files older
  than 14 days are removed) and Claude is told the path so it can open it with its `Read` tool (it can read images too).
  Saved files never replace each other: the name contains the time and Telegram's unique file id, and an existing name gets a
  numeric suffix. Several photos sent at once (an album) arrive as separate Telegram messages; the bot waits
  `album_wait_seconds` (2 s) after the last one and answers **one** prompt for the whole album, with the caption.
  The Bot API limit for downloads is 20 MB. Stickers, voice messages and videos are ignored. Only allowed users' files are
  downloaded.
- Prompts typed in Telegram are not echoed back (they are already visible); only Claude's output is mirrored.
- Telegram limits: about 20 messages per minute per group (the bot throttles itself), 4096 characters per message
  (long answers are split into several messages, at most 12).

## Tests

```bash
python3 tests/test_bot.py -v
```

The tests use a fake Telegram Bot API and a fake `claude`; no token or network is needed.
