import os, re, sys, ast, json, time, uuid, base64, shutil, psutil
import threading, subprocess, urllib.request, importlib.util
from pathlib import Path
from fastapi import FastAPI, APIRouter, Request, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

ADMIN_TOKEN = os.environ.get("API_TOKEN", "kosaihost348").strip()
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = str(os.environ.get("ADMIN_CHAT_ID", "")).strip()
PORT, CURRENT_URL, START_TIME, RESUMED_COUNT = 7860, "", time.time(), 0

BASE_DIR = Path("bot_host_data")
BOTS_DIR = BASE_DIR / "bots"
USERS_FILE = Path("/tmp/.sys_auth_store.json")
BOTS_DIR.mkdir(parents=True, exist_ok=True)

LOCK = threading.Lock()
PROCS, STARTING, STOPPED, FAIL_IPS = {}, set(), set(), {}
ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
router = APIRouter(prefix="/api")

PIP_MAP = {"telegram": "python-telegram-bot", "telebot": "pyTelegramBotAPI", "PIL": "Pillow", "yaml": "PyYAML", "bs4": "beautifulsoup4", "dotenv": "python-dotenv", "Crypto": "pycryptodome", "jwt": "PyJWT", "aiogram": "aiogram", "pyrogram": "pyrogram", "telethon": "Telethon"}
BANNED = {"opencv-python", "opencv-python-headless", "cv2", "numpy", "ddddocr", "tensorflow", "torch"}
STDLIB = getattr(sys, "stdlib_module_names", {"os", "sys", "time", "json", "re", "math", "random", "asyncio"})

def load_users():
    with LOCK:
        old_f = BASE_DIR / "users.json"
        if old_f.exists() and not USERS_FILE.exists():
            shutil.move(str(old_f), str(USERS_FILE))
        d = json.loads(USERS_FILE.read_text("utf-8")) if USERS_FILE.exists() else {}
        if ADMIN_TOKEN not in d:
            d[ADMIN_TOKEN] = {"name": "Admin (Kosai)", "role": "admin", "max_ram_mb": 4096, "max_bots": 15, "bots": {}}
            USERS_FILE.write_text(json.dumps(d, indent=2), "utf-8")
        return d

def save_users(d):
    with LOCK:
        USERS_FILE.write_text(json.dumps(d, indent=2), "utf-8")

def auth(request: Request, authorization: str = Header(None)):
    ip = request.client.host if request.client else "0.0.0.0"
    now = time.time()
    rec = FAIL_IPS.get(ip, [0, 0])
    if rec[0] >= 5 and now - rec[1] < 600:
        raise HTTPException(429, "Too many failed attempts")
    if not authorization or not authorization.startswith("Bearer "):
        FAIL_IPS[ip] = [rec[0] + 1, now]
        raise HTTPException(401, "Invalid Key")
    tok = authorization.split(" ", 1)[1].strip()
    if tok not in load_users():
        FAIL_IPS[ip] = [rec[0] + 1, now]
        raise HTTPException(401, "Invalid Key")
    FAIL_IPS.pop(ip, None)
    return tok

def is_run(bid):
    p = PROCS.get(bid)
    return p is not None and p.poll() is None

def metrics(bid):
    if bid in STARTING:
        return True, 0.0, 10
    p = PROCS.get(bid)
    if not p or p.poll() is not None:
        return False, 0.0, 0
    try:
        pr = psutil.Process(p.pid)
        return True, round(pr.cpu_percent(0.05), 1), int(pr.memory_info().rss / 1048576)
    except Exception:
        return True, 0.0, 15

def needed_pkgs(bdir, fname):
    try:
        tree = ast.parse((bdir / fname).read_text("utf-8", errors="ignore"))
    except Exception:
        return []
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module}
    out = []
    for n in sorted(names):
        if n in STDLIB or n == "__future__" or n.lower() in BANNED or (bdir / f"{n}.py").exists() or (bdir / n).is_dir():
            continue
        if importlib.util.find_spec(n) is not None:
            continue
        pkg = PIP_MAP.get(n, n)
        if pkg.lower() not in BANNED:
            out.append(pkg)
    return out

