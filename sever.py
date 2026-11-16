import os, re, sys, ast, json, time, uuid, base64, shutil, psutil
import threading, subprocess, urllib.request, importlib.util, importlib.machinery
from pathlib import Path
from fastapi import FastAPI, APIRouter, Request, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

ADMIN_TOKEN = os.environ.get("API_TOKEN", "kosaihost348").strip()
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = str(os.environ.get("ADMIN_CHAT_ID", "")).strip()
PORT, CURRENT_URL, START_TIME = 7860, "", time.time()

BASE_DIR = Path("bot_host_data")
BOTS_DIR = BASE_DIR / "bots"
USERS_FILE = BASE_DIR / "users.json"
BASE_DIR.mkdir(parents=True, exist_ok=True)
BOTS_DIR.mkdir(parents=True, exist_ok=True)

DATA_LOCK = threading.Lock()
RUNNING_PROCS, STARTING_BOTS, STOPPED_BOTS = {}, set(), set()
ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
router = APIRouter(prefix="/api")

PIP_MAP = {
    "telegram": "python-telegram-bot", "telebot": "pyTelegramBotAPI", "PIL": "Pillow",
    "yaml": "PyYAML", "bs4": "beautifulsoup4", "dotenv": "python-dotenv",
    "Crypto": "pycryptodome", "jwt": "PyJWT", "discord": "discord.py",
    "aiogram": "aiogram", "pyrogram": "pyrogram", "telethon": "Telethon",
}
BANNED = {"opencv-python", "opencv-python-headless", "cv2", "numpy", "ddddocr", "tensorflow", "torch"}
STDLIB = getattr(sys, "stdlib_module_names", {"os", "sys", "time", "json", "re", "math", "random", "asyncio"})

def load_users():
    with DATA_LOCK:
        data = json.loads(USERS_FILE.read_text("utf-8")) if USERS_FILE.exists() else {}
        if ADMIN_TOKEN not in data:
            data[ADMIN_TOKEN] = {"name": "Admin (Kosai)", "role": "admin", "max_ram_mb": 4096, "max_bots": 15, "bots": {}}
            USERS_FILE.write_text(json.dumps(data, indent=2), "utf-8")
        return data

def save_users(data):
    with DATA_LOCK:
        USERS_FILE.write_text(json.dumps(data, indent=2), "utf-8")

def require_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid Access Key")
    tok = authorization.split(" ", 1)[1].strip()
    if tok not in load_users():
        raise HTTPException(401, "Invalid Access Key")
    return tok

def is_running(bid):
    p = RUNNING_PROCS.get(bid)
    return p is not None and p.poll() is None

def get_metrics(bid):
    if bid in STARTING_BOTS:
        return True, 0.0, 10
    p = RUNNING_PROCS.get(bid)
    if not p or p.poll() is not None:
        return False, 0.0, 0
    try:
        proc = psutil.Process(p.pid)
        cpu = proc.cpu_percent(0.05)
        ram = int(proc.memory_info().rss / 1048576)
        for c in proc.children(recursive=True):
            cpu += c.cpu_percent(0.0)
            ram += int(c.memory_info().rss / 1048576)
        return True, round(cpu, 1), ram
    except Exception:
        return True, 0.0, 15

def kill_orphan(bdir):
    pf = bdir / "pid.txt"
    try:
        pid = int(pf.read_text().strip())
        if psutil.pid_exists(pid):
            psutil.Process(pid).terminate()
    except Exception:
        pass
    pf.unlink(missing_ok=True)

def needed_pkgs(bdir, fname):
    try:
        tree = ast.parse((bdir / fname).read_text("utf-8", errors="ignore"))
    except Exception:
        return []
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    libs, missing = bdir / "libs", []
    for n in sorted(names):
        if n in STDLIB or n == "__future__" or n.lower() in BANNED:
            continue
        if (bdir / f"{n}.py").exists() or (bdir / n).is_dir():
            continue
        try:
            if importlib.util.find_spec(n) is not None:
                continue
        except Exception:
            pass
        if libs.is_dir() and importlib.machinery.PathFinder.find_spec(n, [str(libs)]):
            continue
        pkg = PIP_MAP.get(n, n)
        if pkg.lower() not in BANNED:
            missing.append(pkg)
    return missing

