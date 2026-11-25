"""
ASHIE - a free, local-first voice assistant for your Windows PC.

Ears  : faster-whisper (offline, runs on CPU so your GPU stays free)
Lock  : resemblyzer voiceprint check (offline) - only the owner's voice is obeyed
Voice : Microsoft neural voice via edge-tts (free, no key) -> Piper (offline neural,
        free) -> Google gTTS -> Windows SAPI voice, picking the best one available
Brain : Groq cloud models when online (fast, smart) -> local Ollama model when offline
        (Ollama is only started when needed and shut down when idle)
Memory: last 20 exchanges (survive restarts) + auto-learned facts + rolling summary
        of older conversations
Eyes  : RapidOCR (offline) + Windows UI Automation
Dash  : a local web dashboard (reminders, notes, tasks, memory, activity) at http://127.0.0.1:8765
Hands : pyautogui, Chrome (with your profile), Word/Outlook COM, WhatsApp Web, Gmail,
        system volume, screen brightness, screenshots

First run: Ashie records 3 short voice samples to learn your voice (or run
`python ashie.py --enroll-voice` any time to redo it). See the setup guide
for every download this script needs.
"""
import os, sys, re, time, json, ast, shutil, difflib, asyncio, datetime, tempfile, threading
import queue, subprocess, operator, traceback, urllib.request, urllib.parse, urllib.error, webbrowser
import ctypes, html
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
import numpy as np
import speech_recognition as sr
import pyautogui
import pyperclip
import psutil

# =====================================================================
#  CONFIG  (edit this section)
# =====================================================================
USER_NAME = "User"            # your name (what Ashie calls you). Can also be set in config.json
ASSISTANT_NAME = "Ashie"
USER_PROFILE = ""               # optional: a line about you, e.g. "Lives in London, works as a designer, loves cooking."

WAKE_VARIANTS = {"ashie", "ashi", "ashy", "ashe", "asha", "ashee", "aashi", "aashie", "ashley", "archie",
                 "ash", "ashii", "ashiee", "aashe", "aashy", "ashye"}
ACTIVE_SECONDS = 0           # 0 = EVERY command must start with "Ashie" / "Hey Ashie" (no open conversation window)
HOTKEY = ""                  # "" = no push-to-talk key, wake word only. (Set e.g. "f8" in config.json to re-enable)
MIC_INDEX = None             # None = default mic. Run `python ashie.py --mics` to list devices

VOICE = "en-US-AvaNeural"    # young, natural US female. Others: en-US-EmmaNeural, en-US-JennyNeural, en-GB-SoniaNeural (British), en-GB-LibbyNeural (British, younger)
VOICE_RATE = "+4%"
VOICE_PITCH = "+0Hz"         # leave at 0 for the most natural sound; try "+2Hz" for a slightly brighter voice

# Offline fallback voice (used only when there's no internet / edge-tts fails).
# Piper sounds far more natural than the built-in Windows SAPI voice. Drop the
# files from the setup guide into %USERPROFILE%\Ashie\piper\ using this name;
# if they're missing, Ashie silently falls back to the robotic Windows voice.
PIPER_VOICE_NAME = "en_GB-jenny_dioco-medium"  # natural female offline voice

# --- Voice lock: only respond to YOUR voice -------------------------------
VOICE_LOCK_ENABLED = False    # False = NO voice recognition: saying "Ashie" / "Hey Ashie" always triggers her,
                              # whoever is talking. Toggle by voice: "turn off voice lock" / "turn on voice lock"
                              # (saved to config.json), or start with:  python ashie.py --no-voice-lock
VOICE_MATCH_THRESHOLD = 0.72  # 0-1, higher = stricter. Lower it a bit if she keeps ignoring you.

# --- Wake-word speed --------------------------------------------------------
WAKE_FAST = True              # idle listening uses a tiny model just to spot her name (~10x faster)
WAKE_MODEL = "tiny.en"        # tiny.en = fastest, base.en = a bit more accurate at catching "Ashie"
PAUSE_THRESHOLD = 0.65        # seconds of silence that end a phrase (was 0.9). Lower = snappier, but may cut slow talkers

WHISPER_MODEL = "small.en"   # "base.en" = faster/lighter, "small.en" = better with accents
WHISPER_THREADS = 4

# --- Cloud brain (Groq). Used first whenever you're online ---------------
# The API key is NOT stored in this file. Run once:  python ashie.py --set-groq-key
# (or set the GROQ_API_KEY environment variable). Each list is tried in order, so if
# a model is retired or rate-limited Ashie moves to the next one automatically.
# llama-3.3-70b-versatile and llama-3.1-8b-instant were shut down by Groq on 16 Aug 2026.
# At startup Ashie also asks Groq which models are live and drops/replaces dead ones automatically.
GROQ_CHAT_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
GROQ_FAST_MODELS = ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]   # command routing / agent JSON (20b = quicker)
GROQ_CODE_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
# True = at startup (and every 6 h) Ashie asks Groq which models are live RIGHT NOW and picks the newest big
# model for chat/code and the newest small/fast one for routing. The lists above are only the fallback.
# Set "groq_auto_models": false in config.json to use the lists above exactly as written.
GROQ_AUTO_MODELS = True

# --- Memory ---------------------------------------------------------------
HISTORY_TURNS = 20           # how many of your latest exchanges she keeps in context (saved to disk)
MAX_FACTS = 150              # long-term facts she learns about you (oldest dropped beyond this)
EPI_MAX = 4000               # older exchanges kept for semantic recall (least important dropped first)
EMBED_MODEL = "BAAI/bge-small-en-v1.5"   # used if you `pip install fastembed` (small, offline, CPU only)
EPI_MIN_SCORE_FAST = 0.62    # how similar an old memory must be to get pulled back in (fastembed)
EPI_MIN_SCORE_HASH = 0.50    # built-in recall: share of your meaningful words an old memory must contain
PROFILE_EVERY = 12           # re-learn how you talk after this many new exchanges

LLM_MODEL = "qwen2.5:3b"            # offline brain, ~2 GB VRAM, fits your RTX 3050 6 GB easily
LOCAL_CTX = 4096                    # keep constant: changing it between calls makes Ollama reload the model
CODE_MODEL = "qwen2.5-coder:3b"     # used only for coding tasks (set to None to disable)
KEEP_ALIVE = "2m"                   # model leaves VRAM 2 minutes after last use
STOP_OLLAMA_WHEN_IDLE = True        # also close Ollama itself after idle time
OLLAMA_IDLE_STOP_SECONDS = 300

CHROME_PROFILE = "Default"   # chrome://version -> "Profile Path" last folder (Default / Profile 1 ...)
DEFAULT_CITY = ""            # weather city, "" = auto-detect (e.g. "Srinagar")
EMAIL_METHOD = "gmail"       # "gmail" (Chrome, uses your logged-in Gmail) or "outlook" (desktop Outlook)
GMAIL_ADDRESS = ""           # OPTIONAL but most reliable: your Gmail address + an App Password (see SETUP_GUIDE) lets Ashie
GMAIL_APP_PASSWORD = ""      # send mail with real attachments in the background, no browser clicking needed
AI_DEFAULT_SITE = "claude"   # chatbot used for "ask AI ..." when you don't name one: claude, chatgpt, deepseek, gemini, perplexity, copilot, grok
AI_WAIT_SECONDS = 150        # how long she waits for a chatbot website to finish answering
FILE_SEARCH_SECONDS = 25     # how long one file search on your PC may run

# --- Dashboard (local web UI) ---------------------------------------------
DASHBOARD_ENABLED = True
DASHBOARD_PORT = 8765
DASHBOARD_OPEN_BROWSER = False   # True = open it in your browser every time Ashie starts

# Names you can say -> phone (with country code) / email. Add your people here.
CONTACTS = {
    # "rahul": {"phone": "+919876543210", "email": "rahul@gmail.com"},
    # "mom":   {"phone": "+91XXXXXXXXXX"},
}
# =====================================================================

HOME = Path.home()
DATA = Path(os.environ.get("ASHIE_HOME") or (HOME / "Ashie"))
DATA.mkdir(parents=True, exist_ok=True)
os.environ["ASHIE_HOME"] = str(DATA)
CONFIG_PATH = DATA / "config.json"


_CONFIG_NOTE = ""


def apply_config():
    """Anything in ~/Ashie/config.json overrides the defaults above (keys are case-insensitive)."""
    global _CONFIG_NOTE
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        _CONFIG_NOTE = f"config loaded: {CONFIG_PATH}"
    except FileNotFoundError:
        _CONFIG_NOTE = f"no config file at {CONFIG_PATH} (using defaults in ashie.py)"
        return
    except Exception as e:
        _CONFIG_NOTE = f"CONFIG ERROR in {CONFIG_PATH}: {e}  <- fix this file, it is being ignored"
        return
    for k, v in cfg.items():
        K = k.upper()
        if K.isupper() and K in globals() and not K.startswith("_"):
            globals()[K] = set(w.lower() for w in v) if K == "WAKE_VARIANTS" else v


apply_config()
DOCS = DATA / "documents"
PROJECTS = DATA / "projects"
PIPER_DIR = DATA / "piper"
PIPER_EXE = PIPER_DIR / "piper.exe"
PIPER_MODEL = PIPER_DIR / f"{PIPER_VOICE_NAME}.onnx"
VOICE_PROFILE_PATH = DATA / "voice_profile.npy"
for d in (DATA, DOCS, PROJECTS, PIPER_DIR):
    d.mkdir(parents=True, exist_ok=True)

try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

pyautogui.FAILSAFE = True    # slam mouse into a screen corner = emergency stop
pyautogui.PAUSE = 0.05


def load_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


import store            # reminders / notes / tasks / activity / status shared with the dashboard


# =====================================================================
#  MEMORY
#   - HISTORY : last HISTORY_TURNS exchanges (you said / she replied), saved to disk so
#               she still has the thread after a restart
#   - facts   : lasting things she learns about you ("remember...", or auto-detected)
#   - summary : rolling summary of older exchanges that fell out of the 20-exchange window
# =====================================================================
HISTORY_PATH = DATA / "history.json"
GROQ_KEY_PATH = DATA / "groq_key.txt"
_mem_lock = threading.RLock()

MEM = load_json(DATA / "memory.json", {})
if not isinstance(MEM, dict):
    MEM = {}
MEM.setdefault("facts", [])
MEM.setdefault("summary", "")
MEM.setdefault("pending", [])
MEM.setdefault("profile", "")          # learned description of how you talk / what you're like
MEM.setdefault("samples", [])          # a few of your own conversational phrasings
MEM.setdefault("stats", {})            # usage patterns (skills, hours, message length)
MEM.setdefault("since_profile", 0)

_h = load_json(HISTORY_PATH, [])
HISTORY = deque([e for e in _h if isinstance(e, dict) and e.get("user")][-HISTORY_TURNS:], maxlen=HISTORY_TURNS)


def save_mem():
    with _mem_lock:
        (DATA / "memory.json").write_text(json.dumps(MEM, indent=1, ensure_ascii=False), encoding="utf-8")


def save_history():
    with _mem_lock:
        HISTORY_PATH.write_text(json.dumps(list(HISTORY), indent=1, ensure_ascii=False), encoding="utf-8")


def load_groq_key():
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key and GROQ_KEY_PATH.exists():
        key = GROQ_KEY_PATH.read_text(encoding="utf-8").strip()
    return key or None


GROQ_KEY = load_groq_key()


def add_fact(fact):
    fact = re.sub(r"\s+", " ", (fact or "")).strip().rstrip(".")
    if len(fact) < 3 or len(fact) > 200:
        return False
    with _mem_lock:
        for old in MEM["facts"]:
            if difflib.SequenceMatcher(None, fact.lower(), old.lower()).ratio() > 0.85:
                return False
        MEM["facts"].append(fact)
        del MEM["facts"][:-MAX_FACTS]
        save_mem()
    return True


def forget_fact(query):
    q = set(re.findall(r"[a-z0-9']+", (query or "").lower())) - {"the", "a", "an", "that", "my", "about", "i", "is"}
    best, score = None, 0
    with _mem_lock:
        for f in MEM["facts"]:
            s = len(q & set(re.findall(r"[a-z0-9']+", f.lower())))
            if s > score:
                best, score = f, s
        if best:
            MEM["facts"].remove(best)
            save_mem()
    return best


def clear_history(archive=False):
    """archive=True ('new conversation'): the chat moves into long-term memory instead of vanishing."""
    with _mem_lock:
        if archive:
            for e in list(HISTORY):
                MEM["pending"].append(e)
                threading.Thread(target=EPI.add, args=(e,), daemon=True).start()
            save_mem()
        HISTORY.clear()
        save_history()


def clear_all_memory():
    with _mem_lock:
        MEM["facts"], MEM["summary"], MEM["pending"] = [], "", []
        MEM["profile"], MEM["samples"], MEM["stats"], MEM["since_profile"] = "", [], {}, 0
        save_mem()
    EPI.clear()
    clear_history()


def history_messages(n=HISTORY_TURNS, max_chars=None):
    """Your last n exchanges as chat messages, newest kept first if they don't all fit.
    The cloud model gets all 20; the small local model gets as many as fit its context window."""
    if max_chars is None:
        max_chars = 12000 if groq_available() else 4500
    picked, used = [], 0
    for e in reversed(list(HISTORY)[-n:]):
        size = len(e["user"]) + len(e.get("assistant", ""))
        if picked and used + size > max_chars:
            break
        picked.append(e)
        used += size
    msgs = []
    for e in reversed(picked):
        msgs.append({"role": "user", "content": e["user"]})
        if e.get("assistant"):
            msgs.append({"role": "assistant", "content": e["assistant"]})
    return msgs


def recent_context(n=3):
    items = list(HISTORY)[-n:]
    return "\n".join(f"{USER_NAME}: {e['user']}\n{ASSISTANT_NAME}: {e.get('assistant', '')[:280]}" for e in items)


_folding = False


def _fold_summary():
    """Squash exchanges that fell out of the 20-exchange window into the rolling summary."""
    global _folding
    if _folding or not (groq_available() or ollama_up()):
        return
    _folding = True
    try:
        with _mem_lock:
            batch, old = list(MEM["pending"]), MEM["summary"]
        if not batch:
            return
        convo = "\n".join(f"{USER_NAME}: {e['user']}\n{ASSISTANT_NAME}: {e.get('assistant', '')}" for e in batch)
        out = llm([{"role": "system", "content": "You maintain a compact memory of past conversations between "
                                                 "a user and their voice assistant."},
                   {"role": "user", "content": f"Existing summary:\n{old or '(none)'}\n\nNew conversations:\n{convo}\n\n"
                                               "Write the updated summary in at most 120 words. Keep lasting details "
                                               "(people, projects, plans, preferences, unfinished to-dos). Drop small "
                                               "talk and one-off commands. Plain text, no lists."}],
                  task="fast", max_tokens=250, temp=0.2)
        if out:
            with _mem_lock:
                MEM["summary"] = out.strip()[:1200]
                MEM["pending"] = MEM["pending"][len(batch):]
                save_mem()
    except Exception as e:
        print("(summary error)", e)
    finally:
        _folding = False


def record_exchange(user, reply):
    user, reply = (user or "").strip(), (reply or "").strip()
    if not user:
        return
    ex = {"t": datetime.datetime.now().isoformat(timespec="seconds"), "user": user[:500], "assistant": reply[:600]}
    with _mem_lock:
        if len(HISTORY) == HISTORY.maxlen:
            old = HISTORY[0]
            MEM["pending"].append(old)            # -> folded into the rolling summary
            threading.Thread(target=EPI.add, args=(old,), daemon=True).start()   # -> kept as a recallable vector
        HISTORY.append(ex)
        del MEM["pending"][:-60]          # never let the to-be-summarised backlog grow forever offline
        save_history()
        save_mem()
    if len(MEM["pending"]) >= 8:
        threading.Thread(target=_fold_summary, daemon=True).start()


FACT_HINT = re.compile(r"\b(i am|i'm|i like|i love|i hate|i prefer|i work|i live|i study|i use|i have|i got|i want|"
                       r"my (?:\w+ ){0,2}(?:is|are|name|birthday|favou?rite)|call me|i usually|i always|i never)\b", re.I)


def _learn(text):
    try:
        if not (groq_available() or ollama_up()):
            return
        out = llm_json([{"role": "user", "content": (
            f"The speaker is {USER_NAME}. Message: \"{text}\"\n"
            f"Extract only LASTING facts about {USER_NAME} (preferences, people in their life, projects, routines, "
            "personal details) that would still be useful to remember months from now. Ignore commands, questions, "
            "small talk and temporary things. Reply as JSON: {\"facts\": [\"short third-person sentence\"]} "
            "with an empty list if there is nothing worth keeping.")}],
            task="fast", max_tokens=150, temp=0.1)
        facts = out.get("facts") if isinstance(out, dict) else None
        for f in (facts or [])[:3]:
            if isinstance(f, str) and add_fact(f):
                print("(learned)", f)
    except Exception as e:
        print("(learn error)", e)


def learn_async(text):
    if text and FACT_HINT.search(text):
        threading.Thread(target=_learn, args=(text,), daemon=True).start()


# =====================================================================
#  LONG-TERM MEMORY  (everything older than the last 20 exchanges)
#   Layer 1  facts        short lasting statements about you
#   Layer 2  summary      rolling compressed story of older conversations
#   Layer 3  episodes     every older exchange stored as a vector; the few that are relevant to what
#                         you're saying right now are pulled back in (semantic recall)
#   Layer 4  style/habits a learned profile of how you talk and what you usually do, plus samples of
#                         your own phrasing, so she can match your way of talking
#  Embeddings: fastembed (small offline model, CPU) if installed, otherwise a built-in lightweight
#  hashing embedding, so this works either way.
# =====================================================================
import zlib, math

EPI_PATH = DATA / "episodes.json"
EPI_VEC_PATH = DATA / "episodes_vec.npy"
_STOP = set("a an the and or but if of to in on at for with is are was were be been am i you he she it we they my "
            "your me this that these those do does did have has had will would can could should so as by from about "
            "just not no yes please ashie hey ok okay what whats how when where why who which tell know get got give gave "
            "make made use used need want like going thing things something any some more also again then there here now "
            "still really very should shall may might let lets".split())


def _stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[:-len(suf)]
    return w


def _hash_embed(text, dim=512):
    """Built-in embedding: signed hashing of words + word pairs (stable across restarts, no downloads)."""
    v = np.zeros(dim, dtype=np.float32)
    words = [_stem(w) for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _STOP]
    for g in words + [a + "_" + b for a, b in zip(words, words[1:])]:
        h = zlib.crc32(g.encode("utf-8"))
        v[h % dim] += 1.0 if (h >> 17) & 1 else -1.0
    n = float(np.linalg.norm(v))
    return v / n if n else v


def _toks(text):
    return {_stem(w) for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _STOP and len(w) > 1}


def _importance(user):
    """0 = not worth remembering ('mute', 'next tab'); higher = keep longer when the store is full."""
    words = re.findall(r"[a-z']+", user.lower())
    fact = bool(FACT_HINT.search(user))
    if len(words) < 3 and not fact:
        return 0
    return 1 + (2 if fact else 0) + (1 if len(words) >= 10 else 0)


def _ago(iso):
    try:
        d = (datetime.datetime.now() - datetime.datetime.fromisoformat(iso)).days
    except Exception:
        return "earlier"
    return "today" if d < 1 else "yesterday" if d < 2 else f"{d} days ago" if d < 14 else \
        f"{d // 7} weeks ago" if d < 60 else f"{d // 30} months ago"


class EpisodeStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.recs, self.vecs, self.backend = [], None, "hash"
        self._fast = None
        self._tok, self._df = [], {}     # word sets + document frequencies (for the built-in keyword recall)
        self._ready = threading.Event()
        self._started = False
        self._load()

    # ---- storage
    def _load(self):
        d = load_json(EPI_PATH, {})
        self.recs = [r for r in d.get("recs", []) if isinstance(r, dict) and r.get("user")]
        self.backend = d.get("backend", "hash")
        try:
            vecs = np.load(EPI_VEC_PATH).astype(np.float32) if EPI_VEC_PATH.exists() else None
        except Exception:
            vecs = None
        if self.recs and (vecs is None or len(vecs) != len(self.recs)):
            self.backend = "hash"                       # vectors missing/corrupt: rebuild cheaply
            vecs = self._embed([self._text(r) for r in self.recs], "hash")
        self.vecs = vecs
        self._rebuild_tokens()

    def _rebuild_tokens(self):
        self._tok = [_toks(self._text(r)) for r in self.recs]
        df = {}
        for ts in self._tok:
            for t in ts:
                df[t] = df.get(t, 0) + 1
        self._df = df

    def _save(self):
        with self.lock:
            tmp = EPI_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps({"backend": self.backend, "recs": self.recs}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, EPI_PATH)
            if self.vecs is not None and len(self.recs):
                with open(str(EPI_VEC_PATH) + ".tmp", "wb") as f:
                    np.save(f, self.vecs.astype(np.float16))
                os.replace(str(EPI_VEC_PATH) + ".tmp", EPI_VEC_PATH)

    @staticmethod
    def _text(r):
        return r["user"] + "\n" + r.get("assistant", "")

    # ---- embedding backends
    def start(self):
        """Loads the semantic model in the background (called once at startup)."""
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._init_backend, daemon=True).start()

    def _init_backend(self):
        try:
            from fastembed import TextEmbedding
            self._fast = TextEmbedding(model_name=EMBED_MODEL, cache_dir=str(DATA / "embed_cache"), threads=2)
            list(self._fast.embed(["warm up"]))
        except ImportError:
            self._fast = None
            print("(memory: using the built-in lightweight recall. For smarter recall: pip install fastembed)")
        except Exception as e:
            self._fast = None
            print(f"(memory: semantic model unavailable - {type(e).__name__}; using built-in recall)")
        finally:
            self._ready.set()
        with self.lock:
            want = "fast" if self._fast else "hash"
            if self.recs and self.backend != want:
                print(f"(memory: re-indexing {len(self.recs)} memories with the '{want}' embeddings...)")
                self.vecs = self._embed([self._text(r) for r in self.recs], want)
            self.backend = want
            self._save()

    def _embed(self, texts, backend):
        if backend == "fast" and self._fast:
            arr = np.array(list(self._fast.embed(texts)), dtype=np.float32)
            return arr / (np.linalg.norm(arr, axis=1, keepdims=True) + 1e-9)
        return np.stack([_hash_embed(t) for t in texts])

    # ---- operations
    def add(self, ex):
        """Stores one older exchange. Skips trivia and near-duplicates."""
        imp = _importance(ex.get("user", ""))
        if imp <= 0:
            return False
        if self._started:
            self._ready.wait(90)
        with self.lock:
            try:
                v = self._embed([self._text(ex)], self.backend)
            except Exception as e:
                print("(memory embed error)", e)
                return False
            if self.vecs is not None and len(self.recs) and float((self.vecs @ v[0]).max()) > 0.97:
                return False
            self.recs.append({"t": ex.get("t", ""), "user": ex["user"], "assistant": ex.get("assistant", ""), "imp": imp})
            self.vecs = v if self.vecs is None or not len(self.vecs) else np.vstack([self.vecs, v])
            ts = _toks(self._text(ex))
            self._tok.append(ts)
            for t in ts:
                self._df[t] = self._df.get(t, 0) + 1
            if len(self.recs) > EPI_MAX:
                self._prune()
            self._save()
        return True

    def _prune(self):
        """Over capacity: drop the least important, then the oldest."""
        order = sorted(range(len(self.recs)), key=lambda i: (self.recs[i].get("imp", 1), self.recs[i].get("t", "")))
        drop = set(order[:len(self.recs) - EPI_MAX])
        keep = [i for i in range(len(self.recs)) if i not in drop]
        self.recs = [self.recs[i] for i in keep]
        self.vecs = self.vecs[keep]
        self._rebuild_tokens()

    def recall(self, query, k=4):
        """Older exchanges most relevant to `query`: [(score, rec), ...]"""
        if not self.recs or not query.strip():
            return []
        if self._started and self.backend == "fast" and not self._ready.wait(3):
            return []
        with self.lock:
            if self.backend != "fast":
                return self._recall_keywords(query, k)
            if not self._fast:
                return []
            try:
                q = self._embed([query], "fast")[0]
            except Exception:
                return []
            sims = self.vecs @ q
            out = []
            for i in np.argsort(-sims)[:k * 2]:
                if sims[i] < EPI_MIN_SCORE_FAST:
                    break
                out.append((float(sims[i]), self.recs[int(i)]))
            return out[:k]

    def _recall_keywords(self, query, k):
        """Built-in recall: rare words count more than common ones (IDF). A memory must contain at least one
        fairly rare word you just said, and cover EPI_MIN_SCORE_HASH of your meaningful words."""
        n = len(self.recs)
        terms = [t for t in _toks(query) if self._df.get(t, 0) > 0]
        if not terms:
            return []
        norm_ = math.log(n + 2)
        idf = {t: math.log((n - self._df[t] + 0.5) / (self._df[t] + 0.5) + 1) / norm_ for t in terms}
        total = sum(idf.values())
        scored = []
        for i, ts in enumerate(self._tok):
            m = sum(idf[t] for t in terms if t in ts)
            if m >= 0.5 and m / total >= EPI_MIN_SCORE_HASH:
                scored.append((m / total + 0.1 * m, i))
        scored.sort(reverse=True)
        return [(sc, self.recs[i]) for sc, i in scored[:k]]

    def clear(self):
        with self.lock:
            self.recs, self.vecs, self._tok, self._df = [], None, [], {}
            for p in (EPI_PATH, EPI_VEC_PATH):
                try:
                    p.unlink()
                except Exception:
                    pass

    def __len__(self):
        return len(self.recs)


