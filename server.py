import os
import re
import sys
import ast
import json
import time
import uuid
import base64
import shutil
import platform
import psutil
import threading
import subprocess
import urllib.request
import importlib.util
import importlib.machinery
from pathlib import Path
from fastapi import FastAPI, APIRouter, Request, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

ADMIN_TOKEN = os.environ.get("API_TOKEN", "kosaihost348").strip()
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = str(os.environ.get("ADMIN_CHAT_ID", "")).strip()
PORT = 7860
CURRENT_URL = ""

BASE_DIR = Path("bot_host_data")
BOTS_DIR = BASE_DIR / "bots"
USERS_FILE = BASE_DIR / "users.json"
BASE_DIR.mkdir(parents=True, exist_ok=True)
BOTS_DIR.mkdir(parents=True, exist_ok=True)

START_TIME = time.time()
DATA_LOCK = threading.Lock()
RUNNING_PROCS = {}
STARTING_BOTS = set()
STOPPED_BOTS = set()
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
router = APIRouter(prefix="/api")

PIP_NAMES = {
    "telegram": "python-telegram-bot",
    "telebot": "pyTelegramBotAPI",
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "sklearn": "scikit-learn",
    "dateutil": "python-dateutil",
    "Crypto": "pycryptodome",
    "jwt": "PyJWT",
    "discord": "discord.py",
    "googleapiclient": "google-api-python-client",
    "serial": "pyserial",
    "attr": "attrs",
    "magic": "python-magic",
    "docx": "python-docx",
    "fitz": "PyMuPDF",
    "aiogram": "aiogram",
    "pyrogram": "pyrogram",
    "telethon": "Telethon",
}
BANNED_PKGS = {"opencv-python", "opencv-python-headless", "cv2", "numpy", "ddddocr", "tensorflow", "torch"}
STDLIB = getattr(sys, "stdlib_module_names", {
    "os", "sys", "time", "json", "re", "math", "random", "asyncio", "ast",
    "subprocess", "pathlib", "urllib", "http", "datetime", "threading", "base64",
    "uuid", "shutil", "inspect", "importlib", "functools", "collections", "io",
})


def load_users():
    with DATA_LOCK:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8")) if USERS_FILE.exists() else {}
        if ADMIN_TOKEN not in data:
            data[ADMIN_TOKEN] = {
                "name": "Admin (Kosai)",
                "role": "admin",
                "max_ram_mb": 4096,
                "max_bots": 15,
                "bots": {},
            }
            USERS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data


def save_users(data):
    with DATA_LOCK:
        USERS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def require_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid Access Key")
    token = authorization.split(" ", 1)[1].strip()
    if token not in load_users():
        raise HTTPException(status_code=401, detail="Invalid Access Key")
    return token


def is_bot_running(bid: str) -> bool:
    proc = RUNNING_PROCS.get(bid)
    return proc is not None and proc.poll() is None


def get_bot_metrics(bid: str):
    if bid in STARTING_BOTS:
        return True, 0.0, 10
    proc = RUNNING_PROCS.get(bid)
    if proc is None or proc.poll() is not None:
        return False, 0.0, 0
    try:
        p = psutil.Process(proc.pid)
        cpu = p.cpu_percent(interval=0.05)
        ram_mb = int(p.memory_info().rss / (1024 * 1024))
        for child in p.children(recursive=True):
            cpu += child.cpu_percent(interval=0.0)
            ram_mb += int(child.memory_info().rss / (1024 * 1024))
        return True, round(cpu, 1), ram_mb
    except Exception:
        return True, 0.0, 15


def kill_orphan(bdir: Path):
    pf = bdir / "pid.txt"
    try:
        pid = int(pf.read_text().strip())
        if psutil.pid_exists(pid):
            p = psutil.Process(pid)
            p.terminate()
            p.wait(timeout=2)
    except Exception:
        pass
    try:
        pf.unlink()
    except FileNotFoundError:
        pass


def needed_packages(bdir: Path, fname: str):
    try:
        tree = ast.parse((bdir / fname).read_text(encoding="utf-8", errors="ignore"))
    except (SyntaxError, ValueError):
        return []
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])

    libs = bdir / "libs"
    missing = []
    for n in sorted(names):
        if n in STDLIB or n == "__future__" or n.lower() in BANNED_PKGS:
            continue
        if (bdir / f"{n}.py").exists() or (bdir / n).is_dir():
            continue
        try:
            if importlib.util.find_spec(n) is not None:
                continue
        except (ImportError, ValueError):
            pass
        if libs.is_dir() and importlib.machinery.PathFinder.find_spec(n, [str(libs)]):
            continue
        pkg = PIP_NAMES.get(n, n)
        if pkg.lower() not in BANNED_PKGS:
            missing.append(pkg)
    return missing


def find_bot_filename(bdir: Path) -> str:
    meta = bdir / "meta.json"
    return json.loads(meta.read_text(encoding="utf-8")).get("filename", "bot.py") if meta.exists() else "bot.py"


def install_packages(bdir: Path, log_path: Path, bid: str, extra=()):
    pkgs = list(dict.fromkeys(list(extra) + needed_packages(bdir, find_bot_filename(bdir))))
    if not pkgs:
        return
    libs = bdir / "libs"
    libs.mkdir(exist_ok=True)
    for pkg in pkgs:
        if bid in STOPPED_BOTS or pkg.lower() in BANNED_PKGS:
            continue
        with open(log_path, "ab") as log:
            log.write(f"--- installing {pkg} ---\n".encode())
            log.flush()
            try:
                r = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                     "--no-input", "--target", str(libs), pkg],
                    stdout=log, stderr=subprocess.STDOUT, timeout=300,
                )
                if r.returncode != 0:
                    log.write(f"--- could not install {pkg} ---\n".encode())
            except Exception as e:
                log.write(f"--- install error ({pkg}): {e} ---\n".encode())