def launch_bot(bid, bdir, retries=0, tried=frozenset(), extra=()):
    proc, pos, lp = None, 0, bdir / "console.log"
    try:
        if not bdir.is_dir() or bid in STOPPED:
            return
        shutil.rmtree(bdir / "libs", ignore_errors=True)
        meta = json.loads((bdir / "meta.json").read_text("utf-8"))
        fname, btok = meta.get("filename", "bot.py"), meta.get("bot_token", "")
        with open(lp, "ab") as log:
            log.write(f"\n--- starting {fname} ---\n".encode())
        pkgs = list(dict.fromkeys(list(extra) + needed_pkgs(bdir, fname)))
        for pkg in pkgs:
            if bid in STOPPED:
                return
            with open(lp, "ab") as log:
                log.write(f"--- installing {pkg} ---\n".encode())
                subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", pkg], stdout=log, stderr=subprocess.STDOUT, timeout=300)
        if bid in STOPPED:
            return
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONUNBUFFERED": "1"}
        if btok:
            env["BOT_TOKEN"] = btok
        pos = lp.stat().st_size if lp.exists() else 0
        with open(lp, "ab") as log:
            proc = subprocess.Popen([sys.executable, "-u", fname], cwd=str(bdir), env=env, stdout=log, stderr=subprocess.STDOUT)
        PROCS[bid] = proc
    except Exception as e:
        with open(lp, "ab") as log:
            log.write(f"--- error: {e} ---\n".encode())
    finally:
        STARTING.discard(bid)
    if proc:
        threading.Thread(target=watch_bot, args=(bid, bdir, proc, pos, retries, tried), daemon=True).start()

def watch_bot(bid, bdir, proc, pos, retries, tried):
    proc.wait()
    if PROCS.get(bid) is not proc or bid in STOPPED or proc.returncode == 0 or retries >= 5:
        return
    try:
        with open(bdir / "console.log", "rb") as f:
            f.seek(pos)
            tail = f.read().decode("utf-8", "replace")
        found = re.findall(r"No module named '([\w.\-]+)'", tail)
        if found:
            pkg = PIP_MAP.get(found[-1].split(".")[0], found[-1].split(".")[0])
            if pkg not in tried and pkg.lower() not in BANNED:
                STARTING.add(bid)
                launch_bot(bid, bdir, retries + 1, frozenset(tried) | {pkg}, (pkg,))
    except Exception:
        pass

def start_bot(bid, bdir):
    if not is_run(bid) and bid not in STARTING:
        STOPPED.discard(bid)
        STARTING.add(bid)
        threading.Thread(target=launch_bot, args=(bid, bdir), daemon=True).start()

def stop_bot(bid):
    STOPPED.add(bid)
    STARTING.discard(bid)
    p = PROCS.pop(bid, None)
    if p and p.poll() is None:
        p.terminate()

def resume_saved_bots():
    count = 0
    for u in load_users().values():
        for bid, b in u.get("bots", {}).items():
            bdir = BOTS_DIR / bid
            if bdir.is_dir() and not b.get("paused", False):
                start_bot(bid, bdir)
                count += 1
    return count