def launch_bot(bid, bdir, retries=0, tried=frozenset(), extra=()):
    proc, log_pos, log_path = None, 0, bdir / "console.log"
    try:
        if not bdir.is_dir() or bid in STOPPED_BOTS:
            return
        kill_orphan(bdir)
        meta = json.loads((bdir / "meta.json").read_text("utf-8"))
        fname, btok = meta.get("filename", "bot.py"), meta.get("bot_token", "")
        with open(log_path, "ab") as log:
            log.write(f"\n--- starting {fname} ---\n".encode())
        pkgs = list(dict.fromkeys(list(extra) + needed_pkgs(bdir, fname)))
        if pkgs:
            libs = bdir / "libs"
            libs.mkdir(exist_ok=True)
            for pkg in pkgs:
                if bid in STOPPED_BOTS:
                    return
                with open(log_path, "ab") as log:
                    log.write(f"--- installing {pkg} ---\n".encode())
                    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", str(libs), pkg], stdout=log, stderr=subprocess.STDOUT, timeout=300)
        if bid in STOPPED_BOTS:
            return
        env = os.environ.copy()
        env.pop("API_TOKEN", None)
        env.pop("ADMIN_BOT_TOKEN", None)
        if btok:
            env["BOT_TOKEN"] = btok
        if (bdir / "libs").is_dir():
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(bdir / "libs"), env.get("PYTHONPATH")]))
        log_pos = log_path.stat().st_size if log_path.exists() else 0
        with open(log_path, "ab") as log:
            proc = subprocess.Popen([sys.executable, "-u", fname], cwd=str(bdir), env=env, stdout=log, stderr=subprocess.STDOUT)
        RUNNING_PROCS[bid] = proc
        (bdir / "pid.txt").write_text(str(proc.pid))
    except Exception as e:
        with open(log_path, "ab") as log:
            log.write(f"--- error: {e} ---\n".encode())
    finally:
        STARTING_BOTS.discard(bid)
    if proc:
        threading.Thread(target=watch_bot, args=(bid, bdir, proc, log_pos, retries, tried), daemon=True).start()

def watch_bot(bid, bdir, proc, log_pos, retries, tried):
    proc.wait()
    if RUNNING_PROCS.get(bid) is not proc or bid in STOPPED_BOTS or proc.returncode == 0 or retries >= 5:
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
    pkg = PIP_MAP.get(mod, mod)
    if pkg in tried or pkg.lower() in BANNED or mod in STDLIB or bid in STARTING_BOTS:
        return
    STARTING_BOTS.add(bid)
    launch_bot(bid, bdir, retries + 1, frozenset(tried) | {pkg}, extra=(pkg,))

def start_bot(bid, bdir):
    if not is_running(bid) and bid not in STARTING_BOTS:
        STOPPED_BOTS.discard(bid)
        STARTING_BOTS.add(bid)
        threading.Thread(target=launch_bot, args=(bid, bdir), daemon=True).start()

def stop_bot(bid, bdir):
    STOPPED_BOTS.add(bid)
    STARTING_BOTS.discard(bid)
    kill_orphan(bdir)
    p = RUNNING_PROCS.pop(bid, None)
    if p and p.poll() is None:
        p.terminate()