EPI = EpisodeStore()


def recall_block(query):
    """Text for the system prompt: older conversations relevant to what's being said now."""
    big = groq_available()
    hits = EPI.recall(query, k=4 if big else 2)
    if not hits:
        return ""
    cut = 260 if big else 170
    lines = [f"- ({_ago(r.get('t', ''))}) {USER_NAME}: {r['user'][:cut]} / You: {r.get('assistant', '')[:cut]}"
             for _s, r in hits]
    return "\nOlder conversations that may be relevant (use only if they actually help):\n" + "\n".join(lines)


# ---- style & habits ------------------------------------------------------------------
_COMMAND_START = {"open", "close", "play", "click", "type", "press", "scroll", "search", "go", "switch", "set",
                  "take", "start", "stop", "volume", "mute", "copy", "paste", "select", "tap", "launch", "turn"}
_profiling = False


def _hour_label(h):
    return "the morning" if 5 <= h < 12 else "the afternoon" if 12 <= h < 17 else \
        "the evening" if 17 <= h < 22 else "late at night"


def note_usage(user, skill):
    """Cheap, offline pattern tracking: what they ask for, when, how long their messages are, how they phrase things."""
    words = re.findall(r"[A-Za-z']+", user)
    with _mem_lock:
        st = MEM["stats"]
        st["n"] = st.get("n", 0) + 1
        st["words"] = st.get("words", 0) + len(words)
        sk = st.setdefault("skills", {})
        sk[skill] = sk.get(skill, 0) + 1
        hr = st.setdefault("hours", {})
        h = str(datetime.datetime.now().hour)
        hr[h] = hr.get(h, 0) + 1
        if len(words) >= 6 and words[0].lower() not in _COMMAND_START:
            s = MEM["samples"]
            if user not in s:
                s.append(user[:200])
                del s[:-30]
        MEM["since_profile"] = MEM.get("since_profile", 0) + 1
        due = MEM["since_profile"] >= PROFILE_EVERY and not _profiling
        save_mem()
    if due:
        threading.Thread(target=_update_profile, daemon=True).start()


def _stats_line():
    st = MEM["stats"]
    n = st.get("n", 0)
    if n < 20:
        return ""
    hours = st.get("hours", {})
    top_h = []
    for h in sorted(hours, key=hours.get, reverse=True):
        lab = _hour_label(int(h))
        if lab not in top_h:
            top_h.append(lab)
        if len(top_h) == 2:
            break
    top_s = [k for k, _ in sorted(st.get("skills", {}).items(), key=lambda kv: -kv[1]) if k not in ("rule", "chat")][:3]
    parts = [f"mostly talks to you in {' and '.join(top_h)}", f"usually speaks in about {st.get('words', 0) / n:.0f}-word messages"]
    if top_s:
        parts.append("most used skills: " + ", ".join(top_s))
    return "; ".join(parts)


def style_block():
    big = groq_available()
    out = ""
    if MEM.get("profile"):
        out += f"\nWhat {USER_NAME} is like and how they talk (learned over time): {MEM['profile']}"
    habits = _stats_line()
    if habits:
        out += f"\nHabits: {habits}."
    s = MEM.get("samples", [])
    if s:
        import random
        n = 4 if big else 2
        pick = s[-2:] + random.sample(s[:-2], min(len(s[:-2]), max(0, n - 2)))
        out += (f"\nExamples of how they phrase things (only to match their tone, never quote them): "
                + " | ".join(p[:120] for p in pick))
    if out:
        out += f"\nMatch their way of talking naturally (vocabulary, energy, humour) while staying yourself."
    return out


def _update_profile():
    global _profiling
    if _profiling or not (groq_available() or ollama_up()):
        return
    _profiling = True
    try:
        with _mem_lock:
            if MEM.get("since_profile", 0) < PROFILE_EVERY:      # another thread just did it
                return
            recent = [e["user"] for e in list(HISTORY)[-14:]]
            old, samples = MEM.get("profile", ""), MEM["samples"][-8:]
        msgs = "\n".join("- " + m for m in recent + samples if m)
        out = llm([{"role": "system", "content": "You study how a person talks and behaves so their voice assistant can "
                                                 "feel like it truly knows them."},
                   {"role": "user", "content": (
                       f"Existing profile of {USER_NAME}:\n{old or '(none yet)'}\n\nHabits: {_stats_line() or 'unknown'}\n\n"
                       f"Their recent messages to the assistant:\n{msgs}\n\n"
                       "Write the UPDATED profile in at most 110 words, plain text. Cover: how they talk (language mix such as "
                       "bilingual blends, slang, formality, typical message length, humour), what they care about and work on, what they "
                       "usually asks for, and how they like answers (short or detailed, jokes or not). Describe patterns only; "
                       "never record passwords, OTPs or payment details. Keep older insights that still hold.")}],
                  task="chat", max_tokens=260, temp=0.3)
        if out:
            with _mem_lock:
                MEM["profile"] = out.strip()[:900]
                MEM["since_profile"] = 0
                save_mem()
            print("(updated what I know about how you talk)")
    except Exception as e:
        print("(profile error)", e)
    finally:
        _profiling = False


# =====================================================================
#  VOICE OUTPUT
#  Order: Microsoft neural (edge-tts) -> Piper (offline neural) -> Google gTTS -> Windows SAPI
#  Every failure now prints its REAL reason, and playback has its own fallback so a pygame
#  problem can never push her onto the robotic voice by itself.
# =====================================================================
_speak_lock = threading.Lock()
_edge_off_until = 0
_edge_hint_shown = False
FALLBACK_VOICE = "en-US-EmmaNeural"   # tried automatically if VOICE itself returns no audio
_capture = None                      # list that collects what she says during a turn (for memory)
LAST_SKILL = "rule"                  # which skill handled the current turn (for habit tracking)


def clean_for_speech(t):
    t = re.sub(r"https?://\S+", "link", t)
    t = re.sub(r"[*#`_>~|]", "", t)
    t = re.sub(r"[\U00010000-\U0010ffff☀-➿]", "", t)
    return re.sub(r"\s+", " ", t).strip()[:700]


def _rm(path):
    if path:
        try:
            os.remove(path)
        except Exception:
            pass


async def _edge_synth(text, path, voice=None):
    import edge_tts
    comm = edge_tts.Communicate(text, voice or VOICE, rate=VOICE_RATE, pitch=VOICE_PITCH)
    await asyncio.wait_for(comm.save(path), timeout=20)


def _play_pygame(path):
    import pygame
    if not pygame.mixer.get_init():
        pygame.mixer.init()
    pygame.mixer.music.load(path)
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        time.sleep(0.05)
    try:
        pygame.mixer.music.unload()   # frees the file so it can be deleted (pygame >= 2.0.1 only)
    except Exception:
        pass


def _play_mci(path):
    """Windows' built-in media player (no extra libraries). Plays mp3 and wav."""
    try:
        mci = ctypes.windll.winmm.mciSendStringW
        alias = "ashie_snd"
        kind = "waveaudio" if path.lower().endswith(".wav") else "mpegvideo"
        mci(f"close {alias}", None, 0, 0)
        if mci(f'open "{path}" type {kind} alias {alias}', None, 0, 0) != 0:
            return False
        rc = mci(f"play {alias} wait", None, 0, 0)
        mci(f"close {alias}", None, 0, 0)
        return rc == 0
    except Exception as e:
        print("(windows audio playback failed)", repr(e))
        return False


def play_audio(path):
    try:
        _play_pygame(path)
        return True
    except Exception as e:
        print("(pygame playback failed, using the Windows player)", repr(e))
    return _play_mci(path)


def _edge_hint(msg):
    low = msg.lower()
    if any(k in low for k in ("403", "invalid response status", "handshake", "sec-ms-gec")):
        print("   FIX: run  pip install -U edge-tts   (Microsoft rotated its auth; old versions get 403).\n"
              "        Also: Windows Settings > Time & language > Date & time > 'Sync now' (a wrong clock causes 403).")
    elif "no module named" in low or "modulenotfound" in low:
        print("   FIX: run  pip install -U edge-tts")
    elif any(k in low for k in ("ssl", "certificate")):
        print("   FIX: an antivirus/VPN is intercepting HTTPS. Try  pip install -U certifi  or pause 'web/HTTPS scanning'.")
    elif any(k in low for k in ("cannot connect", "getaddrinfo", "timeout", "timed out", "connect")):
        print("   Can't reach speech.platform.bing.com - check internet / VPN / firewall / school-office network.")
    elif "noaudio" in low:
        print(f"   The voice name '{VOICE}' may be wrong. Try VOICE = \"en-US-AvaNeural\".")
    else:
        print("   Run  python ashie.py --test-voice  for a full diagnosis.")


def _speak_edge(text):
    global _edge_off_until, _edge_hint_shown
    if time.time() < _edge_off_until:
        return False
    voices = [VOICE] + ([FALLBACK_VOICE] if FALLBACK_VOICE != VOICE else [])
    last = None
    for voice in voices:
        path = None
        try:
            fd, path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            asyncio.run(_edge_synth(text, path, voice))
            if os.path.getsize(path) < 1000:
                raise RuntimeError("edge-tts returned an empty audio file")
        except Exception as e:
            last = e
            _rm(path)
            if type(e).__name__ in ("NoAudioReceived", "RuntimeError"):
                continue      # maybe a bad voice name: try the backup voice
            break             # network / auth problem: another voice won't help
        ok = play_audio(path)
        _rm(path)
        return ok
    msg = f"{type(last).__name__}: {last}"
    print(f"(online voice failed -> {msg})")
    if not _edge_hint_shown:
        _edge_hint_shown = True
        _edge_hint(msg)
    _edge_off_until = time.time() + 20   # brief pause so we don't retry on every sentence
    return False


def _speak_piper(text):
    """Offline neural voice (Piper). Needs piper.exe + the .onnx voice file in
    %USERPROFILE%\\Ashie\\piper\\ (see setup guide)."""
    if not (PIPER_EXE.exists() and PIPER_MODEL.exists()):
        return False
    wav_path = None
    try:
        fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        p = subprocess.run([str(PIPER_EXE), "-m", str(PIPER_MODEL), "-f", wav_path],
                           input=text.encode("utf-8"), capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if p.returncode != 0 or not os.path.exists(wav_path) or os.path.getsize(wav_path) < 100:
            return False
        return play_audio(wav_path)
    except Exception as e:
        print("(piper voice unavailable)", e)
        return False
    finally:
        _rm(wav_path)


def _speak_gtts(text):
    """Second online voice (Google). Keeps working when Microsoft's voice server is blocked."""
    path = None
    try:
        from gtts import gTTS
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        gTTS(text, lang="en", tld="com").save(path)
        return play_audio(path)
    except ImportError:
        return False
    except Exception as e:
        print("(google voice unavailable)", type(e).__name__)
        return False
    finally:
        _rm(path)


def _speak_offline(text):
    try:
        import pyttsx3
        eng = pyttsx3.init()
        for v in eng.getProperty("voices"):
            if any(k in (v.name or "").lower() for k in ("zira", "female", "hazel")):
                eng.setProperty("voice", v.id)
                break
        eng.setProperty("rate", 185)
        eng.say(text)
        eng.runAndWait()
    except Exception as e:
        print("(no voice output available)", e)


def speak(text):
    text = (text or "").strip()
    if not text:
        return
    print(f"\n[{ASSISTANT_NAME}] {text}")
    if _capture is not None and threading.current_thread() is threading.main_thread():
        _capture.append(text)          # remembered as part of this turn's conversation
    spoken = clean_for_speech(text)
    store.set_status(state="speaking", last_reply=text[:240])
    with _speak_lock:
        if not (_speak_edge(spoken) or _speak_piper(spoken) or _speak_gtts(spoken)):
            _speak_offline(spoken)
    time.sleep(0.25)
    if threading.current_thread() is threading.main_thread():
        flush_mic()          # drop what the mic heard of her own voice so she can't wake herself up
    store.set_status(state="listening")


def flush_mic():
    try:
        st = SRC.stream.pyaudio_stream
        n = st.get_read_available()
        if n:
            st.read(n, exception_on_overflow=False)
    except Exception:
        pass


def test_voice():
    """python ashie.py --test-voice : shows exactly why the online voice does or doesn't work."""
    import importlib.metadata as md
    try:
        print("edge-tts version:", md.version("edge-tts"))
    except Exception:
        print("edge-tts is NOT installed -> pip install -U edge-tts")
        return
    try:
        import pygame
        print("pygame version:", pygame.version.ver)
    except Exception as e:
        print("pygame problem (will use the Windows player instead):", repr(e))
    print(f"Synthesising with {VOICE} ...")
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        asyncio.run(_edge_synth(f"Hi {USER_NAME}, this is my online voice. If you can hear this, it works.", path, VOICE))
        print("Synthesis OK, file size:", os.path.getsize(path), "bytes. Playing...")
        print("Playback OK." if play_audio(path) else "Playback FAILED (pygame and the Windows player).")
    except Exception as e:
        traceback.print_exc()
        _edge_hint(f"{type(e).__name__}: {e}")
    finally:
        _rm(path)


# =====================================================================
#  VOICE INPUT (offline Whisper)
# =====================================================================
rec = sr.Recognizer()
rec.pause_threshold = float(PAUSE_THRESHOLD)
rec.non_speaking_duration = min(0.35, float(PAUSE_THRESHOLD))
rec.dynamic_energy_threshold = True
SRC = None
_whisper = None
_whisper_fast = None
HALLUCINATIONS = {"you", "thank you", "thanks", "thanks for watching", "bye", "okay", "uh", "um", "hmm", "the"}
WHISPER_PROMPT = (f"Ashie, {USER_NAME}, hey Ashie, open Chrome, WhatsApp, Gmail, Word, VS Code, Spotify, "
                  "send an email, text, click, scroll, settings, Wi-Fi.")


def init_whisper():
    global _whisper, _whisper_fast
    from faster_whisper import WhisperModel
    print(f"Loading offline speech model '{WHISPER_MODEL}' (CPU)...")
    _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=WHISPER_THREADS)
    _whisper_fast = _whisper
    if WAKE_FAST and WAKE_MODEL and WAKE_MODEL != WHISPER_MODEL:
        try:
            print(f"Loading fast wake-word model '{WAKE_MODEL}'...")
            _whisper_fast = WhisperModel(WAKE_MODEL, device="cpu", compute_type="int8", cpu_threads=2)
        except Exception as e:
            print("(fast wake model unavailable, using the main model)", repr(e))
            _whisper_fast = _whisper


_last_wav = None


def transcribe_array(arr, fast=False):
    """fast=True uses the tiny wake-word model (quick, rougher); otherwise the main model."""
    global _last_wav
    _last_wav = arr
    if len(arr) < (4800 if fast else 8000):
        return ""
    model = _whisper_fast if (fast and _whisper_fast is not None) else _whisper
    segs, _ = model.transcribe(arr, language="en", beam_size=1, vad_filter=True, without_timestamps=True,
                               initial_prompt=WHISPER_PROMPT, condition_on_previous_text=False)
    text = " ".join(s.text.strip() for s in segs if getattr(s, "no_speech_prob", 0) < 0.6).strip()
    if re.sub(r"[^a-z ]", "", text.lower()).strip() in HALLUCINATIONS:
        return ""
    return text


def transcribe(audio):
    raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
    return transcribe_array(np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0)


def hear_audio(timeout=2, limit=12):
    """Records one phrase and returns it as a 16 kHz float array (None if nobody spoke)."""
    try:
        audio = rec.listen(SRC, timeout=timeout, phrase_time_limit=limit)
    except sr.WaitTimeoutError:
        return None
    raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


# =====================================================================
#  VOICE LOCK (speaker verification - only YOU can command her)
# =====================================================================
_voice_encoder = None


def get_voice_encoder():
    """Loads the (small, offline) speaker-embedding model on first use only."""
    global _voice_encoder
    if _voice_encoder is None:
        from resemblyzer import VoiceEncoder
        print("Loading voice-lock model (first time only)...")
        _voice_encoder = VoiceEncoder()
    return _voice_encoder


def speaker_embedding(wav_16k):
    try:
        from resemblyzer import preprocess_wav
        wav = preprocess_wav(wav_16k, source_sr=16000)
        if len(wav) < 1600:  # too short to be reliable (<0.1s of speech)
            return None
        return get_voice_encoder().embed_utterance(wav)
    except Exception as e:
        print("(voice-lock error)", e)
        return None


