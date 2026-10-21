"""Offline tests: a fake Telegram Bot API and a fake `claude` that writes session files like the real one."""
import http.server
import json
import os
import queue
import socketserver
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bot  # noqa: E402

TOKEN = "123:TEST"
USER, OTHER, CHAT = 111, 222, -1001


class FakeTelegram:
    def __init__(self):
        self.calls, self.updates, self.next_thread, self.next_update = [], queue.Queue(), 100, 1
        self.deleted, self.reject_html, self.files = set(), False, {}
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):                                   # file download: /file/bot<token>/docs/<file_id>
                data = fake.files.get(self.path.rsplit("/", 1)[-1])
                self.send_response(200 if data is not None else 404)
                self.end_headers()
                if data is not None:
                    self.wfile.write(data)

            def do_POST(self):
                method = self.path.rsplit("/", 1)[-1]
                params = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                if fake.reject_html and params.get("parse_mode") == "HTML":
                    body = json.dumps({"ok": False, "error_code": 400,
                                       "description": "Bad Request: can't parse entities: unsupported start tag"}).encode()
                elif params.get("message_thread_id") in fake.deleted and method != "createForumTopic":
                    body = json.dumps({"ok": False, "error_code": 400,
                                       "description": "Bad Request: message thread not found"}).encode()
                else:
                    try:
                        body = json.dumps({"ok": True, "result": fake.handle(method, params)}).encode()
                    except KeyError:
                        body = json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: wrong file_id"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.server = S(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def handle(self, method, params):
        if method == "getUpdates":
            out = []
            try:
                out.append(self.updates.get(timeout=0.5))
                while True:
                    out.append(self.updates.get_nowait())
            except queue.Empty:
                pass
            return out
        self.calls.append((method, params))
        if method == "getFile":
            data = self.files.get(params["file_id"])
            if data is None:
                raise KeyError("no such file")
            return {"file_id": params["file_id"], "file_size": len(data), "file_path": "docs/" + params["file_id"]}
        if method == "createForumTopic":
            self.next_thread += 1
            return {"message_thread_id": self.next_thread, "name": params["name"]}
        return True

    def say(self, user, text, thread=None, chat=CHAT, chat_type="supergroup", document=None, photo=None, album=None):
        msg = {"message_id": self.next_update, "from": {"id": user, "is_bot": False},
               "chat": {"id": chat, "type": chat_type}}
        if album:
            msg["media_group_id"] = album
        if document or photo:
            msg["caption"] = text
            msg["document" if document else "photo"] = document or photo
        else:
            msg["text"] = text
        if thread:
            msg["message_thread_id"] = thread
        self.updates.put({"update_id": self.next_update, "message": msg})
        self.next_update += 1

    def messages(self, thread=None):
        return [p for m, p in self.calls if m == "sendMessage" and p.get("message_thread_id") == thread]

    def sent(self, thread=None):
        return [p["text"] for m, p in self.calls if m == "sendMessage" and p.get("message_thread_id") == thread]

    def topics(self):
        return [p["name"] for m, p in self.calls if m == "createForumTopic"]


FAKE_CLAUDE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, re, sys
    args = sys.argv[1:]
    sid = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1]
    prompt = sys.stdin.read()
    key = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(os.getcwd()))
    d = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", key)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, sid + ".jsonl"), "a") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt + "\\n"}}) + "\\n")
        f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "echo: " + prompt.strip()}]}}) + "\\n")
    with open(os.environ["FAKE_ARGS_LOG"], "a") as f:
        f.write(json.dumps(args) + "\\n")
    with open(os.environ["FAKE_ARGS_LOG"] + ".prompts", "a") as f:
        f.write(json.dumps(prompt.strip()) + "\\n")
    print(json.dumps({"type": "result", "is_error": False, "result": "echo: " + prompt.strip(), "session_id": sid}))