def launch_bot_thread(bid: str, bdir: Path, retries: int = 0, tried=frozenset(), extra=()):
    proc = None
    log_pos = 0
    log_path = bdir / "console.log"
    try:
        if not bdir.is_dir() or bid in STOPPED_BOTS:
            return
        kill_orphan(bdir)
        meta = json.loads((bdir / "meta.json").read_text(encoding="utf-8"))
        fname = meta.get("filename", "bot.py")
        bot_token = meta.get("bot_token", "")

        if log_path.exists() and log_path.stat().st_size > 1_000_000:
            log_path.write_bytes(log_path.read_bytes()[-200_000:])

        with open(log_path, "ab") as log:
            log.write(f"\n--- starting {fname} ---\n".encode())

        install_packages(bdir, log_path, bid, extra)
        if bid in STOPPED_BOTS or not bdir.is_dir():
            return

        env = os.environ.copy()
        env.pop("API_TOKEN", None)
        env.pop("ADMIN_BOT_TOKEN", None)
        if bot_token:
            env["BOT_TOKEN"] = bot_token

        libs = bdir / "libs"
        if libs.is_dir():
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(libs), env.get("PYTHONPATH")]))

        log_pos = log_path.stat().st_size if log_path.exists() else 0
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(
                [sys.executable, "-u", fname],
                cwd=str(bdir),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        RUNNING_PROCS[bid] = proc
        (bdir / "pid.txt").write_text(str(proc.pid))
    except Exception as e:
        with open(log_path, "ab") as log:
            log.write(f"--- start failed: {e} ---\n".encode())
        proc = None
    finally:
        STARTING_BOTS.discard(bid)

    if proc is not None:
        threading.Thread(target=watch_bot_thread, args=(bid, bdir, proc, log_pos, retries, tried), daemon=True).start()


def watch_bot_thread(bid: str, bdir: Path, proc: subprocess.Popen, log_pos: int, retries: int, tried):
    proc.wait()
    if RUNNING_PROCS.get(bid) is not proc or bid in STOPPED_BOTS or proc.returncode == 0 or retries >= 5 or not bdir.is_dir():
        return
    try:
        with open(bdir / "console.log", "rb") as f:
            f.seek(log_pos)
            tail = f.read().decode("utf-8", "replace")
    except Exception:
        return

    found = re.findall(r"No module named '([\w.\-]+)'", tail)
    if not found:
        return
    mod = found[-1].split(".")[0]
    pkg = PIP_NAMES.get(mod, mod)
    if pkg in tried or pkg.lower() in BANNED_PKGS or mod in STDLIB or bid in STARTING_BOTS or is_bot_running(bid):
        return

    STARTING_BOTS.add(bid)
    with open(bdir / "console.log", "ab") as log:
        log.write(f"--- missing '{mod}': auto-installing {pkg} and restarting ---\n".encode())
    launch_bot_thread(bid, bdir, retries + 1, frozenset(tried) | {pkg}, extra=(pkg,))


def start_bot_async(bid: str, bdir: Path):
    if is_bot_running(bid) or bid in STARTING_BOTS:
        return
    STOPPED_BOTS.discard(bid)
    STARTING_BOTS.add(bid)
    threading.Thread(target=launch_bot_thread, args=(bid, bdir), daemon=True).start()


def stop_bot_sync(bid: str, bdir: Path):
    STOPPED_BOTS.add(bid)
    STARTING_BOTS.discard(bid)
    kill_orphan(bdir)
    proc = RUNNING_PROCS.pop(bid, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except Exception:
            proc.kill()


# ---- Telegram Admin Bot + Menu Buttons ----
MAIN_MENU_KEYBOARD = {
    "keyboard": [
        [{"text": "🔑 Get Access Key"}, {"text": "📊 Server Status"}],
        [{"text": "🤖 Active Bots"}, {"text": "⚡ V2Ray Key"}],
        [{"text": "➕ New User Key"}, {"text": "👥 All Users"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def tg_call(method: str, payload: dict):
    if not ADMIN_BOT_TOKEN:
        return None
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception:
        return None


def setup_tg_menu():
    tg_call("setMyCommands", {
        "commands": [
            {"command": "start", "description": "Open Main Menu"},
            {"command": "key", "description": "Get Admin Access Key"},
            {"command": "status", "description": "Check Server & Quota"},
            {"command": "bots", "description": "List Hosted Bots"},
            {"command": "v2ray", "description": "Generate V2Ray Key"},
            {"command": "newuser", "description": "Create User Access Key"},
        ]
    })
    tg_call("setChatMenuButton", {
        "chat_id": ADMIN_CHAT_ID,
        "menu_button": {"type": "commands"},
    })


def notify_admin(text: str):
    if not ADMIN_CHAT_ID:
        return
    tg_call("sendMessage", {
        "chat_id": ADMIN_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": MAIN_MENU_KEYBOARD,
    })


def get_link_str():
    return f"{ADMIN_TOKEN}@{CURRENT_URL or f'http://127.0.0.1:{PORT}'}"


def poll_admin_bot():
    if not ADMIN_BOT_TOKEN or not ADMIN_CHAT_ID:
        return
    setup_tg_menu()
    base = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}"
    offset = 0
    while True:
        try:
            with urllib.request.urlopen(f"{base}/getUpdates?timeout=30&offset={offset}", timeout=40) as r:
                data = json.loads(r.read())
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                if str((msg.get("chat") or {}).get("id") or "") != ADMIN