def _cosine(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def is_your_voice(wav_16k):
    """True if the audio matches the enrolled voice profile. Fails OPEN
    (lets it through) if the model errors out, so a bug can't brick Ashie."""
    if not VOICE_LOCK_ENABLED or not VOICE_PROFILE_PATH.exists():
        return True
    emb = speaker_embedding(wav_16k)
    if emb is None:
        return True
    profile = np.load(VOICE_PROFILE_PATH)
    return _cosine(emb, profile) >= VOICE_MATCH_THRESHOLD


def set_voice_lock(on):
    """Turns speaker verification on/off right now and remembers it in config.json."""
    global VOICE_LOCK_ENABLED
    VOICE_LOCK_ENABLED = bool(on)
    try:
        cfg = load_json(CONFIG_PATH, {})
        for k in [k for k in cfg if k.lower() == "voice_lock_enabled"]:
            del cfg[k]
        cfg["voice_lock_enabled"] = VOICE_LOCK_ENABLED
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print("(couldn't save voice-lock setting)", e)
    store.set_status(voice_lock=VOICE_LOCK_ENABLED)
    store.log("system", "Voice lock " + ("ON" if on else "OFF"))


def enroll_voice():
    """Records a few short samples and saves your voiceprint. Run this once
    with: python ashie.py --enroll-voice   (or Ashie offers it automatically
    the first time she starts up with no profile saved yet)."""
    speak("Let's set up your voice lock. After each beep, say a full sentence "
          "out loud, like telling me about your day. Three samples, please.")
    embs = []
    for i in range(3):
        time.sleep(0.4)
        beep()
        print(f"Recording sample {i + 1}/3 - speak now...")
        try:
            audio = rec.listen(SRC, timeout=6, phrase_time_limit=6)
        except sr.WaitTimeoutError:
            continue
        raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
        arr = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
        emb = speaker_embedding(arr)
        if emb is not None:
            embs.append(emb)
        else:
            speak("That one was too quiet, let's redo it.")
    if len(embs) < 2:
        speak("I couldn't get clean samples. Run with --enroll-voice again when it's quieter.")
        return False
    np.save(VOICE_PROFILE_PATH, np.mean(embs, axis=0))
    speak(f"All set. From now on I'll only take commands from your voice, {USER_NAME}.")
    return True


def listen(timeout=3, limit=14):
    try:
        audio = rec.listen(SRC, timeout=timeout, phrase_time_limit=limit)
    except sr.WaitTimeoutError:
        return ""
    try:
        text = transcribe(audio)
    except Exception as e:
        print("(transcribe error)", e)
        return ""
    if text and VOICE_LOCK_ENABLED and VOICE_PROFILE_PATH.exists() and not is_your_voice(_last_wav):
        print(f"🔒 ignored (voice didn't match {USER_NAME}):", text)
        return ""
    if text:
        print("🎤 heard:", text)
        store.set_status(last_heard=text[:240])
    return text


def norm(t):
    t = re.sub(r"[,!?;]", "", t.lower().strip())
    return t.rstrip(". ").strip()


YES = {"yes", "yeah", "yep", "yup", "sure", "confirm", "confirmed", "ok", "okay", "haan", "han", "send",
       "proceed", "affirmative"}
NO = {"no", "nope", "nah", "cancel", "stop", "don't", "dont", "nahi", "nahin", "negative", "abort", "wait"}


def ask(question, timeout=10):
    speak(question)
    for _ in range(2):
        a = listen(timeout=timeout, limit=15)
        if a:
            return a.strip()
    return ""


def confirm(question):
    for attempt in range(3):
        speak(f"{question} Say yes or no." if attempt == 0 else "Sorry, was that a yes or a no?")
        a = norm(listen(timeout=8, limit=5))
        w = set(re.findall(r"[a-z']+", a))
        if w & YES:
            return True
        if w & NO:
            speak("Okay, cancelled.")
            return False
    speak("I didn't hear a clear yes, so I cancelled it.")
    return False


# =====================================================================
#  BRAIN
#   1) Groq cloud models (fast + smart) whenever you're online, trying each model in turn
#   2) Local Ollama model when offline / rate-limited / Groq is down
#  Both get the same persona, facts, summary and your last 20 exchanges.
# =====================================================================
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OLLAMA = "http://127.0.0.1:11434"
_ollama_last_use = 0
_llm_warned = set()
_groq_down_until = 0       # internet / Groq trouble: skip Groq until this time
_groq_dead = False         # key rejected (401): skip Groq for the rest of this run
_model_cool = {}           # model -> time until we try it again (rate-limited / retired)
_pulling = set()           # local models currently downloading
LAST_BRAIN = ""            # which brain answered last (also printed to the console)

# Talk to Ollama directly, never via a Windows/system proxy (a proxy or VPN breaks 127.0.0.1 calls).
_local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _local_open(req, timeout=10):
    return _local_opener.open(req, timeout=timeout)


# ------------------------------------------------------------------ Groq (cloud)
class GroqError(Exception):
    def __init__(self, kind, msg="", wait=0):
        super().__init__(msg)
        self.kind, self.wait = kind, wait


def groq_available():
    return bool(GROQ_KEY) and not _groq_dead and time.time() >= _groq_down_until


def _groq_request(body):
    req = urllib.request.Request(
        GROQ_URL, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + GROQ_KEY, "Content-Type": "application/json",
                 "Accept": "application/json",
                 # Groq sits behind Cloudflare, which rejects Python's default user-agent with a 403
                 "User-Agent": "Ashie-Assistant/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "ignore")
        except Exception:
            detail = ""
        try:
            msg = json.loads(detail)["error"]["message"]
        except Exception:
            msg = detail[:200] or str(e)
        if e.code == 401:
            raise GroqError("auth", msg)
        if e.code == 429:
            wait = 60
            try:
                wait = max(5, min(3600, float(e.headers.get("retry-after", 60))))
            except Exception:
                pass
            raise GroqError("rate", msg, wait)
        if e.code == 404 or "decommission" in msg.lower() or "model_not_found" in detail:
            raise GroqError("gone", msg)
        if e.code == 403 or e.code >= 500:
            raise GroqError("net", f"HTTP {e.code}: {msg}")
        raise GroqError("bad", f"HTTP {e.code}: {msg}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise GroqError("net", repr(e))


def _groq_chat(model, messages, json_mode, max_tokens, temp, effort="low"):
    is_oss = "gpt-oss" in model
    ml = model.lower()
    thinky = is_oss or any(k in ml for k in ("qwen3", "deepseek-r1", "r1-distill", "reasoner", "-thinking"))
    # reasoning models "think" first and the thinking counts toward the limit, so leave headroom
    body = {"model": model, "messages": messages, "temperature": temp,
            "max_completion_tokens": max_tokens + (700 if thinky else 0)}
    extras = {}
    if is_oss:
        extras["reasoning_effort"] = effort
        extras["include_reasoning"] = False
    elif thinky:
        extras["reasoning_format"] = "hidden"
    if json_mode:
        extras["response_format"] = {"type": "json_object"}
    try:
        data = _groq_request({**body, **extras})
    except GroqError as e:
        if e.kind == "bad" and extras:
            data = _groq_request(body)      # a model rejected an optional setting: retry plainly
        else:
            raise
    try:
        text = (data["choices"][0]["message"].get("content") or "").strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()     # some models leak their reasoning
    except Exception:
        text = ""
    if not text:
        raise GroqError("empty", "empty reply")
    return text


_NOT_CHAT = ("whisper", "tts", "guard", "safeguard", "compound", "orpheus", "embed", "prompt", "distil")


def _size_b(mid):
    """Parameter count in billions parsed from a model id ('...-120b' -> 120). 0 if the id doesn't say."""
    m = re.search(r"(?<![a-z\d.])(\d+(?:\.\d+)?)b(?![a-z])", mid.lower())
    return float(m.group(1)) if m else 0.0


_BIG_HINT = re.compile(r"kimi|k2|maverick|405b|large|opus|gpt-oss-120b|qwen3-?(?:max|235)|\bpro\b", re.I)
_FAST_HINT = re.compile(r"instant|flash|mini|scout|lite|small|oss-20b", re.I)
_last_refresh = 0


def refresh_groq_models():
    """Asks Groq which models are live and prunes the lists, so a retired model can't break Ashie again."""
    global _last_refresh
    if not GROQ_KEY:
        return
    _last_refresh = time.time()
    try:
        req = urllib.request.Request("https://api.groq.com/openai/v1/models",
                                     headers={"Authorization": "Bearer " + GROQ_KEY, "User-Agent": "Ashie-Assistant/2.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            live_full = [m for m in json.loads(r.read().decode("utf-8")).get("data", []) if m.get("active", True)]
            live = [m["id"] for m in live_full]
    except Exception as e:
        print("(couldn't check Groq's model list - using the built-in names)", repr(e)[:100])
        return
    chat_ok = [m for m in live if not any(x in m.lower() for x in _NOT_CHAT)]
    if not chat_ok:
        return
    if GROQ_AUTO_MODELS:
        # rank by what Groq says is live right now: newest big model for quality, newest small one for speed
        created = {m["id"]: (m.get("created") or 0) for m in live_full}
        newest = lambda ids: sorted(ids, key=lambda i: created.get(i, 0), reverse=True)
        big = [m for m in chat_ok if _size_b(m) >= 60 or _BIG_HINT.search(m)]
        small = [m for m in chat_ok if m not in big]
        quick = [m for m in chat_ok if 0 < _size_b(m) <= 40 or _FAST_HINT.search(m)]
        best = newest(big) + newest(small)
        picks = {"chat": best, "code": best, "fast": newest(quick) + best}
        for name, chain in (("chat", GROQ_CHAT_MODELS), ("fast", GROQ_FAST_MODELS), ("code", GROQ_CODE_MODELS)):
            old = [m for m in chain if m in live]                      # your old picks stay as fallbacks
            merged = list(dict.fromkeys(picks[name][:3] + old))[:5]
            chain[:] = merged
    else:
        for name, chain in (("chat", GROQ_CHAT_MODELS), ("fast", GROQ_FAST_MODELS), ("code", GROQ_CODE_MODELS)):
            keep = [m for m in chain if m in live]
            if not keep:
                keep = chat_ok[:2]
                print(f"(groq {name}: none of my usual models are live, switching to {keep})")
            chain[:] = keep
    print("Groq models in use -> chat:", GROQ_CHAT_MODELS, "| fast:", GROQ_FAST_MODELS)


def groq_llm(messages, task="chat", json_mode=False, max_tokens=350, temp=0.5):
    global _groq_down_until, _groq_dead, LAST_BRAIN
    if not groq_available():
        return None
    chains = {"chat": GROQ_CHAT_MODELS, "fast": GROQ_FAST_MODELS, "code": GROQ_CODE_MODELS}
    effort = "medium" if task == "code" else "low"
    for model in chains.get(task, GROQ_CHAT_MODELS):
        if time.time() < _model_cool.get(model, 0):
            continue
        try:
            out = _groq_chat(model, messages, json_mode, max_tokens, temp, effort)
            LAST_BRAIN = "groq " + model
            print(f"(brain: {LAST_BRAIN})")
            return out
        except GroqError as e:
            print(f"(groq {model}: {e.kind} - {str(e)[:140]})")
            if e.kind == "auth":
                _groq_dead = True
                if "groqkey" not in _llm_warned:
                    _llm_warned.add("groqkey")
                    speak("Groq rejected my API key, so I'll use my offline brain. "
                          "Run ashie dot py with the set groq key option to fix it.")
                return None
            if e.kind == "net":
                _groq_down_until = time.time() + 30     # probably offline: go local, re-check in 30s
                return None
            if e.kind == "rate":
                _model_cool[model] = time.time() + e.wait
            elif e.kind == "gone":
                _model_cool[model] = time.time() + 3600
            # "bad" / "empty": just try the next model
    return None


# ------------------------------------------------------------------ Ollama (local)
def find_ollama():
    exe = shutil.which("ollama")
    if exe:
        return exe
    for p in (r"%LocalAppData%\Programs\Ollama\ollama.exe", r"%ProgramFiles%\Ollama\ollama.exe"):
        p = os.path.expandvars(p)
        if os.path.exists(p):
            return p
    return None


def ollama_up():
    try:
        with _local_open(urllib.request.Request(OLLAMA + "/api/tags"), timeout=1.5):
            return True
    except Exception:
        return False


def ensure_ollama():
    global _ollama_last_use
    if ollama_up():
        _ollama_last_use = time.time()
        return True
    exe = find_ollama()
    if not exe:
        if "noollama" not in _llm_warned:
            _llm_warned.add("noollama")
            speak("My offline brain, Ollama, isn't installed yet. Check the setup steps.")
        return False
    env = os.environ.copy()
    env.update({"OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_NUM_PARALLEL": "1",
                "OLLAMA_KEEP_ALIVE": KEEP_ALIVE, "OLLAMA_FLASH_ATTENTION": "1"})
    print("Starting local AI engine...")
    try:
        subprocess.Popen([exe, "serve"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        print("(couldn't start Ollama)", e)
        return False
    for _ in range(60):
        if ollama_up():
            _ollama_last_use = time.time()
            return True
        time.sleep(0.5)
    print("(Ollama didn't come up within 30 seconds)")
    return False


def stop_ollama():
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower().startswith("ollama"):
                p.terminate()
        except Exception:
            pass


def maybe_stop_ollama():
    if _pulling:
        return
    if STOP_OLLAMA_WHEN_IDLE and _ollama_last_use and time.time() - _ollama_last_use > OLLAMA_IDLE_STOP_SECONDS:
        if ollama_up():
            print("(AI engine idle - shutting it down to free RAM/GPU)")
            stop_ollama()


def local_models():
    try:
        with _local_open(urllib.request.Request(OLLAMA + "/api/tags"), timeout=4) as r:
            return [m.get("name", "") for m in json.loads(r.read()).get("models", [])]
    except Exception:
        return []


def _has_model(name, installed):
    return name in installed or (":" not in name and name + ":latest" in installed)


def _internet_up():
    import socket
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2).close()
        return True
    except Exception:
        return False


def _pull_worker(model):
    global _ollama_last_use
    try:
        speak(f"I'm downloading my offline brain, {model}, in the background. It can take a few minutes.")
        req = urllib.request.Request(OLLAMA + "/api/pull", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"model": model, "name": model, "stream": True}).encode())
        last = 0
        with _local_open(req, timeout=900) as r:
            for line in r:
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                _ollama_last_use = time.time()
                if j.get("error"):
                    raise RuntimeError(j["error"])
                if j.get("total") and time.time() - last > 5:
                    print(f"   downloading {model}: {int(j.get('completed', 0) * 100 / j['total'])}%")
                    last = time.time()
                if j.get("status") == "success":
                    speak("My offline brain is ready.")
                    return
    except Exception as e:
        print("(offline model download failed)", e)
        _model_cool["pull:" + model] = time.time() + 600
    finally:
        _pulling.discard(model)


def _auto_pull(model):
    if model in _pulling or time.time() < _model_cool.get("pull:" + model, 0):
        return
    if not _internet_up():
        if ("nomodel:" + model) not in _llm_warned:
            _llm_warned.add("nomodel:" + model)
            speak(f"I'm missing my offline brain {model}. When you're online, run: ollama pull {model}")
        return
    _pulling.add(model)
    threading.Thread(target=_pull_worker, args=(model,), daemon=True).start()


def pick_local_model(wanted):
    """Returns an installed model to use; downloads the main one in the background if missing."""
    installed = local_models()
    if _has_model(wanted, installed):
        return wanted
    if wanted == LLM_MODEL:
        _auto_pull(wanted)
    elif ("nocode:" + wanted) not in _llm_warned:
        _llm_warned.add("nocode:" + wanted)
        print(f"(coding model {wanted} isn't installed - using {LLM_MODEL}. To install: ollama pull {wanted})")
    if _has_model(LLM_MODEL, installed):
        return LLM_MODEL
    pool = [m for m in installed if "embed" not in m.lower()]
    return pool[0] if pool else None


def _fit_for_local(messages, max_tokens):
    """Ollama silently chops the START of a prompt that doesn't fit the context window, which
    deletes the persona. Keep the system prompt + newest messages and drop old history instead."""
    budget = max(1500, (LOCAL_CTX - max_tokens - 150) * 3)      # ~3 chars per token, on the safe side
    msgs = [dict(m) for m in messages]

    def total():
        return sum(len(m["content"]) for m in msgs)

    while total() > budget and len(msgs) > 2:
        i = next((k for k, m in enumerate(msgs[:-1]) if m["role"] != "system"), None)
        if i is None:
            break
        del msgs[i]
        while i < len(msgs) - 1 and msgs[i]["role"] == "assistant":
            del msgs[i]
    if total() > budget:                                          # still too big: trim the longest message
        longest = max(msgs, key=lambda m: len(m["content"]))
        longest["content"] = longest["content"][:max(500, len(longest["content"]) - (total() - budget))]
    return msgs


def local_llm(messages, task="chat", json_mode=False, max_tokens=350, temp=0.5, model=None):
    global _ollama_last_use, LAST_BRAIN
    if not ensure_ollama():
        return None
    wanted = model or (CODE_MODEL if task == "code" and CODE_MODEL else LLM_MODEL)
    use = pick_local_model(wanted)
    if not use:
        return None
    body = {"model": use, "messages": _fit_for_local(messages, max_tokens), "stream": False,
            "keep_alive": KEEP_ALIVE,
            "options": {"num_ctx": LOCAL_CTX, "num_predict": max_tokens, "temperature": temp}}
    if json_mode:
        body["format"] = "json"
    try:
        req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with _local_open(req, timeout=240) as r:
            out = json.loads(r.read())["message"]["content"].strip()
        _ollama_last_use = time.time()
        LAST_BRAIN = "local " + use
        print(f"(brain: {LAST_BRAIN})")
        return out or None
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            detail = ""
        print("(local AI error)", e.code, detail)
        if e.code == 404:
            _auto_pull(use)
        return None
    except Exception as e:
        print("(local AI error)", repr(e))
        return None


# ------------------------------------------------------------------ one entry point
def llm(messages, json_mode=False, model=None, max_tokens=350, temp=0.5, ctx=None, task="chat"):
    """task: 'chat' (best quality), 'fast' (routing / agent JSON), 'code'.
    Groq first when online, then the local model. `ctx` is accepted but ignored (a constant
    context size keeps Ollama from reloading the model between calls)."""
    out = None
    if model is None:
        out = groq_llm(messages, task, json_mode, max_tokens, temp)
    return out or local_llm(messages, task, json_mode, max_tokens, temp, model)


def llm_json(messages, **kw):
    out = llm(messages, json_mode=True, **kw)
    if not out:
        return None
    try:
        return json.loads(out)
    except Exception:
        m = re.search(r"\{.*\}", out, re.S)
        try:
            return json.loads(m.group()) if m else None
        except Exception:
            return None


def persona(query=None):
    big = groq_available()                      # cloud model has room for more memory than the local one
    facts = "\n".join("- " + f for f in MEM["facts"][-(60 if big else 15):]) or "- (nothing yet)"
    summary = (MEM.get("summary") or "").strip()
    earlier = f"\nWhat you remember from older conversations: {summary}" if summary else ""
    now = datetime.datetime.now().strftime("%A %d %B %Y, %I:%M %p")
    return (f"You are {ASSISTANT_NAME}, a cheerful, witty young woman who is the voice assistant living on "
            f"{USER_NAME}'s Windows PC. {USER_NAME} is the person you work for. {USER_PROFILE} "
            "Talk like a close friend: warm, casual and brief (1 to 3 short sentences). Your replies are spoken "
            "aloud, so never use markdown, lists, code blocks or emojis. Use the conversation so far to understand "
            "follow-up questions. If you don't know something, say so honestly. "
            "Be honest about actions: you can only really do things through your skills (writing documents, emailing with real "
            "attachments, finding files, using chatbot websites, making notes). In a plain chat reply you have NOT done anything on "
            "the PC, so never say you attached, sent, saved or opened something. If they ask for an action, tell them to say it as a "
            "command, for example: Ashie, attach it to the mail. "
            f"Things you remember about {USER_NAME}:\n{facts}{earlier}\n{artifacts_block()}{style_block()}"
            f"{recall_block(query) if query else ''}\nNow: {now}.")


def chat_reply(text):
    msgs = [{"role": "system", "content": persona(text)}] + history_messages() + [{"role": "user", "content": text}]
    return llm(msgs, max_tokens=220, temp=0.7)


def set_groq_key():
    """python ashie.py --set-groq-key : saves your key to %USERPROFILE%\\Ashie\\groq_key.txt and tests it."""
    global GROQ_KEY, _groq_dead, _groq_down_until
    key = input("Paste your Groq API key (starts with gsk_) and press Enter: ").strip().strip('"')
    if not key.startswith("gsk_") or len(key) < 20:
        print("That doesn't look like a Groq key (it should start with gsk_). Nothing saved.")
        return
    GROQ_KEY_PATH.write_text(key, encoding="utf-8")
    GROQ_KEY, _groq_dead, _groq_down_until = key, False, 0
    print(f"Saved to {GROQ_KEY_PATH}. Testing...")
    test_brain()


def test_brain():
    """python ashie.py --test-brain : checks the Groq models, the local model and your memory."""
    print("=== Brain test ===")
    if GROQ_KEY:
        print(f"Groq key found (ends ...{GROQ_KEY[-4:]}).")
        seen = []
        for model in GROQ_CHAT_MODELS + GROQ_FAST_MODELS + GROQ_CODE_MODELS:
            if model in seen:
                continue
            seen.append(model)
            t0 = time.time()
            try:
                out = _groq_chat(model, [{"role": "user", "content": "Reply with just the word: pong"}],
                                 False, 20, 0.0)
                print(f"  OK    {model:34s} {time.time() - t0:.1f}s  -> {out[:30]!r}")
            except GroqError as e:
                print(f"  FAIL  {model:34s} {e.kind}: {str(e)[:110]}")
    else:
        print("No Groq key. Run:  python ashie.py --set-groq-key   (or set the GROQ_API_KEY environment variable)")
    print("--- Local AI (Ollama) ---")
    exe = find_ollama()
    print("Ollama:", exe or "NOT INSTALLED (download from ollama.com)")
    if exe and ensure_ollama():
        inst = local_models()
        print("Installed models:", ", ".join(inst) or "(none)")
        for m in [LLM_MODEL] + ([CODE_MODEL] if CODE_MODEL else []):
            print(f"  {'OK     ' if _has_model(m, inst) else 'MISSING'} {m}" +
                  ("" if _has_model(m, inst) else f"   -> run: ollama pull {m}"))
        if _has_model(LLM_MODEL, inst):
            t0 = time.time()
            out = local_llm([{"role": "user", "content": "Reply with just the word: pong"}], max_tokens=10, temp=0.0)
            print(f"  local chat test: {out!r} in {time.time() - t0:.1f}s (first call loads the model, so it's slower)")
    print(f"--- Memory ---\n{len(HISTORY)} recent exchanges, {len(MEM['facts'])} facts, "
          f"{len(EPI)} older memories ({EPI.backend} embeddings), summary: {'yes' if MEM.get('summary') else 'none yet'}, "
          f"style profile: {'yes' if MEM.get('profile') else 'not yet'}   (files in {DATA})")


# =====================================================================
#  SCREEN: OCR + UI AUTOMATION
# =====================================================================
_ocr = None
OCR_OK = True


def screen_ocr():
    """Returns list of (text, (cx, cy), (x0, y0, x1, y1))."""
    global _ocr, OCR_OK
    if not OCR_OK:
        return []
    try:
        if _ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            _ocr = RapidOCR()
        arr = np.array(pyautogui.screenshot())
        res, _ = _ocr(arr)
        out = []
        for box, text, _score in (res or []):
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            out.append((text, (int(sum(xs) / 4), int(sum(ys) / 4)), (min(xs), min(ys), max(xs), max(ys))))
        return out
    except ImportError:
        OCR_OK = False
        print("(OCR not installed: pip install rapidocr-onnxruntime)")
        return []
    except Exception as e:
        print("(ocr error)", e)
        return []


def wait_for_text(patterns, timeout=60):
    if not OCR_OK:
        time.sleep(min(timeout, 8))
        return "assumed"
    end = time.time() + timeout
    while time.time() < end:
        blob = " | ".join(t.lower() for t, *_ in screen_ocr())
        for p in patterns:
            if p in blob:
                return p
        time.sleep(0.5)
    return None


def active_title():
    try:
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        return buf.value
    except Exception:
        return ""


def is_browser():
    return any(b in active_title().lower() for b in ("chrome", "edge", "firefox", "brave", "opera"))


def do_click(x, y, clicks=1, button="left"):
    pyautogui.moveTo(x, y, duration=0.2)
    pyautogui.click(clicks=clicks, button=button)


CLICKABLE = {"Button", "Hyperlink", "MenuItem", "ListItem", "TabItem", "CheckBox", "RadioButton",
             "SplitButton", "ComboBox", "Edit", "TreeItem", "DataItem"}


def uia_elements():
    from pywinauto import Desktop
    win = Desktop(backend="uia").window(active_only=True).wrapper_object()
    out = []
    for el in win.descendants():
        try:
            name = (el.element_info.name or "").strip()
            if not name:
                continue
            r = el.rectangle()
            if r.width() <= 0 or r.height() <= 0 or not el.is_visible():
                continue
            out.append((name, el.element_info.control_type, r))
        except Exception:
            continue
    return out


def uia_click(label, clicks=1, button="left"):
    try:
        items = uia_elements()
    except Exception:
        return False
    best, score = None, 0
    for name, ctype, r in items:
        n = name.lower()
        if n == label:
            s = 1.0
        elif label in n:
            s = 0.6 + 0.35 * len(label) / len(n)
        else:
            s = difflib.SequenceMatcher(None, label, n).ratio() * 0.9
        if ctype in CLICKABLE:
            s += 0.05
        if s > score:
            best, score = r, s
    if best is not None and score >= 0.65:
        do_click((best.left + best.right) // 2, (best.top + best.bottom) // 2, clicks, button)
        return True
    return False


def ocr_click(label, clicks=1, button="left"):
    best, score = None, 0
    for text, _c, (x0, y0, x1, y1) in screen_ocr():
        tl = text.lower()
        if label in tl:
            s = 1.0
            idx = tl.index(label)
            cx = x0 + (idx + len(label) / 2) / max(len(tl), 1) * (x1 - x0)
        else:
            s = difflib.SequenceMatcher(None, label, tl).ratio()
            cx = (x0 + x1) / 2
        if s > score:
            best, score = (int(cx), int((y0 + y1) / 2)), s
    if best and score >= 0.8:
        do_click(*best, clicks, button)
        return True
    return False


def click_label(label, clicks=1, button="left", quiet=False):
    label = label.strip().lower()
    order = (ocr_click, uia_click) if is_browser() else (uia_click, ocr_click)
    for fn in order:
        if fn(label, clicks, button):
            if not quiet:
                speak(f"Clicked {label}.")
            return True
    if is_browser():  # last resort: browser find-in-page then activate focused link
        pyautogui.hotkey("ctrl", "f")
        time.sleep(0.2)
        type_text(label)
        pyautogui.press("enter")
        time.sleep(0.2)
        pyautogui.press("escape")
        pyautogui.press("enter")
        if not quiet:
            speak(f"I tried opening {label}.")
        return True
    if not quiet:
        speak(f"I couldn't find {label} on the screen.")
    return False


def list_clickables():
    names = []
    try:
        if is_browser():
            names = [t for t, *_ in screen_ocr() if 2 < len(t) < 40]
        else:
            names = [n for n, ct, _ in uia_elements() if ct in CLICKABLE and len(n) < 50]
    except Exception:
        pass
    names = list(dict.fromkeys(names))
    speak("I can see: " + ", ".join(names[:12]) if names else "I couldn't read anything clickable here.")


def clean_label(r):
    """'the AI mode button on chrome' -> 'ai mode'"""
    r = (r or "").lower().strip().strip("\"'. ")
    prev = None
    while prev != r:
        prev = r
        r = re.sub(r"\s+(?:on|in|at) (?:the |my |this )?(?:screen|page|browser|chrome|window|tab|website|site)$", "", r)
        r = re.sub(r"^(?:on|the|a|an|that|button|link|option|icon|says|saying|reads|called|named)\s+", "", r)
        r = re.sub(r"\s+(?:button|link|icon|option)$", "", r).strip()
    return r


def ocr_screen_text():
    """All visible text on the screen in natural reading order (rows top to bottom, left to right)."""
    items = screen_ocr()
    if not items:
        return ""
    heights = sorted(b[3] - b[1] for _t, _c, b in items)
    med = heights[len(heights) // 2] or 10
    items.sort(key=lambda it: it[1][1])
    rows, cur, cur_y = [], [], None
    for it in items:
        cy = it[1][1]
        if cur_y is None or abs(cy - cur_y) <= med * 0.6:
            cur.append(it)
            cur_y = sum(i[1][1] for i in cur) / len(cur)
        else:
            rows.append(cur)
            cur, cur_y = [it], cy
    if cur:
        rows.append(cur)
    return "\n".join(" ".join(i[0] for i in sorted(r, key=lambda it: it[2][0])) for r in rows)


def skill_copy_screen():
    if not OCR_OK:
        return speak("My screen reader isn't installed. Run: pip install rapidocr-onnxruntime")
    text = ocr_screen_text()
    if not text.strip():
        return speak("I couldn't read any text on the screen.")
    pyperclip.copy(text)
    speak(f"Copied about {len(text.split())} words from the screen. Paste it anywhere.")


def skill_click(label):
    label = clean_label(label) or clean_label(ask("Which button should I press?"))
    if label:
        click_label(label)


def tab_by_name(name):
    """Switch to the browser tab whose title contains `name`."""
    name = (name or "").lower().strip()
    if not name:
        return False
    try:
        from pywinauto import Desktop
        win = Desktop(backend="uia").window(active_only=True).wrapper_object()
        best, best_len = None, 10 ** 9
        for el in win.descendants(control_type="TabItem"):
            title = (el.window_text() or "").lower()
            if name in title and len(title) < best_len:
                best, best_len = el, len(title)
        if best is not None:
            r = best.rectangle()
            do_click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
            return True
    except Exception as e:
        print("(tab lookup via UI Automation failed, trying the screen)", type(e).__name__)
    W, H = pyautogui.size()                       # fallback: read the tab strip with OCR
    for text, (cx, cy), _ in screen_ocr():
        if cy < H * 0.12 and name in text.lower():
            do_click(cx, cy)
            return True
    return False


def skill_tab(target):
    t = re.sub(r"\b(the|a|my|browser|chrome|tab|tabs|window|one)\b", " ", (target or "next").lower())
    t = re.sub(r"\s+", " ", t).strip() or "next"
    if t in ("next", "forward", "right", "other", "another"):
        pyautogui.hotkey("ctrl", "tab")
    elif t in ("previous", "prior", "prev", "back", "left"):
        pyautogui.hotkey("ctrl", "shift", "tab")
    elif t == "last":
        pyautogui.hotkey("ctrl", "9")
    elif t == "first":
        pyautogui.hotkey("ctrl", "1")
    elif t == "new":
        pyautogui.hotkey("ctrl", "t")
    elif re.fullmatch(r"\d+|one|two|three|four|five|six|seven|eight", t):
        pyautogui.hotkey("ctrl", str(min(num(t), 8)))
    elif not tab_by_name(t):
        speak(f"I couldn't find a tab called {t}.")


def type_text(s):
    pyperclip.copy(s)
    pyautogui.hotkey("ctrl", "v")


def focus_window(name):
    try:
        from pywinauto import Desktop
        for w in Desktop(backend="uia").windows():
            if name in w.window_text().lower() and w.window_text():
                if w.is_minimized():
                    w.restore()
                w.set_focus()
                return True
    except Exception:
        pass
    return False


# =====================================================================
#  APPS, CHROME, WEB
# =====================================================================
APPS = {
    "notepad": "notepad", "calculator": "calc", "paint": "mspaint", "file explorer": "explorer",
    "explorer": "explorer", "files": "explorer", "task manager": "taskmgr", "command prompt": "cmd",
    "cmd": "cmd", "terminal": "wt", "powershell": "powershell", "control panel": "control",
    "edge": "msedge", "firefox": "firefox", "brave": "brave", "word": "winword", "excel": "excel",
    "powerpoint": "powerpnt", "outlook": "outlook", "vs code": "code", "vscode": "code",
    "visual studio code": "code", "snipping tool": "snippingtool", "settings": "ms-settings:",
    "spotify": "spotify", "discord": "discord", "steam": "steam", "camera": "microsoft.windows.camera:",
    "clock": "ms-clock:", "store": "ms-windows-store:", "photos": "ms-photos:",
}
PROCS = {"chrome": "chrome.exe", "google chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe",
         "notepad": "notepad.exe", "word": "WINWORD.EXE", "excel": "EXCEL.EXE", "powerpoint": "POWERPNT.EXE",
         "spotify": "Spotify.exe", "discord": "Discord.exe", "vs code": "Code.exe", "vscode": "Code.exe"}
SITES = {"youtube": "https://youtube.com", "google": "https://google.com", "gmail": "https://mail.google.com",
         "facebook": "https://facebook.com", "twitter": "https://x.com", "x": "https://x.com",
         "instagram": "https://instagram.com", "reddit": "https://reddit.com", "github": "https://github.com",
         "linkedin": "https://linkedin.com", "netflix": "https://netflix.com", "amazon": "https://amazon.in",
         "wikipedia": "https://wikipedia.org", "maps": "https://maps.google.com", "chatgpt": "https://chatgpt.com",
         "whatsapp": "https://web.whatsapp.com", "whatsapp web": "https://web.whatsapp.com",
         "drive": "https://drive.google.com", "stack overflow": "https://stackoverflow.com"}
SETTINGS = {"wifi": "network-wifi", "wi-fi": "network-wifi", "network": "network", "bluetooth": "bluetooth",
            "display": "display", "sound": "sound", "battery": "batterysaver", "apps": "appsfeatures",
            "update": "windowsupdate", "privacy": "privacy", "personalization": "personalization",
            "default apps": "defaultapps", "storage": "storagesense", "notifications": "notifications",
            "keyboard": "typing", "mouse": "mousetouchpad", "time": "dateandtime", "accounts": "yourinfo"}
FOLDERS = {"downloads": "Downloads", "documents": "Documents", "desktop": "Desktop", "pictures": "Pictures",
           "music": "Music", "videos": "Videos"}


def sh(cmd):
    subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def find_chrome():
    for p in (r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
              r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
              r"%LocalAppData%\Google\Chrome\Application\chrome.exe"):
        p = os.path.expandvars(p)
        if os.path.exists(p):
            return p
    return shutil.which("chrome")


def open_chrome(url=None):
    """Always opens Chrome in your chosen profile, so the 'choose account' screen never appears."""
    exe = find_chrome()
    if not exe:
        webbrowser.open(url or "https://www.google.com")
        return
    subprocess.Popen([exe, f"--profile-directory={CHROME_PROFILE}", "--no-first-run",
                      "--no-default-browser-check", url or "https://www.google.com"])
    for _ in range(40):
        if "chrome" in active_title().lower():
            break
        time.sleep(0.25)
    time.sleep(0.8)


def open_app(name):
    target = APPS.get(name)
    if name in ("chrome", "google chrome"):
        open_chrome()
    elif target:
        sh(f'start "" "{target}"' if target.endswith(":") else f"start {target}")
    else:  # universal fallback: Start menu search
        pyautogui.press("win")
        time.sleep(0.7)
        type_text(name)
        time.sleep(1.0)
        pyautogui.press("enter")
    speak(f"Opening {name}.")


def ddgs_cls():
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        from duckduckgo_search import DDGS
        return DDGS


_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/126.0 Safari/537.36", "Accept-Language": "en-IN,en;q=0.9"}


def _http_get(url, timeout=10):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _rss_items(url, n):
    """Reads an RSS feed with the standard library only (no packages that can break)."""
    root = ET.fromstring(_http_get(url))
    out = []
    for it in root.iter("item"):
        title = html.unescape(it.findtext("title") or "").strip()
        body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))).strip()
        if title:
            out.append({"title": title, "body": body or title, "href": (it.findtext("link") or "").strip()})
        if len(out) >= n:
            break
    return out


def _google_news(q, n):
    return _rss_items("https://news.google.com/rss/search?q=" + urllib.parse.quote(q) +
                      "&hl=en-IN&gl=IN&ceid=IN:en", n)


def _bing_rss(q, n):
    return _rss_items("https://www.bing.com/search?format=rss&setlang=en-IN&q=" + urllib.parse.quote(q), n)


def _ddgs_search(q, n, news=False):
    """ddgs is flaky ('No results found' when one backend is blocked), so try several settings."""
    cls = ddgs_cls()
    tries = [{}, {"region": "in-en"}, {"region": "wt-wt"}] if news else \
            [{}, {"region": "in-en"}, {"backend": "duckduckgo"}, {"backend": "bing"}, {"backend": "brave"},
             {"backend": "google"}, {"backend": "yahoo"}]
    last = None
    for kw in tries:
        try:
            d = cls()
            res = list(d.news(q, max_results=n, **kw) if news else d.text(q, max_results=n, **kw))
            if res:
                return [{"title": r.get("title", ""), "body": r.get("body", "") or r.get("title", ""),
                         "href": r.get("href") or r.get("url", "")} for r in res]
        except Exception as e:
            last = e
    if last:
        raise last
    return []


NEWSY = re.compile(r"\b(news|headlines?|latest|breaking|today'?s|current events|happening)\b", re.I)


def web_search(q, n=5, news=None):
    """Tries several free sources in order until one gives results. Returns [{'title','body','href'}]."""
    q = (q or "").strip()
    if not q:
        return []
    news = bool(NEWSY.search(q)) if news is None else news
    steps = [("ddgs news", lambda: _ddgs_search(q, n, True)), ("google news", lambda: _google_news(q, n)),
             ("bing", lambda: _bing_rss(q, n)), ("ddgs", lambda: _ddgs_search(q, n))] if news else \
            [("ddgs", lambda: _ddgs_search(q, n)), ("bing", lambda: _bing_rss(q, n)),
             ("google news", lambda: _google_news(q, n))]
    for name, fn in steps:
        try:
            res = fn()
            if res:
                print(f"(search via {name}: {len(res)} results)")
                return res[:n]
            print(f"(search via {name}: nothing)")
        except Exception as e:
            print(f"(search via {name} failed: {type(e).__name__}: {str(e)[:90]})")
    return []


# =====================================================================
#  SMALL UTILITIES
# =====================================================================
def after(t, *keys):
    for k in keys:
        if t == k:
            return ""
        if t.startswith(k + " "):
            return t[len(k) + 1:].strip()
    return None


def num(s, default=1):
    m = re.search(r"\d+", s or "")
    if m:
        return int(m.group())
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10}
    for w, v in words.items():
        if w in (s or "").split():
            return v
    return default


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.Mod: operator.mod}


def safe_eval(expr):
    for a, b in (("multiplied by", "*"), ("divided by", "/"), ("times", "*"), ("into", "*"), ("plus", "+"),
                 ("minus", "-"), ("power", "**"), ("x", "*"), ("mod", "%")):
        expr = expr.replace(a, b)

    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError
    return ev(ast.parse(expr.strip(), mode="eval").body)


def parse_duration(t):
    total = 0
    for n, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(hour|hr|minute|min|second|sec)", t):
        n = float(n)
        total += n * (3600 if unit.startswith(("hour", "hr")) else 60 if unit.startswith("min") else 1)
    return int(total)


def spoken_to_email(s):
    s = s.lower().strip().rstrip(".")
    s = re.sub(r"\s+at the rate(?: of)?\s+|\s+at\s+", "@", s)
    s = re.sub(r"\s+dot\s+", ".", s)
    for a, b in ((" underscore ", "_"), (" dash ", "-"), (" hyphen ", "-")):
        s = s.replace(a, b)
    return s.replace(" ", "")


def valid_email(e):
    return bool(re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", e or ""))


def spell_email(e):
    sym = {".": "dot", "@": "at", "_": "underscore", "-": "dash"}
    return " ".join(sym.get(c, c) for c in e)


def find_contact(name):
    name = (name or "").lower().strip()
    if name in CONTACTS:
        return CONTACTS[name]
    close = difflib.get_close_matches(name, list(CONTACTS), n=1, cutoff=0.8)
    return CONTACTS[close[0]] if close else None


def safe_name(s):
    return re.sub(r"[^\w\- ]", "", s)[:40].strip().replace(" ", "_") or "ashie_doc"


# =====================================================================
#  DEVICE CONTROLS (volume / brightness / screenshot)
# =====================================================================
def _volume_iface():
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    speakers = AudioUtilities.GetSpeakers()
    iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def set_mute(muted):
    try:
        _volume_iface().SetMute(1 if muted else 0, None)
        speak("Muted." if muted else "Unmuted.")
    except Exception as e:
        speak("I couldn't change the volume."); print("(volume error)", e)


def set_volume(pct):
    try:
        v = max(0, min(100, int(pct)))
        _volume_iface().SetMasterVolumeLevelScalar(v / 100, None)
        speak(f"Volume set to {v} percent.")
    except Exception as e:
        speak("I couldn't change the volume."); print("(volume error)", e)


def nudge_volume(delta_pct):
    try:
        iface = _volume_iface()
        cur = iface.GetMasterVolumeLevelScalar()
        new = max(0.0, min(1.0, cur + delta_pct / 100))
        iface.SetMasterVolumeLevelScalar(new, None)
        speak("Okay.")
    except Exception as e:
        speak("I couldn't change the volume."); print("(volume error)", e)


def set_brightness(pct):
    try:
        import screen_brightness_control as sbc
        sbc.set_brightness(max(0, min(100, int(pct))))
        speak(f"Brightness set to {pct} percent.")
    except Exception as e:
        speak("I couldn't change the brightness on this screen."); print("(brightness error)", e)


def nudge_brightness(delta_pct):
    try:
        import screen_brightness_control as sbc
        cur = sbc.get_brightness()[0]
        sbc.set_brightness(max(0, min(100, cur + delta_pct)))
        speak("Okay.")
    except Exception as e:
        speak("I couldn't change the brightness on this screen."); print("(brightness error)", e)


def take_screenshot():
    try:
        path = DATA / f"screenshot_{datetime.datetime.now():%Y%m%d_%H%M%S}.png"
        pyautogui.screenshot().save(str(path))
        speak("Done. Saved to your Ashie folder.")
    except Exception as e:
        speak("I couldn't take a screenshot."); print("(screenshot error)", e)


# =====================================================================
#  SKILLS
# =====================================================================
def header_has(name):
    W, H = pyautogui.size()
    tokens = [t for t in re.split(r"\W+", name.lower()) if len(t) > 1]
    for text, (cx, cy), _ in screen_ocr():
        if cy < H * 0.22 and cx > W * 0.25 and any(t in text.lower() for t in tokens):
            return True
    return False


def skill_whatsapp(contact, message):
    contact = (contact or "").strip() or ask("Who should I message?")
    if not contact:
        return speak("Okay, never mind.")
    message = (message or "").strip() or ask(f"What should I say to {contact}?")
    if not message:
        return speak("Okay, never mind.")
    info = find_contact(contact)
    phone = re.sub(r"\D", "", info.get("phone", "")) if info else ""
    speak(f"Opening WhatsApp in Chrome for {contact}.")
    if phone:
        open_chrome(f"https://web.whatsapp.com/send?phone={phone}&text={urllib.parse.quote(message)}")
    else:
        open_chrome("https://web.whatsapp.com/")

    warned = False
    end = time.time() + 180
    ready = None
    while time.time() < end:
        blob = " | ".join(t.lower() for t, *_ in screen_ocr()) if OCR_OK else ""
        if any(k in blob for k in ("type a message", "search or start", "or search", "chats")):
            ready = True
            break
        if any(k in blob for k in ("scan", "qr code", "link with phone", "use whatsapp on your computer")) and not warned:
            speak("WhatsApp wants a QR scan. Scan it once with your phone and I'll continue.")
            warned = True
        if not OCR_OK:
            time.sleep(10)
            ready = True
            break
        time.sleep(1)
    if not ready:
        return speak("WhatsApp Web didn't load in time.")
    time.sleep(2.5)

    if not phone:
        pyautogui.hotkey("ctrl", "alt", "/")  # focus WhatsApp search
        time.sleep(0.6)
        type_text(contact)
        time.sleep(2.2)
        pyautogui.press("enter")
        time.sleep(1.8)
        if OCR_OK and not header_has(contact):
            if not confirm(f"I couldn't confirm the open chat is {contact}. Continue anyway?"):
                return
        type_text(message)
        time.sleep(0.6)
        if OCR_OK and message[:10].lower() not in " ".join(t.lower() for t, *_ in screen_ocr()):
            click_label("type a message", quiet=True)
            type_text(message)

    shown = message if len(message) < 200 else message[:200] + "..."
    if confirm(f"I've typed this message to {contact}: {shown}. Should I send it?"):
        pyautogui.press("enter")
        speak("Sent!")
    else:
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("backspace")


def draft_email(instructions, subject_hint, to_name):
    out = llm([{"role": "system", "content": persona()},
               {"role": "user", "content": (
                   f"Write a short, polite, natural email from {USER_NAME}"
                   f"{' to ' + to_name if to_name else ''}. What it should say: {instructions}. "
                   f"{'Subject: ' + subject_hint + '. ' if subject_hint else ''}"
                   f"Under 120 words, plain text, sign off with '{USER_NAME}'. "
                   "Reply exactly in this format:\nSUBJECT: <subject>\nBODY:\n<email body>")}],
              max_tokens=400, temp=0.6)
    if out:
        m = re.search(r"SUBJECT:\s*(.+?)\s*\n+\s*BODY:\s*(.+)", out, re.S | re.I)
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return (subject_hint or "Hello"), (instructions or "Hello!")


def write_in_word(text, name="Ashie document"):
    """Writes the text into a real .docx (always saved to disk), opens it in Word and returns the file path."""
    path = str(DOCS / f"{safe_name(name)}_{datetime.datetime.now():%Y%m%d_%H%M%S}.docx")
    try:
        import win32com.client
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = True
        doc = word.Documents.Add()
        doc.Content.Text = text.replace("\r\n", "\n").replace("\n", "\r")
        doc.SaveAs2(path)
        try:
            word.Activate()
        except Exception:
            pass
        if os.path.exists(path):
            return path
    except Exception as e:
        print("(Word COM failed, writing the .docx directly)", e)
    try:
        write_docx_file(path, text)
        os.startfile(path)
        return path
    except Exception as e:
        print("(couldn't write or open the document)", e)
        return None


def skill_write_doc(instructions):
    instructions = (instructions or "").strip() or ask("What should I write?")
    if not instructions:
        return
    speak("On it. Writing now.")
    out = llm([{"role": "system", "content": persona()},
               {"role": "user", "content": f"Write this for {USER_NAME}: {instructions}\n"
                                           "Plain text only, no markdown symbols. Start with a short title line."}],
              max_tokens=900, temp=0.6, ctx=4096)
    if not out:
        return speak("I couldn't reach my brain for that one.")
    title = re.sub(r"[#*]+", "", out.strip().splitlines()[0]).strip()[:60] or "Document"
    p = write_in_word(out, title)
    remember_artifact("doc", title, p, out)
    note_action(f"wrote the document '{title}' ({len(out.split())} words)" + (f", saved at {p}" if p else ", but could not save it as a file"))
    speak("Done. It's open in Word and saved in your Ashie documents folder. Tell me who to send it to whenever you like."
          if p else "I wrote it, but I couldn't save it as a file.")


def skill_code(instructions):
    instructions = (instructions or "").strip() or ask("What should I code?")
    if not instructions:
        return
    speak("Let me write that.")
    out = llm([{"role": "system", "content": "You are an expert programmer. Reply with the first line "
                                             "'FILENAME: <name.ext>' then ONE fenced code block with the full "
                                             "working code, then nothing else."},
               {"role": "user", "content": instructions}],
              task="code", max_tokens=1200, temp=0.2)
    if not out:
        return speak("I couldn't reach my coding brain.")
    fm = re.search(r"FILENAME:\s*([\w.\-]+)", out)
    cm = re.search(r"```[\w+#-]*\n(.*?)```", out, re.S)
    code = (cm.group(1) if cm else out).strip()
    name = fm.group(1) if fm else "ashie_script.py"
    path = PROJECTS / name
    path.write_text(code + "\n", encoding="utf-8")
    code_exe = shutil.which("code")
    if code_exe:
        subprocess.Popen([code_exe, str(path)], shell=True)
    else:
        os.startfile(str(path))
    speak(f"Done. I saved {name} in your Ashie projects folder.")
    if name.endswith(".py") and confirm("Do you want me to run it now?"):
        try:
            r = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=30,
                               cwd=str(PROJECTS))
            speak((r.stdout or r.stderr or "It ran with no output.").strip()[:250])
        except Exception as e:
            speak(f"It didn't finish: {e}")


def skill_web(q):
    res = web_search(q)
    if not res:
        out = llm([{"role": "system", "content": persona()},
                   {"role": "user", "content": f"{q}\n\n(Live web search failed just now. Answer in 1-3 spoken "
                                               "sentences from what you know, and mention briefly that it "
                                               "might be out of date.)"}], max_tokens=200)
        return speak(out or "I couldn't search the web right now.")
    ctx = "\n".join(f"- {r.get('title', '')}: {r.get('body', '')}" for r in res)
    out = llm([{"role": "system", "content": persona()},
               {"role": "user", "content": f"Question: {q}\n\nWeb results:\n{ctx}\n\nAnswer in 1-3 spoken "
                                           "sentences using only these results."}], max_tokens=200)
    speak(out or res[0].get("body", "I found results but couldn't summarise them."))


def skill_weather(city):
    city = (city or DEFAULT_CITY or "").strip()
    try:
        url = "https://wttr.in/" + urllib.parse.quote(city) + "?format=%l:+%C,+%t,+feels+like+%f,+humidity+%h,+wind+%w"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        speak(urllib.request.urlopen(req, timeout=10).read().decode().strip())
    except Exception:
        skill_web(f"weather {city} today")


def skill_news(topic):
    topic = re.sub(r"^(?:the |latest |top |today'?s |recent |breaking )*(?:news|headlines)\s*(?:about|on|for|in)?\s*", "",
                   (topic or "").strip(), flags=re.I) or (topic or "").strip() or "top news today"
    items = web_search(topic if "news" in topic.lower() else topic + " news", n=5, news=True)
    if not items:
        return speak("I couldn't fetch the news right now. Looks like my news sources are unreachable.")
    speak("Here are the headlines. " + " ... ".join(re.sub(r"\s+-\s+[^-]{2,40}$", "", i["title"]) for i in items[:3]))


def skill_youtube(q):
    url = None
    try:
        for v in ddgs_cls()().videos(q, max_results=5):
            if "youtube.com/watch" in v.get("content", ""):
                url = v["content"]
                break
    except Exception:
        pass
    speak(f"Playing {q} on YouTube.")
    open_chrome(url or "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q))


def skill_timer(seconds, label=""):
    try:
        seconds = int(float(seconds))
    except Exception:
        seconds = 0
    if seconds <= 0:
        seconds = int(num(ask("For how long?"), 0) * 60)
        if seconds <= 0:
            return
    due = datetime.datetime.now() + datetime.timedelta(seconds=seconds)
    store.add_reminder(label or "Timer", due, kind="timer")      # saved to disk: survives restarts, shows on the dashboard
    mins, secs = divmod(seconds, 60)
    speak(f"Timer set for {mins} minutes" + (f" {secs} seconds" if secs else "") + ".")


def skill_reminder(text, when, repeat="none"):
    """when: 'YYYY-MM-DD HH:MM' from the router, or natural language like 'tomorrow at 9'."""
    text = (text or "").strip() or ask("What should I remind you about?")
    if not text:
        return
    due = None
    if when:
        try:
            due = datetime.datetime.fromisoformat(str(when).strip().replace(" ", "T")[:19])
        except ValueError:
            due = store.parse_when(str(when))
    if due is None:
        answer = ask("When should I remind you?")
        due = store.parse_when(answer or "")
        repeat = repeat if repeat != "none" else store.parse_repeat(answer or "")
    if due is None:
        speak("Sorry, I couldn't work out the time.")
        return
    if due <= datetime.datetime.now():
        due += datetime.timedelta(days=1)
    store.add_reminder(text, due, repeat)
    speak(f"Okay, I'll remind you {store.human_when(due)}: {text}." + (f" Repeating {repeat}." if repeat != "none" else ""))


def fire_reminder(r):
    store.mark_fired(r["id"])
    store.log("reminder", "Fired: " + r["text"])
    late = (datetime.datetime.now() - datetime.datetime.fromisoformat(r["due"])).total_seconds()
    try:
        import winsound
        winsound.Beep(1200, 400)
    except Exception:
        pass
    if r.get("kind") == "timer":
        speak(f"Hey {USER_NAME}, time's up!" + (f" {r['text']}." if r["text"] != "Timer" else ""))
    else:
        speak(f"Hey {USER_NAME}, reminder: {r['text']}." + (" Sorry, this one is a little late." if late > 300 else ""))


def reminder_loop():
    while True:
        try:
            for r in store.due_reminders():
                fire_reminder(r)
        except Exception as e:
            print("(reminder error)", e)
        time.sleep(1)


def skill_list_reminders():
    items = [r for r in store.list_reminders() if not r["done"]]
    if not items:
        speak("You have no reminders.")
        return
    speak(f"You have {len(items)} reminder{'s' if len(items) != 1 else ''}. " +
          "; ".join(f"{r['text']}, {store.human_when(r['due'])}" for r in items[:4]) + ".")


def skill_cancel_reminder(query):
    q = set(re.findall(r"[a-z0-9']+", (query or "").lower())) - {"the", "my", "a", "reminder", "about", "to", "for"}
    best, score = None, 0
    for r in store.list_reminders():
        s = len(q & set(re.findall(r"[a-z0-9']+", r["text"].lower())))
        if not r["done"] and (s > score or (best is None and not q)):
            best, score = r, s
    if best:
        store.delete_reminder(best["id"])
        speak(f"Okay, I cancelled the reminder: {best['text']}.")
    else:
        speak("I couldn't find a matching reminder.")


def skill_note(text):
    text = text or ask("What should I note down?")
    if text:
        store.add_note(text)
        speak("Noted.")


def skill_task(text):
    text = (text or "").strip() or ask("What's the task?")
    if text:
        store.add_task(text)
        speak("Added to your to-do list.")


def skill_remember(text):
    text = (text or "").strip() or ask("What should I remember?")
    if text:
        ok = add_fact(text)
        if ok:
            store.log("memory", "Learned: " + text[:90])
        speak("Got it, I'll remember that." if ok else "I already have that noted.")


def skill_screen(question):
    q = (question or "explain what's on my screen").lower()
    text = pyperclip.paste() if "clipboard" in q or "copied" in q else ocr_screen_text()
    if not text.strip():
        return speak("I couldn't read any text.")
    out = llm([{"role": "system", "content": persona()},
               {"role": "user", "content": f"{question}\n\nText from {USER_NAME}'s screen:\n{text[:3500]}"}],
              max_tokens=250, ctx=4096)
    speak(out or "I read it but couldn't think of a good answer.")


# ---------------------------------------------------------------- generic agent
RISKY = re.compile(r"\b(send|submit|delete|remove|erase|pay|purchase|buy|order|checkout|uninstall|format|post|"
                   r"publish|transfer|sign out|log out|shut ?down|restart|install|confirm|accept|agree|allow)\b", re.I)
MSG_WINDOWS = re.compile(r"whatsapp|gmail|mail|outlook|messenger|telegram|discord|slack|teams|inbox|compose", re.I)

AGENT_SYS = """You control a Windows PC to achieve the user's GOAL. Each turn reply with ONE JSON action:
{"tool":"open_app","name":"notepad"}
{"tool":"open_url","url":"https://..."}
{"tool":"click","text":"label visible on screen"}
{"tool":"type","text":"text to type"}
{"tool":"press","key":"enter"}   (also combos like ctrl+t, alt+left)
{"tool":"scroll","direction":"down"}
{"tool":"wait","seconds":2}
{"tool":"ask","question":"question for the user"}
{"tool":"say","text":"something to tell the user"}
{"tool":"done","summary":"short spoken summary"}
Rules: use only labels that appear in SCREEN. One action per turn. After opening something, wait before clicking.
Never type passwords, OTPs or payment details: tell the user to do it. If finished or impossible, use done."""


def observe():
    title = active_title()
    try:
        if is_browser():
            items = [t for t, *_ in screen_ocr() if 1 < len(t) < 45]
        else:
            items = [n for n, ct, _ in uia_elements() if ct in CLICKABLE and len(n) < 45]
            if not items:
                items = [t for t, *_ in screen_ocr() if 1 < len(t) < 45]
    except Exception:
        items = []
    items = list(dict.fromkeys(items))[:50]
    return f"Window: {title}\nVisible: " + " | ".join(items)


def skill_agent(goal, max_steps=14, quiet=False):
    if not quiet:
        speak("Okay, working on it.")
    log, seen = [], []
    for step in range(max_steps):
        obs = observe()
        out = llm_json([{"role": "system", "content": AGENT_SYS},
                        {"role": "user", "content": f"GOAL: {goal}\nDONE SO FAR:\n" + ("\n".join(log[-8:]) or "(nothing)") +
                                                    f"\nSCREEN:\n{obs}\nNext action JSON:"}],
                        task="fast", max_tokens=120, temp=0.2)
        if not out or "tool" not in out:
            return speak("I got confused about the next step, so I stopped.")
        tool = str(out["tool"])
        sig = json.dumps(out, sort_keys=True)
        seen.append(sig)
        if seen.count(sig) >= 3:
            return speak("I'm going in circles, so I'll stop here. Tell me what to try next.")
        try:
            if tool == "done":
                return speak(out.get("summary", "All done."))
            elif tool == "say":
                speak(out.get("text", ""))
                res = "said"
            elif tool == "ask":
                res = "user said: " + (ask(out.get("question", "")) or "(no answer)")
            elif tool == "open_app":
                open_app(out.get("name", ""))
                time.sleep(2.5)
                res = "opened"
            elif tool == "open_url":
                open_chrome(out.get("url", ""))
                time.sleep(2)
                res = "opened"
            elif tool == "click":
                lab = out.get("text", "")
                if RISKY.search(lab) and not confirm(f"I'm about to click '{lab}'. Is that okay?"):
                    return
                res = "clicked" if click_label(lab, quiet=True) else "NOT FOUND on screen"
                time.sleep(1)
            elif tool == "type":
                if "password" in obs.lower():
                    return speak("That looks like a password field. Please type it yourself and then tell me to continue.")
                type_text(out.get("text", ""))
                res = "typed"
            elif tool == "press":
                key = str(out.get("key", "")).lower()
                if key == "enter" and MSG_WINDOWS.search(active_title()):
                    if not confirm("Pressing enter here may send something. Should I go ahead?"):
                        return
                pyautogui.hotkey(*key.split("+")) if "+" in key else pyautogui.press(key)
                res = "pressed"
                time.sleep(0.6)
            elif tool == "scroll":
                pyautogui.scroll(-600 if "down" in str(out.get("direction", "down")) else 600)
                res = "scrolled"
            elif tool == "wait":
                time.sleep(min(float(out.get("seconds", 2)), 10))
                res = "waited"
            else:
                res = "unknown tool"
        except pyautogui.FailSafeException:
            raise
        except Exception as e:
            res = f"error {e}"
        log.append(f"{step + 1}. {sig} -> {res}")
    speak("That's taking too many steps, so I'll stop. Want me to keep going?")


# =====================================================================
#  WORKSPACE MEMORY: what Ashie wrote / found / noted, so "send it", "attach that",
#  "make notes of that" refer to something real instead of a guess
# =====================================================================
ARTIFACTS_PATH = DATA / "artifacts.json"
_art_lock = threading.RLock()
ARTIFACTS = load_json(ARTIFACTS_PATH, [])
if not isinstance(ARTIFACTS, list):
    ARTIFACTS = []
ARTIFACTS = [a for a in ARTIFACTS if isinstance(a, dict) and a.get("kind")][-25:]


def _save_artifacts():
    try:
        with _art_lock:
            ARTIFACTS_PATH.write_text(json.dumps(ARTIFACTS, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def remember_artifact(kind, title, path=None, text=None):
    """kind: doc | file | notes | email | ai_answer | explanation"""
    rec = {"kind": kind, "title": (title or kind)[:80], "path": str(path) if path else "",
           "text": (text or "")[:12000], "t": datetime.datetime.now().isoformat(timespec="seconds")}
    with _art_lock:
        ARTIFACTS.append(rec)
        del ARTIFACTS[:-25]
    _save_artifacts()
    return rec


def last_artifact(*kinds):
    with _art_lock:
        for a in reversed(ARTIFACTS):
            if not kinds or a.get("kind") in kinds:
                return a
    return None


def artifacts_block(n=4):
    with _art_lock:
        recent = list(ARTIFACTS[-n:])
    if not recent:
        return ""
    lines = []
    for a in reversed(recent):
        where = f" (file {Path(a['path']).name})" if a.get("path") else ""
        lines.append(f"- {a['kind']}: \"{a['title']}\"{where}")
    out = "Things you made or found recently (newest first):\n" + "\n".join(lines)
    d = last_artifact("doc")
    if d and d.get("text"):
        out += f"\nBeginning of the latest document you wrote: {d['text'][:300]!r}"
    return out


def note_action(text):
    """Goes into the conversation memory (never spoken), so follow-ups know what really happened."""
    try:
        if _capture is not None:
            _capture.append(f"[{text}]")
    except NameError:
        pass


_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def write_docx_file(path, text):
    """Writes a genuine .docx with only the standard library (used when Word itself isn't available)."""
    import zipfile
    from xml.sax.saxutils import escape
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    lines = _XML_BAD.sub("", text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    paras = "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(ln)}</w:t></w:r></w:p>' if ln.strip() else "<w:p/>"
                    for ln in lines)
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{W}"><w:body>{paras}'
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
                'w:left="1440" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>')
    styles = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="{W}"><w:docDefaults><w:rPrDefault>'
              '<w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:sz w:val="24"/></w:rPr></w:rPrDefault>'
              '<w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
              '</w:docDefaults></w:styles>')
    ctypes_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/'
                  'package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
                  'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/'
                  'document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                  '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                  'wordprocessingml.styles+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    drels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/'
             'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
             'relationships/styles" Target="styles.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes_xml)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/_rels/document.xml.rels", drels)
    return path


def read_docx_text(path):
    import zipfile
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"<w:p/>|</w:p>", "\n", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml)).strip()


