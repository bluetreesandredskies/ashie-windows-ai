import os, sys, tempfile, datetime
os.environ["ASHIE_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import store

NOW = datetime.datetime(2026, 11, 24, 9, 0)


def test_relative_and_clock_times():
    assert store.parse_when("in 20 minutes", NOW) == NOW + datetime.timedelta(minutes=20)
    assert store.parse_when("in half an hour", NOW) == NOW + datetime.timedelta(minutes=30)
    assert store.parse_when("tomorrow at 9", NOW) == datetime.datetime(2026, 11, 25, 9, 0)
    assert store.parse_when("at 6 pm", NOW) == datetime.datetime(2026, 11, 24, 18, 0)
    assert store.parse_when("friday at 5 pm", NOW) == datetime.datetime(2026, 11, 27, 17, 0)
    assert store.parse_when("banana", NOW) is None


def test_split_reminder():
    assert store.split_reminder("remind me to call mom at 6 pm")[:2] == ("call mom", "at 6 pm")
    assert store.split_reminder("remind me in 10 minutes to stretch")[:2] == ("stretch", "in 10 minutes")
    assert store.split_reminder("remind me to stretch every day at 8 am")[2] == "daily"
    assert store.split_reminder("remind me to be nice") is None


def test_reminder_lifecycle_and_repeat():
    past = datetime.datetime.now() - datetime.timedelta(minutes=1)
    once = store.add_reminder("once", past)
    daily = store.add_reminder("daily", past, "daily")
    assert {r["id"] for r in store.due_reminders()} >= {once["id"], daily["id"]}
    store.mark_fired(once["id"]); store.mark_fired(daily["id"])
    by_id = {r["id"]: r for r in store.list_reminders()}
    assert by_id[once["id"]]["done"] is True
    assert by_id[daily["id"]]["done"] is False
    assert datetime.datetime.fromisoformat(by_id[daily["id"]]["due"]) > datetime.datetime.now()


def test_notes_and_tasks():
    n = store.add_note("hello", pinned=True)
    t = store.add_task("buy milk")
    assert store.list_notes()[0]["id"] == n["id"]
    store.update_task(t["id"], done=True)
    assert store.list_tasks()[0]["done"] is True
    assert store.delete_note(n["id"]) and store.delete_task(t["id"])
