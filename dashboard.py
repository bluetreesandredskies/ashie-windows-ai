"""
Ashie dashboard - a local web UI for your assistant (standard library only, no extra installs).

  * Runs automatically inside ashie.py at http://127.0.0.1:8765 (see DASHBOARD_* in config.json)
  * Or on its own:   python dashboard.py            (reads/writes your real ~/Ashie data)
                     python dashboard.py --demo     (temporary sample data, great for a first look)
It only listens on your own computer (127.0.0.1); nothing is sent anywhere.
"""
import os, sys, json, time, tempfile, datetime, threading, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

if "--demo" in sys.argv and __name__ == "__main__":
    os.environ["ASHIE_HOME"] = tempfile.mkdtemp(prefix="ashie_demo_")
import store

HERE = Path(__file__).resolve().parent
INDEX = HERE / "dashboard" / "index.html"
HOOKS = {}          # ashie.py plugs in live memory/command handlers here


def _json(name, default):
    return store._read(name, default)


def memory_snapshot():
    if "memory" in HOOKS:
        return HOOKS["memory"]()
    mem, hist = _json("memory.json", {}), _json("history.json", [])
    return {"facts": mem.get("facts", []), "summary": mem.get("summary", ""), "profile": mem.get("profile", ""),
            "stats": mem.get("stats", {}), "samples": len(mem.get("samples", [])),
            "history": hist[-20:], "episodes": len(_json("episodes.json", {}).get("recs", []))}


def state():
    cfg = _json("config.json", {})
    return {"now": store._now().isoformat(), "status": store.get_status(), "memory": memory_snapshot(),
            "reminders": store.list_reminders(), "notes": store.list_notes(), "tasks": store.list_tasks(),
            "activity": store.activity(40), "user": cfg.get("user_name", "friend"),
            "assistant": cfg.get("assistant_name", "Ashie"), "demo": bool(os.environ.get("ASHIE_DEMO"))}


def act(name, d):
    """Every dashboard button lands here. Returns a JSON-able dict."""
    g = lambda k, default="": (d.get(k, default) if isinstance(d, dict) else default)
    if name == "parse":
        due = store.parse_when(g("text"))
        return {"due": due.isoformat() if due else None, "human": store.human_when(due) if due else None,
                "repeat": store.parse_repeat(g("text"))}
    if name == "reminder.add":
        text, when = g("text").strip(), g("when").strip()
        due = store.parse_when(when) if when else None
        if g("due"):
            try:
                due = datetime.datetime.fromisoformat(g("due"))
            except ValueError:
                pass
        if not text or not due:
            return {"error": "I need a reminder text and a time I can understand, e.g. 'in 20 minutes' or 'tomorrow at 9'."}
        return {"ok": store.add_reminder(text, due, g("repeat") or store.parse_repeat(when))}
    if name == "reminder.done":
        return {"ok": store.update_reminder(g("id"), done=True)}
    if name == "reminder.reopen":
        return {"ok": store.update_reminder(g("id"), done=False)}
    if name == "reminder.snooze":
        return {"ok": store.snooze_reminder(g("id"), int(g("minutes", 10)))}
    if name == "reminder.delete":
        return {"ok": store.delete_reminder(g("id"))}
    if name == "note.add":
        return {"ok": store.add_note(g("text"), source="dashboard")} if g("text").strip() else {"error": "Empty note"}
    if name == "note.pin":
        return {"ok": store.update_note(g("id"), pinned=bool(g("pinned")))}
    if name == "note.delete":
        return {"ok": store.delete_note(g("id"))}
    if name == "task.add":
        return {"ok": store.add_task(g("text"), source="dashboard")} if g("text").strip() else {"error": "Empty task"}
    if name == "task.toggle":
        return {"ok": store.update_task(g("id"), done=bool(g("done")))}
    if name == "task.delete":
        return {"ok": store.delete_task(g("id"))}
    if name == "fact.add":
        if "fact_add" in HOOKS:
            return {"ok": HOOKS["fact_add"](g("text"))}
        mem = _json("memory.json", {})
        mem.setdefault("facts", []).append(g("text").strip()[:200])
        store._write("memory.json", mem)
        store.log("memory", "Learned: " + g("text")[:80])
        return {"ok": True}
    if name == "fact.delete":
        if "fact_delete" in HOOKS:
            return {"ok": HOOKS["fact_delete"](g("text"))}
        mem = _json("memory.json", {})
        mem["facts"] = [f for f in mem.get("facts", []) if f != g("text")]
        store._write("memory.json", mem)
        return {"ok": True}
    if name == "command":
        if "command" not in HOOKS:
            return {"error": "Ashie isn't running, so she can't act on commands. Start ashie.py first."}
        HOOKS["command"](g("text"))
        store.log("system", "Typed command: " + g("text")[:80])
        return {"ok": True}
    return {"error": "unknown action"}