def read_file_text(path, limit=8000):
    """Plain text of a file Ashie can read (txt/md/code/csv/docx/pdf). '' if it can't."""
    ext = Path(path).suffix.lower()
    try:
        if ext == ".docx":
            return read_docx_text(path)[:limit]
        if ext == ".pdf":
            for mod in ("pypdf", "PyPDF2"):
                try:
                    reader = __import__(mod).PdfReader(path)
                    return "\n".join((p.extract_text() or "") for p in reader.pages[:12])[:limit]
                except ImportError:
                    continue
            return ""
        if ext in {".txt", ".md", ".py", ".js", ".json", ".csv", ".html", ".css", ".c", ".cpp", ".java", ".log", ".ini",
                   ".ts", ".tex", ".xml", ".yml", ".yaml", ".rtf"}:
            return Path(path).read_text(encoding="utf-8", errors="ignore")[:limit]
    except Exception as e:
        print("(couldn't read file)", e)
    return ""


def ensure_artifact_file(a):
    """A real file on disk for this artifact (writes a .docx from its text if it only exists as text)."""
    p = a.get("path") or ""
    if p and os.path.exists(p):
        return p
    if a.get("text"):
        p = str(DOCS / f"{safe_name(a.get('title', 'Ashie file'))}_{datetime.datetime.now():%Y%m%d_%H%M%S}.docx")
        try:
            write_docx_file(p, a["text"])
            a["path"] = p
            _save_artifacts()
            return p
        except Exception as e:
            print("(couldn't write the file)", e)
    return ""


