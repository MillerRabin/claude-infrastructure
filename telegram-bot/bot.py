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
import html as htmllib
import json
import logging
import os
import re
import subprocess
import sys
import shutil
import threading
import time
import tomllib
import urllib.error
import urllib.request
import uuid

log = logging.getLogger("claude-tg")
DEFAULT_CLAUDE_DIR = os.path.expanduser("~/.claude")
OWN_COMMANDS = {"/help", "/sessions", "/open", "/resync", "/mode", "/model", "/tools", "/stop", "/whoami"}
TOOL_MODES = ("quote", "spoiler", "plain", "off")
RENDER_LIMIT = 3600                      # characters of HTML per message (Telegram allows 4096 of visible text)
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,63}$")     # alias (sonnet, opus, opusplan, ...) or a full model id   # everything else goes to Claude
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
        self.uploads_dir = expand(raw.get("uploads_dir", "~/.local/state/claude-tg/uploads"))
        self.album_wait = float(raw.get("album_wait_seconds", 2.0))          # pause that closes an album
        self.album_max_wait = float(raw.get("album_max_wait_seconds", 8.0))
        self.max_inline_bytes = int(raw.get("max_inline_bytes", 150000))     # text files up to this size go into the prompt
        self.max_download_bytes = int(raw.get("max_download_bytes", 20 * 1024 * 1024))   # Bot API limit is 20 MB
        self.tool_display = raw.get("tool_display", "quote")     # quote | spoiler | plain | off
        if self.tool_display not in TOOL_MODES:
            raise ValueError("tool_display must be one of %s" % ", ".join(TOOL_MODES))
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

    def download(self, file_id, limit):
        """Download a file sent to the bot. Returns (bytes, telegram_file_path)."""
        info = self.call("getFile", file_id=file_id)
        if info.get("file_size", 0) > limit:
            raise TGError("file is larger than %d bytes" % limit)
        url = self.url.replace("/bot", "/file/bot", 1) + info["file_path"]
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read(limit + 1)
        if len(data) > limit:
            raise TGError("file is larger than %d bytes" % limit)
        return data, info["file_path"]

    def send_html(self, chat_id, html, thread=None):
        self._throttle(chat_id)
        params = {"chat_id": chat_id, "text": html, "parse_mode": "HTML", "disable_web_page_preview": True}
        if thread:
            params["message_thread_id"] = thread
        try:
            self.call("sendMessage", **params)
        except TGError as e:
            if "parse entities" not in e.description.lower():
                raise
            self.send(chat_id, strip_tags(html), thread)         # formatting rejected: plain text

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

    def default_model(self, group):
        return (self.data.get("models") or {}).get(group)

    def set_default_model(self, group, model):
        with self.lock:
            models = self.data.setdefault("models", {})
            if model:
                models[group] = model
            else:
                models.pop(group, None)
        self.save()


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


# --------------------------------------------------------------------------- attachments

TEXT_EXT = {".txt", ".md", ".log", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".csv", ".tsv", ".xml", ".html",
            ".py", ".sh", ".js", ".ts", ".sql", ".go", ".rs", ".c", ".h", ".cpp", ".java", ".env", ".service", ".patch", ".diff"}


def safe_name(name, default="file"):
    name = os.path.basename((name or "").replace("\\", "/")) or default
    name = re.sub(r"[^\w.-]", "_", name)[:80].lstrip(".") or default
    return name


def decode_text(data, name, mime):
    """The text of a file when it is text, else None."""
    ext = os.path.splitext(name)[1].lower()
    if b"\x00" in data[:4096]:
        return None
    if not ((mime or "").startswith("text/") or ext in TEXT_EXT or mime in ("application/json", "application/xml")):
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def fenced(text):
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return "%s\n%s\n%s" % (fence, text.rstrip("\n"), fence)


# --------------------------------------------------------------------------- rendering

def esc(text):
    return htmllib.escape(text, quote=False)


