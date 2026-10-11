# claude-infrastructure

Инфраструктура для Claude Code: автозапуск двух сессий в tmux при старте системы (Armbian / Debian, systemd).

| tmux-сессия | каталог запуска            | аккаунт Claude                 |
|-------------|----------------------------|--------------------------------|
| `namesilo`  | `~/projects/NameSilo`      | основной (`~/.claude`)         |
| `personal`  | `~/projects/millerrabin`   | второй (`CLAUDE_CONFIG_DIR=~/.claude-2`) |

Оба Claude запускаются с `--resume` (открывается список сессий каталога).

## Файлы

- `bin/claude-tmux-start` — создаёт недостающие сессии (идемпотентно).
- `bin/cl1`, `bin/cl2` — подключиться к `namesilo` / `personal` (создаёт, если нет).
- `systemd/claude-tmux.service` — запускает `claude-tmux-start` при загрузке от пользователя `millerrabin`.

## Установка

```bash
install -m 755 bin/* ~/.local/bin/
sudo ln -sfn ~/.local/bin/cl1 /usr/local/bin/cl1   # чтобы работало при `ssh host cl1`
sudo ln -sfn ~/.local/bin/cl2 /usr/local/bin/cl2
sudo cp systemd/claude-tmux.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now claude-tmux.service
```

В `claude-tmux.service` поправьте `User`, `Group`, `HOME` и пути под своего пользователя.

## Использование

```bash
ssh -t user@host cl1        # сессия namesilo
ssh -t user@host cl2        # сессия personal
```

Отключиться, не останавливая Claude: `Ctrl+b`, затем `d`.
Перезапуск: `sudo systemctl restart claude-tmux` (закрывает обе сессии и поднимает заново).
Только досоздать недостающие: `claude-tmux-start`.
