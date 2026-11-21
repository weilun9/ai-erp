"""AI Butler ERP desktop app: one exe that runs the whole ERP on this computer.

What it does, in order:
1. Start Docker Desktop if it is not running (and remember that it did).
2. Start the ERP's own PostgreSQL + pgvector container `aibutler-erp-db` (data in the Docker
   volume `aibutler-erp-data`, port 127.0.0.1:5434 - apart from the developer database).
3. Bring the database up to date (Alembic migrations bundled in the exe).
4. First run only: start with the sample shop (the demo story) or an empty company.
5. Serve the API and the built web app on 127.0.0.1 (this computer only) and show it in a
   desktop window.
Closing the window stops the server and the container, and Docker Desktop too when this app
started it, so nothing keeps running in the background.

Settings and data live in %LOCALAPPDATA%\\AIButlerERP: `config.env` (made on first run:
JWT secret, database password, port), `inbox/` (scanned papers), `kb/`, `logs/`.
API keys (LLM_*, VISION_*) go in a `.env` next to the exe or in that folder's `config.env`.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

APP = "AI Butler ERP"
CONTAINER = "aibutler-erp-db"
VOLUME = "aibutler-erp-data"
IMAGE = "pgvector/pgvector:pg16"
DB_PORT = 5434
FROZEN = getattr(sys, "frozen", False)
REPO = Path(__file__).resolve().parents[1]
# the bundled files in the exe; from source: the backend's migrations and the frontend build
BUNDLE = Path(getattr(sys, "_MEIPASS", REPO))
ALEMBIC = BUNDLE / "alembic" if FROZEN else REPO / "backend" / "alembic"
WEB = BUNDLE / "web" if FROZEN else REPO / "frontend" / "dist"
EXE_DIR = Path(sys.executable).parent if FROZEN else REPO
HOME = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AIButlerERP"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DOCKER_DESKTOP = (
    Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    / "Docker"
    / "Docker"
    / "Docker Desktop.exe"
)

if not FROZEN:  # run from the repository: python desktop/launcher.py
    sys.path.insert(0, str(REPO / "backend"))

log = logging.getLogger("launcher")


# --- settings ---------------------------------------------------------------------------------


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_config() -> dict[str, str]:
    """config.env in the data folder (made once), then a .env next to the exe for keys."""
    HOME.mkdir(parents=True, exist_ok=True)
    cfg_path = HOME / "config.env"
    cfg = _read_env(cfg_path)
    changed = False
    for key, make in (
        ("JWT_SECRET", lambda: secrets.token_urlsafe(48)),
        ("POSTGRES_PASSWORD", lambda: secrets.token_urlsafe(24)),
        ("APP_PORT", lambda: str(_free_port())),
    ):
        if not cfg.get(key):
            cfg[key] = make()
            changed = True
    if changed:
        lines = [
            "# AI Butler ERP desktop settings (made on first run). Keep this file private.",
            "# Add LLM_BASE_URL / LLM_API_KEY / LLM_MODEL (and VISION_*) here or in a .env",
            "# next to the exe to switch the AI butler and scan recognition on.",
        ] + [f"{k}={v}" for k, v in cfg.items()]
        cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # API keys may also sit in a .env next to the exe; config.env wins where both set one
    keys = {
        k: v
        for k, v in _read_env(EXE_DIR / ".env").items()
        if k.startswith(("LLM_", "VISION_", "EMBED_", "KB_", "AI_"))
    }
    return {**keys, **cfg}


def _free_port() -> int:
    for port in range(8765, 8800):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return 8799


def app_env(cfg: dict[str, str]) -> dict[str, str]:
    """Environment for the ERP server (and the sample-data run)."""
    env = {
        "APP_ENV": "production",
        "DESKTOP_MODE": "1",
        "JWT_SECRET": cfg["JWT_SECRET"],
        "POSTGRES_USER": "erp",
        "POSTGRES_PASSWORD": cfg["POSTGRES_PASSWORD"],
        "POSTGRES_DB": "erp",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": str(DB_PORT),
        "INBOX_DIR": str(HOME / "inbox"),
        "KB_DIR": str(HOME / "kb"),
        "FRONTEND_DIR": str(WEB),
        "EMBED_PROVIDER": cfg.get("EMBED_PROVIDER", "none"),
    }
    for k, v in cfg.items():
        if k.startswith(("LLM_", "VISION_", "KB_", "AI_")) and k not in env:
            env[k] = v
    return env


# --- Docker -----------------------------------------------------------------------------------


def _docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=NO_WINDOW,
    )


def docker_up() -> bool:
    try:
        return _docker("info", timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_docker(say) -> bool:
    """True if this app started Docker Desktop (so it stops it again on exit)."""
    if docker_up():
        return False
    if shutil.which("docker") is None or not DOCKER_DESKTOP.is_file():
        raise RuntimeError(
            "Docker Desktop is not installed. Install it from docker.com, then open this app again."
        )
    say("Starting Docker Desktop… (first start can take a minute)")
    subprocess.Popen([str(DOCKER_DESKTOP)], creationflags=NO_WINDOW)
    for _ in range(90):
        time.sleep(2)
        if docker_up():
            return True
    raise RuntimeError("Docker Desktop did not start. Open it once by hand, then try again.")


def ensure_database(cfg: dict[str, str], say) -> None:
    exists = _docker("inspect", CONTAINER).returncode == 0
    if not exists:
        say("Creating the database (first run; downloads the PostgreSQL image if needed)…")
        r = _docker(
            "run",
            "-d",
            "--name",
            CONTAINER,
            "-e",
            "POSTGRES_USER=erp",
            "-e",
            f"POSTGRES_PASSWORD={cfg['POSTGRES_PASSWORD']}",
            "-e",
            "POSTGRES_DB=erp",
            "-p",
            f"127.0.0.1:{DB_PORT}:5432",
            "-v",
            f"{VOLUME}:/var/lib/postgresql/data",
            IMAGE,
            timeout=900,
        )
        if r.returncode != 0:
            raise RuntimeError(f"Could not create the database container: {r.stderr.strip()[:300]}")
    else:
        say("Starting the database…")
        r = _docker("start", CONTAINER)
        if r.returncode != 0:
            raise RuntimeError(f"Could not start the database container: {r.stderr.strip()[:300]}")
    for _ in range(60):
        if (
            _docker(
                "exec", CONTAINER, "pg_isready", "-U", "erp", "-d", "erp", timeout=15
            ).returncode
            == 0
        ):
            return
        time.sleep(1)
    raise RuntimeError("The database did not become ready.")


def stop_everything(started_docker: bool) -> None:
    try:
        _docker("stop", CONTAINER, timeout=60)
    except Exception:
        log.debug("ignored while shutting down", exc_info=True)
    if started_docker:
        try:
            subprocess.run(
                ["docker", "desktop", "stop"],
                check=False,
                capture_output=True,
                timeout=90,
                creationflags=NO_WINDOW,
            )
        except Exception:
            log.debug("ignored while shutting down", exc_info=True)
        subprocess.run(
            ["taskkill", "/IM", "Docker Desktop.exe", "/F"],
            check=False,
            capture_output=True,
            creationflags=NO_WINDOW,
        )


# --- the ERP itself ---------------------------------------------------------------------------


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC))
    command.upgrade(cfg, "head")


def has_company() -> bool:
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models import Company

    with SessionLocal() as db:
        return db.scalar(select(Company.id).limit(1)) is not None


def make_company(name: str, username: str, password: str) -> None:
    from app.db.session import SessionLocal
    from app.services.seed import bootstrap

    with SessionLocal() as db:
        bootstrap(
            db,
            company_code="MAIN",
            company_name=name,
            hq_code="HQ",
            hq_name="Head Office",
            admin_username=username,
            admin_password=password,
        )
        db.commit()


def seed_sample(env: dict[str, str]) -> None:
    """The demo story, run into this database by a second copy of the exe (app settings are
    read once per process, and the demo needs its own)."""
    url = f"postgresql://erp:{env['POSTGRES_PASSWORD']}@127.0.0.1:{DB_PORT}/erp"
    child_env = {
        **os.environ,
        **env,
        "DATABASE_URL": url,
        "DEMO_RESET_REMOTE": "1",
        "LLM_BASE_URL": "",
        "EMBED_PROVIDER": "none",
        "APP_ENV": "dev",
    }
    child_env.pop("FRONTEND_DIR", None)
    cmd = (
        [sys.executable, "--seed-sample"] if FROZEN else [sys.executable, __file__, "--seed-sample"]
    )
    log_path = HOME / "logs" / "sample-data.log"
    with open(log_path, "w", encoding="utf-8") as out:
        r = subprocess.run(
            cmd,
            check=False,
            env=child_env,
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW,
            timeout=1800,
        )
    if r.returncode != 0:
        raise RuntimeError(f"Loading the sample shop failed; see {log_path}")


def serve(port: int) -> threading.Thread:
    import uvicorn

    from app.main import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="erp-server")
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    serve.server = server  # type: ignore[attr-defined]
    return thread


# --- window -----------------------------------------------------------------------------------

SPLASH = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;height:100vh;display:grid;place-items:center;background:#0A0A0B;color:#fff;
font-family:Inter,Segoe UI,system-ui,sans-serif}
.box{width:min(460px,90vw);display:flex;flex-direction:column;gap:14px}
h1{margin:0;font-weight:600;font-size:26px;letter-spacing:-.02em}
#msg{color:#A3AAB6;font-size:14px;min-height:20px}
.bar{height:4px;border-radius:4px;background:rgba(255,255,255,.08);overflow:hidden}
.bar i{display:block;height:100%;width:35%;background:linear-gradient(90deg,#34D399,#22D3EE);
animation:run 1.2s ease-in-out infinite}
@keyframes run{from{transform:translateX(-100%)}to{transform:translateX(300%)}}
.err{color:#FDA4AF;font-size:14px;white-space:pre-wrap}
.choice{display:none;flex-direction:column;gap:10px}
button{height:44px;border-radius:999px;border:0;font:inherit;font-size:14px;cursor:pointer}
.pri{background:#fff;color:#0A0A0B;font-weight:500}.sec{background:transparent;color:#fff;
box-shadow:inset 0 0 0 1px rgba(255,255,255,.18)}
input{height:40px;border-radius:14px;border:1px solid rgba(255,255,255,.12);
background:rgba(255,255,255,.05);color:#fff;padding:0 14px;font:inherit}
label{font-size:12px;color:#9AA1AE;display:flex;flex-direction:column;gap:4px}
.muted{color:#9AA1AE;font-size:13px}
</style></head><body><div class="box">
<h1>AI Butler ERP</h1>
<div id="msg">Starting…</div><div class="bar" id="bar"><i></i></div>
<div class="err" id="err"></div>
<div class="choice" id="choice">
 <span class="muted">First run. How do you want to start?</span>
 <button class="pri" onclick="pywebview.api.sample()">Start with the sample shop</button>
 <span class="muted">A Malaysian grocery shop with items, documents and a till. Log in as
 <b>owner</b> / <b>demo-password-2026</b> and change it in Settings.</span>
 <hr style="border:0;border-top:1px solid rgba(255,255,255,.08);width:100%">
 <label>Company name<input id="co" value="My Shop Sdn Bhd"></label>
 <label>Owner user name<input id="un" value="owner"></label>
 <label>Owner password (10+ characters)<input id="pw" type="password"></label>
 <button class="sec" onclick="startEmpty()">Start empty</button>
</div></div>
<script>
const $=id=>document.getElementById(id)
function say(t){$('msg').textContent=t}
function fail(t){$('bar').style.display='none';$('err').textContent=t}
function ask(){$('bar').style.display='none';$('choice').style.display='flex';say('')}
function busy(t){$('choice').style.display='none';$('bar').style.display='block';say(t)}
function startEmpty(){
 pywebview.api.empty($('co').value,$('un').value,$('pw').value).then(e=>{$('err').textContent=e||''})
}
</script></body></html>"""


