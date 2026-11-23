"""
Ashie shared storage (standard library only).

Reminders, notes, tasks, the activity feed and the live status all live as small JSON
files in the Ashie data folder (default ~/Ashie, override with the ASHIE_HOME env var).
Both the voice assistant (ashie.py) and the dashboard (dashboard.py) use this module, so
whatever you say to Ashie shows up on the dashboard and vice versa.
"""
import os, re, json, time, uuid, threading, datetime
from pathlib import Path

DATA = Path(os.environ.get("ASHIE_HOME") or (Path.home() / "Ashie"))
DATA.mkdir(parents=True, exist_ok=True)
_lock = threading.RLock()
_mtimes = {}

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
REPEATS = ("none", "daily", "weekdays", "weekly")


def _now():
    return datetime.datetime.now().replace(microsecond=0)


def _read(name, default):
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(name, obj):
    with _lock:
        tmp = DATA / (name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, DATA / name)


def _id():
    return uuid.uuid4().hex[:8]


# ------------------------------------------------------------------ activity + status
def log(kind, text):
    """kind: skill | reminder | memory | note | task | system"""
    with _lock:
        items = _read("activity.json", [])
        items.append({"t": _now().isoformat(), "kind": kind, "text": str(text)[:200]})
        _write("activity.json", items[-200:])


def activity(n=40):
    return list(reversed(_read("activity.json", [])[-n:]))


def set_status(**kw):
    with _lock:
        st = _read("status.json", {})
        st.update(kw)
        st["heartbeat"] = time.time()
        _write("status.json", st)


def get_status():
    st = _read("status.json", {})
    st["online"] = (time.time() - st.get("heartbeat", 0)) < 10
    return st


# ------------------------------------------------------------------ reminders
def add_reminder(text, due, repeat="none", kind="reminder"):
    if isinstance(due, str):
        due = datetime.datetime.fromisoformat(due)
    repeat = repeat if repeat in REPEATS else "none"
    r = {"id": _id(), "text": (text or "").strip()[:200] or "Reminder", "due": due.replace(microsecond=0).isoformat(),
         "repeat": repeat, "kind": kind, "done": False, "created": _now().isoformat()}
    with _lock:
        items = _read("reminders.json", [])
        items.append(r)
        _write("reminders.json", items)
    log("reminder", f"Set: {r['text']} ({r['due'].replace('T', ' ')})")
    return r


def list_reminders():
    items = _read("reminders.json", [])
    return sorted(items, key=lambda r: (r.get("done", False), r.get("due", "")))


def update_reminder(rid, **fields):
    with _lock:
        items = _read("reminders.json", [])
        for r in items:
            if r["id"] == rid:
                r.update({k: v for k, v in fields.items() if k in ("text", "due", "repeat", "done")})
                _write("reminders.json", items)
                return r
    return None


def delete_reminder(rid):
    with _lock:
        items = _read("reminders.json", [])
        keep = [r for r in items if r["id"] != rid]
        _write("reminders.json", keep)
        return len(keep) != len(items)


def snooze_reminder(rid, minutes=10):
    due = (_now() + datetime.timedelta(minutes=minutes)).isoformat()
    return update_reminder(rid, due=due, done=False)


def _advance(due, repeat):
    now = _now()
    step = datetime.timedelta(days=7 if repeat == "weekly" else 1)
    nxt = due + step
    while nxt <= now or (repeat == "weekdays" and nxt.weekday() >= 5):
        nxt += step
    return nxt


def due_reminders():
    now = _now()
    return [r for r in _read("reminders.json", [])
            if not r.get("done") and datetime.datetime.fromisoformat(r["due"]) <= now]


def mark_fired(rid):
    with _lock:
        items = _read("reminders.json", [])
        for r in items:
            if r["id"] == rid:
                if r.get("repeat", "none") != "none":
                    r["due"] = _advance(datetime.datetime.fromisoformat(r["due"]), r["repeat"]).isoformat()
                else:
                    r["done"] = True
                r["fired"] = _now().isoformat()
        _write("reminders.json", items)


