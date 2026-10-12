#!/usr/bin/env python3
"""Telegram front-end for Claude Code sessions (standard library only).

Every Claude account is a Telegram supergroup with Topics enabled; every Claude session is a topic.
  * New messages of every recent session (also the ones typed in tmux) are mirrored into its topic.
  * A message written into a topic continues that session with `claude -p --resume <id>`.
  * A message written into the group's General topic starts a new session (and creates its topic).
Only the Telegram user ids listed in the config can do anything; everybody else is ignored.
"""
import collections
import glob
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
import uuid

log = logging.getLogger("claude-tg")
DEFAULT_CLAUDE_DIR = os.path.expanduser("~/.claude")
OWN_COMMANDS = {"/help", "/sessions", "/open", "/resync", "/mode", "/stop", "/whoami"}   # everything else goes to Claude
MAX_CHUNK = 3800            # Telegram limit is 4096 characters
MAX_CHUNKS = 12


def expand(p):
    return os.path.expanduser(p) if isinstance(p, str) else p


def project_key(path):
    """Claude stores sessions in a directory named after the working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(expand(path)))


def chunks(text):
    text = text.strip()
    out = []
    while text and len(out) < MAX_CHUNKS:
        if len(text) <= MAX_CHUNK:
            out.append(text)
            break
        cut = text.rfind("\n", 0, MAX_CHUNK)
        cut = cut if cut > MAX_CHUNK // 2 else MAX_CHUNK
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    else:
        if text:
            out[-1] += "\n…(truncated)"
    return out


# --------------------------------------------------------------------------- config

class Group:
    def __init__(self, raw):
        self.name = raw["name"]
        self.chat_id = int(raw["chat_id"])
        self.config_dir = expand(raw.get("config_dir", DEFAULT_CLAUDE_DIR))
        self.project_dir = expand(raw["project_dir"])
        self.transcripts = os.path.join(self.config_dir, "projects", project_key(self.project_dir))


class Config:
    def __init__(self, path):
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        with open(expand(raw["token_file"])) as f:
            self.token = f.read().strip()
        self.allowed = {int(x) for x in raw.get("allowed_user_ids", [])}
        self.recent_days = float(raw.get("recent_days", 7))
        self.mirror_tools = bool(raw.get("mirror_tools", True))
        self.state_file = expand(raw.get("state_file", "~/.local/state/claude-tg/state.json"))
        self.claude_bin = expand(raw.get("claude_bin", "~/.local/bin/claude"))
        self.api_base = raw.get("api_base", "https://api.telegram.org")
        self.run_timeout = int(raw.get("run_timeout_seconds", 3600))
        self.default_mode = raw.get("default_mode", "full")      # full | safe
        self.groups = [Group(g) for g in raw.get("groups", [])]


# --------------------------------------------------------------------------- telegram

class TGError(Exception):
    def __init__(self, description, code=0):
        super().__init__(description)
        self.description, self.code = description, code


class TG:
    def __init__(self, base, token):
        self.url = "%s/bot%s/" % (base.rstrip("/"), token)
        self.hist = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def _throttle(self, chat_id):
        """Telegram allows about 20 messages per minute into one group."""
        while True:
            with self.lock:
                q, now = self.hist[chat_id], time.time()
                while q and now - q[0] > 60:
                    q.popleft()
                if len(q) < 18:
                    q.append(now)
                    return
                wait = 60 - (now - q[0]) + 0.2
            time.sleep(wait)

    def call(self, method, _timeout=70, **params):
        data = json.dumps(params).encode()
        for attempt in range(3):
            req = urllib.request.Request(self.url + method, data=data, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=_timeout) as r:
                    body = json.load(r)
            except urllib.error.HTTPError as e:
                try:
                    body = json.load(e)
                except Exception:
                    raise TGError("HTTP %s" % e.code, e.code)
            if body.get("ok"):
                return body["result"]
            retry = (body.get("parameters") or {}).get("retry_after")
            if body.get("error_code") == 429 and retry and attempt < 2:
                time.sleep(retry + 1)
                continue
            raise TGError(body.get("description", "error"), body.get("error_code", 0))

    def send(self, chat_id, text, thread=None):
        for part in chunks(text) or [""]:
            if not part:
                continue
            self._throttle(chat_id)
            params = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
            if thread:
                params["message_thread_id"] = thread
            self.call("sendMessage", **params)


# --------------------------------------------------------------------------- state

class State:
    def __init__(self, path):
        self.path, self.lock = path, threading.RLock()
        self.data = {"offset": 0, "topics": {}}
        try:
            with open(path) as f:
                self.data.update(json.load(f))
        except FileNotFoundError:
            pass

    def save(self):
        with self.lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)

    def topics(self, group):
        return self.data["topics"].setdefault(group, {})


# --------------------------------------------------------------------------- transcripts

SKIP_PREFIXES = ("<command-", "<local-command", "<system-reminder", "Caveat:", "[Request interrupted")


def short(s, n=200):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_line(block):
    name, inp = block.get("name", "tool"), block.get("input") or {}
    detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("url") \
        or inp.get("description") or inp.get("prompt") or ""
    return "🔧 %s%s" % (name, (": " + short(detail)) if detail else "")


def parse_line(line, mirror_tools=True):
    """One transcript line -> list of (kind, text); kind is user | assistant | tool | title."""
    try:
        d = json.loads(line)
    except ValueError:
        return []
    out = []
    for key in ("customTitle", "aiTitle"):
        if isinstance(d.get(key), str) and d[key].strip():
            out.append(("title", d[key].strip()))
    if d.get("isSidechain") or d.get("isMeta"):
        return out
    msg = d.get("message")
    if d.get("type") == "user" and isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                return out
            c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        if isinstance(c, str) and c.strip() and not c.lstrip().startswith(SKIP_PREFIXES):
            out.append(("user", c.strip()))
    elif d.get("type") == "assistant" and isinstance(msg, dict) and isinstance(msg.get("content"), list):
        for b in msg["content"]:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                out.append(("assistant", b["text"].strip()))
            elif b.get("type") == "tool_use" and mirror_tools:
                out.append(("tool", tool_line(b)))
    return out


def norm(text):
    return " ".join(text.split())


# --------------------------------------------------------------------------- bot

class Bot:
    def __init__(self, cfg):
        self.cfg, self.tg, self.state = cfg, TG(cfg.api_base, cfg.token), State(cfg.state_file)
        self.groups = {g.chat_id: g for g in cfg.groups}
        self.sid_locks = collections.defaultdict(threading.Lock)
        self.procs = {}
        self.bot_prompts = collections.defaultdict(collections.deque)   # prompts sent through Telegram
        self.backoff = {}                                               # group -> time before which it is skipped
        self.stop = threading.Event()

    # ---- topics -----------------------------------------------------------------
    def find_sid(self, group, thread):
        for sid, st in self.state.topics(group.name).items():
            if st.get("thread") == thread:
                return sid
        return None

    def create_topic(self, group, sid, title, pos):
        name = short(title or sid[:8], 100)
        res = self.tg.call("createForumTopic", chat_id=group.chat_id, name=name)
        st = {"thread": res["message_thread_id"], "pos": pos, "title": name, "mode": self.cfg.default_mode, "events": 0}
        with self.state.lock:
            self.state.topics(group.name)[sid] = st
        self.state.save()
        self.tg.send(group.chat_id, "Сессия %s. Пишите сюда, чтобы продолжить её (режим: %s, /mode full|safe, /stop)." %
                     (sid[:8], st["mode"]), st["thread"])
        log.info("topic created group=%s session=%s", group.name, sid[:8])
        return st

    def recreate_topic(self, group, sid, st):
        try:
            res = self.tg.call("createForumTopic", chat_id=group.chat_id, name=short(st.get("title") or sid[:8], 100))
            st["thread"] = res["message_thread_id"]
            self.state.save()
        except TGError as e:
            log.warning("cannot recreate topic: %s", e)

    def topic_exists(self, group, thread):
        """None when Telegram could not tell (rate limit etc.)."""
        try:
            self.tg.call("sendChatAction", _timeout=15, chat_id=group.chat_id, message_thread_id=thread, action="typing")
            return True
        except TGError as e:
            if "thread not found" in e.description.lower() or "topic_id_invalid" in e.description.lower():
                return False
            return None

    def resync_group(self, group):
        """Recreate the topics that were deleted in Telegram. Returns (checked, recreated, unknown)."""
        topics, recreated, unknown = self.state.topics(group.name), 0, 0
        for sid, st in list(topics.items()):
            alive = self.topic_exists(group, st["thread"])
            time.sleep(0.4)
            if alive is None:
                unknown += 1
            elif not alive:
                self.recreate_topic(group, sid, st)
                try:
                    self.tg.send(group.chat_id, "Тема восстановлена (сессия %s). Прежние сообщения темы в Telegram потеряны, "
                                 "сессия цела." % sid[:8], st["thread"])
                except TGError as e:
                    log.warning("resync notice: %s", e)
                recreated += 1
        return len(topics), recreated, unknown

    def send_topic(self, group, sid, st, text):
        try:
            self.tg.send(group.chat_id, text, st["thread"])
        except TGError as e:
            if "thread not found" in e.description.lower() or "topic" in e.description.lower() and "deleted" in e.description.lower():
                self.recreate_topic(group, sid, st)
                self.tg.send(group.chat_id, text, st["thread"])
            else:
                raise

    # ---- mirroring --------------------------------------------------------------
    def tail(self, group, sid, st, path):
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size < st["pos"]:
            st["pos"] = 0
        if size == st["pos"]:
            return
        with open(path, "rb") as f:
            f.seek(st["pos"])
            raw = f.read()
        end = raw.rfind(b"\n") + 1
        if end == 0:
            return
        lines, st["pos"] = raw[:end].decode("utf-8", "replace").splitlines(), st["pos"] + end
        parts = []
        for line in lines:
            for kind, text in parse_line(line, self.cfg.mirror_tools):
                if kind == "title":
                    if short(text, 100) != st.get("title"):
                        try:
                            self.tg.call("editForumTopic", chat_id=group.chat_id, message_thread_id=st["thread"],
                                         name=short(text, 100))
                            st["title"] = short(text, 100)
                        except TGError as e:
                            log.debug("rename: %s", e)
                elif kind == "user":
                    q = self.bot_prompts[sid]
                    if norm(text) in q:                       # typed in Telegram, already visible there
                        q.remove(norm(text))
                    else:
                        parts.append("👤 " + text)
                elif kind == "assistant":
                    parts.append("🤖 " + text)
                else:
                    parts.append(text)
        if parts:
            st["events"] = st.get("events", 0) + len(parts)
            self.send_topic(group, sid, st, "\n\n".join(parts))
        self.state.save()

    def mirror_once(self):
        if not self.cfg.allowed:                      # setup mode: nothing is mirrored
            return
        now = time.time()
        for g in self.cfg.groups:
            if self.backoff.get(g.name, 0) > now:
                continue
            made = 0                                  # at most 3 new topics per group and cycle
            for path in glob.glob(os.path.join(g.transcripts, "*.jsonl")):
                sid = os.path.basename(path)[:-6]
                st = self.state.topics(g.name).get(sid)
                try:
                    if st is None:
                        if now - os.path.getmtime(path) > self.cfg.recent_days * 86400 or made >= 3:
                            continue
                        st = self.create_topic(g, sid, self.title_of(path), os.path.getsize(path))   # no history replay
                        made += 1
                    self.tail(g, sid, st, path)
                except TGError as e:
                    log.warning("mirror %s/%s: %s (group paused for 60 s)", g.name, sid[:8], e)
                    self.backoff[g.name] = time.time() + 60
                    break
                except Exception:
                    log.exception("mirror %s/%s", g.name, sid[:8])

    @staticmethod
    def title_of(path):
        title = None
        try:
            with open(path, errors="ignore") as f:
                for n, line in enumerate(f):
                    if n > 400 or (title and n > 40):
                        break
                    for kind, text in parse_line(line, False):
                        if kind == "title":
                            title = text
                        elif kind == "user" and not title:
                            title = title or short(text, 60)
        except OSError:
            pass
        return title

    def mirror_loop(self):
        while not self.stop.is_set():
            try:
                self.mirror_once()
            except Exception:
                log.exception("mirror loop")
            self.stop.wait(2)

    # ---- running claude ---------------------------------------------------------
    def claude_cmd(self, sid, mode, new):
        cmd = [self.cfg.claude_bin, "-p", "--output-format", "json"]
        cmd += ["--session-id", sid] if new else ["--resume", sid]
        if mode == "safe":
            cmd += ["--permission-mode", "default", "--allowedTools", "Read", "Glob", "Grep"]
        else:
            cmd += ["--dangerously-skip-permissions"]
        return cmd

    def run_claude(self, group, sid, st, prompt, new=False, slash=False):
        with self.sid_locks[sid]:
            self.bot_prompts[sid].append(norm(prompt))
            env = os.environ.copy()
            if group.config_dir != DEFAULT_CLAUDE_DIR:
                env["CLAUDE_CONFIG_DIR"] = group.config_dir
            before, typing = st.get("events", 0), threading.Event()

            def keep_typing():
                while not typing.is_set():
                    try:
                        self.tg.call("sendChatAction", _timeout=10, chat_id=group.chat_id,
                                     message_thread_id=st["thread"], action="typing")
                    except Exception:
                        pass
                    typing.wait(4)
            threading.Thread(target=keep_typing, daemon=True).start()
            err, result = None, ""
            try:
                p = subprocess.Popen(self.claude_cmd(sid, st.get("mode", "full"), new), cwd=group.project_dir, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.procs[sid] = p
                try:
                    out, errout = p.communicate(prompt, timeout=self.cfg.run_timeout)
                except subprocess.TimeoutExpired:
                    p.kill()
                    out, errout = p.communicate()
                    err = "timeout after %s s" % self.cfg.run_timeout
                try:
                    data = json.loads(out)
                    data = data[-1] if isinstance(data, list) else data
                    result = data.get("result") or ""
                    if data.get("is_error"):
                        err = err or result or "claude returned an error"
                except ValueError:
                    pass
                if p.returncode not in (0, None) and not err:
                    err = (errout or out or "exit code %s" % p.returncode).strip()
            except Exception as e:
                err = str(e)
            finally:
                typing.set()
                self.procs.pop(sid, None)
            if err:
                self.send_topic(group, sid, st, "⚠️ " + short(err, 1500))
                return
            if slash:                                # slash command output is not written to the session file
                self.send_topic(group, sid, st, result.strip() or "✅ Команда выполнена (без вывода).")
                return
            for _ in range(10):                      # give the mirror a moment to post the answer
                if st.get("events", 0) > before:
                    return
                time.sleep(1)
            if result:
                self.send_topic(group, sid, st, "🤖 " + result)

    # ---- telegram updates -------------------------------------------------------
    def reply(self, msg, text):
        self.tg.send(msg["chat"]["id"], text, msg.get("message_thread_id"))

    def handle(self, msg):
        user, chat = msg.get("from") or {}, msg["chat"]
        text = (msg.get("text") or "").strip()
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if user.get("is_bot") or not text:
            return
        if cmd == "/whoami" and (not self.cfg.allowed or user.get("id") in self.cfg.allowed):
            self.reply(msg, "user id: %s\nchat id: %s\nthread: %s" %
                       (user.get("id"), chat["id"], msg.get("message_thread_id")))
            return
        if user.get("id") not in self.cfg.allowed:
            return                                    # everybody else is ignored silently
        group = self.groups.get(chat["id"])
        if group is None:
            if chat.get("type") == "private":
                self.reply(msg, "Группы: " + ", ".join("%s (%s)" % (g.name, g.chat_id) for g in self.cfg.groups))
            return
        thread = msg.get("message_thread_id")
        if thread == 1:                               # id of the General topic
            thread = None
        args = text.split()[1:]
        slash = False
        if text.startswith("//"):                     # "//help" sends "/help" to Claude even if the bot has its own
            text, cmd, slash = text[1:], "", True
        elif cmd and cmd not in OWN_COMMANDS:         # a Claude command such as /usage, /context, /compact
            cmd, slash = "", True
        if cmd == "/help":
            self.reply(msg, "В теме: текст продолжает сессию.\n/mode full|safe — права Claude\n/stop — прервать запуск\n"
                            "В General: текст начинает новую сессию.\n/sessions — недавние сессии\n/open <id> — создать тему для сессии\n/resync — восстановить удалённые темы\n"
                            "Остальные команды с / (/usage, /context, /cost, /model, /compact ...) уходят в Claude. "
                            "//команда отправляет её в Claude, даже если такая есть у бота.")
        elif cmd == "/sessions":
            self.cmd_sessions(group, msg)
        elif cmd == "/open":
            self.cmd_open(group, msg, args)
        elif cmd == "/resync":
            checked, recreated, unknown = self.resync_group(group)
            self.reply(msg, "Проверено тем: %d, восстановлено: %d, не удалось проверить: %d." % (checked, recreated, unknown))
        elif cmd == "/mode" and thread:
            sid = self.find_sid(group, thread)
            st = self.state.topics(group.name).get(sid) if sid else None
            if not st:
                return
            if args and args[0] in ("full", "safe"):
                st["mode"] = args[0]
                self.state.save()
            self.reply(msg, "Режим: %s (%s)" % (st["mode"], "полный доступ" if st["mode"] == "full" else "только чтение"))
        elif cmd == "/stop" and thread:
            sid = self.find_sid(group, thread)
            p = self.procs.get(sid)
            if p:
                p.kill()
            self.reply(msg, "Останавливаю." if p else "Сейчас ничего не запущено.")
        elif cmd:
            self.reply(msg, "Эта команда работает внутри темы сессии. См. /help")
        elif thread:
            sid = self.find_sid(group, thread)
            if sid:
                self.run_claude(group, sid, self.state.topics(group.name)[sid], text, slash=slash)
            else:                                     # a topic created by hand: bind a new session to it
                sid = str(uuid.uuid4())
                made = ((msg.get("reply_to_message") or {}).get("forum_topic_created") or {}).get("name") or ""
                st = {"thread": thread, "pos": 0, "title": short(made, 100), "mode": self.cfg.default_mode, "events": 0}
                with self.state.lock:
                    self.state.topics(group.name)[sid] = st
                self.state.save()
                log.info("new session bound to a manual topic group=%s session=%s", group.name, sid[:8])
                self.run_claude(group, sid, st, text, new=True, slash=slash)
        elif slash:
            self.reply(msg, "Команды Claude (%s) выполняются в теме сессии: откройте тему и отправьте её там." % text.split()[0])
        else:                                         # General: a new session
            sid = str(uuid.uuid4())
            st = self.create_topic(group, sid, short(text, 60), 0)
            self.run_claude(group, sid, st, text, new=True)

    def cmd_sessions(self, group, msg):
        rows = []
        for path in sorted(glob.glob(os.path.join(group.transcripts, "*.jsonl")), key=os.path.getmtime, reverse=True)[:25]:
            sid = os.path.basename(path)[:-6]
            mark = "●" if sid in self.state.topics(group.name) else "○"
            when = time.strftime("%d.%m %H:%M", time.localtime(os.path.getmtime(path)))
            rows.append("%s %s  %s  %s" % (mark, sid[:8], when, short(self.title_of(path) or "", 50)))
        self.reply(msg, "● есть тема, ○ нет (/open <id>)\n" + "\n".join(rows or ["(пусто)"]))

    def cmd_open(self, group, msg, args):
        if not args:
            return self.reply(msg, "Использование: /open <первые символы id>")
        hits = [p for p in glob.glob(os.path.join(group.transcripts, args[0] + "*.jsonl"))]
        if len(hits) != 1:
            return self.reply(msg, "Не нашёл однозначно (%d совпадений)." % len(hits))
        sid = os.path.basename(hits[0])[:-6]
        if sid in self.state.topics(group.name):
            return self.reply(msg, "Тема уже есть.")
        self.create_topic(group, sid, self.title_of(hits[0]), os.path.getsize(hits[0]))

    def poll_loop(self):
        while not self.stop.is_set():
            try:
                updates = self.tg.call("getUpdates", _timeout=70, offset=self.state.data["offset"],
                                       timeout=50, allowed_updates=["message"])
            except TGError as e:
                log.warning("getUpdates: %s", e)
                self.stop.wait(5)
                continue
            except Exception as e:
                log.warning("getUpdates failed: %s", e)
                self.stop.wait(5)
                continue
            for u in updates:
                self.state.data["offset"] = u["update_id"] + 1
                if "message" in u:
                    threading.Thread(target=self.safe_handle, args=(u["message"],), daemon=True).start()
            if updates:
                self.state.save()

    def safe_handle(self, msg):
        try:
            self.handle(msg)
        except Exception:
            log.exception("handle")

    def run(self):
        if not self.cfg.allowed:
            log.warning("allowed_user_ids is empty: SETUP MODE, only /whoami works, nothing is executed or mirrored")
        else:
            threading.Thread(target=self.mirror_loop, daemon=True).start()
        self.poll_loop()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    path = sys.argv[1] if len(sys.argv) > 1 else expand("~/.config/claude-tg/config.toml")
    Bot(Config(path)).run()


if __name__ == "__main__":
    main()