def split_raw(text, limit):
    """Cut a raw text into pieces whose escaped form fits into `limit` characters."""
    pieces, text = [], text.strip()
    while text:
        n = min(len(text), limit)
        while n > 1 and len(esc(text[:n])) > limit:
            n = int(n * 0.8)
        cut = text.rfind("\n", 0, n) if n < len(text) else n
        cut = cut if cut > n // 2 else n
        pieces.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return pieces


# --- Markdown (what Claude writes) -> the HTML subset Telegram understands ---------------------------
CODE_MARK, TAG_MARK = "\x01", "\x00"
SAFE_URL = re.compile(r"^(https?://|tg://|mailto:)", re.I)


def _wrap(tag, inner):
    """Wrap `inner` in <tag>, but never around code: Telegram does not allow code inside bold/italic."""
    parts = re.split("(\x01\\d+\x01)", inner)
    return "".join(p if (not p or p.startswith(CODE_MARK)) else "<%s>%s</%s>" % (tag, p, tag) for p in parts)


def inline_md(text, bold=False):
    stash = []

    def keep(marker, html):
        stash.append(html)
        return "%s%d%s" % (marker, len(stash) - 1, marker)
    text = re.sub(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", lambda m: keep(CODE_MARK, "<code>%s</code>" % esc(m.group(2).strip())), text)
    text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|~>])", lambda m: keep(TAG_MARK, esc(m.group(1))), text)

    def link(m):
        url = m.group(2).strip()
        if not SAFE_URL.match(url):
            return m.group(0)
        href = esc(url).replace('"', "%22")
        return keep(TAG_MARK, '<a href="%s">' % href) + m.group(1) + keep(TAG_MARK, "</a>")
    text = re.sub(r"\[([^\[\]\n]+)\]\(([^()\s]+)\)", link, text)
    text = esc(text)
    text = re.sub(r"\*\*\*(?!\s)(.+?)(?<!\s)\*\*\*", lambda m: _wrap("b", _wrap("i", m.group(1))), text)
    text = re.sub(r"(?<![\w*])\*\*(?![\s*])(.+?)(?<![\s*])\*\*(?![\w*])", lambda m: _wrap("b", m.group(1)), text)
    text = re.sub(r"(?<!\w)__(?![\s_])(.+?)(?<![\s_])__(?!\w)", lambda m: _wrap("b", m.group(1)), text)
    text = re.sub(r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])", lambda m: _wrap("i", m.group(1)), text)
    text = re.sub(r"(?<!\w)_(?![\s_])(.+?)(?<![\s_])_(?!\w)", lambda m: _wrap("i", m.group(1)), text)
    text = re.sub(r"~~(?!\s)(.+?)(?<!\s)~~", lambda m: _wrap("s", m.group(1)), text)
    if bold:
        text = _wrap("b", text)
    return re.sub("[\x00\x01](\\d+)[\x00\x01]", lambda m: stash[int(m.group(1))], text)