MENU_KB = {"keyboard": [[{"text": "🔑 Get Access Key"}, {"text": "📊 Server Status"}], [{"text": "🤖 Active Bots"}, {"text": "⚡ V2Ray Key"}], [{"text": "➕ New User Key"}, {"text": "👥 All Users"}]], "resize_keyboard": True, "is_persistent": True}

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
    tg_call("setMyCommands", {"commands": [{"command": "start", "description": "Menu"}, {"command": "key", "description": "Access Key"}, {"command": "status", "description": "Status"}, {"command": "bots", "description": "Bots"}, {"command": "v2ray", "description": "V2Ray"}, {"command": "newuser", "description": "New User"}]})
    base, off = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}", 0
    while True:
        try:
            with urllib.request.urlopen(f"{base}/getUpdates?timeout=30&offset={off}", timeout=40) as r:
                data = json.loads(r.read())
            for u in data.get("result", []):
                off = u["update_id"] + 1
                m = u.get("message") or {}
                if str((m.get("chat") or {}).get("id") or "") != ADMIN_CHAT_ID:
                    continue
                cmd = (m.get("text") or "").strip().lower()
                if cmd in ("/start", "/menu", "menu", "/key", "key", "🔑 get access key"):
                    notify(f"🔑 <b>Access Key:</b>\n\n<code>{link_str()}</code>")
                elif cmd in ("/status", "status", "📊 server status"):
                    hrs, rem = divmod(int(time.time() - START_TIME), 3600)
                    mem = psutil.virtual_memory()
                    notify(f"📊 <b>Online</b> | Up {hrs}h {rem//60}m\nRAM: {int(mem.used/1048576)}/{int(mem.total/1048576)} MB")
                elif cmd in ("/bots", "bots", "🤖 active bots"):
                    bots = load_users().get(ADMIN_TOKEN, {}).get("bots", {})
                    notify("🤖 No bots yet." if not bots else "\n".join([f"• <b>{v['name']}</b> — {'🟢' if metrics(k)[0] else '🔴'}" for k, v in bots.items()]))
                elif cmd in ("/v2ray", "v2ray", "⚡ v2ray key"):
                    h = (CURRENT_URL or "kosai.trycloudflare.com").replace("https://", "")
                    notify(f"⚡ <b>V2Ray:</b>\n\n<code>vless://{uuid.uuid4()}@{h}:443?encryption=none&security=tls&sni=m.facebook.com&type=ws&host=m.facebook.com&path=%2Fws#Kosai-VIP</code>")
                elif cmd in ("/newuser", "newuser", "➕ new user key"):
                    users = load_users()
                    nk = "bh_" + uuid.uuid4().hex[:16]
                    users[nk] = {"name": f"User-{len(users)}", "role": "user", "max_ram_mb": 200, "max_bots": 2, "bots": {}}
                    save_users(users)
                    notify(f"✅ <b>New User:</b>\n\n<code>{nk}@{CURRENT_URL}</code>")
                elif cmd in ("/users", "users", "👥 all users"):
                    notify("\n".join([f"• <b>{v['name']}</b>: <code>{k}</code>" for k, v in load_users().items()]))
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
                    extra = f"\n♻️ Restored <b>{RESUMED_COUNT}</b> saved bot(s)" if RESUMED_COUNT else ""
                    notify(f"🚀 <b>Server Online!</b>{extra}\n\n<code>{link_str()}</code>")
            p.wait()
        except Exception:
            pass
        time.sleep(5)

def auto_save_cycle():
    time.sleep(350 * 60)
    for bid in list(PROCS.keys()):
        stop_bot(bid)
    notify("🔄 <b>6-Hour Auto-Save:</b> Restarting server...")
    time.sleep(2)
    os._exit(0)

@app.get("/")
def root():
    return {"ok": 1}

@router.get("/dashboard")
def dashboard(tok: str = Depends(auth)):
    u = load_users()[tok]
    bl, tram, tcpu = [], 0, 0.0
    for bid, b in u["bots"].items():
        r, c, m = metrics(bid)
        if r:
            tram, tcpu = tram + m, tcpu + c
        bl.append({"id": bid, "name": b["name"], "filename": b["filename"], "running": r, "cpu": c, "ram_mb": m})
    hrs, rem = divmod(int(time.time() - START_TIME), 3600)
    return {"role": u["role"], "name": u["name"], "uptime": f"Up {hrs}h {rem//60}m", "cpu_percent": round(tcpu, 1), "ram_used_mb": tram, "ram_limit_mb": u["max_ram_mb"], "bots": bl}