# =====================================================================
#  FILE SEARCH  ("find my physics assignment pdf in downloads", "the resume I made last week on D drive")
# =====================================================================
TYPE_ALIASES = {
    "word": "word", "docx": "word", "doc": "word", "pdf": "pdf", "excel": "excel", "xlsx": "excel", "spreadsheet": "excel",
    "sheet": "excel", "csv": "excel", "powerpoint": "ppt", "ppt": "ppt", "pptx": "ppt", "presentation": "ppt", "slides": "ppt",
    "slide": "ppt", "image": "image", "photo": "image", "picture": "image", "pic": "image", "screenshot": "image",
    "png": "image", "jpg": "image", "jpeg": "image", "video": "video", "movie": "video", "clip": "video", "song": "audio",
    "music": "audio", "audio": "audio", "recording": "audio", "txt": "text", "python": "code", "script": "code",
    "code": "code", "zip": "zip", "archive": "zip", "folder": "folder", "directory": "folder"}
TYPE_EXTS = {"word": {".docx", ".doc", ".rtf", ".odt"}, "pdf": {".pdf"}, "excel": {".xlsx", ".xls", ".xlsm", ".csv"},
             "ppt": {".pptx", ".ppt"}, "image": {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".heic"},
             "video": {".mp4", ".mkv", ".mov", ".avi", ".webm"}, "audio": {".mp3", ".wav", ".m4a", ".flac", ".ogg"},
             "text": {".txt", ".md"}, "code": {".py", ".js", ".ts", ".java", ".c", ".cpp", ".html", ".css", ".ipynb"},
             "zip": {".zip", ".rar", ".7z"}}
GENERIC_DOC = TYPE_EXTS["word"] | TYPE_EXTS["pdf"] | TYPE_EXTS["text"]
NAME_STOP = {"the", "a", "an", "my", "me", "of", "for", "to", "in", "on", "at", "from", "file", "files", "named", "called",
             "about", "that", "this", "one", "with", "and", "i", "it", "is", "was", "had", "have", "made", "wrote", "saved",
             "downloaded", "some", "something", "please", "find", "open", "search", "where", "which", "got", "get", "who",
             "latest", "recent", "newest", "last", "new", "latest", "document", "documents", "folder"}
RECENT_WORDS = {"latest", "recent", "newest", "last", "recently"}
LOC_STOP = {"in", "on", "my", "the", "folder", "directory", "drive", "inside", "under", "from", "at", "computer", "pc",
            "laptop", "somewhere", "a", "called", "named", "of", "system", "it", "there", "where", "is", "i", "saved", "kept"}
SKIP_DIRS = {"windows", "program files", "program files (x86)", "programdata", "appdata", "$recycle.bin",
             "system volume information", "node_modules", ".git", ".venv", "venv", "env", "__pycache__", "site-packages",
             ".cache", ".npm", ".gradle", ".m2", "dist-packages", "recovery", "msocache", "perflogs", ".conda", ".nuget",
             "anaconda3", "miniconda3", ".docker", ".ollama", ".vscode", "windows.old"}


def _known_dirs():
    h = HOME
    names = {"desktop": "Desktop", "documents": "Documents", "downloads": "Downloads", "pictures": "Pictures",
             "videos": "Videos", "music": "Music"}
    bases = [h] + [p for p in h.glob("OneDrive*") if p.is_dir()]
    out = {}
    for key, folder in names.items():
        out[key] = [b / folder for b in bases if (b / folder).is_dir()]
    out["document"] = out["documents"]
    out["download"] = out["downloads"]
    out["photos"] = out["pictures"]
    out["onedrive"] = [b for b in bases if b != h]
    return out


def _fixed_drives():
    drives = []
    try:
        for p in psutil.disk_partitions(all=False):
            if "cdrom" in (p.opts or "").lower() or not p.mountpoint:
                continue
            drives.append(Path(p.mountpoint))
    except Exception:
        pass
    return drives


def _walk(roots, deadline, want_dirs=False, seen=None):
    seen = seen if seen is not None else set()
    for root in roots:
        if str(root) in seen:
            continue                      # already covered (e.g. Downloads is inside the home folder)
        seen.add(str(root))
        stack = [str(root)]
        while stack:
            if time.time() > deadline:
                return
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            nm = e.name.lower()
                            if nm in SKIP_DIRS or nm.startswith("$") or nm.startswith("."):
                                continue
                            if e.path in seen:
                                continue
                            seen.add(e.path)
                            stack.append(e.path)
                            if want_dirs:
                                yield e
                        elif not want_dirs and e.is_file(follow_symlinks=False):
                            if e.name.startswith("~$") or e.name.lower().endswith((".tmp", ".lnk", ".ini", ".dll", ".sys")):
                                continue
                            yield e
                    except OSError:
                        continue


def _words(s):
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def _tok_match(t, stem_nosep, words):
    if t in stem_nosep:
        return 1.0
    best = 0.0
    for w in words:
        if len(t) > 2 and len(w) > 2:
            r = difflib.SequenceMatcher(None, t, w).ratio()
            if r >= 0.8:
                best = max(best, 0.9 * r)
        if len(t) >= 4 and len(w) >= 4 and (w.startswith(t) or t.startswith(w)):
            best = max(best, 0.8)
    return best


def parse_file_query(hint, ftype=""):
    """-> (name tokens, set of extensions or None, wants_folder, wants_recent)"""
    words = _words(hint) + _words(ftype)
    exts, tokens, folder = set(), [], False
    recent = any(w in RECENT_WORDS for w in words)
    for w in words:
        if w in TYPE_ALIASES:
            k = TYPE_ALIASES[w]
            if k == "folder":
                folder = True
            else:
                exts |= TYPE_EXTS[k]
            continue
        if w in ("document", "documents"):
            exts |= GENERIC_DOC
            continue
        if w in NAME_STOP or w in RECENT_WORDS:
            continue
        tokens.append(w)
    return list(dict.fromkeys(tokens)), (exts or None), folder, recent


def location_roots(location):
    """Turns 'downloads', 'd drive', 'the college folder on D drive' into folders to search. ([], hints) if unknown."""
    loc = (location or "").lower()
    if not loc.strip():
        return [], []
    known = _known_dirs()
    roots, consumed = [], set()
    for key, paths in known.items():
        if re.search(r"\b" + re.escape(key) + r"\b", loc):
            roots += [p for p in paths if p.exists()]
            consumed |= set(key.split())
    for m in re.finditer(r"\b([a-z])\s*(?:drive|:)", loc):
        dp = Path(m.group(1).upper() + ":\\")
        if dp.exists():
            roots.append(dp)
        consumed |= {m.group(1), "drive"}
    hints = [w for w in _words(loc) if w not in consumed and w not in LOC_STOP]
    if hints:
        base = roots or ([HOME] + _fixed_drives())
        found = []
        deadline = time.time() + 8
        for e in _walk(base, deadline, want_dirs=True):
            nw = _words(e.name)
            nosep = "".join(nw)
            ok = [_tok_match(h, nosep, nw) >= 0.8 for h in hints]
            if ok and all(ok):
                found.append(Path(e.path))
                if len(found) >= 12:
                    break
        if found:
            return found, hints
        return roots, hints
    return roots, []


def default_roots():
    roots = []
    for key in ("desktop", "documents", "downloads", "pictures", "videos", "music"):
        roots += _known_dirs().get(key, [])
    roots.append(HOME)
    sysdrive = Path(os.environ.get("SystemDrive", "C:") + "\\")
    roots += [d for d in _fixed_drives() if str(d).upper() != str(sysdrive).upper()]
    out, seen = [], set()
    for r in roots:
        if str(r) not in seen:
            seen.add(str(r))
            out.append(r)
    return out


def search_files(hint, location="", ftype="", seconds=None, limit=5):
    """Fuzzy search by name hint + optional extension/location. Newest-first when 'latest' is said. -> list of dicts."""
    seconds = seconds or FILE_SEARCH_SECONDS
    tokens, exts, want_folder, recent = parse_file_query(hint, ftype)
    roots, loc_hints = location_roots(location)
    roots = roots or default_roots()
    deadline = time.time() + seconds
    now = time.time()
    cands, textlike, seen_dirs = [], [], set()
    for e in _walk(roots, deadline, want_dirs=want_folder, seen=seen_dirs):
        try:
            name = e.name
            stem = name.rsplit(".", 1)[0] if (not want_folder and "." in name) else name
            ext = Path(name).suffix.lower()
            if exts and not want_folder and ext not in exts:
                continue
            nw = _words(stem)
            nosep = "".join(nw)
            pw = _words(" ".join(Path(e.path).parent.parts[-3:]))
            if tokens:
                per = []
                for t in tokens:
                    nm = _tok_match(t, nosep, nw)
                    pm = 0.5 * _tok_match(t, "".join(pw), pw)
                    per.append(max(nm, pm))
                score = sum(per) / len(tokens)
                if score < 0.5:
                    if ext in {".txt", ".md", ".py", ".csv", ".json", ".docx"} and len(textlike) < 4000:
                        textlike.append(e.path)
                    continue
            else:
                score = 0.5
            mt = e.stat().st_mtime
            age = (now - mt) / 86400
            score += 0.06 if age <= 3 else (0.03 if age <= 30 else 0)
            cands.append({"score": min(score, 1.2), "path": e.path, "mtime": mt, "why": "name"})
        except OSError:
            continue
    best = max([c["score"] for c in cands], default=0)
    # nothing convincing by name -> look inside small text-like files for the words
    if tokens and best < 0.6 and textlike and time.time() < deadline + 8 and not want_folder:
        stop_at = time.time() + 8
        for p in textlike:
            if time.time() > stop_at:
                break
            try:
                if os.path.getsize(p) > 1_500_000:
                    continue
                body = (read_docx_text(p) if p.lower().endswith(".docx") else
                        Path(p).read_text(encoding="utf-8", errors="ignore")).lower()
                hit = sum(1 for t in tokens if t in body) / len(tokens)
                if hit >= 0.6:
                    cands.append({"score": 0.55 + 0.3 * hit, "path": p, "mtime": os.path.getmtime(p), "why": "content"})
            except Exception:
                continue
    if recent and cands:
        top = max(c["score"] for c in cands)
        cands = [c for c in cands if c["score"] >= top - 0.25]
        cands.sort(key=lambda c: -c["mtime"])
    else:
        cands.sort(key=lambda c: (-c["score"], -c["mtime"]))
    return cands[:limit]


_ORD = {"one": 1, "first": 1, "1": 1, "two": 2, "second": 2, "2": 2, "to": 2, "three": 3, "third": 3, "3": 3,
        "four": 4, "fourth": 4, "4": 4, "five": 5, "fifth": 5, "5": 5}


def _pick_number(answer, n):
    for w in re.findall(r"[a-z0-9]+", (answer or "").lower()):
        if w in ("none", "cancel", "neither", "nothing", "no", "nope", "stop"):
            return 0
        if w in _ORD and _ORD[w] <= n:
            return _ORD[w]
    return 0


def find_file_interactive(hint, location="", ftype="", say=True):
    """Finds one file for the user, asking which if it's unclear. Returns the path or None (user already told)."""
    if not (hint or "").strip() and not (location or "").strip() and not ftype:
        hint = ask("Which file? Tell me a bit about its name and where it is.")
        if not hint:
            return None
    if say:
        speak("Searching your computer.")
    res = search_files(hint, location, ftype)
    if not res:
        speak("I couldn't find a match. Try giving me part of the file name, or tell me which folder or drive it's in.")
        return None
    top = res[0]
    if len(res) == 1 or top["score"] - res[1]["score"] >= 0.15 or top["score"] >= 0.97:
        return top["path"]
    opts = res[:3]
    desc = ". ".join(f"{i + 1}: {Path(o['path']).name}, in {Path(o['path']).parent.name or 'the drive root'}"
                     for i, o in enumerate(opts))
    n = _pick_number(ask(f"I found a few. {desc}. Which one?"), len(opts))
    if not n:
        speak("Okay, I won't pick any of them.")
        return None
    return opts[n - 1]["path"]