MENU_KB = {
    "keyboard": [
        [{"text": "🔑 Get Access Key"}, {"text": "📊 Server Status"}],
        [{"text": "🤖 Active Bots"}, {"text": "⚡ V2Ray Key"}],
        [{"text": "➕ New User Key"}, {"text": "👥 All Users"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

def tg_call(method, payload):
    if not ADMIN_BOT_TOKEN:
        return
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/{method}", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        pass

def notify(text):
    if ADMIN_CHAT_ID:
        tg_call("sendMessage", {"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": MENU_KB})

def link_str():
    return f"{ADMIN_TOKEN}@{CURRENT_URL or f'http://127.0.0.1:{PORT}'}"

def poll_tg():
    if not ADMIN_BOT_TOKEN or not ADMIN_CHAT_ID:
        return
    tg_call("setMyCommands", {"commands": [
        {"command": "start", "description": "Main Menu"}, {"command": "key", "description": "Get Access Key"},
        {"command": "status", "description": "Server Status"}, {"command": "bots", "description": "Hosted Bots"},
        {"command": "v2ray", "description": "V2Ray Key"}, {"command": "newuser", "description": "New User Key"},
    ]})
    base, offset = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}", 0
    while True:
        try:
            with urllib.request.urlopen(f"{base}/getUpdates?timeout=30&offset={offset}", timeout=40) as r:
                data = json.loads(r.read())
            for u in data.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                if str((m.get("chat") or {}).get("id") or "") != ADMIN_CHAT_ID:
                    continue
                cmd = (m.get("text") or "").strip().lower()
                if cmd in ("/start", "/menu", "menu"):
                    notify(f"🎛 <b>Control Panel</b>\n\n<code>{link_str()}</code>")
                elif cmd in ("/key", "key", "🔑 get access key"):
                    notify(f"🔑 <b>Access Key (Tap to copy):</b>\n\n<code>{link_str()}</code>")
                elif cmd in ("/status", "status", "📊 server status"):
                    hrs, rem = divmod(int(time.time() - START_TIME), 3600)
                    mem = psutil.virtual_memory()
                    active = sum(1 for p in RUNNING_PROCS.values() if p.poll() is None)
                    notify(f"📊 <b>Status:</b> 🟢 Online\n• Uptime: {hrs}h {rem//60}m\n• Active Bots: {active}\n• RAM: {int(mem.used/1048576)}/{int(mem.total/1048576)} MB")
                elif cmd in ("/bots", "bots", "🤖 active bots"):
                    bots = load_users().get(ADMIN_TOKEN, {}).get("bots", {})
                    notify("🤖 No bots hosted yet." if not bots else "\n".join([f"• <b>{v['name']}</b> — {'🟢 Run' if get_metrics(k)[0] else '🔴 Stop'}" for k, v in bots.items()]))
                elif cmd in ("/v2ray", "v2ray", "⚡ v2ray key"):
                    h = (CURRENT_URL or "kosai.trycloudflare.com").replace("https://", "")
                    notify(f"⚡ <b>V2Ray Key:</b>\n\n<code>vless://{uuid.uuid4()}@{h}:443?encryption=none&security=tls&sni=m.facebook.com&type=ws&host=m.facebook.com&path=%2Fws#Kosai-VIP</code>")
                elif cmd in ("/newuser", "newuser", "➕ new user key"):
                    users = load_users()
                    nk = "bh_" + uuid.uuid4().hex[:16]
                    users[nk] = {"name": f"User-{len(users)}", "role": "user", "max_ram_mb": 200, "max_bots": 2, "bots": {}}
                    save_users(users)
                    notify(f"✅ <b>New User Key:</b>\n\n<code>{nk}@{CURRENT_URL}</code>")
                elif cmd in ("/users", "users", "👥 all users"):
                    users = load_users()
                    notify("\n".join([f"• <b>{v['name']}</b>: <code>{k}</code>" for k, v in users.items()]))
        except Exception:
            time.sleep(5)

def run_tunnel():
    global CURRENT_URL
    bin_p = Path("/usr/local/bin/cloudflared")
    if not bin_p.exists():
        urllib.request.urlretrieve("https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64", str(bin_p))
        os.chmod(bin_p, 0o755)
    while True:
        try:
            p = subprocess.Popen([str(bin_p), "tunnel", "--url", f"http://127.0.0.1:{PORT}", "--no-autoupdate"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            sent = False
            for line in p.stdout:
                m = re.search(r"https://[\w-]+\.trycloudflare\.com", line)
                if m and not sent:
                    sent, CURRENT_URL = True, m.group(0)
                    print(f"\n=== {link_str()} ===\n", flush=True)
                    notify(f"🚀 <b>GitHub 24/7 Server Online!</b>\n\n<code>{link_str()}</code>")
            p.wait()
        except Exception:
            pass
        time.sleep(5)

@app.get("/")
def root():
    return {"status": "online", "tunnel": CURRENT_URL}

@router.get("/dashboard")
def dashboard(tok: str = Depends(require_user)):
    u = load_users()[tok]
    bl, tram, tcpu = [], 0, 0.0
    for bid, b in u["bots"].items():
        r, c, m = get_metrics(bid)
        if r:
            tram, tcpu = tram + m, tcpu + c
        bl.append({"id": bid, "name": b["name"], "filename": b["filename"], "running": r, "cpu": c, "ram_mb": m})
    hrs, rem = divmod(int(time.time() - START_TIME), 3600)
    return {"role": u["role"], "name": u["name"], "uptime": f"Up {hrs}h {rem//60}m", "cpu_percent": round(tcpu, 1), "ram_used_mb": tram, "ram_limit_mb": u["max_ram_mb"], "bots": bl}

@router.post("/bots/create")
async def create_bot(req: Request, tok: str = Depends(require_user)):
    users = load_users()
    u = users[tok]
    if len(u["bots"]) >= u["max_bots"]:
        raise HTTPException(400, "Bot limit reached")
    f = await req.form()
    bname = str(f.get("bot_name", "MyBot")).strip() or "MyBot"
    btok = str(f.get("bot_token", "")).strip()
    up = f.get("file")
    if not up:
        raise HTTPException(400, "No file selected")
    bid = uuid.uuid4().hex[:8]
    bdir = BOTS_DIR / bid
    bdir.mkdir(parents=True, exist_ok=True)
    fname = Path(getattr(up, "filename", "bot.py")).name
    (bdir / fname).write_bytes(await up.read())
    (bdir / "meta.json").write_text(json.dumps({"name": bname, "filename": fname, "bot_token": btok}), "utf-8")
    (bdir / "console.log").write_text(f">>> Created {bname}\n", "utf-8")
    u["bots"][bid] = {"name": bname, "filename": fname, "bot_token": btok}
    save_users(users)
    start_bot(bid, bdir)
    return {"status": "started", "bot_id": bid}

@router.post("/bots/{bid}/{act}")
def bot_action(bid: str, act: str, tok: str = Depends(require_user)):
    users = load_users()
    if bid not in users[tok]["bots"]:
        raise HTTPException(404, "Not found")
    bdir = BOTS_DIR / bid
    if act == "start":
        start_bot(bid, bdir)
    elif act == "stop":
        stop_bot(bid, bdir)
    elif act == "delete":
        stop_bot(bid, bdir)
        shutil.rmtree(bdir, ignore_errors=True)
        del users[tok]["bots"][bid]
        save_users(users)
    return {"status": act}

@router.get("/bots/{bid}/logs")
def bot_logs(bid: str, tok: str = Depends(require_user)):
    if bid not in load_users()[tok]["bots"]:
        raise HTTPException(404, "Not found")
    lp = BOTS_DIR / bid / "console.log"
    r, c, m = get_metrics(bid)
    return {"running": r, "cpu": c, "ram_mb": m, "logs": lp.read_text("utf-8", errors="replace")[-8000:] if lp.exists() else "No logs."}

@router.get("/bots/{bid}/files")
def bot_files(bid: str, tok: str = Depends(require_user)):
    if bid not in load_users()[tok]["bots"]:
        raise HTTPException(404, "Not found")
    return {"files": [{"name": x.name, "size": x.stat().st_size} for x in (BOTS_DIR / bid).iterdir() if x.is_file()]}

@router.post("/bots/{bid}/terminal")
async def bot_term(bid: str, req: Request, tok: str = Depends(require_user)):
    if bid not in load_users()[tok]["bots"]:
        raise HTTPException(404, "Not found")
    cmd = str((await req.json()).get("cmd", "")).strip()
    res = subprocess.run(cmd, shell=True, cwd=str(BOTS_DIR / bid), capture_output=True, text=True, timeout=60) if cmd else None
    return {"output": ANSI_RE.sub("", res.stdout + res.stderr) or "(Done)" if res else ""}

@router.post("/terminal")
async def global_term(req: Request, tok: str = Depends(require_user)):
    cmd = str((await req.json()).get("cmd", "")).strip()
    res = subprocess.run(cmd, shell=True, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=60) if cmd else None
    return {"output": ANSI_RE.sub("", res.stdout + res.stderr) or "(Done)" if res else ""}

class V2Req(BaseModel):
    protocol: str = "vless"
    host: str = "kosai.trycloudflare.com"
    sni: str = "m.facebook.com"
    remark: str = "Kosai-VIP"
    port: int = 443

@router.post("/v2ray/generate")
def gen_v2(req: V2Req, tok: str = Depends(require_user)):
    p, u = req.protocol.lower(), str(uuid.uuid4())
    if p == "vmess":
        obj = {"v": "2", "ps": req.remark, "add": req.host, "port": str(req.port), "id": u, "aid": "0", "scy": "auto", "net": "ws", "type": "none", "host": req.sni, "path": "/ws", "tls": "tls", "sni": req.sni}
        k = "vmess://" + base64.b64encode(json.dumps(obj).encode()).decode()
    elif p == "trojan":
        k = f"trojan://{u}@{req.host}:{req.port}?security=tls&sni={req.sni}&type=ws&host={req.sni}&path=%2Fws#{req.remark}"
    else:
        k = f"vless://{u}@{req.host}:{req.port}?encryption=none&security=tls&sni={req.sni}&type=ws&host={req.sni}&path=%2Fws#{req.remark}"
    return {"protocol": p.upper(), "uuid": u, "key": k}

@router.get("/admin/users")
def list_users(tok: str = Depends(require_user)):
    users = load_users()
    if users[tok]["role"] != "admin":
        raise HTTPException(403, "Admin required")
    return {"users": [{"key": k, "name": v["name"], "role": v["role"], "max_ram_mb": v["max_ram_mb"], "bots_count": len(v["bots"])} for k, v in users.items()]}

@router.post("/admin/users")
async def add_user(req: Request, tok: str = Depends(require_user)):
    users = load_users()
    if users[tok]["role"] != "admin":
        raise HTTPException(403, "Admin required")
    d = await req.json()
    nk = "bh_" + uuid.uuid4().hex[:16]
    users[nk] = {"name": str(d.get("name", "User")).strip(), "role": "user", "max_ram_mb": int(d.get("max_ram_mb", 200)), "max_bots": int(d.get("max_bots", 2)), "bots": {}}
    save_users(users)
    return {"access_key": nk, "full_link": f"{nk}@{CURRENT_URL}"}

app.include_router(router)

if __name__ == "__main__":
    threading.Thread(target=poll_tg, daemon=True).start()
    threading.Thread(target=run_tunnel, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
# === OK ===