@router.post("/bots/create")
async def create_bot(req: Request, tok: str = Depends(auth)):
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
    u["bots"][bid] = {"name": bname, "filename": fname, "bot_token": btok, "paused": False}
    save_users(users)
    start_bot(bid, bdir)
    return {"status": "started", "bot_id": bid}

@router.post("/bots/{bid}/{act}")
def bot_act(bid: str, act: str, tok: str = Depends(auth)):
    users = load_users()
    if bid not in users[tok]["bots"]:
        raise HTTPException(404, "Not found")
    bdir = BOTS_DIR / bid
    if act == "start":
        users[tok]["bots"][bid]["paused"] = False
        save_users(users)
        start_bot(bid, bdir)
    elif act == "stop":
        users[tok]["bots"][bid]["paused"] = True
        save_users(users)
        stop_bot(bid)
    elif act == "delete":
        stop_bot(bid)
        shutil.rmtree(bdir, ignore_errors=True)
        del users[tok]["bots"][bid]
        save_users(users)
    return {"status": act}

@router.get("/bots/{bid}/logs")
def bot_logs(bid: str, tok: str = Depends(auth)):
    if bid not in load_users()[tok]["bots"]:
        raise HTTPException(404, "Not found")
    lp = BOTS_DIR / bid / "console.log"
    r, c, m = metrics(bid)
    return {"running": r, "cpu": c, "ram_mb": m, "logs": lp.read_text("utf-8", errors="replace")[-8000:] if lp.exists() else "No logs."}

@router.get("/bots/{bid}/files")
def bot_files(bid: str, tok: str = Depends(auth)):
    if bid not in load_users()[tok]["bots"]:
        raise HTTPException(404, "Not found")
    return {"files": [{"name": x.name, "size": x.stat().st_size} for x in (BOTS_DIR / bid).iterdir() if x.is_file()]}

def run_sh(cmd, cwd, tok):
    if load_users()[tok]["role"] != "admin":
        raise HTTPException(403, "Admin only")
    if not cmd:
        return {"output": "", "result": "", "out": ""}
    res = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=60)
    out = ANSI_RE.sub("", (res.stdout or "") + (res.stderr or "")) or "(Done)"
    return {"output": out, "result": out, "out": out}

@router.post("/bots/{bid}/terminal")
async def bot_term(bid: str, req: Request, tok: str = Depends(auth)):
    if bid not in load_users()[tok]["bots"]:
        raise HTTPException(404, "Not found")
    return run_sh(str((await req.json()).get("cmd", "")).strip(), BOTS_DIR / bid, tok)

@router.post("/terminal")
async def global_term(req: Request, tok: str = Depends(auth)):
    return run_sh(str((await req.json()).get("cmd", "")).strip(), BASE_DIR, tok)

class V2Req(BaseModel):
    protocol: str = "vless"
    host: str = "kosai.trycloudflare.com"
    sni: str = "m.facebook.com"
    remark: str = "Kosai-VIP"
    port: int = 443

@router.post("/v2ray/generate")
def gen_v2(req: V2Req, tok: str = Depends(auth)):
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
def list_users(tok: str = Depends(auth)):
    users = load_users()
    if users[tok]["role"] != "admin":
        raise HTTPException(403, "Admin required")
    return {"users": [{"key": k, "name": v["name"], "role": v["role"], "max_ram_mb": v["max_ram_mb"], "bots_count": len(v["bots"])} for k, v in users.items()]}

@router.post("/admin/users")
async def add_user(req: Request, tok: str = Depends(auth)):
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
    RESUMED_COUNT = resume_saved_bots()
    threading.Thread(target=poll_tg, daemon=True).start()
    threading.Thread(target=run_tunnel, daemon=True).start()
    threading.Thread(target=auto_save_cycle, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
# END