''')


def jl(**kw):
    return json.dumps(kw) + "\n"


class BotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tg = FakeTelegram()
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.proj)
        self.cfgdir = os.path.join(self.tmp, "cfg")
        key = bot.project_key(self.proj)
        self.tdir = os.path.join(self.cfgdir, "projects", key)
        os.makedirs(self.tdir)
        self.claude = os.path.join(self.tmp, "claude")
        with open(self.claude, "w") as f:
            f.write(FAKE_CLAUDE)
        os.chmod(self.claude, os.stat(self.claude).st_mode | stat.S_IEXEC)
        self.args_log = os.path.join(self.tmp, "args.log")
        os.environ["FAKE_ARGS_LOG"] = self.args_log
        with open(os.path.join(self.tmp, "token"), "w") as f:
            f.write(TOKEN)
        self.old = os.path.join(self.tdir, "aaaaaaaa-0000-0000-0000-000000000001.jsonl")
        with open(self.old, "w") as f:
            f.write(jl(type="user", message={"role": "user", "content": "old question"}))
            f.write(jl(type="assistant", message={"role": "assistant", "content": [{"type": "text", "text": "old answer"}]}))
            f.write(jl(type="ai-title", aiTitle="Old topic"))
        self.threads = []

    def start(self, allowed=(USER,)):
        cfg = os.path.join(self.tmp, "config.toml")
        with open(cfg, "w") as f:
            f.write(textwrap.dedent('''\
            token_file = "%s/token"
            allowed_user_ids = %s
            state_file = "%s/state.json"
            claude_bin = "%s"
            api_base = "%s"
            recent_days = 7
            uploads_dir = "%s/uploads"
            max_inline_bytes = 500
            album_wait_seconds = 0.6
            [[groups]]
            name = "g"
            chat_id = %d
            config_dir = "%s"
            project_dir = "%s"
        ''' % (self.tmp, list(allowed), self.tmp, self.claude, self.tg.url, self.tmp, CHAT, self.cfgdir, self.proj)))
        self.bot = bot.Bot(bot.Config(cfg))
        self.bot.mirror_loop_interval = 0.2
        for target in (self.bot.poll_loop, self.bot.mirror_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self.threads.append(t)

    def tearDown(self):
        self.bot.stop.set()

    def wait(self, cond, what, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.1)
        self.fail("timeout waiting for: " + what + "\ncalls: " + json.dumps(self.tg.calls, ensure_ascii=False)[:1500])

    def args_lines(self):
        with open(self.args_log) as f:
            return f.read().splitlines()

    def last_args(self):
        with open(self.args_log) as f:
            return json.loads(f.read().splitlines()[-1])

    def thread_of(self, title_part):
        for m, p in self.tg.calls:
            if m == "createForumTopic" and title_part in p["name"]:
                return self.tg.calls and [x for x in self.bot.state.topics("g").values() if x["title"] == p["name"]][0]["thread"]

    # ---------------------------------------------------------------------------
    def test_topic_for_recent_session_without_history_replay(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        self.assertEqual(self.tg.topics(), ["Old topic"])
        thread = self.thread_of("Old topic")
        time.sleep(1)
        self.assertFalse(any("old answer" in t for t in self.tg.sent(thread)), "history must not be replayed")
        with open(self.old, "a") as f:                  # activity typed in tmux
            f.write(jl(type="user", message={"role": "user", "content": "from tmux"}))
            f.write(jl(type="assistant", message={"role": "assistant", "content": [
                {"type": "text", "text": "tmux answer"}, {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}]}))
            f.write(jl(type="user", message={"role": "user", "content": [{"type": "tool_result", "content": "x"}]}))
        self.wait(lambda: any("tmux answer" in t for t in self.tg.sent(thread)), "mirrored answer")
        text = "\n".join(self.tg.sent(thread))
        self.assertIn("👤 from tmux", text)
        self.assertIn("🤖 tmux answer", text)
        self.assertIn("🔧 Bash: ls -la", text)
        self.assertNotIn("tool_result", text)

    def test_message_in_topic_resumes_session_and_answer_is_mirrored_once(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        self.tg.say(USER, "hello claude", thread=thread)
        self.wait(lambda: any("echo: hello claude" in t for t in self.tg.sent(thread)), "answer mirrored")
        time.sleep(1.5)
        texts = self.tg.sent(thread)
        self.assertEqual(sum("echo: hello claude" in t for t in texts), 1, texts)
        self.assertFalse(any("👤 hello claude" in t for t in texts), "own prompt must not be echoed")
        args = self.last_args()
        self.assertIn("--resume", args)
        self.assertIn("aaaaaaaa-0000-0000-0000-000000000001", args)
        self.assertIn("--dangerously-skip-permissions", args)

    def test_general_message_starts_new_session_and_topic(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "initial topic")
        self.tg.say(USER, "start a brand new thing")
        self.wait(lambda: "start a brand new thing" in self.tg.topics(), "new topic")
        thread = [p for m, p in self.tg.calls if m == "createForumTopic"][-1]
        new = [x for x in self.bot.state.topics("g").values() if x["title"] == "start a brand new thing"][0]["thread"]
        self.wait(lambda: any("echo: start a brand new thing" in t for t in self.tg.sent(new)), "answer in new topic")
        args = self.last_args()
        self.assertIn("--session-id", args)

    def test_message_in_manual_topic_starts_session_bound_to_it(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "initial topic")
        before = len(self.tg.topics())
        self.tg.say(USER, "hello from a topic I made", thread=777)   # thread 777 was created by hand
        self.wait(lambda: any("echo: hello from a topic I made" in t for t in self.tg.sent(777)), "answer in the same topic")
        self.assertEqual(len(self.tg.topics()), before, "no extra topic must be created")
        self.assertIn("--session-id", self.last_args())
        bound = [sid for sid, st in self.bot.state.topics("g").items() if st["thread"] == 777]
        self.assertEqual(len(bound), 1)
        self.tg.say(USER, "and a follow-up", thread=777)             # now it is an ordinary topic: --resume
        self.wait(lambda: any("echo: and a follow-up" in t for t in self.tg.sent(777)), "follow-up answer")
        self.assertIn("--resume", self.last_args())
        self.assertIn(bound[0], self.last_args())

    def test_general_topic_id_one_is_not_a_session_topic(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "initial topic")
        self.tg.say(USER, "text in General sent with thread id 1", thread=1)
        self.wait(lambda: "text in General sent with thread id 1" in self.tg.topics(), "new topic from General")
        self.assertFalse([1 for st in self.bot.state.topics("g").values() if st["thread"] == 1])

    def prompts(self):
        with open(self.args_log + ".prompts") as f:
            return [json.loads(line) for line in f.read().splitlines()]

    def test_claude_slash_commands_are_forwarded(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        self.tg.say(USER, "/usage", thread=thread)
        self.wait(lambda: os.path.exists(self.args_log + ".prompts"), "claude run for /usage")
        self.assertEqual(self.prompts()[-1], "/usage")
        self.tg.say(USER, "//help", thread=thread)                     # the bot has its own /help: escape with //
        self.wait(lambda: len(self.prompts()) >= 2, "escaped command")
        self.assertEqual(self.prompts()[-1], "/help")
        self.tg.say(USER, "/mode", thread=thread)                      # a bot command is never forwarded
        self.wait(lambda: any("Режим:" in t for t in self.tg.sent(thread)), "bot answers /mode")
        time.sleep(1)
        self.assertEqual(len(self.prompts()), 2)

    def test_model_per_topic_and_group_default(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        self.tg.say(USER, "/model", thread=thread)
        self.wait(lambda: any("по умолчанию аккаунта" in t for t in self.tg.sent(thread)), "shows the default")
        self.tg.say(USER, "/model opus", thread=thread)
        self.wait(lambda: any("Модель темы: opus" in t for t in self.tg.sent(thread)), "model set")
        self.tg.say(USER, "work on it", thread=thread)
        self.wait(lambda: os.path.exists(self.args_log), "claude run")
        args = self.last_args()
        self.assertEqual(args[args.index("--model") + 1], "opus")
        self.tg.say(USER, "/model --evil", thread=thread)                 # not a model name: never reaches the argv
        self.wait(lambda: any("Недопустимое" in t for t in self.tg.sent(thread)), "rejected")
        self.tg.say(USER, "/model default", thread=thread)
        self.wait(lambda: sum("по умолчанию аккаунта" in t for t in self.tg.sent(thread)) >= 2, "cleared")
        self.tg.say(USER, "again", thread=thread)
        self.wait(lambda: len(self.args_lines()) >= 2, "second run")
        self.assertNotIn("--model", self.last_args())
        # General: default model for new sessions
        self.tg.say(USER, "/model fable")
        self.wait(lambda: any("Модель новых сессий группы: fable" in t for t in self.tg.sent(None)), "group default set")
        self.tg.say(USER, "brand new session")
        self.wait(lambda: any("--session-id" in line for line in self.args_lines()), "new session run")
        args = self.last_args()
        self.assertEqual(args[args.index("--model") + 1], "fable")
        new = [st for st in self.bot.state.topics("g").values() if st["title"] == "brand new session"][0]
        self.assertEqual(new["model"], "fable")

    def tool_activity(self, command="ls -la"):
        with open(self.old, "a") as f:
            f.write(jl(type="assistant", message={"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": command}},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/etc/hosts"}}]}))

    def test_tool_calls_are_collapsible_and_escaped(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        self.tool_activity("echo '<b>&</b>' > /tmp/x")
        self.wait(lambda: any("<blockquote expandable>" in m["text"] for m in self.tg.messages(thread)), "tool block")
        msg = [m for m in self.tg.messages(thread) if "<blockquote expandable>" in m["text"]][0]
        self.assertEqual(msg["parse_mode"], "HTML")
        self.assertIn("<blockquote expandable>", msg["text"])
        self.assertIn("&lt;b&gt;&amp;&lt;/b&gt;", msg["text"])            # command text is escaped
        self.assertNotIn("<b>", msg["text"])
        self.assertNotIn("Инструменты", msg["text"])                     # no header line, only the calls
        self.assertTrue(msg["text"].startswith("<blockquote expandable>🔧 Bash"))
        self.assertIn("🔧 Read: /etc/hosts", msg["text"])                # both calls in one block

    def test_assistant_markdown_is_formatted_in_the_topic(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        with open(self.old, "a") as f:
            f.write(jl(type="assistant", message={"role": "assistant", "content": [
                {"type": "text", "text": "Порт **`13316`** — *жив*.\n```sh\nss -lnt\n```"}]}))
        self.wait(lambda: any("<code>13316</code>" in m["text"] for m in self.tg.messages(thread)), "formatted message")
        msg = [m for m in self.tg.messages(thread) if "<code>13316</code>" in m["text"]][0]
        self.assertEqual(msg["parse_mode"], "HTML")
        self.assertIn("<i>жив</i>", msg["text"])
        self.assertIn('<pre><code class="language-sh">ss -lnt</code></pre>', msg["text"])
        self.assertNotIn("**", msg["text"])

    def test_tools_command_changes_display(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        self.tg.say(USER, "/tools spoiler", thread=thread)
        self.wait(lambda: any("в этой теме: spoiler" in t for t in self.tg.sent(thread)), "spoiler set")
        self.tool_activity()
        self.wait(lambda: any("<tg-spoiler>" in m["text"] for m in self.tg.messages(thread)), "spoiler block")
        self.tg.say(USER, "/tools off", thread=thread)
        self.wait(lambda: any("в этой теме: off" in t for t in self.tg.sent(thread)), "off set")
        before = len(self.tg.messages(thread))
        self.tool_activity("echo hidden-from-telegram")
        with open(self.old, "a") as f:
            f.write(jl(type="assistant", message={"role": "assistant", "content": [{"type": "text", "text": "after off"}]}))
        self.wait(lambda: any("after off" in m["text"] for m in self.tg.messages(thread)), "text still mirrored")
        self.assertFalse(any("hidden-from-telegram" in m["text"] for m in self.tg.messages(thread)[before:]))
        self.tg.say(USER, "/tools nonsense", thread=thread)
        self.wait(lambda: any("Режимы:" in t for t in self.tg.sent(thread)), "bad mode rejected")

    def test_html_rejected_falls_back_to_plain_text(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        self.tg.reject_html = True
        self.tool_activity()
        self.wait(lambda: any("🔧 Bash: ls -la" in m["text"] and "parse_mode" not in m for m in self.tg.messages(thread)),
                  "plain fallback")
        self.assertFalse(any("<blockquote" in m["text"] for m in self.tg.messages(thread)[-3:]))

    def test_text_attachment_goes_into_the_prompt(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        self.tg.files["f1"] = "первая строка\nвторая ``` строка\n".encode()
        self.tg.say(USER, "посмотри файл", thread=thread,
                    document={"file_id": "f1", "file_name": "notes.txt", "mime_type": "text/plain"})
        self.wait(lambda: os.path.exists(self.args_log + ".prompts"), "claude run")
        prompt = self.prompts()[-1]
        self.assertIn("посмотри файл", prompt)
        self.assertIn("[Вложение: notes.txt]", prompt)
        self.assertIn("первая строка", prompt)
        self.assertIn("````", prompt)                                   # fence longer than the backticks in the file
        self.assertIn("--resume", self.last_args())
        self.assertIn("--add-dir", self.last_args())

    def test_binary_and_large_attachments_are_saved_not_inlined(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        self.tg.files["f2"] = b"\x00\x01binary" + b"x" * 50
        self.tg.say(USER, "", thread=thread, document={"file_id": "f2", "file_name": "../../evil name.bin"})
        self.wait(lambda: os.path.exists(self.args_log + ".prompts"), "claude run")
        prompt = self.prompts()[-1]
        path = prompt.split("сохранено: ")[1].split(",")[0]
        uploads = os.path.realpath(os.path.join(self.tmp, "uploads"))
        self.assertTrue(os.path.realpath(path).startswith(uploads + os.sep), path)   # never outside the uploads dir
        self.assertNotIn("..", os.path.relpath(path, uploads))
        self.assertTrue(path.endswith("evil_name.bin"))
        with open(path, "rb") as f:
            self.assertTrue(f.read().startswith(b"\x00\x01binary"))
        args = self.last_args()
        self.assertEqual(args[args.index("--add-dir") + 1], os.path.join(self.tmp, "uploads"))
        self.tg.files["f3"] = ("long text " * 100).encode()                          # text, but above max_inline_bytes
        self.tg.say(USER, "big", thread=thread, document={"file_id": "f3", "file_name": "big.txt", "mime_type": "text/plain"})
        self.wait(lambda: len(self.prompts()) >= 2, "second run")
        self.assertIn("сохранено:", self.prompts()[-1])
        self.assertNotIn("long text long text", self.prompts()[-1])

    def test_photo_is_saved_and_largest_size_used(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        self.tg.files["small"], self.tg.files["large"] = b"\xff\xd8small", b"\xff\xd8large-image"
        self.tg.say(USER, "что на фото", thread=thread, photo=[{"file_id": "small"}, {"file_id": "large"}])
        self.wait(lambda: os.path.exists(self.args_log + ".prompts"), "claude run")
        prompt = self.prompts()[-1]
        self.assertIn("что на фото", prompt)
        self.assertIn("изображение", prompt)
        path = prompt.split("сохранено: ")[1].split(",")[0]
        self.assertTrue(path.endswith(".jpg"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"\xff\xd8large-image")

    def test_attachment_in_general_starts_a_new_session(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "initial topic")
        self.tg.files["f4"] = b"log line"
        self.tg.say(USER, "", document={"file_id": "f4", "file_name": "app.log", "mime_type": "text/plain"})
        self.wait(lambda: "app.log" in self.tg.topics(), "topic named after the file")
        self.wait(lambda: os.path.exists(self.args_log), "claude run")
        self.assertIn("--session-id", self.last_args())
        self.assertIn("log line", self.prompts()[-1])

    def uploaded(self):
        found = []
        for root, _dirs, files in os.walk(os.path.join(self.tmp, "uploads")):
            found += [os.path.join(root, f) for f in files]
        return sorted(found)

    def test_files_sent_in_the_same_second_never_overwrite_each_other(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        for i in range(4):                                             # same file name, same second, different content
            self.tg.files["same%d" % i] = b"\x00data-%d" % i + b"x" * 30
            self.tg.say(USER, "doc %d" % i, thread=thread, document={"file_id": "same%d" % i, "file_name": "report.bin"})
        for i in range(3):                                             # photos without file_unique_id
            self.tg.files["ph%d" % i] = b"\xff\xd8photo-%d" % i
            self.tg.say(USER, "photo %d" % i, thread=thread, photo=[{"file_id": "ph%d" % i}])
        self.wait(lambda: os.path.exists(self.args_log + ".prompts") and len(self.prompts()) >= 7, "seven runs", timeout=40)
        files = self.uploaded()
        self.assertEqual(len(files), 7, files)                         # nothing was overwritten
        contents = sorted(open(f, "rb").read() for f in files)
        self.assertEqual(len(set(contents)), 7)
        for i in range(4):
            self.assertIn(b"\x00data-%d" % i + b"x" * 30, contents)

    def test_album_of_photos_is_one_prompt_with_distinct_files(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        for i in range(3):
            self.tg.files["al%d" % i] = b"\xff\xd8album-photo-%d" % i
            self.tg.say(USER, "сравни эти фото" if i == 0 else "", thread=thread,
                        photo=[{"file_id": "al%d" % i, "file_unique_id": "u%d" % i}], album="grp-1")
        self.wait(lambda: os.path.exists(self.args_log + ".prompts"), "one run for the album")
        time.sleep(2.5)                                                # no second run may follow
        prompts = self.prompts()
        self.assertEqual(len(prompts), 1, prompts)
        self.assertIn("сравни эти фото", prompts[0])
        self.assertIn("Получено вложений: 3", prompts[0])
        paths = [p.split("сохранено: ")[1].split(",")[0] for p in prompts[0].split("\n\n") if "сохранено:" in p]
        self.assertEqual(len(paths), 3)
        self.assertEqual(len(set(paths)), 3)
        self.assertEqual(sorted(open(p, "rb").read() for p in paths),
                         [b"\xff\xd8album-photo-0", b"\xff\xd8album-photo-1", b"\xff\xd8album-photo-2"])

    def test_album_from_a_stranger_is_ignored(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        self.tg.files["x1"] = b"\xff\xd8x"
        self.tg.say(OTHER, "hi", thread=thread, photo=[{"file_id": "x1"}], album="grp-2")
        time.sleep(2)
        self.assertFalse([1 for m, p in self.tg.calls if m == "getFile"])
        self.assertFalse(os.path.exists(self.args_log + ".prompts"))

    def test_attachment_from_a_stranger_is_ignored_and_failures_are_reported(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        self.tg.files["f5"] = b"secret"
        self.tg.say(OTHER, "hi", thread=thread, document={"file_id": "f5", "file_name": "a.txt", "mime_type": "text/plain"})
        time.sleep(2)
        self.assertFalse([1 for m, p in self.tg.calls if m == "getFile"], "a stranger's file must not even be downloaded")
        self.assertFalse(os.path.exists(self.args_log + ".prompts"))
        self.tg.say(USER, "", thread=thread, document={"file_id": "missing", "file_name": "x.txt"})
        self.wait(lambda: any("Не удалось получить вложение" in t for t in self.tg.sent(thread)), "error reported")

    def test_claude_command_in_general_gets_a_hint(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        self.tg.say(USER, "/usage")
        self.wait(lambda: any("выполняются в теме" in t for t in self.tg.sent(None)), "hint")
        time.sleep(1)
        self.assertFalse(os.path.exists(self.args_log + ".prompts"), "no claude run from General")

    def test_mode_safe_changes_flags(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        self.tg.say(USER, "/mode safe", thread=thread)
        self.wait(lambda: any("Режим: safe" in t for t in self.tg.sent(thread)), "mode reply")
        self.tg.say(USER, "read only please", thread=thread)
        self.wait(lambda: os.path.exists(self.args_log), "claude run")
        args = self.last_args()
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertIn("--allowedTools", args)

    def test_resync_recreates_deleted_topic(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        self.tg.deleted.add(thread)                       # the user deleted the topic in Telegram
        self.tg.say(USER, "/resync")                      # General topic
        self.wait(lambda: len(self.tg.topics()) == 2, "topic recreated")
        new = self.bot.state.topics("g")["aaaaaaaa-0000-0000-0000-000000000001"]["thread"]
        self.assertNotEqual(new, thread)
        self.wait(lambda: any("восстановлено: 1" in t for t in self.tg.sent(None)), "summary reply")
        self.wait(lambda: any("Тема восстановлена" in t for t in self.tg.sent(new)), "notice in new topic")

    def test_other_users_are_ignored(self):
        self.start()
        self.wait(lambda: self.tg.topics(), "topic created")
        thread = self.thread_of("Old topic")
        before = len(self.tg.calls)
        self.tg.say(OTHER, "rm -rf /", thread=thread)
        self.tg.say(OTHER, "/whoami", thread=thread)
        time.sleep(2)
        self.assertFalse(os.path.exists(self.args_log), "claude must not run for a stranger")
        self.assertFalse([c for c in self.tg.calls[before:] if "rm -rf" in json.dumps(c)])
        self.assertFalse(any("user id" in t for t in self.tg.sent(thread)))

    def test_setup_mode_only_answers_whoami(self):
        self.start(allowed=())
        self.tg.say(OTHER, "/whoami", chat=555, chat_type="private")
        self.wait(lambda: any(m == "sendMessage" for m, _ in self.tg.calls), "whoami reply")
        self.assertIn("user id: %d" % OTHER, self.tg.calls[-1][1]["text"])
        self.tg.say(OTHER, "do something")
        time.sleep(1.5)
        self.assertFalse(self.tg.topics(), "setup mode must not mirror anything")
        self.assertFalse(os.path.exists(self.args_log))


class ParseTest(unittest.TestCase):
    def test_parse_lines(self):
        j = lambda **k: json.dumps(k)
        self.assertEqual(bot.parse_line(j(type="user", message={"content": "hi\n"})), [("user", "hi")])
        self.assertEqual(bot.parse_line(j(type="user", message={"content": "<system-reminder>x"})), [])
        self.assertEqual(bot.parse_line(j(type="user", isMeta=True, message={"content": "meta"})), [])
        self.assertEqual(bot.parse_line(j(type="user", message={"content": [{"type": "tool_result"}]})), [])
        self.assertEqual(bot.parse_line(j(type="assistant", isSidechain=True, message={"content": [{"type": "text", "text": "x"}]})), [])
        self.assertEqual(bot.parse_line(j(type="assistant", message={"content": [{"type": "thinking", "thinking": "..."}]})), [])
        self.assertEqual(bot.parse_line(j(type="ai-title", aiTitle="T")), [("title", "T")])
        self.assertEqual(bot.parse_line("not json"), [])

    def md(self, text):
        return "\n\n".join(bot.render([("md", text)], "off"))

    def test_markdown_inline(self):
        self.assertEqual(self.md("Туннель **`127.0.0.1:13316`**."), "🤖 Туннель <code>127.0.0.1:13316</code>.")
        self.assertEqual(self.md("**bold** *it* ~~del~~ `c`"), "🤖 <b>bold</b> <i>it</i> <s>del</s> <code>c</code>")
        self.assertEqual(self.md("***both***"), "🤖 <b><i>both</i></b>")
        self.assertEqual(self.md("**a `x` b**"), "🤖 <b>a </b><code>x</code><b> b</b>")      # no code inside bold
        for plain in ("snake_case_name", "2 * 3 * 4", "a_b_c and _x_y", "**unclosed"):
            self.assertNotIn("<i>", self.md(plain))
        self.assertIn("<i>x</i>", self.md("_x_"))

    def test_markdown_blocks(self):
        out = self.md("# Title\n- one **b**\n  - nested\n1. first\n\n> quote *q*\n\n---\ntext")
        self.assertIn("<b>Title</b>", out)
        self.assertIn("• one <b>b</b>", out)
        self.assertIn("  • nested", out)
        self.assertIn("1. first", out)
        self.assertIn("<blockquote>quote <i>q</i></blockquote>", out)
        self.assertIn("──────────", out)
        code = self.md("```python\nprint('<a>')\n```\nafter")
        self.assertIn('<pre><code class="language-python">print(\'&lt;a&gt;\')</code></pre>', code)
        self.assertTrue(code.rstrip().endswith("after"))
        table = self.md("| A | B |\n|---|---|\n| **x** | `y` |")
        self.assertIn("<pre><code>A  B\nx  y</code></pre>", table)

    def test_markdown_never_injects_html(self):
        out = self.md("<script>x</script> & [bad](javascript:alert(1)) [ok](https://e.com/?a=1&b=\"2\") **<b>**")
        self.assertNotIn("<script", out)
        self.assertNotIn('href="javascript', out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn('<a href="https://e.com/?a=1&amp;b=%222%22">ok</a>', out)
        self.assertEqual(out.count("<b>"), out.count("</b>"))
        self.assertEqual(bot.strip_tags(self.md("**a** `b` <x>")), "🤖 a b <x>")

    def test_markdown_long_answers_are_split_safely(self):
        long_code = "```\n" + "\n".join("line %d %s" % (i, "x" * 60) for i in range(400)) + "\n```"
        for text in (long_code, "word " * 4000, "- item\n" * 2000):
            messages = bot.render([("md", text)], "off")
            self.assertTrue(messages)
            self.assertTrue(all(len(m) <= bot.RENDER_LIMIT + 400 for m in messages), [len(m) for m in messages])
            for m in messages:                               # tags stay balanced inside every message
                self.assertEqual(m.count("<pre>") + m.count("<pre "), m.count("</pre>"))

    def test_render_limits_and_modes(self):
        segs = [("tool", "🔧 Bash: " + "x" * 300) for _ in range(40)] + [("text", "🤖 " + "a" * 9000)]
        for mode in ("quote", "spoiler", "plain"):
            messages = bot.render(segs, mode)
            self.assertTrue(all(len(m) <= bot.RENDER_LIMIT + 100 for m in messages), mode)
            self.assertLessEqual(len(messages), bot.MAX_CHUNKS)
        self.assertEqual(bot.render([("tool", "🔧 x")], "off"), [])
        self.assertEqual(bot.strip_tags("<blockquote expandable>a &lt;b&gt;</blockquote>"), "a <b>")

    def test_force_ipv4_resolves_only_ipv4(self):
        import socket
        original = socket.getaddrinfo
        try:
            bot.force_ipv4()
            infos = socket.getaddrinfo("localhost", 80, type=socket.SOCK_STREAM)
            self.assertTrue(infos)
            self.assertEqual({i[0] for i in infos}, {socket.AF_INET})
            infos = socket.getaddrinfo("localhost", 80, socket.AF_INET6, socket.SOCK_STREAM)   # even when IPv6 is asked for
            self.assertEqual({i[0] for i in infos}, {socket.AF_INET})
        finally:
            socket.getaddrinfo = original

    def test_chunks(self):
        self.assertEqual(bot.chunks("short"), ["short"])
        parts = bot.chunks("line\n" * 2000)
        self.assertTrue(all(len(p) <= bot.MAX_CHUNK + 20 for p in parts))
        self.assertLessEqual(len(parts), bot.MAX_CHUNKS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