def skill_find_file(hint="", location="", ftype="", action="open", raw=""):
    p = find_file_interactive(f"{hint}", location, ftype)
    if not p:
        return
    name = Path(p).name
    remember_artifact("file", name, p)
    note_action(f"found file {name} at {p}")
    action = (action or "open").lower()
    try:
        if action in ("folder", "show", "reveal", "location", "where"):
            subprocess.Popen(f'explorer /select,"{p}"')
            speak(f"It's in {Path(p).parent.name or 'the drive root'}. I've shown it in the folder.")
        elif action == "read":
            txt = read_file_text(p)
            if not txt.strip():
                os.startfile(p)
                return speak(f"I can't read that kind of file myself, so I opened {name} for you.")
            out = llm([{"role": "system", "content": persona()},
                       {"role": "user", "content": f"{USER_NAME} asked: {raw or 'what is in this file'}\n\nFile {name}:\n{txt[:6000]}\n\n"
                                                   "Answer in 2-4 spoken sentences."}], max_tokens=260, ctx=4096)
            speak(out or f"I found {name} but couldn't summarise it.")
        else:
            os.startfile(p)
            speak(f"Opening {name}.")
    except Exception as e:
        print("(open error)", e)
        speak(f"I found {name} but couldn't open it.")


# =====================================================================
#  EMAIL ATTACHMENTS (real ones, verified - she never says "attached" unless it is)
# =====================================================================
ATTACH_WORDS = re.compile(r"\b(attach(?:ed|ing|ment|ments)?|send (?:it|that|this|the (?:letter|document|doc|file|essay|report|"
                          r"resume|application|assignment))|what you (?:wrote|made)|(?:email|mail|gmail) (?:it|that))\b", re.I)
GENERIC_SEND = re.compile(r"^(?:please\s+)?(?:send|email|mail|attach|forward)?\s*(?:it|that|this|the (?:letter|document|doc|file)|"
                          r"what you (?:wrote|made))?(?:\s+(?:over|to\s+.*|on gmail|in gmail|as an attachment|with it attached))?\W*$", re.I)
_FILE_DIALOG_TITLES = ("open", "upload", "insert file", "choose file", "select file", "attach", "file upload")


def _ocr_blob():
    try:
        return " | ".join(t.lower() for t, *_ in screen_ocr()) if OCR_OK else ""
    except Exception:
        return ""


def _norm_alnum(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _file_dialog_open(timeout=12):
    end = time.time() + timeout
    while time.time() < end:
        t = active_title().lower().strip()
        if t and any(t == k or t.startswith(k) for k in _FILE_DIALOG_TITLES):
            return True
        time.sleep(0.3)
    return False


def _pick_in_file_dialog(path):
    time.sleep(0.6)
    pyperclip.copy(path)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.4)
    pyautogui.press("enter")


def _wait_attachment(path, timeout=40):
    """'ok' once the file name shows on screen and no upload is running, 'unverified' without OCR, else 'failed'."""
    if not OCR_OK:
        time.sleep(6)
        return "unverified"
    stem = _norm_alnum(Path(path).stem)[:12]
    name = _norm_alnum(Path(path).name)[:14]
    end = time.time() + timeout
    seen = False
    while time.time() < end:
        blob = _norm_alnum(_ocr_blob())
        if stem and (stem in blob or name in blob):
            seen = True
            if "uploading" not in blob and "attaching" not in blob:
                time.sleep(1.5)
                return "ok"
        time.sleep(1.5)
    return "ok" if seen else "failed"


def gmail_attach(path):
    """Attach a file to the Gmail compose window that is open in front. -> 'ok' | 'unverified' | 'failed'."""
    path = str(Path(path).resolve())
    if not os.path.exists(path):
        return "failed"
    # method 1: the paperclip button (Gmail calls it "Attach files")
    clicked = False
    for _ in range(2):
        for lab in ("attach files", "attach file"):
            try:
                if uia_click(lab):
                    clicked = True
                    break
            except Exception:
                pass
        if clicked:
            break
        time.sleep(1.2)
    if clicked and _file_dialog_open():
        _pick_in_file_dialog(path)
        st = _wait_attachment(path)
        if st != "failed":
            return st
    # method 2: put the file on the clipboard and paste it into the message
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", f'Set-Clipboard -Path "{path}"'], timeout=15,
                       capture_output=True)
        try:
            uia_click("message body")
        except Exception:
            pass
        time.sleep(0.4)
        pyautogui.hotkey("ctrl", "v")
        return _wait_attachment(path, timeout=25)
    except Exception as e:
        print("(clipboard attach failed)", e)
    return "failed"


def _gmail_creds():
    addr = (GMAIL_ADDRESS or os.environ.get("GMAIL_ADDRESS", "")).strip()
    pw = (GMAIL_APP_PASSWORD or os.environ.get("GMAIL_APP_PASSWORD", "")).strip()
    f = DATA / "gmail_app_password.txt"
    if not pw and f.exists():
        pw = f.read_text(encoding="utf-8").strip()
    pw = pw.replace(" ", "")
    return (addr, pw) if addr and pw else None


def build_mime(sender, to, subject, body, attachments):
    import mimetypes
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg.set_content(body)
    for p in attachments:
        ctype, _ = mimetypes.guess_type(p)
        main, sub = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(Path(p).read_bytes(), maintype=main, subtype=sub, filename=Path(p).name)
    return msg


def send_smtp(addr, subject, body, attachments):
    import smtplib
    user, pw = _gmail_creds()
    msg = build_mime(user, addr, subject, body, attachments)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)


def _gmail_open_compose(addr, subj, body):
    short = len(body) <= 1400
    url = ("https://mail.google.com/mail/?view=cm&fs=1&to=" + urllib.parse.quote(addr) + "&su=" + urllib.parse.quote(subj) +
           (("&body=" + urllib.parse.quote(body)) if short else ""))
    open_chrome(url)
    warned, ready = False, False
    end = time.time() + 90
    while time.time() < end:
        blob = _ocr_blob() if OCR_OK else "send subject"
        if "sign in" in blob and not warned:
            speak("Gmail needs you to sign in. I'll wait.")
            warned = True
        if "send" in blob and ("subject" in blob or "new message" in blob or subj[:10].lower() in blob):
            ready = True
            break
        time.sleep(1)
    time.sleep(1.5)
    if ready and not short:
        pyperclip.copy(body)
        pyautogui.hotkey("ctrl", "v")      # long text goes in through the clipboard
        time.sleep(0.6)
    return ready


def _send_and_verify():
    pyautogui.hotkey("ctrl", "enter")
    time.sleep(3)
    sent = OCR_OK and any("message sent" in t.lower() or t.lower().strip() == "sent" for t, *_ in screen_ocr())
    return bool(sent) or not OCR_OK


def _deliver_email(addr, subj, body, att_path, fallback_text):
    fname = Path(att_path).name if att_path else ""
    spoken_to = spell_email(addr)
    # 1) straight from Gmail with an app password: no browser, real attachments, most reliable
    if EMAIL_METHOD != "outlook" and _gmail_creds():
        if not confirm(f"I'll send this to {spoken_to}, subject: {subj}." + (f" {fname} will be attached." if fname else "")):
            return False
        try:
            send_smtp(addr, subj, body, [att_path] if att_path else [])
            speak("Email sent" + (f", with {fname} attached." if fname else "."))
            return True
        except Exception as e:
            print("(SMTP failed)", e)
            speak("Sending straight through Gmail failed, so I'll do it in the browser instead.")
    # 2) desktop Outlook
    if EMAIL_METHOD == "outlook":
        try:
            import win32com.client
            ol = win32com.client.Dispatch("Outlook.Application")
            mail = ol.CreateItem(0)
            mail.To, mail.Subject, mail.Body = addr, subj, body
            if att_path:
                mail.Attachments.Add(att_path)
            mail.Display()
            if confirm(f"Send this email to {spoken_to}, subject: {subj}." + (f" {fname} is attached." if fname else "")):
                mail.Send()
                speak("Email sent!")
                return True
            return False
        except Exception as e:
            print("(Outlook failed, falling back to Gmail)", e)
    # 3) Gmail in Chrome
    ready = _gmail_open_compose(addr, subj, body)
    status = ""
    if att_path:
        status = gmail_attach(att_path) if ready else "failed"
        if status == "failed":
            speak(f"I couldn't attach {fname} automatically.")
            if fallback_text and confirm("Shall I put the full text in the body of the email instead?"):
                pyautogui.hotkey("ctrl", "w")
                time.sleep(1)
                ready = _gmail_open_compose(addr, subj, fallback_text)
                att_path, fname, status = "", "", ""
            else:
                speak(f"I've left the email open. Your file is saved as {fname} in your Ashie documents folder, "
                      "so you can attach it yourself.")
                return False
    desc = f"Your email to {spoken_to} is ready. Subject: {subj}."
    if att_path:
        desc += f" {fname} is attached." if status == "ok" else f" I added {fname}, but I couldn't double-check it."
    if confirm(desc + " Should I send it?"):
        ok = _send_and_verify()
        speak("Email sent!" if ok else "I pressed send. Please glance at the screen to be sure.")
        return ok
    return False


def skill_attach(file_hint="", raw=""):
    """'attach it to the mail' - adds a file to the email that is open on screen."""
    if (file_hint or "").strip():
        path = find_file_interactive(file_hint)
        if not path:
            return
        remember_artifact("file", Path(path).name, path)
    else:
        a = last_artifact("doc", "notes", "file", "explanation")
        path = ensure_artifact_file(a) if a else ""
        if not path:
            return speak("I haven't written or found any file yet. Tell me which file to attach, or ask me to write one.")
    title = active_title().lower()
    if not any(k in title for k in ("gmail", "compose", "mail", "outlook", "inbox", "message")):
        return speak("I don't see an email open on screen. Open the draft first, or tell me to email it and I'll do it all.")
    speak(f"Attaching {Path(path).name}.")
    st = gmail_attach(path)
    note_action(f"attach {Path(path).name}: {st}")
    if st == "ok":
        speak("Attached. I can see it in the email.")
    elif st == "unverified":
        speak("I attached it, but I can't confirm it on screen, so please check.")
    else:
        speak(f"I couldn't attach it. The file is {Path(path).name} in {Path(path).parent.name}, so you can add it with the paperclip.")


def skill_email(to, subject, instructions, use_word=False, attach_last=False, file_hint="", paste=False, raw=""):
    to = (to or "").strip()
    info = find_contact(to) if to else None
    addr = (info or {}).get("email", "")
    if not addr and to:
        cand = spoken_to_email(to)
        addr = cand if valid_email(cand) else ""
    for _ in range(2):
        if addr:
            break
        a = ask("What's the email address? You can say it like name at gmail dot com.")
        if not a:
            return speak("Okay, I'll skip the email.")
        cand = spoken_to_email(a)
        if valid_email(cand):
            addr = cand
        else:
            info = find_contact(a)
            addr = (info or {}).get("email", "")
    if not addr:
        return speak("I couldn't get a valid email address. Add the person to CONTACTS to make it easy.")
    instructions = (instructions or "").strip()
    raw = raw or ""

    # ---- is there a document / file that should go with this mail?
    doc, att_path = None, ""
    if file_hint:
        att_path = find_file_interactive(file_hint)
        if not att_path:
            return
        remember_artifact("file", Path(att_path).name, att_path)
        doc = {"title": Path(att_path).stem.replace("_", " "), "text": "", "path": att_path}
    elif attach_last or paste or ATTACH_WORDS.search(raw) or ATTACH_WORDS.search(instructions):
        doc = last_artifact("doc", "notes", "file", "explanation")
        if doc:
            att_path = ensure_artifact_file(doc)
        elif re.search(r"\battach|\bsend (?:it|that|this)\b", raw + " " + instructions, re.I):
            return speak("I haven't written or found anything to send yet. Ask me to write it first, or tell me which file.")
    title = (doc or {}).get("title", "")
    doc_text = (doc or {}).get("text", "")
    if att_path and not doc_text:
        doc_text = read_file_text(att_path, 6000)

    if doc and paste and doc_text:
        subj, body, att_path = (subject or title or "Letter"), doc_text, ""
    else:
        if not instructions and not att_path:
            instructions = ask("What should the email say?")
            if not instructions:
                return speak("Okay, never mind.")
        speak("Drafting your email.")
        about = instructions
        if att_path:
            extra = "" if (not instructions or GENERIC_SEND.match(instructions)) else instructions
            about = (f"A short cover note. The file '{Path(att_path).name}' ({title}) is attached to this email. " + extra).strip()
        subj, body = draft_email(about, subject or title, to if "@" not in to else "")
        if use_word and not att_path:
            p = write_in_word(f"Subject: {subj}\n\n{body}", "Email " + subj)
            speak("I wrote it in Word." + (" Saved in your Ashie documents folder." if p else ""))

    sent = _deliver_email(addr, subj, body, att_path, doc_text if att_path else "")
    if sent:
        remember_artifact("email", subj, text=body)
        note_action(f"emailed {addr}, subject '{subj}'" + (f", with attachment {Path(att_path).name}" if att_path else ""))
    else:
        note_action(f"email to {addr} prepared but not sent")


# =====================================================================
#  CHATBOT WEBSITES: ask Claude / ChatGPT / DeepSeek / Gemini..., read the reply (scrolling if needed),
#  then answer, explain, make notes, or carry out the steps it gives
# =====================================================================
AI_SITES = {
    "claude": {"title": "Claude", "url": "https://claude.ai/new", "names": ("claude",),
               "inputs": ("how can claude help", "write your prompt", "reply to claude", "message claude", "talk to claude")},
    "chatgpt": {"title": "ChatGPT", "url": "https://chatgpt.com/", "names": ("chatgpt", "chat gpt", "gpt", "openai"),
                "inputs": ("ask anything", "message chatgpt")},
    "deepseek": {"title": "DeepSeek", "url": "https://chat.deepseek.com/", "names": ("deepseek", "deep seek", "deepsek"),
                 "inputs": ("message deepseek", "ask anything")},
    "gemini": {"title": "Gemini", "url": "https://gemini.google.com/app", "names": ("gemini", "bard"),
               "inputs": ("enter a prompt here", "ask gemini")},
    "perplexity": {"title": "Perplexity", "url": "https://www.perplexity.ai/", "names": ("perplexity",),
                   "inputs": ("ask anything", "ask a follow-up")},
    "copilot": {"title": "Copilot", "url": "https://copilot.microsoft.com/", "names": ("copilot",),
                "inputs": ("message copilot", "ask me anything")},
    "grok": {"title": "Grok", "url": "https://grok.com/", "names": ("grok",),
             "inputs": ("what do you want to know", "ask anything")},
    "qwen": {"title": "Qwen", "url": "https://chat.qwen.ai/", "names": ("qwen", "quen"),
             "inputs": ("how can i help you", "message qwen")},
    "mistral": {"title": "Le Chat", "url": "https://chat.mistral.ai/chat", "names": ("mistral", "le chat"),
                "inputs": ("ask le chat", "ask anything")},
}
LOGIN_HINTS = ("log in", "login", "sign in", "sign up", "continue with google", "create account", "welcome back")
BUSY_HINTS = ("stop generating", "stop response", "stop streaming", "is thinking", "thinking...", "thinking…",
              "generating", "searching the web", "researching")
NOISE_LINES = {"copy", "retry", "share", "edit", "regenerate", "good response", "bad response", "like", "dislike", "read aloud",
               "show more", "new chat", "cancel", "send", "copy code", "download", "listen", "sources", "thinking",
               "deepthink", "search", "ask anything"}
FOOTER_HINTS = ("can make mistakes", "ai-generated", "for reference only", "double-check", "verify important",
                "check important info")
DANGEROUS = re.compile(r"(rm\s+-rf|del\s+/[sfq]|rd\s+/s|format\s+[a-z]:|diskpart|reg\s+delete|shutdown\s|cipher\s+/w|bcdedit|"
                       r"vssadmin|net\s+user|takeown|powershell.*-enc|invoke-expression|\biex\b|curl[^|\n]*\|\s*(?:sh|bash)|"
                       r"remove-item.*-recurse|wipe|erase the (?:disk|drive))", re.I)


def ai_site_from(text):
    t = (text or "").lower()
    if re.search(r"\b(?:ask|tell|use|on|with|in|using|open)\s+(?:the\s+)?cloud(?:\s+ai)?\b", t):
        return "claude"          # speech-to-text often hears "Claude" as "cloud"
    for key, v in AI_SITES.items():
        if any(re.search(r"\b" + re.escape(n) + r"\b", t) for n in v["names"]):
            return key
    return None


def _ai_wait_ready(site, timeout=60):
    info = AI_SITES[site]
    if not OCR_OK:
        time.sleep(7)
        return True
    end, warned = time.time() + timeout, False
    while time.time() < end:
        blob = _ocr_blob()
        if any(lab in blob for lab in info["inputs"]):
            return True
        if any(h in blob for h in LOGIN_HINTS) and not warned:
            speak(f"{info['title']} wants you to log in. I'll wait a bit.")
            warned = True
            end = max(end, time.time() + 90)
        time.sleep(1.2)
    return False


def _ai_submit(site, q):
    info = AI_SITES[site]
    for lab in info["inputs"]:
        try:
            if ocr_click(lab):
                break
        except Exception:
            pass
    time.sleep(0.3)
    before = _norm_alnum(_ocr_blob())
    pyperclip.copy(q)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.7)
    pyautogui.press("enter")
    time.sleep(3)
    if OCR_OK and _norm_alnum(_ocr_blob()) == before:      # nothing changed: Enter didn't send it
        for lab in ("send message", "send", "submit"):
            try:
                if uia_click(lab):
                    return
            except Exception:
                pass
        pyautogui.press("enter")


def _ai_wait_answer(max_wait=None):
    """Waits until the page stops changing and no 'stop generating' style indicator is visible."""
    max_wait = max_wait or AI_WAIT_SECONDS
    if not OCR_OK:
        time.sleep(min(max_wait, 25))
        return True
    t0, last, stable = time.time(), "", 0
    time.sleep(4)
    while time.time() - t0 < max_wait:
        blob = _ocr_blob()
        sig = _norm_alnum(blob)[-700:]
        busy = any(h in blob for h in BUSY_HINTS)
        stable = stable + 1 if (sig == last and not busy) else 0
        last = sig
        if stable >= 3:
            return True
        time.sleep(2)
    return False


def _tail_pattern(q):
    words = re.sub(r"\s+", " ", q.strip())[-40:].split()
    words = words[1:] if len(words) > 3 else words          # first word of the tail may be cut in half
    return r"\s+".join(re.escape(w) for w in words) if words else ""


def ai_clean(text, q):
    """Keeps just the reply that came after our question, minus page chrome."""
    text = (text or "").replace("\r", "")
    pat = _tail_pattern(q)
    if pat:
        hits = list(re.finditer(pat, text))
        if hits:
            text = text[hits[-1].end():]
    lines = [ln.rstrip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln.strip().lower() not in NOISE_LINES]
    while lines and (not lines[-1].strip() or any(h in lines[-1].lower() for h in FOOTER_HINTS)):
        lines.pop()
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out[:14000]


def _merge_pages(pages):
    """Joins OCR'd pages (top to bottom), dropping the lines repeated where consecutive pages overlap."""
    acc = []
    norm = lambda s: re.sub(r"\W+", "", s.lower())
    for page in pages:
        lines = [l for l in page.split("\n") if l.strip()]
        k = 0
        for n in range(min(len(acc), len(lines), 14), 0, -1):
            if all(difflib.SequenceMatcher(None, norm(a), norm(b)).ratio() >= 0.9 for a, b in zip(acc[-n:], lines[:n])):
                k = n
                break
        acc += lines[k:]
    return "\n".join(acc)


def _ai_read_scrolling(q, max_pages=12):
    """OCR fallback: scroll up until our own question shows, then stitch the pages back together."""
    pages, last_sig = [], None
    pat = _tail_pattern(q)
    for _ in range(max_pages):
        txt = ocr_screen_text()
        pages.append(txt)
        sig = _norm_alnum(txt)[:300]
        if (pat and re.search(pat, txt)) or sig == last_sig:
            break
        last_sig = sig
        pyautogui.scroll(500)
        time.sleep(0.8)
    return ai_clean(_merge_pages(list(reversed(pages))), q)


def _ai_read(q):
    """Select-all + copy gives the exact text (maths included); OCR + scrolling is the fallback."""
    text = ""
    try:
        W, H = pyautogui.size()
        pyautogui.click(int(W * 0.5), int(H * 0.5))          # leave the input box so Ctrl+A selects the page
        time.sleep(0.3)
        pyperclip.copy("")
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.7)
        text = pyperclip.paste() or ""
        pyautogui.click(int(W * 0.5), int(H * 0.5))          # drop the selection
    except Exception as e:
        print("(copy read failed)", e)
    ans = ai_clean(text, q) if text else ""
    if len(ans) < 40 and OCR_OK:
        print("(copy gave too little; reading the page with OCR and scrolling)")
        ans = _ai_read_scrolling(q)
    return ans


def _ai_explain(ans, q, mode, notes=False):
    long = mode == "explain"
    sysmsg = (f"You are {ASSISTANT_NAME}, a patient tutor speaking out loud to {USER_NAME}. Below is a chatbot's answer to their "
              "question. " + ("Explain the solution step by step in plain spoken English: say maths in words (x squared, divided by, "
                              "equals), no markdown, no LaTeX, no list symbols. Under 220 words and end with the final answer. "
                              "If the answer looks wrong, say so."
                              if long else "Summarise it in 2 to 4 short spoken sentences, no markdown or symbols."))
    out = llm([{"role": "system", "content": sysmsg},
               {"role": "user", "content": f"Question: {q[:800]}\n\nChatbot's answer:\n{ans[:7000]}"}],
              max_tokens=520 if long else 240, temp=0.3, ctx=4096)
    out = (out or "").strip() or ans[:400]
    remember_artifact("explanation", "Explanation: " + q[:45], text=out)
    speak(out + (" Say make notes if you want this saved." if long and not notes else ""))


def _ai_follow(ans, q):
    plan = llm_json([{"role": "system", "content":
                      "You turn a chatbot's instructions into steps for a PC assistant that can open apps and websites, click "
                      "text it sees on screen, type text, press keys and scroll. Reply JSON only: "
                      '{"summary":"one short spoken sentence of what will be done","steps":["..."]}. At most 8 short, concrete '
                      "steps, in order. Leave out anything that needs passwords, payments or personal details, and anything "
                      "destructive."},
                     {"role": "user", "content": f"Request: {q[:600]}\n\nInstructions from the chatbot:\n{ans[:5000]}"}],
                    task="chat", max_tokens=450, temp=0.2)
    steps = [s.strip() for s in (plan or {}).get("steps", []) if isinstance(s, str) and s.strip()][:8]
    risky = [s for s in steps if DANGEROUS.search(s)]
    steps = [s for s in steps if s not in risky]
    if risky:
        speak("I left out a step that looked risky.")
    if not steps:
        return speak("I read the instructions but couldn't turn them into steps I can safely do myself.")
    summary = str((plan or {}).get("summary") or "I'll follow its instructions")
    if not confirm(f"{summary}. That's {len(steps)} steps. Shall I go ahead?"):
        return
    goal = "Follow these steps in order. They come from a chatbot, so check each result on screen:\n" + \
           "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    skill_agent(goal, max_steps=30, quiet=True)