class Api:
    """What the splash page calls (first run choice)."""

    def __init__(self) -> None:
        self.choice: tuple | None = None
        self.ready = threading.Event()

    def sample(self) -> None:
        self.choice = ("sample",)
        self.ready.set()

    def empty(self, name: str, username: str, password: str) -> str:
        if len((password or "").strip()) < 10:
            return "Password must be 10+ characters"
        if not name.strip() or not username.strip():
            return "Fill in the company and user name"
        self.choice = ("empty", name.strip(), username.strip(), password)
        self.ready.set()
        return ""


def main() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        filename=HOME / "logs" / "launcher.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if "--seed-sample" in sys.argv:  # the second copy that loads the sample shop
        from app import demo

        demo.main()
        return

    import webview

    cfg = load_config()
    env = app_env(cfg)
    os.environ.update(env)
    port = int(cfg["APP_PORT"])
    api = Api()
    window = webview.create_window(
        APP, html=SPLASH, js_api=api, width=1280, height=860, min_size=(900, 600)
    )
    state = {"started_docker": False}

    def js(code: str) -> None:
        try:
            window.evaluate_js(code)
        except Exception:
            log.debug("ignored while shutting down", exc_info=True)

    def say(text: str) -> None:
        log.info(text)
        js(f"say({text!r})")

    def start() -> None:
        try:
            state["started_docker"] = ensure_docker(say)
            ensure_database(cfg, say)
            say("Updating the database…")
            migrate()
            if not has_company():
                js("ask()")
                api.ready.wait()
                if api.choice[0] == "sample":
                    js("busy('Loading the sample shop… (about a minute)')")
                    seed_sample(env)
                else:
                    js("busy('Creating your company…')")
                    make_company(*api.choice[1:])
            say("Opening…")
            serve(port)
            window.load_url(f"http://127.0.0.1:{port}/")
        except Exception as exc:
            log.exception("start failed")
            js(f"fail({str(exc)!r})")

    def closing() -> None:
        server = getattr(serve, "server", None)
        if server is not None:
            server.should_exit = True
        stop_everything(state["started_docker"])

    window.events.closed += closing
    webview.start(start, private_mode=False, storage_path=str(HOME / "webview"))


if __name__ == "__main__":
    main()