# ------------------------------------------------------------------ notes
def _migrate_notes():
    old = DATA / "notes.txt"
    if (DATA / "notes.json").exists() or not old.exists():
        return
    out = []
    for line in old.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d)\]\s*(.*)", line)
        if m:
            out.append({"id": _id(), "text": m.group(2), "created": m.group(1).replace(" ", "T") + ":00",
                        "pinned": False, "source": "import"})
    _write("notes.json", out)


def list_notes():
    _migrate_notes()
    items = _read("notes.json", [])
    items.sort(key=lambda n: n.get("created", ""), reverse=True)          # newest first
    return sorted(items, key=lambda n: not n.get("pinned"))               # pinned on top (stable)


def add_note(text, pinned=False, source="voice"):
    _migrate_notes()
    n = {"id": _id(), "text": text.strip()[:1000], "created": _now().isoformat(), "pinned": pinned, "source": source}
    with _lock:
        items = _read("notes.json", [])
        items.append(n)
        _write("notes.json", items)
    log("note", n["text"][:80])
    return n


def update_note(nid, **fields):
    with _lock:
        items = _read("notes.json", [])
        for n in items:
            if n["id"] == nid:
                n.update({k: v for k, v in fields.items() if k in ("text", "pinned")})
                _write("notes.json", items)
                return n
    return None


def delete_note(nid):
    with _lock:
        items = _read("notes.json", [])
        keep = [n for n in items if n["id"] != nid]
        _write("notes.json", keep)
        return len(keep) != len(items)


# ------------------------------------------------------------------ tasks
def list_tasks():
    return _read("tasks.json", [])


def add_task(text, source="voice"):
    t = {"id": _id(), "text": text.strip()[:200], "done": False, "created": _now().isoformat(), "source": source}
    with _lock:
        items = _read("tasks.json", [])
        items.append(t)
        _write("tasks.json", items)
    log("task", "Added: " + t["text"])
    return t


def update_task(tid, **fields):
    with _lock:
        items = _read("tasks.json", [])
        for t in items:
            if t["id"] == tid:
                t.update({k: v for k, v in fields.items() if k in ("text", "done")})
                _write("tasks.json", items)
                return t
    return None


def delete_task(tid):
    with _lock:
        items = _read("tasks.json", [])
        keep = [t for t in items if t["id"] != tid]
        _write("tasks.json", keep)
        return len(keep) != len(items)


# ------------------------------------------------------------------ natural-language time
_NUMW = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty five": 45}
_UNIT = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration_s(t):
    """'in 2 hours 30 minutes', 'half an hour', 'an hour' -> seconds (0 if none)."""
    t = t.lower()
    total = 0
    if re.search(r"half an? hour", t):
        total += 1800
        t = re.sub(r"half an? hour", " ", t)
    for n, unit in re.findall(r"(\d+(?:\.\d+)?|an?|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty)\s*"
                              r"(day|hour|hr|minute|min|second|sec)s?\b", t):
        v = float(n) if n[0].isdigit() else _NUMW.get(n, 1)
        total += v * _UNIT["h" if unit in ("hour", "hr") else unit[0]]
    return int(total)


def parse_repeat(t):
    t = t.lower()
    if re.search(r"every\s*(week\s*day|weekday)|weekdays", t):
        return "weekdays"
    if re.search(r"every\s*week|weekly|every\s+(" + "|".join(WEEKDAYS) + ")", t):
        return "weekly"
    if re.search(r"every\s*day|daily|every\s*(morning|evening|night)", t):
        return "daily"
    return "none"