class Handler(BaseHTTPRequestHandler):
    server_version = "AshieDashboard"

    def log_message(self, *a):
        pass

    def _local(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]")     # blocks DNS-rebinding tricks

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._local():
            return self._send(403, {"error": "forbidden"})
        if self.path.split("?")[0] == "/api/state":
            return self._send(200, state())
        if self.path.split("?")[0] in ("/", "/index.html"):
            try:
                return self._send(200, INDEX.read_bytes(), "text/html")
            except OSError:
                return self._send(404, {"error": "dashboard/index.html is missing"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._local() or self.headers.get("X-Ashie") != "1":
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 100_000)) or b"{}")
            name = self.path.replace("/api/", "", 1)
            self._send(200, act(name, body))
        except Exception as e:
            self._send(400, {"error": str(e)})


def start(port=8765, hooks=None, background=True, open_browser=False):
    HOOKS.update(hooks or {})
    srv = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    url = f"http://127.0.0.1:{port}"
    if background:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    if not background:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return url


def seed_demo():
    now = store._now()
    H = datetime.timedelta
    store.add_reminder("Stand up and stretch", now + H(minutes=18), "none")
    store.add_reminder("Stand-up meeting", now.replace(hour=10, minute=0) + H(days=1), "weekdays")
    store.add_reminder("Call mom", now + H(hours=3), "none")
    store.add_reminder("Drink water", now + H(minutes=45), "daily")
    store.add_note("Idea: let Ashie read my unread emails every morning", pinned=True)
    store.add_note("Wi-Fi guest password is on the fridge")
    store.add_task("Finish the quarterly report")
    store.add_task("Buy printer ink")
    store.update_task(store.list_tasks()[1]["id"], done=True)
    store.log("skill", "weather: what's the weather like")
    store.log("memory", "Learned: Prefers short answers")
    store.log("system", "Dashboard started in demo mode")
    store._write("memory.json", {
        "facts": ["Loves building things with Python", "Lives in a cold city and drinks too much tea",
                  "Prefers short, casual answers", "Has a sister named Mira"],
        "summary": "Recently working on a side project and planning a trip next month.", "profile":
        "Talks casually, mixes in humour, asks for quick practical help while coding.",
        "stats": {"n": 64, "skills": {"web": 18, "timer": 11, "note": 9, "youtube": 8, "weather": 7, "chat": 6, "reminder": 5},
                  "hours": {"9": 8, "10": 11, "14": 6, "21": 17, "22": 14, "23": 8}}, "samples": ["a"] * 12})
    store._write("history.json", [
        {"t": now.isoformat(), "user": "remind me to stretch in 20 minutes", "assistant": "Okay, I'll remind you in 18 minutes: stretch."},
        {"t": now.isoformat(), "user": "remember that my sister's name is Mira", "assistant": "Got it, I'll remember that."}])
    store.set_status(state="listening", brain="Cloud brain", started=time.time() - 5400, model="demo", voice_lock=True,
                     last_heard="remember that my sister's name is Mira", last_reply="Got it, I'll remember that.", hotkey="F8")


if __name__ == "__main__":
    port = 8765
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    if "--demo" in sys.argv:
        os.environ["ASHIE_DEMO"] = "1"
        seed_demo()
        HOOKS["command"] = lambda t: store.log("skill", "demo command: " + t)

        def beat():
            while True:
                store.set_status(state="listening")
                time.sleep(3)
        threading.Thread(target=beat, daemon=True).start()
    print(f"Ashie dashboard: http://127.0.0.1:{port}   (Ctrl+C to stop)")
    start(port, background=False, open_browser="--no-browser" not in sys.argv)