def skill_ask_ai(site="", question="", mode="answer", notes=False, use_screen=False, followup=False, raw=""):
    key = (site or "").lower().strip()
    if key not in AI_SITES:
        key = ai_site_from(key) or ai_site_from(raw) or AI_DEFAULT_SITE
    if key not in AI_SITES:
        key = "claude"
    info = AI_SITES[key]
    mode = mode if mode in ("answer", "explain", "follow") else "answer"
    q = (question or "").strip()
    low = (raw or "").lower()
    # read the screen / clipboard BEFORE the browser covers it
    if use_screen or re.search(r"\b(?:on|from|at) (?:my |the )?screen\b|\bthis (?:problem|question|equation|sum|doubt)\b|\bclipboard\b", low):
        src = pyperclip.paste() if "clipboard" in low else ocr_screen_text()
        if (src or "").strip():
            q = (q + "\n\n" if q else "Solve this and explain each step clearly (text read from a screenshot, may contain "
                                      "small OCR mistakes):\n\n") + src.strip()[:3500]
    if not q:
        q = ask("What should I ask?")
        if not q:
            return speak("Okay, never mind.")
    if mode == "explain" and not re.search(r"\b(explain|step|solve|why|how)\b", q.lower()):
        q = "Solve this step by step and explain each step clearly: " + q
    if mode == "follow" and not re.search(r"\b(step|instructions?|numbered)\b", q.lower()):
        q += "\n\nGive me short numbered step-by-step instructions I can follow on a Windows PC."
    speak(f"Asking {info['title']}.")
    if not followup:
        open_chrome(info["url"])
        time.sleep(2)
        if not _ai_wait_ready(key):
            return speak(f"{info['title']} didn't load properly, or I couldn't find its message box.")
    _ai_submit(key, q)
    speak("Waiting for the answer.")
    if not _ai_wait_answer():
        speak("It's taking long, so I'll read what's there.")
    ans = _ai_read(q)
    if len(ans) < 20:
        return speak("I couldn't read the reply from the page.")
    remember_artifact("ai_answer", f"{info['title']}: {q[:50]}", text=ans)
    note_action(f"asked {info['title']}: {q[:120]} (mode {mode}); got a {len(ans)}-character reply")
    if mode == "follow":
        _ai_follow(ans, q)
    else:
        _ai_explain(ans, q, mode, notes)
    if notes:
        skill_make_notes("", source="last", raw=raw)


# =====================================================================
#  NOTES: "make notes on that", "make notes on photosynthesis", "make notes from my screen"
# =====================================================================
def skill_make_notes(topic="", source="", in_word=False, raw=""):
    topic = (topic or "").strip()
    src = (source or "").lower()
    low = (raw or "").lower()
    generic = bool(re.fullmatch(r"(?:that|this|it|the (?:answer|explanation|solution|above|previous)|what (?:you|it|he) (?:said|explained))?",
                                topic.lower()))
    material, label = "", ""
    if src == "screen" or (generic and re.search(r"\b(?:screen|page|window)\b", low)):
        material, label = ocr_screen_text(), "screen"
    elif generic or src == "last":
        a = last_artifact("ai_answer", "explanation", "doc")
        if a:
            material, label = a.get("text", ""), a.get("title", "")
        elif HISTORY:
            material, label = list(HISTORY)[-1].get("assistant", ""), "previous answer"
    if not material.strip() and not topic:
        topic = ask("Notes on what?")
        if not topic:
            return speak("Okay, never mind.")
    if material.strip():
        task = f"Turn this into clear, well-organised study notes:\n\n{material[:7000]}"
    else:
        extra = ""
        if re.search(r"\b(latest|current|recent|news|2025|2026|today)\b", topic.lower()):
            res = web_search(topic, n=4)
            extra = "\n\nWeb results:\n" + "\n".join(f"- {r.get('title', '')}: {r.get('body', '')}" for r in res) if res else ""
        task = f"Write concise, accurate study notes on: {topic}{extra}"
    speak("Making your notes.")
    out = llm([{"role": "system", "content": f"You write study notes for {USER_NAME}. Plain text only, no markdown symbols. "
                                              "First line is a short title. Then headings in CAPITAL LETTERS and bullet points "
                                              "starting with '- '. Write formulas in plain text. Keep it under 350 words, "
                                              "accurate and easy to revise from."},
               {"role": "user", "content": task}], max_tokens=900, temp=0.3, ctx=4096)
    if not out:
        return speak("I couldn't reach my brain to make the notes.")
    out = out.strip()
    title = re.sub(r"[#*]+", "", out.splitlines()[0]).strip()[:60] or (topic or label or "Notes")
    path = None
    if in_word or re.search(r"\bin word\b", low):
        path = write_in_word(out, "Notes " + title)
    else:
        try:
            path = str(DOCS / f"Notes_{safe_name(title)}_{datetime.datetime.now():%Y%m%d_%H%M%S}.docx")
            write_docx_file(path, out)
        except Exception as e:
            print("(notes file error)", e)
            path = None
    store.add_note(out, source="notes")
    remember_artifact("notes", title, path, out)
    note_action(f"made notes '{title}'" + (f", saved at {path}" if path else ""))
    speak(f"Done. I saved '{title}' in your dashboard notes" + (" and as a Word file in your documents folder." if path else "."))
    if in_word and path:
        speak("It's open in Word too.")


# =====================================================================
#  ROUTER (LLM turns speech into a skill call)
# =====================================================================
ROUTER_SYS = """You convert a spoken command into JSON for a PC assistant. Reply with JSON only:
{"skill": "<name>", "args": {...}}
Skills:
- whatsapp: send a WhatsApp message. args: contact, message ("" if not said)
- email: write and send an email, optionally with a file attached. args: to (name or address, "" if unknown), subject ("" if unknown), instructions (what the email should say; "" if the user only says to send the document), use_word (true only if the user says to write it in Word), attach_last (true if the user wants the document/letter/file made earlier sent or attached), file_hint (name hint of an existing file on the PC to attach, "" if none), paste (true only if the user wants the text itself pasted into the mail body instead of attached)
- write_doc: write a letter/essay/report in Word. args: instructions
- attach: attach a file to the email that is already open on screen. args: file_hint ("" = the document made earlier)
- find_file: find/open/read a file or folder on this PC. args: hint (words about its name or content), location (folder/drive words like downloads, desktop, d drive, college folder; "" if none), type (word, pdf, excel, ppt, image, video, audio, text, code, folder or ""), action ("open", "folder" to show where it is, or "read" to summarise it), recent (true if they say latest/recent/newest)
- ask_ai: use a chatbot website (Claude, ChatGPT, DeepSeek, Gemini, Perplexity, Copilot, Grok...) in Chrome. args: site ("" if not named), question (what to ask; "" if the question is on the screen/clipboard), mode ("answer", "explain" for maths/academic problems they want explained, or "follow" to carry out the steps it gives), notes (true if they want notes made too), use_screen (true if the question is on the screen or clipboard), followup (true to continue in the chat already open)
- make_notes: make study notes on a topic, on the previous answer/explanation, or on the screen. args: topic ("" = previous answer), source ("last", "screen" or "topic"), in_word (true if they want them in Word)
- code: write a program or script file. args: instructions
- web: needs up-to-date or factual info from the internet. args: query
- weather: args: city ("" if none)
- news: headlines / "latest news" / "what's happening". args: topic ("" for general top news)
- youtube: play a video or song. args: query
- timer: countdown timer, or a reminder a few minutes/hours from now. args: seconds (number), label
- reminder: remind at a clock time, a date or repeatedly. args: text, when (local time as "YYYY-MM-DD HH:MM" 24h, worked out from the current time you are given), repeat ("none", "daily", "weekdays" or "weekly")
- task: add an item to the to-do list. args: text
- note: save a short quick note. args: text
- remember: remember a fact about the user. args: text
- screen: explain/read/summarise the screen or clipboard. args: question
- click: press/click a button, link or tab by the text visible on it. args: label (just the visible text)
- tab: switch browser tabs. args: target ("next", "previous", "last", "first", "new", a number, or part of a tab's title)
- copy_screen: copy the text visible on the screen to the clipboard. args: {}
- agent: any other task done by operating apps, websites or clicking. args: goal
- chat: casual talk or general knowledge. args: {}
Examples:
"open whatsapp in chrome and text Rahul I'll be late" -> {"skill":"whatsapp","args":{"contact":"Rahul","message":"I'll be late"}}
"open word and write an email to alex at gmail dot com about tomorrow's meeting and send it" -> {"skill":"email","args":{"to":"alex at gmail dot com","subject":"","instructions":"about tomorrow's meeting","use_word":true}}
"who won the cricket match yesterday" -> {"skill":"web","args":{"query":"cricket match result yesterday"}}
"remind me to drink water in 20 minutes" -> {"skill":"timer","args":{"seconds":1200,"label":"drink water"}}
"remind me every weekday at 9 to check email" -> {"skill":"reminder","args":{"text":"check email","when":"2025-01-06 09:00","repeat":"weekdays"}}
"add buy milk to my to do list" -> {"skill":"task","args":{"text":"buy milk"}}
"explain the error on my screen" -> {"skill":"screen","args":{"question":"explain the error on my screen"}}
"look at the screen and press the button that says AI mode" -> {"skill":"click","args":{"label":"AI mode"}}
"change to my youtube tab" -> {"skill":"tab","args":{"target":"youtube"}}
"copy all the text on the screen" -> {"skill":"copy_screen","args":{}}
"open notepad and type a shopping list" -> {"skill":"agent","args":{"goal":"open notepad and type a shopping list"}}
"write a leave letter to my principal in word" -> {"skill":"write_doc","args":{"instructions":"a leave letter to my principal"}}
"send it to alex at gmail dot com on gmail" (after a letter was written) -> {"skill":"email","args":{"to":"alex at gmail dot com","subject":"","instructions":"","use_word":false,"attach_last":true,"file_hint":"","paste":false}}
"attach that to the mail" -> {"skill":"attach","args":{"file_hint":""}}
"find my physics assignment pdf in downloads" -> {"skill":"find_file","args":{"hint":"physics assignment","location":"downloads","type":"pdf","action":"open","recent":false}}
"email my resume from the d drive to rahul" -> {"skill":"email","args":{"to":"rahul","subject":"","instructions":"","use_word":false,"attach_last":false,"file_hint":"resume d drive","paste":false}}
"open chrome and ask deepseek how to solve x squared minus 5x plus 6 equals zero and explain it" -> {"skill":"ask_ai","args":{"site":"deepseek","question":"solve x squared minus 5x plus 6 equals zero","mode":"explain","notes":false,"use_screen":false,"followup":false}}
"ask claude how to create a virtual environment in python and do what it says" -> {"skill":"ask_ai","args":{"site":"claude","question":"how to create a virtual environment in python","mode":"follow","notes":false,"use_screen":false,"followup":false}}
"look at this problem on my screen and get chatgpt to explain it" -> {"skill":"ask_ai","args":{"site":"chatgpt","question":"","mode":"explain","notes":false,"use_screen":true,"followup":false}}
"make notes on that" -> {"skill":"make_notes","args":{"topic":"","source":"last","in_word":false}}
"make notes on newton's laws" -> {"skill":"make_notes","args":{"topic":"newton's laws of motion","source":"topic","in_word":false}}
"latest news about technology" -> {"skill":"news","args":{"topic":"technology"}}
"how are you" -> {"skill":"chat","args":{}}"""


def fallback_route(raw):
    t = raw.lower()
    k = ai_site_from(t)
    if k and re.search(r"\b(ask|tell|use|get|let)\b", t):
        return {"skill": "ask_ai", "args": {"site": k, "question": re.sub(r"^.*?\b" + k + r"\b\s*(?:to|about|that)?\s*", "", t, count=1),
                                            "mode": "explain" if re.search(r"\b(explain|solve|maths?|problem)\b", t) else "answer",
                                            "use_screen": "screen" in t}}
    if re.search(r"\bmake\b.*\bnotes?\b|\bnotes? (?:on|about|of)\b", t):
        return {"skill": "make_notes", "args": {"topic": "" if re.search(r"\b(that|this|it)\b", t) else raw, "source": "last"}}
    if re.search(r"\battach\b", t) and not re.search(r"\bto\s+\S+@|\bat\s+\S+\s+dot\b", t):
        return {"skill": "attach", "args": {"file_hint": ""}}
    if re.match(r"(?:find|locate|look for|where(?:'s| is))\b", t) and re.search(r"\b(file|pdf|doc|document|folder|photo|picture|video)\b", t):
        return {"skill": "find_file", "args": {"hint": raw, "location": "", "type": "", "action": "open"}}
    m = re.match(r"(?:send|text|message|whatsapp)\s+(\w+)\s+(?:saying|that|message|:)?\s*(.+)", t)
    if m and ("whatsapp" in t or "text" in t or "message" in t):
        return {"skill": "whatsapp", "args": {"contact": m.group(1), "message": m.group(2)}}
    if re.search(r"\b(email|mail)\b", t):
        m2 = re.search(r"to\s+(\S+(?:\s+at\s+\S+\s+dot\s+\S+)?)", t)
        return {"skill": "email", "args": {"to": m2.group(1) if m2 else "", "subject": "", "instructions": raw,
                                          "use_word": "word" in t, "attach_last": bool(ATTACH_WORDS.search(t))}}
    return {"skill": "web" if re.match(r"(who|what|when|where|why|how)\b", t) else "chat", "args": {"query": raw}}


SKILLS = {
    "whatsapp": lambda a, raw: skill_whatsapp(a.get("contact"), a.get("message")),
    "email": lambda a, raw: skill_email(a.get("to"), a.get("subject"), a.get("instructions"), bool(a.get("use_word")),
                                        bool(a.get("attach_last")), a.get("file_hint") or "", bool(a.get("paste")), raw),
    "attach": lambda a, raw: skill_attach(a.get("file_hint") or "", raw),
    "find_file": lambda a, raw: skill_find_file(a.get("hint") or "", a.get("location") or "", a.get("type") or "",
                                                a.get("action") or "open", raw),
    "ask_ai": lambda a, raw: skill_ask_ai(a.get("site") or "", a.get("question") or "", a.get("mode") or "answer",
                                          bool(a.get("notes")), bool(a.get("use_screen")), bool(a.get("followup")), raw),
    "make_notes": lambda a, raw: skill_make_notes(a.get("topic") or "", a.get("source") or "", bool(a.get("in_word")), raw),
    "write_doc": lambda a, raw: skill_write_doc(a.get("instructions") or raw),
    "code": lambda a, raw: skill_code(a.get("instructions") or raw),
    "web": lambda a, raw: skill_web(a.get("query") or raw),
    "weather": lambda a, raw: skill_weather(a.get("city")),
    "news": lambda a, raw: skill_news(a.get("topic")),
    "youtube": lambda a, raw: skill_youtube(a.get("query") or raw),
    "timer": lambda a, raw: skill_timer(a.get("seconds") or parse_duration(raw), a.get("label", "")),
    "reminder": lambda a, raw: skill_reminder(a.get("text"), a.get("when"), a.get("repeat") or "none"),
    "task": lambda a, raw: skill_task(a.get("text")),
    "note": lambda a, raw: skill_note(a.get("text")),
    "remember": lambda a, raw: skill_remember(a.get("text")),
    "screen": lambda a, raw: skill_screen(a.get("question") or raw),
    "click": lambda a, raw: skill_click(a.get("label") or ""),
    "tab": lambda a, raw: skill_tab(a.get("target")),
    "copy_screen": lambda a, raw: skill_copy_screen(),
    "agent": lambda a, raw: skill_agent(a.get("goal") or raw),
}


def route_and_run(raw):
    ctx_txt = recent_context(4 if groq_available() else 2)
    art = artifacts_block()
    content = (f"Recent conversation (context only, for follow-ups like 'send it to him' or 'attach it'):\n"
               f"{ctx_txt}\n\n{art}\n\nNew command: {raw}") if ctx_txt else (f"{art}\n\nNew command: {raw}" if art else raw)
    content = f"Current local time: {datetime.datetime.now():%A %Y-%m-%d %H:%M}\n" + content
    plan = llm_json([{"role": "system", "content": ROUTER_SYS}, {"role": "user", "content": content}],
                    task="fast", max_tokens=260, temp=0.1)
    if not plan or "skill" not in plan:
        plan = fallback_route(raw)
    global LAST_SKILL
    skill, args = plan.get("skill"), plan.get("args") or {}
    LAST_SKILL = str(skill or "chat")
    if not isinstance(args, dict):
        args = {}
    print(f"(plan: {skill} {args})")
    if skill in SKILLS:
        return SKILLS[skill](args, raw)
    ans = chat_reply(raw)
    speak(ans or "Sorry, I couldn't reach my brain just now. Basic commands like opening apps still work.")


# =====================================================================
#  FAST RULES (instant, no AI)
# =====================================================================
COMPOUND = re.compile(r"\b(and then|then|and (?:text|message|send|write|email|mail|search|play|type|click|tell|ask|call|open))\b")
EXIT_WORDS = {"exit", "quit", "goodbye", "bye", "bye ashie", "stop assistant", "close assistant", "shut down assistant"}
HELP = ("I can open apps and websites, text people on WhatsApp, write documents in Word and email them with the file attached, "
        "find files on your computer, ask chatbots like Claude or DeepSeek and explain their answers, make notes, "
        "write code, search the web, set timers, read your screen, click things you name, control volume, "
        "windows and settings with your confirmation, and just chat. Start every command with my name.")