def parse_when(text, now=None):
    """Turns 'in 20 minutes', 'at 6 pm', 'tomorrow at 9', 'friday 18:30' into a datetime (or None)."""
    now = now or _now()
    t = (text or "").lower().strip()
    if not t:
        return None
    if re.search(r"\bin\b", t) or re.fullmatch(r"[\d.\s]*\w*\s*(hours?|hrs?|minutes?|mins?|seconds?|secs?)", t):
        secs = parse_duration_s(t)
        if secs:
            return now + datetime.timedelta(seconds=secs)
    day, explicit = now.date(), False
    if "day after tomorrow" in t:
        day, explicit = day + datetime.timedelta(days=2), True
    elif "tomorrow" in t:
        day, explicit = day + datetime.timedelta(days=1), True
    elif "today" in t or "tonight" in t:
        explicit = True
    else:
        for i, w in enumerate(WEEKDAYS):
            if re.search(r"\b" + w + r"\b", t):
                ahead = (i - now.weekday()) % 7 or 7
                day, explicit = day + datetime.timedelta(days=ahead), True
                break
    hour = minute = None
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b", t) or re.search(r"\b(?:at\s+)?(\d{1,2}):(\d{2})\b()", t) \
        or re.search(r"\bat\s+(\d{1,2})()()", t)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        ap = m.group(3)
        if ap:
            hour = hour % 12 + (12 if ap == "p" else 0)
        if hour > 23 or minute > 59:
            return None
    elif "noon" in t:
        hour, minute = 12, 0
    elif "midnight" in t:
        hour, minute = 0, 0
    elif "tonight" in t:
        hour, minute = 20, 0
    elif "evening" in t:
        hour, minute = 18, 0
    elif "afternoon" in t:
        hour, minute = 15, 0
    elif "morning" in t:
        hour, minute = 9, 0
    if hour is None:
        if not explicit:
            return None
        hour, minute = 9, 0
    due = datetime.datetime.combine(day, datetime.time(hour, minute))
    if m and not m.group(3) and hour < 12 and not explicit and due <= now and hour + 12 < 24:
        due = due.replace(hour=hour + 12)        # "at 6" when it's already 9am -> 6pm
    if due <= now and not explicit:
        due += datetime.timedelta(days=1)
    return due


_TIME_CLAUSE = (r"(?:in\s+(?:\d+(?:\.\d+)?|an?|half an?)\s*\w*\s*(?:hours?|hrs?|minutes?|mins?|seconds?|secs?|days?)(?:\s+and\s+\d+\s*\w+)?"
                r"|at\s+\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?|tomorrow(?:\s+(?:morning|evening|afternoon|night|at\s+\S+(?:\s*[ap]m)?))?"
                r"|tonight|today(?:\s+at\s+\S+(?:\s*[ap]m)?)?|on\s+(?:" + "|".join(WEEKDAYS) + r")(?:\s+at\s+\S+(?:\s*[ap]m)?)?"
                r"|every\s+\w+(?:\s+at\s+\S+(?:\s*[ap]m)?)?)")


def split_reminder(raw):
    """'remind me to call mom at 6 pm' -> ('call mom', 'at 6 pm', 'none'). Returns None if unclear."""
    m = re.match(r"\s*(?:please\s+)?remind me\s+(?:to\s+|that\s+|about\s+)?(.*)", raw.strip(), re.I)
    if not m:
        return None
    rest = m.group(1).strip(" .")
    lead = re.match(r"(" + _TIME_CLAUSE + r")\s*(?:to\s+|that\s+|about\s+)?(.+)", rest, re.I)
    if lead:
        when, text = lead.group(1), lead.group(2)
    else:
        found = list(re.finditer(r"\s(" + _TIME_CLAUSE + r")\s*$", rest, re.I))
        if not found:
            return None
        when, text = found[0].group(1), rest[:found[0].start()]
    text = text.strip(" ,.")
    if not text or not (parse_when(when) or parse_repeat(when) != "none"):
        return None
    return text, when, parse_repeat(rest)


def human_when(due, now=None):
    now = now or _now()
    if isinstance(due, str):
        due = datetime.datetime.fromisoformat(due)
    secs = int((due - now).total_seconds())
    if secs < 90:
        return "in a moment" if secs > 0 else "now"
    if secs < 3600:
        return f"in {round(secs / 60)} minutes"
    clock = due.strftime("%I:%M %p").lstrip("0")
    if due.date() == now.date():
        return f"today at {clock}"
    if due.date() == now.date() + datetime.timedelta(days=1):
        return f"tomorrow at {clock}"
    return due.strftime("%A %d %B at ") + clock