def _md_line(line):
    m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
    if m:
        bullet = "•" if m.group(2) in "-*+" else m.group(2)
        return "%s%s %s" % (m.group(1)[:8], bullet, inline_md(m.group(3)))
    m = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
    if m:
        return inline_md(m.group(1), bold=True)
    if re.match(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$", line):
        return "──────────"
    return inline_md(line)


def _pre_blocks(lines, lang=""):
    out, cur, size = [], [], 0
    cls = ' class="language-%s"' % lang if lang and re.match(r"^[\w+#.-]{1,20}$", lang) else ""
    head, tail = "<pre><code%s>" % cls, "</code></pre>"

    def flush():
        if cur:
            out.append(head + "\n".join(esc(x) for x in cur) + tail)
            cur.clear()
    for line in lines:
        line = line if len(esc(line)) < 1500 else line[:1400] + "…"
        if cur and size + len(esc(line)) > RENDER_LIMIT - 200:
            flush()
            size = 0
        cur.append(line)
        size += len(esc(line)) + 1
    flush()
    return out or [head + tail]


def _table_block(rows):
    cells = []
    for row in rows:
        if re.match(r"^\s*\|?[\s:|-]+\|?\s*$", row) and "-" in row:
            continue
        cells.append([re.sub(r"[*`]|__", "", c).strip() for c in row.strip().strip("|").split("|")])
    if not cells:
        return []
    cols = max(len(r) for r in cells)
    widths = [min(40, max((len(r[i]) if i < len(r) else 0) for r in cells)) for i in range(cols)]
    text = "\n".join("  ".join((r[i] if i < len(r) else "").ljust(widths[i]) for i in range(cols)).rstrip() for r in cells)
    return _pre_blocks(text.split("\n"))


def md_blocks(text):
    """Markdown -> list of Telegram-HTML blocks, each shorter than RENDER_LIMIT."""
    lines, blocks, para, i = text.split("\n"), [], [], 0

    def flush():
        if not para:
            return
        group, size = [], 0
        for html in para:
            if group and size + len(html) > RENDER_LIMIT - 300:
                blocks.append("\n".join(group))
                group, size = [], 0
            group.append(html)
            size += len(html) + 1
        blocks.append("\n".join(group))
        para.clear()
    while i < len(lines):
        line = lines[i]
        fence = re.match(r"^\s*(`{3,}|~{3,})\s*([\w+#.-]*)\s*$", line)
        if fence:
            flush()
            ch, body, i = fence.group(1)[0], [], i + 1
            while i < len(lines) and not (lines[i].strip().startswith(ch * 3) and set(lines[i].strip()) <= {ch}):
                body.append(lines[i])
                i += 1
            blocks += _pre_blocks(body, fence.group(2))
            i += 1
        elif line.strip().startswith("|") and i + 1 < len(lines) and lines[i + 1].strip().startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            blocks += _table_block(rows)
        elif re.match(r"^\s{0,3}>", line):
            flush()
            quote = []
            while i < len(lines) and re.match(r"^\s{0,3}>", lines[i]):
                quote.append(inline_md(re.sub(r"^\s{0,3}>\s?", "", lines[i])))
                i += 1
            blocks.append("<blockquote>%s</blockquote>" % "\n".join(quote)[:RENDER_LIMIT - 100])
        elif not line.strip():
            flush()
            i += 1
        else:
            for piece in (split_raw(line, RENDER_LIMIT - 400) if len(esc(line)) > RENDER_LIMIT - 400 else [line]):
                para.append(_md_line(piece))
            i += 1
    flush()
    return blocks


def tool_blocks(lines, mode):
    """Consecutive tool calls -> collapsible Telegram blocks (HTML)."""
    out, cur = [], []

    def flush():
        if not cur:
            return
        body = "\n".join(esc(x) for x in cur)
        if mode == "quote":
            out.append("<blockquote expandable>%s</blockquote>" % body)
        elif mode == "spoiler":
            out.append("<tg-spoiler>%s</tg-spoiler>" % body)
        else:
            out.append(body)
        cur.clear()
    size = 0
    for line in lines:
        line = line if len(esc(line)) < 1000 else line[:900] + "…"
        if cur and size + len(esc(line)) > RENDER_LIMIT - 200:
            flush()
            size = 0
        cur.append(line)
        size += len(esc(line)) + 1
    flush()
    return out


def render(segments, tool_mode):
    """segments: [("text", str) | ("tool", str)] -> list of HTML messages."""
    out, cur = [], ""

    def add(chunk):
        nonlocal cur
        if cur and len(cur) + len(chunk) + 2 > RENDER_LIMIT:
            out.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + chunk
    i = 0
    while i < len(segments):
        kind, text = segments[i]
        if kind == "tool":
            group = []
            while i < len(segments) and segments[i][0] == "tool":
                group.append(segments[i][1])
                i += 1
            if tool_mode != "off":
                for block in tool_blocks(group, tool_mode):
                    add(block)
            continue
        if kind == "md":                              # Claude's answer: Markdown -> Telegram HTML
            try:
                blocks = md_blocks(text)
            except Exception:
                log.exception("markdown conversion failed")
                blocks = [esc(piece) for piece in split_raw(text, RENDER_LIMIT - 100)]
            if blocks:
                first = blocks[0]
                blocks[0] = "🤖 " + ("\n" if first.startswith(("<pre", "<blockquote")) else "") + first
            else:
                blocks = ["🤖"]
            for block in blocks:
                add(block)
        else:
            for piece in split_raw(text, RENDER_LIMIT - 100):
                add(esc(piece))
        i += 1
    if cur:
        out.append(cur)
    return out[:MAX_CHUNKS]


def strip_tags(h):
    return htmllib.unescape(re.sub(r"</?(?:b|i|s|u|a|code|pre|blockquote|tg-spoiler)(?:\s[^>]*)?>", "", h))


# --------------------------------------------------------------------------- bot

class Bot:
    def __init__(self, cfg):
        self.cfg, self.tg, self.state = cfg, TG(cfg.api_base, cfg.token), State(cfg.state_file)
        self.groups = {g.chat_id: g for g in cfg.groups}
        self.sid_locks = collections.defaultdict(threading.Lock)
        self.procs = {}
        self.bot_prompts = collections.defaultdict(collections.deque)   # prompts sent through Telegram
        self.backoff = {}                                               # group -> time before which it is skipped
        self.albums, self.album_lock = {}, threading.Lock()             # media_group_id -> collected messages
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
        st = {"thread": res["message_thread_id"], "pos": pos, "title": name, "mode": self.cfg.default_mode, "events": 0,
              "model": self.state.default_model(group.name)}
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

    def send_topic(self, group, sid, st, text, html=False):
        send = self.tg.send_html if html else self.tg.send
        try:
            send(group.chat_id, text, st["thread"])
        except TGError as e:
            if "thread not found" in e.description.lower() or "topic" in e.description.lower() and "deleted" in e.description.lower():
                self.recreate_topic(group, sid, st)
                send(group.chat_id, text, st["thread"])
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
        parts = []                                    # [("text"|"tool", str)] in transcript order
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
                        parts.append(("text", "👤 " + text))
                elif kind == "assistant":
                    parts.append(("md", text))
                else:
                    parts.append(("tool", text))
        if parts:
            st["events"] = st.get("events", 0) + len(parts)
            for message in render(parts, st.get("tools") or self.cfg.tool_display):
                self.send_topic(group, sid, st, message, html=True)
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
    def claude_cmd(self, sid, mode, new, model=None):
        cmd = [self.cfg.claude_bin, "-p", "--output-format", "json"]
        cmd += ["--session-id", sid] if new else ["--resume", sid]
        if model:
            cmd += ["--model", model]
        os.makedirs(self.cfg.uploads_dir, mode=0o700, exist_ok=True)
        cmd += ["--add-dir", self.cfg.uploads_dir]
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
                p = subprocess.Popen(self.claude_cmd(sid, st.get("mode", "full"), new, st.get("model")), cwd=group.project_dir, env=env,
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
                for message in render([("md", result.strip() or "✅ Команда выполнена (без вывода).")], "off"):
                    self.send_topic(group, sid, st, message, html=True)
                return
            for _ in range(10):                      # give the mirror a moment to post the answer
                if st.get("events", 0) > before:
                    return
                time.sleep(1)
            if result:
                for message in render([("md", result)], "off"):
                    self.send_topic(group, sid, st, message, html=True)

    # ---- attachments ------------------------------------------------------------
    def cleanup_uploads(self, max_age_days=14):
        now = time.time()
        for root, _dirs, files in os.walk(self.cfg.uploads_dir):
            for f in files:
                path = os.path.join(root, f)
                if now - os.path.getmtime(path) > max_age_days * 86400:
                    os.remove(path)

    def save_upload(self, group, name, data):
        """Write a file into the uploads dir under a name that never replaces an existing file."""
        folder = os.path.join(self.cfg.uploads_dir, group.name, time.strftime("%Y%m%d"))
        os.makedirs(folder, mode=0o700, exist_ok=True)
        stem, ext = os.path.splitext("%s-%s" % (time.strftime("%H%M%S"), name))
        root = os.path.realpath(self.cfg.uploads_dir) + os.sep
        for i in range(1000):
            path = os.path.join(folder, stem + ("" if i == 0 else "-%d" % i) + ext)
            if not os.path.realpath(path).startswith(root):
                raise TGError("bad file name")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)       # fails if the file exists
            except FileExistsError:
                continue
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            return path
        raise TGError("cannot find a free file name")

    def attachment_prompt(self, group, msgs, caption):
        """Download the attachments of one message or of a whole album and build the prompt for Claude."""
        self.cleanup_uploads()
        parts = []
        for msg in msgs:
            doc, photos = msg.get("document"), msg.get("photo")
            if doc:
                file_id, name, mime = doc["file_id"], safe_name(doc.get("file_name")), doc.get("mime_type") or ""
            elif photos:
                uid = safe_name(photos[-1].get("file_unique_id") or uuid.uuid4().hex[:10], "photo")
                file_id, name, mime = photos[-1]["file_id"], "photo-%s.jpg" % uid, "image/jpeg"
            else:
                continue
            data, _tg_path = self.tg.download(file_id, self.cfg.max_download_bytes)
            text = decode_text(data, name, mime) if len(data) <= self.cfg.max_inline_bytes else None
            if text is not None:
                parts.append("[Вложение: %s]\n%s" % (name, fenced(text)))
                continue
            path = self.save_upload(group, name, data)
            kind = "изображение" if mime.startswith("image/") else "файл"
            parts.append("[Вложение (%s) сохранено: %s, %d байт. Открой его инструментом Read, если нужно.]" %
                         (kind, path, len(data)))
        if not parts:
            return None
        if len(parts) > 1:
            parts.insert(0, "[Получено вложений: %d]" % len(parts))
        head = caption.strip()
        return (head + "\n\n" if head else "") + "\n\n".join(parts)

    # ---- albums: several photos sent at once arrive as separate messages with one media_group_id ----
    def queue_album(self, msg):
        key = (msg["chat"]["id"], msg.get("message_thread_id"), msg["media_group_id"])
        with self.album_lock:
            entry = self.albums.setdefault(key, {"msgs": [], "timer": None, "first": time.time()})
            entry["msgs"].append(msg)
            if entry["timer"]:
                entry["timer"].cancel()
            wait = 0 if time.time() - entry["first"] > self.cfg.album_max_wait else self.cfg.album_wait
            entry["timer"] = threading.Timer(wait, self.flush_album, args=(key,))
            entry["timer"].daemon = True
            entry["timer"].start()

    def flush_album(self, key):
        with self.album_lock:
            entry = self.albums.pop(key, None)
        if not entry:
            return
        msgs = sorted(entry["msgs"], key=lambda m: m.get("message_id", 0))
        first = dict(msgs[0])
        first["_album"] = msgs
        first["caption"] = next((m["caption"] for m in msgs if m.get("caption")), "")
        self.safe_handle(first)

    # ---- telegram updates -------------------------------------------------------
    def reply(self, msg, text):
        self.tg.send(msg["chat"]["id"], text, msg.get("message_thread_id"))

    def handle(self, msg):
        user, chat = msg.get("from") or {}, msg["chat"]
        has_file = bool(msg.get("document") or msg.get("photo") or msg.get("_album"))
        text = (msg.get("text") or msg.get("caption") or "").strip()
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") and not has_file else ""
        if user.get("is_bot") or not (text or has_file):
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
        if has_file and msg.get("media_group_id") and "_album" not in msg:
            return self.queue_album(msg)              # wait for the other photos of the album
        if has_file:                                  # attachment: becomes (part of) the prompt
            try:
                text = self.attachment_prompt(group, msg.get("_album") or [msg], text)
            except Exception as e:
                log.warning("attachment: %s", e)
                return self.reply(msg, "⚠️ Не удалось получить вложение: %s" % short(str(e), 200))
            if not text:
                return
            cmd = ""
        args = text.split()[1:]
        slash = False
        if text.startswith("//"):                     # "//help" sends "/help" to Claude even if the bot has its own
            text, cmd, slash = text[1:], "", True
        elif cmd and cmd not in OWN_COMMANDS:         # a Claude command such as /usage, /context, /compact
            cmd, slash = "", True
        if cmd == "/help":
            self.reply(msg, "В теме: текст продолжает сессию.\n/mode full|safe — права Claude\n/stop — прервать запуск\n"
                            "/tools quote|spoiler|plain|off — как показывать вызовы инструментов (🔧)\n"
                            "/model [имя|default] — модель темы (sonnet, opus, opusplan, fable, haiku). В General: модель новых сессий\n"
                            "В General: текст начинает новую сессию.\n/sessions — недавние сессии\n/open <id> — создать тему для сессии\n/resync — восстановить удалённые темы\n"
                            "Остальные команды с / (/usage, /context, /cost, /model, /compact ...) уходят в Claude. "
                            "//команда отправляет её в Claude, даже если такая есть у бота.")
        elif cmd == "/sessions":
            self.cmd_sessions(group, msg)
        elif cmd == "/open":
            self.cmd_open(group, msg, args)
        elif cmd == "/model":
            self.cmd_model(group, msg, thread, args)
        elif cmd == "/tools":
            self.cmd_tools(group, msg, thread, args)
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
                st = {"thread": thread, "pos": 0, "title": short(made, 100), "mode": self.cfg.default_mode, "events": 0,
                      "model": self.state.default_model(group.name)}
                with self.state.lock:
                    self.state.topics(group.name)[sid] = st
                self.state.save()
                log.info("new session bound to a manual topic group=%s session=%s", group.name, sid[:8])
                self.run_claude(group, sid, st, text, new=True, slash=slash)
        elif slash:
            self.reply(msg, "Команды Claude (%s) выполняются в теме сессии: откройте тему и отправьте её там." % text.split()[0])
        else:                                         # General: a new session
            sid = str(uuid.uuid4())
            title = short(msg.get("caption") or (msg.get("document") or {}).get("file_name") or text, 60)
            st = self.create_topic(group, sid, title, 0)
            self.run_claude(group, sid, st, text, new=True)

    def cmd_tools(self, group, msg, thread, args):
        """/tools [quote|spoiler|plain|off]: how tool calls are shown in this topic."""
        sid = self.find_sid(group, thread) if thread else None
        st = self.state.topics(group.name).get(sid) if sid else None
        if not st:
            return self.reply(msg, "Команда работает внутри темы сессии.")
        if args and args[0] in TOOL_MODES:
            st["tools"] = args[0]
            self.state.save()
        elif args:
            return self.reply(msg, "Режимы: quote (сворачиваемая цитата), spoiler (скрыто, раскрывается касанием), plain, off.")
        self.reply(msg, "Вызовы инструментов в этой теме: %s." % (st.get("tools") or self.cfg.tool_display) +
                   ("" if args else "\n/tools quote|spoiler|plain|off"))

    def cmd_model(self, group, msg, thread, args):
        """/model [name|default]: model of this topic; in General: default model of new sessions of the group."""
        name = args[0] if args else None
        if name and name not in ("default", "reset") and not MODEL_RE.match(name):
            return self.reply(msg, "Недопустимое имя модели. Примеры: sonnet, opus, opusplan, fable, haiku или полный id модели.")
        clear = name in ("default", "reset")
        if thread:
            sid = self.find_sid(group, thread)
            st = self.state.topics(group.name).get(sid) if sid else None
            if not st:
                return
            if name:
                st["model"] = None if clear else name
                self.state.save()
            self.reply(msg, "Модель темы: %s." % (st.get("model") or "по умолчанию аккаунта") +
                       ("" if name else "\n/model <sonnet|opus|opusplan|fable|haiku|default> — сменить."))
        else:
            if name:
                self.state.set_default_model(group.name, None if clear else name)
            self.reply(msg, "Модель новых сессий группы: %s. Уже существующие темы не меняются." %
                       (self.state.default_model(group.name) or "по умолчанию аккаунта") +
                       ("" if name else "\n/model <имя> — сменить для новых сессий."))

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