def rules(raw, t):
    """Returns None if not handled, 'ok' if handled, 'exit' to quit."""
    if t in EXIT_WORDS:
        speak(f"Bye {USER_NAME}! Call me anytime.")
        return "exit"
    if t in ("help", "what can you do"):
        speak(HELP); return "ok"
    if re.search(r"\bvoice\s*(?:lock|recognition|verification|check|match(?:ing)?)\b", t) or \
            re.search(r"(?:respond|listen|answer)\s+to\s+(?:anyone|everyone|any\s+voice)|(?:stop|don'?t)\s+(?:checking|verifying)\s+my\s+voice|only\s+(?:respond|listen)\s+to\s+me", t):
        if re.match(r"(?:is|what|what's|whats|status|check)\b", t):
            speak("Voice lock is " + ("on" if VOICE_LOCK_ENABLED else "off") + ".")
            return "ok"
        off = re.search(r"\b(?:off|disable|disabled|skip|stop|without|no)\b|don'?t|anyone|everyone|any\s+voice", t)
        on = re.search(r"\b(?:on|enable|enabled|start|only)\b", t)
        if off and not (on and re.search(r"\bturn on\b|\benable\b|only", t)):
            set_voice_lock(False)
            speak(f"Voice lock off. I'll answer whenever anyone says {ASSISTANT_NAME}.")
            return "ok"
        if on:
            if not VOICE_PROFILE_PATH.exists():
                set_voice_lock(True)
                enroll_voice()
            else:
                set_voice_lock(True)
                speak(f"Voice lock on. I'll only obey your voice, {USER_NAME}.")
            return "ok"
        speak("Voice lock is " + ("on" if VOICE_LOCK_ENABLED else "off") + ". Say turn voice lock on or off to change it.")
        return "ok"
    if re.fullmatch(r"who (made|created|built|programmed) you|who is your (creator|maker|developer)", t):
        speak(f"{USER_NAME} set me up, and I'm here to help however I can."); return "ok"
    if t in ("who am i", "what's my name", "what is my name"):
        speak(f"You're {USER_NAME}, the person I work for."); return "ok"
    if t in ("what's your name", "what is your name", "who are you"):
        speak(f"I'm {ASSISTANT_NAME}, {USER_NAME}'s personal assistant."); return "ok"
    if t in ("thank you", "thanks", "thank you ashie"):
        speak(f"Anytime, {USER_NAME}!"); return "ok"

    # "look at the screen and press ..." -> just the action part
    _m = re.match(r"^\s*(?:please\s+)?(?:look at|check|see|scan|watch)\s+(?:the|my|this)\s+screen\s*(?:,|and then|and|then)?\s*"
                  r"(?=(?:press|click|tap|hit|push|select|choose|copy|switch|change)\b)", raw, re.I)
    if _m:
        raw = raw[_m.end():].strip()
        t = norm(raw)

    compound = bool(COMPOUND.search(t))

    # notes / files: let the router (which knows what I made earlier) decide
    if re.match(r"(?:please\s+)?(?:make|write|create|prepare|take|give me)\s+(?:me\s+)?(?:some\s+|a few\s+)?"
                r"(?:short\s+|brief\s+|detailed\s+|study\s+|quick\s+)?notes?\b", t):
        route_and_run(raw); return "ok"
    _FW = (r"(?:files?|documents?|docs?|pdfs?|ppts?|powerpoint|presentations?|spreadsheets?|excel|sheets?|photos?|pictures?|"
           r"images?|videos?|songs?|resume|cv|assignment|report|invoice|certificate|marksheet|syllabus|folder)")
    if not compound and re.match(r"(?:please\s+)?(?:find|search(?: for)?|locate|look for|where(?:'s| is| are)|show me|pull up|get me|"
                                 r"open|bring up)\b", t) and not re.match(r"open folder\b", t) and (
            re.search(r"\b(?:in|on|from|inside) (?:my |the )?(?:downloads?|documents?|desktop|pictures|videos|music|onedrive|[c-z] drive|\w+ folder)\b", t)
            or (re.search(r"\b" + _FW + r"\b", t) and re.search(
                r"\b(?:on|in) my (?:computer|pc|laptop|system|drive)\b|\b(?:my|the) \w+ (?:file|pdf|doc|document|folder|ppt)\b|\bfile\b|\bfolder\b", t))):
        route_and_run(raw); return "ok"

    # dictation & typing
    if t in ("start dictation", "start typing", "dictation mode"):
        speak("Dictation on. Say stop dictation to finish.")
        while True:
            d = listen(timeout=10, limit=20)
            if not d:
                continue
            if norm(d) in ("stop dictation", "stop typing", "end dictation"):
                speak("Dictation off."); break
            if norm(d) == "new line":
                pyautogui.press("enter")
            else:
                type_text(d + " ")
        return "ok"
    m = re.match(r"\s*type\s+(.+)", raw, re.I)
    if m and not compound:
        type_text(m.group(1).strip().rstrip(".")); return "ok"
    if (r := after(t, "press")) and not compound:
        keymap = {"enter": "enter", "escape": "esc", "tab": "tab", "space": "space", "backspace": "backspace",
                  "delete": "delete", "up": "up", "down": "down", "left": "left", "right": "right",
                  "home": "home", "end": "end", "page up": "pageup", "page down": "pagedown"}
        if r in keymap:
            pyautogui.press(keymap[r]); return "ok"
    simple = {"select all": ("ctrl", "a"), "copy": ("ctrl", "c"), "copy this": ("ctrl", "c"), "copy that": ("ctrl", "c"),
              "copy it": ("ctrl", "c"), "copy selected text": ("ctrl", "c"), "copy selection": ("ctrl", "c"), "paste": ("ctrl", "v"), "cut": ("ctrl", "x"),
              "undo": ("ctrl", "z"), "redo": ("ctrl", "y"), "save": ("ctrl", "s"), "find": ("ctrl", "f"),
              "print": ("ctrl", "p"), "new tab": ("ctrl", "t"), "open new tab": ("ctrl", "t"),
              "close tab": ("ctrl", "w"), "close this tab": ("ctrl", "w"), "reopen tab": ("ctrl", "shift", "t"),
              "next tab": ("ctrl", "tab"), "previous tab": ("ctrl", "shift", "tab"), "go back": ("alt", "left"),
              "back": ("alt", "left"), "go forward": ("alt", "right"), "forward": ("alt", "right"),
              "zoom in": ("ctrl", "="), "zoom out": ("ctrl", "-"), "reset zoom": ("ctrl", "0"),
              "incognito": ("ctrl", "shift", "n"), "bookmark this": ("ctrl", "d"), "history": ("ctrl", "h"),
              "minimize": ("win", "down"), "maximize": ("win", "up"), "show desktop": ("win", "d"),
              "minimize all": ("win", "d"), "snap left": ("win", "left"), "snap right": ("win", "right"),
              "switch window": ("alt", "tab"), "next window": ("alt", "tab"), "task view": ("win", "tab")}
    if t in simple:
        pyautogui.hotkey(*simple[t]); return "ok"
    single = {"refresh": "f5", "reload": "f5", "refresh page": "f5", "full screen": "f11", "fullscreen": "f11",
              "page down": "pagedown", "page up": "pageup", "mute": "volumemute", "unmute": "volumemute",
              "pause": "playpause", "next song": "nexttrack", "next track": "nexttrack", "skip": "nexttrack",
              "previous song": "prevtrack", "previous track": "prevtrack"}
    if t in single:
        pyautogui.press(single[t]); return "ok"
    if (r := after(t, "go to tab", "switch to tab", "tab")) and re.search(r"\d|one|two|three|four|five|six|seven|eight", r):
        pyautogui.hotkey("ctrl", str(min(num(r), 8))); return "ok"
    if (r := after(t, "scroll")) is not None:
        if "top" in r: pyautogui.hotkey("ctrl", "home")
        elif "bottom" in r: pyautogui.hotkey("ctrl", "end")
        elif "up" in r: pyautogui.scroll(600 * num(r))
        else: pyautogui.scroll(-600 * num(r))
        return "ok"

    # volume / brightness / media
    if (r := after(t, "volume up", "increase volume", "louder")) is not None:
        [pyautogui.press("volumeup") for _ in range(5 * num(r))]; return "ok"
    if (r := after(t, "volume down", "decrease volume", "lower volume", "quieter")) is not None:
        [pyautogui.press("volumedown") for _ in range(5 * num(r))]; return "ok"
    if (r := after(t, "set volume to", "volume")) and re.search(r"\d", r):
        n = max(0, min(100, num(r)))
        [pyautogui.press("volumedown") for _ in range(50)]
        [pyautogui.press("volumeup") for _ in range(n // 2)]
        speak(f"Volume {n} percent."); return "ok"
    if (r := after(t, "set brightness to", "brightness")) and re.search(r"\d", r):
        try:
            import screen_brightness_control as sbc
            sbc.set_brightness(max(0, min(100, num(r)))); speak("Brightness changed.")
        except Exception:
            speak("Brightness control isn't available on this display.")
        return "ok"

    # info
    if "time" in t.split() and len(t.split()) <= 5 and "timer" not in t:
        speak(datetime.datetime.now().strftime("It's %I:%M %p.")); return "ok"
    if t in ("what's the date", "what is the date", "date", "what day is it", "today's date"):
        speak(datetime.datetime.now().strftime("Today is %A, %d %B.")); return "ok"
    if "battery" in t and "settings" not in t and len(t.split()) <= 6:
        b = psutil.sensors_battery()
        speak(f"Battery is at {int(b.percent)} percent." if b else "I don't see a battery."); return "ok"
    if t in ("system status", "cpu usage", "memory usage", "ram usage"):
        speak(f"CPU {psutil.cpu_percent(interval=0.5):.0f} percent, memory {psutil.virtual_memory().percent:.0f} percent.")
        return "ok"
    if t in ("take a screenshot", "screenshot", "take screenshot"):
        p = HOME / "Pictures" / f"screenshot_{int(time.time())}.png"
        pyautogui.screenshot(str(p)); speak("Screenshot saved in Pictures."); return "ok"
    if (r := after(t, "calculate", "calc")):
        try:
            v = safe_eval(r); speak(f"That's {round(v, 6):g}.")
        except Exception:
            speak("I couldn't work that out.")
        return "ok"
    if re.search(r"\btimer\b", t) and parse_duration(t) and not compound:
        skill_timer(parse_duration(t)); return "ok"
    if re.match(r"\s*(?:please\s+)?remind me\b", raw, re.I) and not compound:
        parts = store.split_reminder(raw)
        if parts:
            skill_reminder(parts[0], parts[1], parts[2]); return "ok"
    if re.match(r"(what|list|show|read|tell me)\b.*\breminders\b", t):
        skill_list_reminders(); return "ok"
    if re.match(r"(cancel|delete|remove|clear) (my |the |that )?reminder", t):
        skill_cancel_reminder(re.sub(r"^(cancel|delete|remove|clear) (my |the |that )?reminder( about| to| for)?", "", t)); return "ok"
    if (r := after(t, "add task", "add a task", "new task", "todo", "to do")) is not None:
        skill_task(r); return "ok"
    if (m := re.match(r"add (.+?) to (?:my |the )?(?:to do|todo|task)(?: list)?$", t)):
        skill_task(m.group(1)); return "ok"
    if re.search(r"\b(open|show) (?:your |the |my )?dashboard\b", t):
        webbrowser.open(f"http://127.0.0.1:{DASHBOARD_PORT}"); speak("Opening my dashboard."); return "ok"
    # memory
    if t in ("clear memory", "clear your memory", "wipe your memory", "erase your memory", "forget everything",
             "forget everything about me"):
        if confirm("This erases everything I've learned about you and our recent chats. Are you sure?"):
            clear_all_memory(); speak("Okay, my memory is wiped.")
        return "ok"
    if t in ("clear history", "clear conversation", "forget our conversation", "forget this conversation"):
        clear_history(); speak("Okay, fresh start."); return "ok"
    if t in ("new conversation", "reset conversation", "start a new conversation"):
        clear_history(archive=True)
        speak("Okay, fresh start. I'll still remember what we talked about."); return "ok"
    if (r := after(t, "forget that", "forget about", "forget")) and not compound:
        gone = forget_fact(r)
        speak(f"Okay, I've forgotten that {gone}." if gone else "I don't have anything like that in my memory.")
        return "ok"
    if (r := after(t, "remember that", "remember")):
        skill_remember(re.sub(r"^\s*remember( that)?\s*", "", raw, flags=re.I).strip()); return "ok"
    if (r := after(t, "take a note", "note down", "note")):
        skill_note(r); return "ok"
    if t in ("what do you remember", "what do you know about me", "what do you remember about me"):
        f = MEM["facts"][-6:]
        if f:
            speak(f"I know {len(MEM['facts'])} things about you. The latest: " + "; ".join(f) + ".")
        elif MEM.get("summary"):
            speak("Mostly from our chats. " + MEM["summary"][:300])
        else:
            speak("Nothing special yet. Tell me to remember things!")
        return "ok"

    # browser tabs, copying screen text, natural-language clicks
    if not compound:
        _m = re.fullmatch(r"(?:change|switch|move|go|jump)(?: over)?(?: to)?(?: the| my| a)?"
                          r"(?: (next|previous|prior|last|first|new|other|another))?(?: browser| chrome)? tabs?"
                          r"(?: (?:in|on) (?:the |my )?(?:browser|chrome))?", t)
        if _m:
            skill_tab(_m.group(1) or "next"); return "ok"
        _m = re.fullmatch(r"(?:change|switch|go|move|jump)(?: over)? to (?:the )?(.+?) (?:tab|page)"
                          r"(?: (?:in|on) (?:the |my )?(?:browser|chrome))?", t)
        if _m:
            skill_tab(_m.group(1)); return "ok"
        if re.match(r"copy\b", t) and re.search(r"\b(screen|page|window)\b", t) and \
                re.search(r"\b(text|words|everything|content|screen)\b", t):
            skill_copy_screen(); return "ok"
        _m = re.search(r"\b(?:press|click|tap|hit|push|select|choose)\b.*?\b(?:says|saying|reads|reading|labell?ed|"
                       r"called|named|titled|with the text)\s+(.+)$", t)
        if _m:
            skill_click(_m.group(1)); return "ok"
        if (r := after(t, "press", "hit", "push")) and re.search(r"\b(button|link|icon|option|menu)\b|^(on|the)\b", r):
            skill_click(r); return "ok"
    if t in ("what have you learned about me", "how do i talk", "what's my style", "what is my style", "describe me"):
        speak(MEM["profile"][:420] if MEM.get("profile") else "I'm still learning how you talk. Chat with me a bit more!")
        return "ok"

    # mouse & clicking
    if t in ("what can i click", "show options", "list options", "what's on screen", "what is on screen"):
        list_clickables(); return "ok"
    if t == "click": pyautogui.click(); return "ok"
    if t == "double click": pyautogui.doubleClick(); return "ok"
    if t == "right click": pyautogui.rightClick(); return "ok"
    if not compound:
        if (r := after(t, "double click")): click_label(r, 2); return "ok"
        if (r := after(t, "right click")): click_label(r, 1, "right"); return "ok"
        if (r := after(t, "click", "select", "choose", "tap", "pick")):
            click_label(clean_label(r)); return "ok"
        if (r := after(t, "move mouse", "move the mouse", "mouse")):
            step = 150 * num(r)
            dx = step if "right" in r else -step if "left" in r else 0
            dy = step if "down" in r else -step if "up" in r else 0
            pyautogui.moveRel(dx, dy, duration=0.2); return "ok"

    # windows
    if (r := after(t, "switch to", "focus")) and not compound:
        if not focus_window(r) and not (is_browser() and tab_by_name(clean_label(r))):
            speak(f"I couldn't find a window or tab called {r}.")
        return "ok"
    if t in ("close window", "close this window", "close this"):
        if confirm("Do you want me to close the current window?"): pyautogui.hotkey("alt", "f4")
        return "ok"

    # volume
    if t in ("mute", "mute volume", "unmute", "unmute volume"):
        set_mute("unmute" not in t); return "ok"
    if t in ("volume up", "turn volume up", "increase volume", "louder"):
        nudge_volume(10); return "ok"
    if t in ("volume down", "turn volume down", "decrease volume", "quieter"):
        nudge_volume(-10); return "ok"
    if (r := after(t, "set volume to", "volume to", "set the volume to")) and not compound:
        set_volume(num(r, 50)); return "ok"

    # brightness
    if t in ("brightness up", "increase brightness", "screen brighter"):
        nudge_brightness(10); return "ok"
    if t in ("brightness down", "decrease brightness", "screen dimmer"):
        nudge_brightness(-10); return "ok"
    if (r := after(t, "set brightness to", "brightness to")) and not compound:
        set_brightness(num(r, 70)); return "ok"

    # screenshot
    if t in ("take a screenshot", "screenshot", "take screenshot", "capture screen"):
        take_screenshot(); return "ok"

    # power & system (always confirm)
    power = {
        ("shut down", "shutdown", "shut down computer", "turn off computer", "power off"):
            ("shut down the computer", "shutdown /s /t 10", "Shutting down in 10 seconds. Say cancel shutdown to stop it."),
        ("restart", "restart computer", "reboot"):
            ("restart the computer", "shutdown /r /t 10", "Restarting in 10 seconds. Say cancel shutdown to stop it."),
        ("sleep", "sleep computer", "go to sleep"):
            ("put the computer to sleep", "rundll32.exe powrprof.dll,SetSuspendState 0,1,0", ""),
        ("lock", "lock computer", "lock screen", "lock the computer"):
            ("lock the computer", "rundll32.exe user32.dll,LockWorkStation", ""),
        ("sign out", "log out", "log off"): ("sign out", "shutdown /l", ""),
        ("wifi off", "turn off wifi", "turn wifi off", "disable wifi"):
            ("turn off Wi-Fi", 'netsh interface set interface "Wi-Fi" disable', "This needs administrator rights."),
        ("wifi on", "turn on wifi", "turn wifi on", "enable wifi"):
            ("turn on Wi-Fi", 'netsh interface set interface "Wi-Fi" enable', ""),
        ("empty recycle bin", "empty the recycle bin"):
            ("empty the recycle bin permanently", 'powershell -command "Clear-RecycleBin -Force"', ""),
    }
    for keys, (desc, cmd, note) in power.items():
        if t in keys:
            if confirm(f"Do you want me to {desc}?"):
                sh(cmd)
                if note: speak(note)
            return "ok"
    if t in ("cancel shutdown", "abort shutdown"):
        sh("shutdown /a"); speak("Shutdown cancelled."); return "ok"
    if (r := after(t, "run command", "execute")):
        if confirm(f"Do you want me to run the command: {r}?"):
            o = subprocess.run(r, shell=True, capture_output=True, text=True, timeout=60)
            speak((o.stdout or o.stderr or "Done.").strip()[:250])
        return "ok"
    if (r := after(t, "close", "quit", "kill")) and not compound:
        proc = PROCS.get(r, r.replace(" ", "") + ".exe").lower()
        v = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == proc]
        if not v: speak(f"I don't see {r} running.")
        elif confirm(f"Do you want me to close {r}? Unsaved work may be lost."):
            for p in v:
                try: p.terminate()
                except Exception: pass
            speak(f"Closed {r}.")
        return "ok"

    # web / opening things
    if (r := after(t, "search youtube for", "youtube search")) and not compound:
        speak(f"Searching YouTube for {r}.")
        open_chrome("https://www.youtube.com/results?search_query=" + urllib.parse.quote(r)); return "ok"
    if (r := after(t, "play")) and not compound:
        skill_youtube(r); return "ok"
    if (r := after(t, "search for", "search", "google", "look up")) and not compound:
        speak(f"Searching for {r}.")
        open_chrome("https://www.google.com/search?q=" + urllib.parse.quote(r)); return "ok"
    if (r := after(t, "go to", "visit", "browse")) and not compound:
        url = SITES.get(r) or ("https://" + r.replace(" dot ", ".").replace(" ", "") if "." in r or " dot " in r
                               else "https://" + r.replace(" ", "") + ".com")
        speak(f"Going to {r}."); open_chrome(url); return "ok"
    if (r := after(t, "open", "launch", "start")) and not compound:
        if "settings" in r:
            key = r.replace("settings", "").strip()
            if key in ("", "windows", "system", "pc"):
                if confirm("Do you want me to open Windows settings?"): sh("start ms-settings:")
            else:
                page = next((v for k, v in SETTINGS.items() if k in key), None)
                if not page: speak(f"I don't know a settings page called {key}.")
                elif confirm(f"Do you want me to open {key} settings?"): sh(f"start ms-settings:{page}")
            return "ok"
        if r.startswith("folder "):
            name = r[7:].strip()
            sh(f'explorer "{HOME / FOLDERS.get(name, name)}"'); speak(f"Opening {name}."); return "ok"
        if r in SITES:
            speak(f"Opening {r}."); open_chrome(SITES[r]); return "ok"
        if len(r.split()) <= 4:
            open_app(r); return "ok"
    return None


# =====================================================================
#  MAIN LOOP
# =====================================================================
HOTKEY_EVT = threading.Event()
COMMAND_Q = queue.Queue()          # commands typed on the dashboard land here
STARTED = time.time()


def _brain_label():
    if groq_available():
        return "Cloud brain"
    return "Local brain" if ollama_up() else "Brain offline"


def status_loop():
    while True:
        try:
            store.set_status(brain=_brain_label(), model=GROQ_CHAT_MODELS[0] if groq_available() else LLM_MODEL,
                             started=STARTED, voice_lock=bool(VOICE_LOCK_ENABLED), hotkey=(HOTKEY or "").upper())
            if GROQ_KEY and time.time() - _last_refresh > 6 * 3600:
                threading.Thread(target=refresh_groq_models, daemon=True).start()
        except Exception:
            pass
        time.sleep(5)


def _dash_memory():
    with _mem_lock:
        return {"facts": list(MEM["facts"]), "summary": MEM.get("summary", ""), "profile": MEM.get("profile", ""),
                "stats": MEM.get("stats", {}), "samples": len(MEM.get("samples", [])),
                "history": list(HISTORY)[-20:], "episodes": len(EPI)}


def start_dashboard():
    try:
        import dashboard
        url = dashboard.start(DASHBOARD_PORT, hooks={
            "memory": _dash_memory,
            "fact_add": lambda t: add_fact(t) or True,
            "fact_delete": lambda t: bool(forget_fact(t)),
            "command": COMMAND_Q.put}, background=True, open_browser=DASHBOARD_OPEN_BROWSER)
        print(f"Dashboard: {url}")
    except OSError:
        print(f"(dashboard: port {DASHBOARD_PORT} is busy - change DASHBOARD_PORT in config.json)")
    except Exception as e:
        print("(dashboard unavailable)", e)


def setup_wizard():
    """python ashie.py --setup : asks a few questions and writes ~/Ashie/config.json."""
    print(f"=== Ashie setup ===  (saved to {CONFIG_PATH}; you can edit that file any time)\n")
    cfg = load_json(CONFIG_PATH, {})
    name = input(f"Your name [{cfg.get('user_name', USER_NAME)}]: ").strip()
    cfg["user_name"] = name or cfg.get("user_name", USER_NAME)
    about = input("One line about you, optional (e.g. 'Lives in Berlin, works as a designer'): ").strip()
    if about:
        cfg["user_profile"] = about
    city = input("Your city for weather, blank = auto-detect: ").strip()
    if city:
        cfg["default_city"] = city
    cfg.setdefault("assistant_name", ASSISTANT_NAME)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    apply_config()
    print("Saved. Next: python ashie.py --set-groq-key (optional cloud brain), then python ashie.py\n")


GREET_WORDS = {"hey", "hay", "hi", "hii", "hiya", "hello", "ok", "okay", "yo", "oh"}


def _is_wake(w):
    return bool(w) and (w in WAKE_VARIANTS or difflib.SequenceMatcher(None, w, ASSISTANT_NAME.lower()).ratio() >= 0.8)


def strip_wake(raw):
    """Finds 'Ashie' / 'Hey Ashie' in the first few words. Also catches 'a she' / 'ash ie' style splits."""
    # Strict: the name must be the FIRST word, or come right after "hey / hi / ok". Saying it mid-sentence doesn't count.
    words = raw.split()
    clean = [re.sub(r"[^a-z]", "", w.lower()) for w in words]
    i = 0
    while i < min(2, len(words)) and clean[i] in GREET_WORDS:
        i += 1
    if i < len(words) and clean[i]:
        if i + 1 < len(words) and clean[i + 1] and len(clean[i]) <= 3 and (clean[i] + clean[i + 1]) in WAKE_VARIANTS:
            return True, " ".join(words[i + 2:]).strip(" ,.!?")      # heard as "a she" / "ash ie"
        if _is_wake(clean[i]):
            return True, " ".join(words[i + 1:]).strip(" ,.!?")
    return False, raw


def wait_for_wake():
    """Idle listening. Returns None (nothing / not for her), '' (name only), or the command text."""
    t0 = time.time()
    arr = hear_audio(timeout=2, limit=12)
    if arr is None:
        return None
    gate = transcribe_array(arr, fast=True)               # tiny model: just spot the name
    if not gate:
        return None
    woke, rest = strip_wake(gate)
    if not woke:
        print("(ignored: no wake word)", "-", gate[:60])
        return None
    if VOICE_LOCK_ENABLED and VOICE_PROFILE_PATH.exists() and not is_your_voice(arr):
        print(f"🔒 ignored (voice didn't match {USER_NAME}):", gate)
        return None
    if rest and _whisper_fast is not _whisper:            # she was named AND given a command -> re-hear it accurately
        full = transcribe_array(arr)
        woke2, rest2 = strip_wake(full)
        if woke2:
            rest = rest2
    print(f"🎤 heard: {gate if not rest else rest}   (wake check {time.time() - t0:.2f}s)")
    store.set_status(last_heard=(rest or gate)[:240])
    return rest


def beep():
    try:
        import winsound
        winsound.Beep(1000, 90)
    except Exception:
        pass


def process(raw):
    """Runs one command and files the exchange (what you said + what she said) into memory."""
    global _capture, LAST_SKILL
    t = norm(raw)
    if not t:
        return True
    _capture = []
    LAST_SKILL = "rule"
    store.set_status(state="thinking")
    try:
        r = rules(raw, t)
        if r == "exit":
            return False
        if r is None:
            route_and_run(raw)
    finally:
        reply = " ".join(_capture or [])
        _capture = None
    if reply:
        record_exchange(raw, reply)
        note_usage(raw, LAST_SKILL)
        learn_async(raw)
        store.log("skill", f"{LAST_SKILL}: {raw[:90]}")
    store.set_status(state="listening")
    return True


def greet():
    h = datetime.datetime.now().hour
    g = "Good morning" if h < 12 else "Good afternoon" if h < 17 else "Good evening"
    speak(f"{g} {USER_NAME}! {ASSISTANT_NAME} here and ready. Start every command with {ASSISTANT_NAME} or hey {ASSISTANT_NAME}.")


def main():
    global SRC, VOICE_LOCK_ENABLED
    if "--no-voice-lock" in sys.argv:
        VOICE_LOCK_ENABLED = False
    if "--setup" in sys.argv:
        return setup_wizard()
    if not CONFIG_PATH.exists() and sys.stdin and sys.stdin.isatty():
        setup_wizard()
    if "--mics" in sys.argv:
        for i, n in enumerate(sr.Microphone.list_microphone_names()):
            print(i, n)
        return
    if "--set-groq-key" in sys.argv:
        return set_groq_key()
    if "--test-voice" in sys.argv:
        return test_voice()
    if "--test-brain" in sys.argv:
        return test_brain()
    print(f"=== {ASSISTANT_NAME} starting ===")
    print(_CONFIG_NOTE)
    print(f"Voice lock: {'ON (only your voice)' if VOICE_LOCK_ENABLED else 'OFF (anyone saying the name triggers her)'}"
          f" | wake model: {WAKE_MODEL if WAKE_FAST else WHISPER_MODEL}")
    if GROQ_KEY:
        print(f"Brain: Groq cloud ({GROQ_CHAT_MODELS[0]}) when online, local {LLM_MODEL} as backup")
    else:
        print(f"Brain: local {LLM_MODEL} only. Run  python ashie.py --set-groq-key  to add the cloud brain")
    print(f"Memory: {len(HISTORY)} recent exchanges, {len(MEM['facts'])} facts, {len(EPI)} older memories loaded")
    EPI.start()          # loads the semantic-memory model in the background while speech loads
    store.set_status(state="starting")
    threading.Thread(target=status_loop, daemon=True).start()
    threading.Thread(target=reminder_loop, daemon=True).start()
    threading.Thread(target=refresh_groq_models, daemon=True).start()
    if DASHBOARD_ENABLED and "--no-dashboard" not in sys.argv:
        start_dashboard()
    init_whisper()
    if HOTKEY:
        try:
            import keyboard
            keyboard.add_hotkey(HOTKEY, HOTKEY_EVT.set)
        except Exception:
            print("(hotkey unavailable - wake word still works)")
    mic = sr.Microphone(device_index=MIC_INDEX)
    with mic as SRC:
        print("Calibrating microphone, stay quiet for a second...")
        rec.adjust_for_ambient_noise(SRC, duration=1.2)
        if "--enroll-voice" in sys.argv:
            enroll_voice()
            return
        if VOICE_LOCK_ENABLED and not VOICE_PROFILE_PATH.exists():
            enroll_voice()
        greet()
        store.set_status(state="listening")
        while True:
            try:
                try:
                    typed = COMMAND_Q.get_nowait()          # typed on the dashboard (no voice, so no wake word)
                except queue.Empty:
                    typed = None
                if typed:
                    if not process(typed):
                        break
                    continue
                # EVERY spoken command must start with "Ashie" / "Hey Ashie"; anything else is ignored.
                if HOTKEY and HOTKEY_EVT.is_set():
                    HOTKEY_EVT.clear()
                    beep()
                    rest = listen(timeout=8, limit=14)
                    if not rest:
                        continue
                else:
                    rest = wait_for_wake()
                    maybe_stop_ollama()
                    if rest is None:
                        continue
                if not rest:                                # only her name was heard: wait for the command itself
                    beep()
                    rest = listen(timeout=7, limit=14)
                    if not rest:
                        speak(f"Yes {USER_NAME}? I'm listening.")
                        continue
                if not process(rest):
                    break
            except pyautogui.FailSafeException:
                speak("Emergency stop triggered.")
            except KeyboardInterrupt:
                break
            except Exception:
                traceback.print_exc()
                speak("Sorry, I hit a snag with that one.")
    store.set_status(state="offline", heartbeat=0)
    stop_ollama()


if __name__ == "__main__":
    main()
