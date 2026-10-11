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
        self.deleted = set()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                method = self.path.rsplit("/", 1)[-1]
                params = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                if params.get("message_thread_id") in fake.deleted and method != "createForumTopic":
                    body = json.dumps({"ok": False, "error_code": 400,
                                       "description": "Bad Request: message thread not found"}).encode()
                else:
                    body = json.dumps({"ok": True, "result": fake.handle(method, params)}).encode()
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
        if method == "createForumTopic":
            self.next_thread += 1
            return {"message_thread_id": self.next_thread, "name": params["name"]}
        return True

    def say(self, user, text, thread=None, chat=CHAT, chat_type="supergroup"):
        msg = {"message_id": self.next_update, "from": {"id": user, "is_bot": False}, "text": text,
               "chat": {"id": chat, "type": chat_type}}
        if thread:
            msg["message_thread_id"] = thread
        self.updates.put({"update_id": self.next_update, "message": msg})
        self.next_update += 1

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
            [[groups]]
            name = "g"
            chat_id = %d
            config_dir = "%s"
            project_dir = "%s"
        ''' % (self.tmp, list(allowed), self.tmp, self.claude, self.tg.url, CHAT, self.cfgdir, self.proj)))
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

    def test_chunks(self):
        self.assertEqual(bot.chunks("short"), ["short"])
        parts = bot.chunks("line\n" * 2000)
        self.assertTrue(all(len(p) <= bot.MAX_CHUNK + 20 for p in parts))
        self.assertLessEqual(len(parts), bot.MAX_CHUNKS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
