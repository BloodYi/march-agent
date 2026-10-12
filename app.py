"""antigravity-chat — a minimal web chat UI backed by the `agy` (antigravity) CLI.

Self-contained FastAPI app. It shells `agy --print` headlessly using `--output-format stream-json`
and streams real-time token events to a lightweight web UI, supporting stateful conversations.

This is NOT the OpenRouter proxy on :3120 — it talks to the real antigravity CLI.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StrictBool
from agy_metadata import read_session_models, read_session_thinking, read_session_historical_error
from agy_client_profile import DesktopProfileProxy
from project_updates import ProjectUpdater
from update_common import UpdateError

logger = logging.getLogger("antigravity-chat")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def _load_env() -> None:
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_env()

PORT = int(os.getenv("PORT", "26903"))
AGY_BIN = os.getenv("AGY_BIN", "agy")
AGY_TIMEOUT = int(os.getenv("AGY_TIMEOUT", "1800"))  # seconds per turn
AGY_SANDBOX = os.getenv("AGY_SANDBOX", "0") not in ("0", "false", "")
AGY_OUTPUT_FORMAT = os.getenv("AGY_OUTPUT_FORMAT", "stream-json")
AGY_PERSIST_CONVERSATIONS = os.getenv("AGY_PERSIST_CONVERSATIONS", "true").lower() in ("1", "true", "yes")
AGY_DISABLE_MCP = os.getenv("AGY_DISABLE_MCP", "false").lower() in ("1", "true", "yes")
AGY_DISABLED_MCP_SERVERS = {
    name.strip().lower()
    for name in os.getenv("AGY_DISABLED_MCP_SERVERS", "").split(",")
    if name.strip()
}
CONVERSATIONS_FILE = Path(os.getenv("AGY_CONVERSATIONS_FILE", str(Path(__file__).parent / ".antigravity_conversations.json")))
MESSAGES_DIR = Path(os.getenv("AGY_MESSAGES_DIR", str(Path(__file__).parent / "data" / "messages")))
MESSAGES_DIR.mkdir(parents=True, exist_ok=True)

# Neutral working dir so agy does not pull in unrelated repo context.
SCRATCH = Path(os.getenv("AGY_SCRATCH", str(Path(__file__).parent / "scratch")))
SCRATCH.mkdir(parents=True, exist_ok=True)
UPLOADS = Path(os.getenv("AGY_UPLOADS", str(Path(__file__).parent / "uploads")))
UPLOADS.mkdir(parents=True, exist_ok=True)
STATIC = Path(__file__).parent / "static"

HOME = os.path.expanduser("~")
DEFAULT_ALLOWED_ROOTS = f"{HOME}"
_allowed_raw = os.getenv("AGY_ALLOWED_ROOTS", DEFAULT_ALLOWED_ROOTS)
ALLOWED_ROOTS = [
    os.path.realpath(os.path.expanduser(p.strip()))
    for p in _allowed_raw.split(os.pathsep)
    if p.strip()
]
if str(UPLOADS.resolve()) not in ALLOWED_ROOTS:
    ALLOWED_ROOTS.append(str(UPLOADS.resolve()))

ACCOUNTS_DIR = Path(os.getenv("AGY_ACCOUNTS_DIR", str(Path(HOME) / ".gemini" / "antigravity-cli" / "accounts")))
ACTIVE_TOKEN_FILE = Path(HOME) / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
try:
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
except Exception:
    pass

DEFAULT_SYSTEM = (
    "You are a helpful, concise assistant chatting with a user in a web UI. "
    "When answering questions, analyzing, or reasoning, you should first output your thinking process "
    "inside <think>...</think> tags, and then provide your final response directly in Markdown. "
    "Do not use tools, do not read or write files, and do not run shell commands unless the user "
    "explicitly asks you to or has provided attached files/images to inspect. "
    "当用户需要获取或下载文件时，如果你生成或修改了文件，请在回答中提供文件下载链接，格式为 [📥 点击下载 文件名](/api/download?path=文件相对或绝对路径)，客户端即可直接下载到本地电脑。"
)

app = FastAPI(title="antigravity-chat", version="1.1.0")

# --------------------------------------------------------------------------- #
# Conversation persistence layer
# --------------------------------------------------------------------------- #

_conversations_lock = asyncio.Lock()


def _sanitize_token(val: str | None, default: str | None = None) -> str | None:
    if not val or not isinstance(val, str):
        return default
    clean = "".join(c for c in val if c.isalnum() or c in ("-", "_")).strip()
    return clean if clean else default


def _read_conversations() -> dict:
    if not CONVERSATIONS_FILE.exists():
        return {}
    try:
        return json.loads(CONVERSATIONS_FILE.read_text())
    except Exception as exc:
        logger.warning(f"Failed to read conversation mapping file {CONVERSATIONS_FILE}: {exc}")
        return {}


def _write_conversations(data: dict) -> None:
    try:
        tmp_file = CONVERSATIONS_FILE.with_suffix(".json.tmp")
        tmp_file.write_text(json.dumps(data, indent=2))
        tmp_file.replace(CONVERSATIONS_FILE)
    except Exception as exc:
        logger.warning(f"Failed to write conversation mapping file {CONVERSATIONS_FILE}: {exc}")


def _ensure_user(data: dict, uid: str) -> dict:
    """Return user's store in multi-conversation format, migrating old format in-memory.

    New format: {"active_id": <cid|None>, "conversations": {<cid>: {"title":..., "updated_at":...}}}
    Old format: {"conversation_id":..., "title":..., "updated_at":...}
    """
    entry = data.get(uid)
    if isinstance(entry, dict) and isinstance(entry.get("conversations"), dict):
        entry.setdefault("active_id", None)
        return entry
    new_entry: dict = {"active_id": None, "conversations": {}}
    if isinstance(entry, dict):
        cid = _sanitize_token(entry.get("conversation_id"))
        if cid:
            new_entry["conversations"][cid] = {
                "title": entry.get("title") or "Conversation",
                "updated_at": entry.get("updated_at") or time.time(),
                "pinned": bool(entry.get("pinned", False)),
            }
            new_entry["active_id"] = cid
    data[uid] = new_entry
    return new_entry


def parse_transcript_to_messages(transcript_path: Path) -> list[dict]:
    if not transcript_path.exists():
        return []
    try:
        lines = transcript_path.read_text(errors="replace").splitlines()
    except Exception as exc:
        logger.warning(f"Failed to read transcript {transcript_path}: {exc}")
        return []

    messages: list[dict] = []
    current_user: dict | None = None
    assistant_steps: list[dict] = []

    def flush_turn() -> None:
        nonlocal current_user, assistant_steps
        if current_user:
            messages.append(current_user)
            current_user = None
        if assistant_steps:
            final_content = ""
            all_thinking = []
            tool_name = None
            for step in assistant_steps:
                c = step.get("content") or ""
                th = step.get("thinking") or ""
                tl = step.get("tool")
                if tl:
                    tool_name = tl
                if th and th.strip() and th.strip() != "...":
                    all_thinking.append(th.strip())
                if c.strip():
                    final_content = c.strip()

            if "<think>" in final_content:
                tm = re.search(r"<think>(.*?)(?:</think>|$)", final_content, re.DOTALL)
                if tm:
                    th_inner = tm.group(1).strip()
                    if th_inner and th_inner != "...":
                        all_thinking.append(th_inner)
                    final_content = re.sub(r"<think>.*?</think>\s*", "", final_content, flags=re.DOTALL).strip()

            combined_thinking = "\n\n".join(all_thinking).strip()
            if final_content or combined_thinking or tool_name:
                messages.append({
                    "role": "assistant",
                    "content": final_content,
                    "thinking": combined_thinking,
                    "thoughtOpen": True,
                    "tool": tool_name,
                })
            assistant_steps = []

    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue

        t = obj.get("type")
        src = obj.get("source")
        content = obj.get("content") or ""
        thinking = obj.get("thinking") or ""
        tool_calls = obj.get("tool_calls") or []

        if t == "USER_INPUT" and src == "USER_EXPLICIT":
            flush_turn()
            m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content, re.DOTALL)
            raw = m.group(1) if m else content
            if "--- Conversation so far ---" in raw:
                parts = raw.split("--- Conversation so far ---")
                after = parts[1].split("--- End conversation ---")[0].strip()
                user_lines = []
                for uline in after.splitlines():
                    if uline.startswith("User: "):
                        user_lines.append(uline[6:])
                    elif user_lines:
                        user_lines[-1] += "\n" + uline
                if user_lines:
                    raw = user_lines[-1]
            elif "Reply as the Assistant to the last User message" in raw:
                raw = raw.split("Reply as the Assistant to the last User message")[0].strip()

            raw = re.sub(r"\[系统设定：.*?\]\s*", "", raw).strip()
            attachments = []
            att_matches = re.finditer(r"\[附带(图片|文件):\s*(.+?)\s*\(本地路径:\s*(.+?)\)[\s\S]*?\]", raw)
            for am in att_matches:
                is_img = am.group(1) == "图片"
                att_name = am.group(2)
                att_path = am.group(3)
                url = None
                if "uploads/" in att_path:
                    url = "/uploads/" + att_path.split("uploads/")[-1]
                attachments.append({
                    "filename": att_name,
                    "path": att_path,
                    "url": url or att_path,
                    "is_image": is_img,
                })
            cleaned_text = re.sub(r"\[附带(图片|文件):.+?\]\s*", "", raw).strip()
            current_user = {"role": "user", "content": cleaned_text or raw}
            if attachments:
                current_user["attachments"] = attachments

        elif t == "PLANNER_RESPONSE" and src == "MODEL":
            tl = None
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    tl = fn.get("name") or tc.get("name") or tl
            assistant_steps.append({"content": content, "thinking": thinking, "tool": tl})

    flush_turn()
    return messages


def save_conversation(user_id: str, conversation_id: str, title: str | None = None) -> dict:
    """Upsert a conversation into the user's list and mark it active."""
    uid = _sanitize_token(user_id, "default") or "default"
    cid = _sanitize_token(conversation_id)
    if not cid:
        return {}
    data = _read_conversations()
    target_uids = set([uid, "default"])
    for target_uid in target_uids:
        udata = _ensure_user(data, target_uid)
        convs = udata["conversations"]
        existing = convs.get(cid, {})
        convs[cid] = {
            "title": title or existing.get("title") or "Conversation",
            "updated_at": time.time(),
            "pinned": bool(existing.get("pinned", False)),
        }
        udata["active_id"] = cid
    _write_conversations(data)
    default_entry = data.get("default", {}).get("conversations", {}).get(cid, {})
    return {"conversation_id": cid, **default_entry}


async def save_conversation_async(user_id: str, conversation_id: str, title: str | None = None) -> dict:
    async with _conversations_lock:
        return await asyncio.to_thread(save_conversation, user_id, conversation_id, title)


def get_conversation_id(user_id: str = "default") -> str | None:
    """Retrieve the active conversation ID."""
    uid = _sanitize_token(user_id, "default") or "default"
    data = _read_conversations()
    udata = _ensure_user(data, uid)
    if udata.get("active_id"):
        return udata.get("active_id")
    return data.get("default", {}).get("active_id")


async def get_conversation_id_async(user_id: str = "default") -> str | None:
    async with _conversations_lock:
        return await asyncio.to_thread(get_conversation_id, user_id)


def list_user_conversations(user_id: str = "default") -> tuple[list, str | None]:
    """Return all conversations across the deployment (pinned-first, updated_at desc, active_id)."""
    uid = _sanitize_token(user_id, "default") or "default"
    data = _read_conversations()

    merged_convs = {}
    active_id = None

    if "default" in data and isinstance(data["default"], dict):
        merged_convs.update(data["default"].get("conversations", {}))
        active_id = data["default"].get("active_id")

    if uid in data and isinstance(data[uid], dict):
        for cid, info in data[uid].get("conversations", {}).items():
            if cid not in merged_convs or info.get("updated_at", 0) > merged_convs[cid].get("updated_at", 0):
                merged_convs[cid] = info
        if data[uid].get("active_id"):
            active_id = data[uid].get("active_id")

    for other_uid, udata in data.items():
        if isinstance(udata, dict) and "conversations" in udata:
            for cid, info in udata["conversations"].items():
                if cid not in merged_convs:
                    merged_convs[cid] = info

    items = [
        {"conversation_id": cid, "title": info.get("title") or "Conversation",
         "updated_at": info.get("updated_at") or 0,
         "pinned": bool(info.get("pinned", False))}
        for cid, info in merged_convs.items()
    ]
    items.sort(key=lambda x: (x["pinned"], x["updated_at"]), reverse=True)
    return items, active_id


async def list_user_conversations_async(user_id: str = "default") -> tuple[list, str | None]:
    async with _conversations_lock:
        return await asyncio.to_thread(list_user_conversations, user_id)


def delete_user_conversation(user_id: str, conversation_id: str) -> bool:
    """Delete one conversation across all user stores and message cache."""
    uid = _sanitize_token(user_id, "default") or "default"
    cid = _sanitize_token(conversation_id)
    if not cid:
        return False
    data = _read_conversations()
    deleted = False
    for target_uid in set([uid, "default"] + list(data.keys())):
        udata = data.get(target_uid)
        if isinstance(udata, dict) and "conversations" in udata and cid in udata["conversations"]:
            del udata["conversations"][cid]
            deleted = True
            if udata.get("active_id") == cid:
                remaining = sorted(udata["conversations"].items(), key=lambda kv: kv[1].get("updated_at") or 0, reverse=True)
                udata["active_id"] = remaining[0][0] if remaining else None
    if deleted:
        _write_conversations(data)
        msg_file = MESSAGES_DIR / f"{cid}.json"
        msg_file.unlink(missing_ok=True)
    return deleted


async def delete_user_conversation_async(user_id: str, conversation_id: str) -> bool:
    async with _conversations_lock:
        return await asyncio.to_thread(delete_user_conversation, user_id, conversation_id)


def set_active_conversation(user_id: str, conversation_id: str) -> bool:
    """Mark an existing conversation as active."""
    uid = _sanitize_token(user_id, "default") or "default"
    cid = _sanitize_token(conversation_id)
    if not cid:
        return False
    data = _read_conversations()
    found = False
    for target_uid in set([uid, "default"]):
        udata = _ensure_user(data, target_uid)
        if cid in udata["conversations"]:
            udata["active_id"] = cid
            udata["conversations"][cid]["updated_at"] = time.time()
            found = True
    if found:
        _write_conversations(data)
    return found


async def set_active_conversation_async(user_id: str, conversation_id: str) -> bool:
    async with _conversations_lock:
        return await asyncio.to_thread(set_active_conversation, user_id, conversation_id)


def rename_conversation(user_id: str, conversation_id: str, title: str) -> bool:
    """Rename a conversation."""
    uid = _sanitize_token(user_id, "default") or "default"
    cid = _sanitize_token(conversation_id)
    clean_title = (title or "").strip()[:80]
    if not cid or not clean_title:
        return False
    data = _read_conversations()
    found = False
    for target_uid in set([uid, "default"] + list(data.keys())):
        udata = data.get(target_uid)
        if isinstance(udata, dict) and "conversations" in udata and cid in udata["conversations"]:
            udata["conversations"][cid]["title"] = clean_title
            found = True
    if found:
        _write_conversations(data)
    return found


async def rename_conversation_async(user_id: str, conversation_id: str, title: str) -> bool:
    async with _conversations_lock:
        return await asyncio.to_thread(rename_conversation, user_id, conversation_id, title)


def set_conversation_pinned(user_id: str, conversation_id: str, pinned: bool) -> bool:
    """Persist a pin for an existing conversation."""
    uid = _sanitize_token(user_id)
    cid = _sanitize_token(conversation_id)
    if not cid or not isinstance(pinned, bool):
        return False
    data = _read_conversations()
    found = False
    for target_uid in set([uid, "default"] + list(data.keys())):
        if not target_uid:
            continue
        udata = data.get(target_uid)
        if isinstance(udata, dict) and "conversations" in udata and cid in udata["conversations"]:
            udata["conversations"][cid]["pinned"] = pinned
            found = True
    if not found:
        return False
    tmp_file = CONVERSATIONS_FILE.with_suffix(".json.tmp")
    try:
        tmp_file.write_text(json.dumps(data, indent=2))
        tmp_file.replace(CONVERSATIONS_FILE)
    except Exception as exc:
        logger.warning(f"Failed to persist conversation pin: {exc}")
        raise HTTPException(status_code=500, detail="could not save conversation pin") from exc
    return True


async def set_conversation_pinned_async(user_id: str, conversation_id: str, pinned: bool) -> bool:
    async with _conversations_lock:
        return await asyncio.to_thread(set_conversation_pinned, user_id, conversation_id, pinned)


def clear_conversation(user_id: str = "default") -> None:
    """Clear ALL conversations for a user."""
    uid = _sanitize_token(user_id, "default") or "default"
    data = _read_conversations()
    if uid in data:
        del data[uid]
        _write_conversations(data)


async def clear_conversation_async(user_id: str = "default") -> None:
    async with _conversations_lock:
        await asyncio.to_thread(clear_conversation, user_id)


def list_conversations() -> dict:
    """List stored conversation mappings."""
    return _read_conversations()


async def list_conversations_async() -> dict:
    async with _conversations_lock:
        return await asyncio.to_thread(list_conversations)

# --------------------------------------------------------------------------- #
# Path jailing & filesystem browsing
# --------------------------------------------------------------------------- #

def _within_allowed(path: str | None) -> str | None:
    if not path or not path.strip():
        return None
    rp = os.path.realpath(os.path.expanduser(path.strip()))
    for root in ALLOWED_ROOTS:
        if rp == root or rp.startswith(root + os.sep):
            return rp
    return None


def _is_project_dir(path: str) -> bool:
    return any(
        os.path.exists(os.path.join(path, m))
        for m in (".git", "CLAUDE.md", "AGENTS.md", "package.json", "pyproject.toml", "README.md")
    )


def browse_fs(path: str | None) -> dict:
    """List immediate subdirectories of a jailed path (or allowed roots if path is empty)."""
    if not path:
        entries = [
            {
                "name": os.path.basename(r) or r,
                "path": r,
                "is_project": _is_project_dir(r),
            }
            for r in ALLOWED_ROOTS
            if os.path.isdir(r)
        ]
        return {"path": "", "parent": None, "up": None, "roots": ALLOWED_ROOTS, "entries": entries}

    rp = _within_allowed(path)
    if rp is None or not os.path.isdir(rp):
        raise HTTPException(status_code=400, detail="Path outside allowed roots or not a directory")

    entries = []
    try:
        filenames = sorted(os.listdir(rp))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to list directory: {exc}")

    for name in filenames:
        if name.startswith("."):
            continue
        full = os.path.join(rp, name)
        if os.path.isdir(full):
            entries.append({"name": name, "path": full, "is_project": _is_project_dir(full)})

    parent = os.path.dirname(rp)
    up = parent if _within_allowed(parent) is not None else ""
    return {"path": rp, "parent": up, "up": up, "roots": ALLOWED_ROOTS, "entries": entries}

# --------------------------------------------------------------------------- #
# Session auth (form login page + HttpOnly session cookie)
# --------------------------------------------------------------------------- #

BASIC_AUTH_USER = os.getenv("BASIC_AUTH_USER", "")
BASIC_AUTH_PASS = os.getenv("BASIC_AUTH_PASS", "")
_AUTH_ENABLED = bool(BASIC_AUTH_USER and BASIC_AUTH_PASS)

_SESSION_COOKIE = "agy_session"
_SESSION_TTL = 7 * 24 * 3600  # 7 days
_sessions: dict[str, float] = {}  # token -> expiry timestamp
project_updater = ProjectUpdater(Path(__file__).parent, PORT)

# Paths reachable without a session (login page/API, health checks, artwork).
_AUTH_EXEMPT = {"/healthz", "/favicon.ico", "/favicon.png", "/login", "/api/login", "/api/me"}
_AUTH_EXEMPT_PREFIX = ("/static/bg-login.jpg", "/static/bg-chat.jpg", "/static/logo.png", "/uploads")


def _issue_session() -> str:
    now = time.time()
    for t in [t for t, exp in _sessions.items() if exp < now]:
        _sessions.pop(t, None)
    token = secrets.token_urlsafe(32)
    _sessions[token] = now + _SESSION_TTL
    return token


def _valid_session(request: Request) -> bool:
    token = request.cookies.get(_SESSION_COOKIE, "")
    if not token:
        return False
    exp = _sessions.get(token)
    if not exp or exp < time.time():
        _sessions.pop(token, None)
        return False
    return True


def _drop_session(request: Request) -> None:
    token = request.cookies.get(_SESSION_COOKIE, "")
    if token:
        _sessions.pop(token, None)


@app.middleware("http")
async def _session_auth(request: Request, call_next):
    path = request.url.path
    # A job-scoped read capability lets the initiating browser observe a restart.
    # This endpoint checks its own short-lived token and cannot start updates.
    if request.method == 'GET' and re.fullmatch(r'/api/updates/jobs/[a-f0-9]{32}', path):
        return await call_next(request)
    if _AUTH_ENABLED and path not in _AUTH_EXEMPT and not path.startswith(_AUTH_EXEMPT_PREFIX):
        if not _valid_session(request):
            if path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"detail": "login required"})
            return RedirectResponse(url="/login", status_code=302)
    return await call_next(request)


@app.get('/api/updates')
async def updates_status(check: bool = False):
    return await asyncio.to_thread(project_updater.check if check else project_updater.info)


@app.post('/api/updates/check')
async def updates_check():
    return await asyncio.to_thread(project_updater.check, True)


class UpdateRequest(BaseModel):
    commit: str


@app.post('/api/updates/install', status_code=202)
async def updates_install(request: Request, req: UpdateRequest):
    if not _AUTH_ENABLED or not _valid_session(request):
        raise HTTPException(status_code=403, detail='请先登录，再更新项目。')
    origin = request.headers.get('origin')
    expected = str(request.url).split('/api/', 1)[0]
    if ((origin and origin.rstrip('/') != expected)
            or request.headers.get('x-requested-with') != 'MarchAgent'):
        raise HTTPException(status_code=403, detail='请在本站的更新窗口中操作。')
    try:
        return await asyncio.to_thread(project_updater.start, req.commit)
    except UpdateError as error:
        raise HTTPException(status_code=409, detail=str(error))


@app.get('/api/updates/jobs/{job_id}')
async def updates_job(job_id: str, request: Request):
    try:
        return project_updater.read_job(job_id, request.headers.get('x-update-token', ''))
    except UpdateError as error:
        raise HTTPException(status_code=403, detail=str(error))


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOADS), name="uploads")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(STATIC / "login.html")


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


@app.post("/api/login")
async def api_login(req: LoginRequest) -> JSONResponse:
    if _AUTH_ENABLED:
        ok = secrets.compare_digest(req.username or "", BASIC_AUTH_USER) and \
            secrets.compare_digest(req.password or "", BASIC_AUTH_PASS)
        if not ok:
            raise HTTPException(status_code=401, detail="invalid credentials")
    token = _issue_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(_SESSION_COOKIE, token, max_age=_SESSION_TTL,
                    httponly=True, samesite="lax", secure=True, path="/")
    return resp


@app.post("/api/logout")
async def api_logout(request: Request) -> JSONResponse:
    _drop_session(request)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me")
async def api_me(request: Request) -> dict:
    if _AUTH_ENABLED and not _valid_session(request):
        raise HTTPException(status_code=401, detail="login required")
    return {"ok": True, "auth": _AUTH_ENABLED}


@app.api_route("/favicon.ico", methods=["GET", "HEAD"])
async def favicon() -> FileResponse:
    return FileResponse(STATIC / "logo.png", media_type="image/png")


@app.api_route("/favicon.png", methods=["GET", "HEAD"])
async def favicon_png() -> FileResponse:
    return FileResponse(STATIC / "logo.png", media_type="image/png")


@app.post("/api/upload")
async def api_upload(request: Request, file: UploadFile | None = File(None)) -> JSONResponse:
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = await request.json()
            orig_name = Path(payload.get("filename") or "upload.png").name
            data_str = payload.get("data") or ""
            if "," in data_str:
                _, data_str = data_str.split(",", 1)
            raw_bytes = base64.b64decode(data_str)
        elif file is not None:
            orig_name = Path(file.filename or "upload").name
            raw_bytes = await file.read()
        else:
            raise HTTPException(status_code=400, detail="no file or data provided")

        clean_stem = "".join(c for c in Path(orig_name).stem if c.isalnum() or c in ("-", "_", " ")).strip() or "file"
        ext = Path(orig_name).suffix.lower()
        if not ext and "application/json" in content_type:
            ext = ".png"
        unique_name = f"{int(time.time())}_{secrets.token_hex(4)}_{clean_stem}{ext}"
        save_path = UPLOADS / unique_name

        save_path.write_bytes(raw_bytes)

        file_size = len(raw_bytes)
        is_image = ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")
        return JSONResponse({
            "filename": orig_name,
            "saved_name": unique_name,
            "url": f"/uploads/{unique_name}",
            "path": str(save_path.resolve()),
            "is_image": is_image,
            "size": file_size,
        })
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(f"Failed to save upload: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to save upload: {exc}")


@app.get("/api/download")
async def api_download(path: str) -> FileResponse:
    """Download a file from the server directly to the user's client."""
    if not path or not path.strip():
        raise HTTPException(status_code=400, detail="path parameter is required")

    clean_path = urllib.parse.unquote(path.strip())
    if clean_path.startswith("file://"):
        clean_path = re.sub(r"^file://+", "/", clean_path)

    clean_path = clean_path.split("#")[0].split("?")[0]
    candidate = Path(clean_path).expanduser()

    if not candidate.is_absolute():
        if (SCRATCH / clean_path).exists():
            candidate = SCRATCH / clean_path
        elif (UPLOADS / clean_path).exists():
            candidate = UPLOADS / clean_path
        else:
            candidate = SCRATCH / clean_path

    rp = _within_allowed(str(candidate))
    if not rp:
        scratch_real = os.path.realpath(str(SCRATCH))
        uploads_real = os.path.realpath(str(UPLOADS))
        cand_real = os.path.realpath(str(candidate))
        if cand_real == scratch_real or cand_real.startswith(scratch_real + os.sep):
            rp = cand_real
        elif cand_real == uploads_real or cand_real.startswith(uploads_real + os.sep):
            rp = cand_real
        else:
            raise HTTPException(status_code=403, detail="Access denied: path is outside allowed directories")

    if not os.path.exists(rp) or not os.path.isfile(rp):
        raise HTTPException(status_code=404, detail=f"File not found: {Path(clean_path).name}")

    filename = Path(rp).name
    safe_filename = urllib.parse.quote(filename)
    return FileResponse(
        path=rp,
        filename=filename,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{safe_filename}",
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


_models_cache: tuple[float, list[dict[str, str]]] | None = None
_MODELS_TTL = 300

_models_label_map: dict[str, str] = {
    "gemini-3.8-flash-high": "Gemini 3.8 Flash (High)",
    "gemini-3.8-flash-medium": "Gemini 3.8 Flash (Medium)",
    "gemini-3.8-flash-low": "Gemini 3.8 Flash (Low)",
    "gemini-3.7-flash-high": "Gemini 3.7 Flash (High)",
    "gemini-3.7-flash-medium": "Gemini 3.7 Flash (Medium)",
    "gemini-3.7-flash-low": "Gemini 3.7 Flash (Low)",
    "gemini-3.6-flash-high": "Gemini 3.6 Flash (High)",
    "gemini-3.6-flash-medium": "Gemini 3.6 Flash (Medium)",
    "gemini-3.6-flash-low": "Gemini 3.6 Flash (Low)",
    "gemini-3.1-pro-high": "Gemini 3.1 Pro (High)",
    "gemini-3.1-pro-low": "Gemini 3.1 Pro (Low)",
    "claude-sonnet-4-6": "Claude Sonnet 4.6 (Thinking)",
    "claude-opus-4-6-thinking": "Claude Opus 4.6 (Thinking)",
    "gpt-oss-120b-medium": "GPT-OSS 120B (Medium)",
}


_RETURNED_MODEL_FIELDS = {"modelVersion", "model_version", "responseModel", "response_model"}
_RESPONSE_ID_FIELDS = {"responseId", "response_id", "ResponseID"}


def _new_route_info(request_id: str | None = None, requested_model: str | None = None) -> dict:
    return {"request_id": request_id, "requested_model": requested_model,
            "cli_config_model": None, "cli_reported_model": None, "returned_models": [], "returned_model_fulls": [],
            "response_ids": [], "upstream_urls": [], "trace_ids": [],
            "observed_step_indices": [], "model_step_indices": [], "uncovered_step_indices": [], "coverage_unknown": False,
            "evidence": [], "status": "unavailable"}


def _metadata_url(value: str) -> str | None:
    """Keep only the endpoint; never expose URL credentials or query values."""
    try:
        url = urllib.parse.urlsplit(value)
        if url.scheme not in ("http", "https") or not url.hostname:
            return None
        host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
        if url.port:
            host += f":{url.port}"
        return urllib.parse.urlunsplit((url.scheme, host, url.path, "", ""))
    except ValueError:
        return None


def _response_evidence(obj: dict) -> list[dict]:
    """Read explicit metadata containers, never response text, tools or configuration."""
    kind = obj.get("event") or obj.get("type") or "metadata"
    if kind not in ("init", "result", "step_update", "metadata", "response_metadata"):
        return []
    scopes = [("", obj)]
    for name in (("init",) if kind == "init" else ("result", "step_update", "metadata")):
        if isinstance(obj.get(name), dict):
            scopes.append((name + ".", obj[name]))
    records = []
    for prefix, payload in scopes:
        containers = [(prefix, payload)]
        if kind != "init" and isinstance(payload.get("response_metadata"), dict):
            containers.append((prefix + "response_metadata.", payload["response_metadata"]))
        for field_prefix, data in containers:
            allowed = {"model"} if kind == "init" else (
                _RETURNED_MODEL_FIELDS | _RESPONSE_ID_FIELDS | {"response_model_full", "responseModelFull", "traceId", "trace_id", "upstream_url"}
                | ({"model"} if kind in ("result", "step_update") else set()))
            for key in sorted(allowed):
                value = data.get(key)
                if not isinstance(value, str) or not value.strip():
                    continue
                if key == "upstream_url":
                    value = _metadata_url(value)
                    if not value:
                        continue
                records.append({"field": field_prefix + key, "value": value,
                                "source": "cli_" + kind})
    return records


def _merge_response_evidence(info: dict, records: list[dict]) -> None:
    for record in records:
        if record in info["evidence"]:
            continue
        info["evidence"].append(record)
        key, value = record["field"].rsplit(".", 1)[-1], record["value"]
        if key in _RETURNED_MODEL_FIELDS and value not in info["returned_models"]:
            info["returned_models"].append(value)
        elif key in ("response_model_full", "responseModelFull") and value not in info["returned_model_fulls"]:
            info["returned_model_fulls"].append(value)
        elif key in _RESPONSE_ID_FIELDS and value not in info["response_ids"]:
            info["response_ids"].append(value)
        elif key == "model":
            target = "cli_config_model" if record["source"] == "cli_init" else "cli_reported_model"
            info[target] = value
        elif key in ("traceId", "trace_id", "Trace"):
            if value not in info["trace_ids"]:
                info["trace_ids"].append(value)
        elif key in ("upstream_url", "URL"):
            if value not in info["upstream_urls"]:
                info["upstream_urls"].append(value)
    count = max(len(info["returned_models"]), len(info["returned_model_fulls"]))
    info["status"] = "conflict" if count > 1 else "available" if count else "unavailable"


def _update_route_coverage(info: dict, response_steps: set[int], coverage_unknown: bool = False) -> None:
    fields = _RETURNED_MODEL_FIELDS | {"response_model_full", "responseModelFull"}
    covered = {index for record in info["evidence"] if record["field"].rsplit(".", 1)[-1] in fields
               for index in record.get("step_indices", []) if index in response_steps}
    info["observed_step_indices"] = sorted(response_steps)
    info["model_step_indices"] = sorted(covered)
    info["uncovered_step_indices"] = sorted(response_steps - covered)
    info["coverage_unknown"] = coverage_unknown
    info["model_conflict"] = max(len(info["returned_models"]), len(info["returned_model_fulls"])) > 1
    if info["status"] in ("available", "conflict") and (coverage_unknown or info["uncovered_step_indices"]):
        info["status"] = "partial"


def _request_log_evidence(log_path: Path) -> list[dict]:
    records = []
    try:
        with log_path.open(errors="replace") as log_file:
            for line in log_file:
                # Unverified log layouts stay unavailable; never scan arbitrary text.
                match = re.fullmatch(r"\[streamGenerateContent\] ResponseID: ([A-Za-z0-9._:-]{1,200})\s*", line)
                if match:
                    records.append({"field": "log.ResponseID", "value": match[1], "source": "request_log"})
    except OSError:
        pass
    return records


@app.get("/api/inspect-route")
async def inspect_route_ep() -> dict:
    return {"ok": True, "data": _new_route_info(),
            "message": "请查看对应消息的请求证据；全局日志不能证明该消息的返回模型。"}


class InspectRouteRequest(BaseModel):
    model: str | None = None


@app.post("/api/inspect-route")
async def inspect_selected_route(req: InspectRouteRequest) -> dict:
    """Fresh synthetic request; never continue or persist a human conversation."""
    info = _new_route_info(requested_model=req.model)
    failed = False
    async def collect(folder):
        nonlocal info, failed
        async for event in _stream_agy(
            prompt="Reply only OK. Do not use tools or access files.",
            model=req.model, cwd=folder, persist_conversation=False,
        ):
            if isinstance(event.get("route_info"), dict):
                info = event["route_info"]
            if event.get("event") == "error":
                failed = True
    try:
        with tempfile.TemporaryDirectory(prefix="agy-model-check-", dir=str(SCRATCH)) as folder:
            await asyncio.wait_for(collect(folder), timeout=90)
    except asyncio.TimeoutError:
        return {"ok": False, "data": info, "message": "检测超时，请稍后重试。"}
    except Exception:
        logger.warning("Selected-model inspection failed")
        failed = True
    return {"ok": not failed, "data": info, "message": "CLI 未完成本次检测，请稍后重试。" if failed
            else "本次没有返回可关联的模型名。" if info["status"] == "unavailable" else "本次检测已完成。"}



# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class Message(BaseModel):
    role: str
    content: str = ""
    attachments: list[dict] | None = None


class ChatRequest(BaseModel):
    messages: list[Message]
    model: str | None = None
    system: str | None = None
    cwd: str | None = None
    conversation_id: str | None = None
    continue_conversation: bool | None = None
    user_id: str | None = "default"
    thinking: bool | None = None


# --------------------------------------------------------------------------- #
# agy invocation
# --------------------------------------------------------------------------- #

THINKING_PROMPT_PREFIX = (
    "[系统设定：回答问题、推理或分析时，请务必先在 <think>...</think> 标签内输出完整的思考过程，然后给出最终回答。]"
)


def _format_message_content(m: Message) -> str:
    content = (m.content or "").strip()
    if m.attachments:
        att_lines = []
        for att in m.attachments:
            name = att.get("filename") or "file"
            path = att.get("path") or att.get("url") or ""
            is_img = att.get("is_image", False)
            if is_img:
                att_lines.append(f"[附带图片: {name} (本地路径: {path})。请使用相应工具查看此图片内容并作答。]")
            else:
                att_lines.append(f"[附带文件: {name} (本地路径: {path})。请使用相应工具查看此文件内容并作答。]")
        prefix = "\n".join(att_lines)
        content = f"{prefix}\n\n{content}" if content else prefix
    return content


def _render_prompt(messages: list[Message], system: str | None, thinking: bool | None = None) -> str:
    """Flatten a chat history into a single prompt for agy print mode."""
    sys_txt = (system or DEFAULT_SYSTEM).strip()
    if thinking and "<think>" not in sys_txt:
        sys_txt = f"{sys_txt}\n\n{THINKING_PROMPT_PREFIX}"
    lines = [sys_txt, "", "--- Conversation so far ---"]
    for m in messages:
        role = {"user": "User", "assistant": "Assistant", "system": "System"}.get(
            m.role, m.role.capitalize()
        )
        lines.append(f"{role}: {_format_message_content(m)}")
    lines.append("--- End conversation ---")
    lines.append("")
    lines.append("Reply as the Assistant to the last User message. "
                 "Output only your reply, with no role prefix.")
    return "\n".join(lines)


async def _resolve_prompt_and_session(req: ChatRequest) -> tuple[str, str | None, bool, str]:
    user_id = _sanitize_token(req.user_id, "default") or "default"
    cid = _sanitize_token(req.conversation_id)

    if not AGY_PERSIST_CONVERSATIONS:
        cid = None

    if AGY_PERSIST_CONVERSATIONS and not cid and req.continue_conversation:
        cid = await get_conversation_id_async(user_id)

    if cid or req.continue_conversation:
        base_prompt = _format_message_content(req.messages[-1]) if req.messages else ""
        continue_last = not cid and bool(req.continue_conversation)
        if req.thinking and base_prompt:
            prompt = f"{THINKING_PROMPT_PREFIX}\n\n{base_prompt}"
        else:
            prompt = base_prompt
    else:
        prompt = _render_prompt(req.messages, req.system, thinking=req.thinking)
        continue_last = False

    return prompt, cid, continue_last, user_id


def _build_argv(
    model: str | None,
    prompt: str = "",
    conversation_id: str | None = None,
    continue_last: bool = False,
    target_cwd: str | None = None,
    effort: str | None = None,
) -> list[str]:
    # agy treats the token after -p as its prompt, even when that token is a flag.
    # Attach the prompt to -p so permission and output flags cannot be swallowed.
    argv = [AGY_BIN, "--dangerously-skip-permissions"]
    if AGY_OUTPUT_FORMAT:
        argv += ["--output-format", AGY_OUTPUT_FORMAT]
    if effort:
        argv += ["--effort", effort]
    if AGY_SANDBOX:
        argv.append("--sandbox")
    if target_cwd:
        argv += ["--add-dir", target_cwd]
    if model and model.strip() and model != "default":
        argv += ["--model", model]
    clean_cid = _sanitize_token(conversation_id)
    if clean_cid:
        argv += ["--conversation", clean_cid]
    elif continue_last:
        argv.append("--continue")
    argv += ["--print-timeout", f"{AGY_TIMEOUT}s"]
    if prompt:
        argv.append(f"-p={prompt}")
    return argv


def _agy_environment() -> dict[str, str]:
    """Run chat turns with known-stalled MCP servers removed."""
    env = os.environ.copy()
    if not AGY_DISABLE_MCP and not AGY_DISABLED_MCP_SERVERS:
        return env

    source_home = Path(os.path.expanduser("~")).resolve()
    token_source = source_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    if not token_source.exists():
        logger.warning("AGY_DISABLE_MCP is enabled but no agy OAuth token was found; using the current HOME")
        return env

    runtime_home = Path(os.getenv("AGY_RUNTIME_HOME", str(SCRATCH / ".agy-home"))).expanduser().resolve()
    if runtime_home == source_home:
        logger.warning("AGY_RUNTIME_HOME points at HOME; MCP isolation is disabled")
        return env

    runtime_config = runtime_home / ".gemini" / "config"
    runtime_config.mkdir(parents=True, exist_ok=True)
    mcp_config = {"mcpServers": {}}
    if not AGY_DISABLE_MCP:
        source_config = source_home / ".gemini" / "config" / "mcp_config.json"
        try:
            source_data = json.loads(source_config.read_text())
            servers = source_data.get("mcpServers", {})
            if isinstance(servers, dict):
                mcp_config = {
                    **source_data,
                    "mcpServers": {
                        name: config
                        for name, config in servers.items()
                        if name.lower() not in AGY_DISABLED_MCP_SERVERS
                    },
                }
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to filter agy MCP config %s: %s", source_config, exc)
    (runtime_config / "mcp_config.json").write_text(json.dumps(mcp_config, indent=2) + "\n")

    # Reuse the user's token without copying credentials into the runtime directory.
    runtime_token = runtime_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    runtime_token.parent.mkdir(parents=True, exist_ok=True)
    if not runtime_token.is_symlink():
        if runtime_token.exists():
            runtime_token.unlink()
        runtime_token.symlink_to(token_source)

    source_installation_id = source_home / ".gemini" / "antigravity-cli" / "installation_id"
    if source_installation_id.exists():
        runtime_installation_id = runtime_home / ".gemini" / "antigravity-cli" / "installation_id"
        if not runtime_installation_id.is_symlink():
            if runtime_installation_id.exists():
                runtime_installation_id.unlink()
            runtime_installation_id.symlink_to(source_installation_id)

    env["HOME"] = str(runtime_home)
    return env


def _parse_ndjson_line(line_str: str) -> list[dict]:
    events: list[dict] = []
    line_clean = line_str.strip()
    if not line_clean:
        return events
    try:
        obj = json.loads(line_clean)
        if not isinstance(obj, dict):
            return [{"event": "delta", "delta": str(obj)}]

        def parse_tool(payload: dict) -> dict:
            tool_info = payload.get("tool_info")
            tool_name = payload.get("tool_name") or payload.get("tool") or payload.get("name")
            args = payload.get("args") or payload.get("input")
            if isinstance(tool_info, dict):
                tool_name = tool_name or tool_info.get("name")
                args = args or tool_info.get("parameters") or tool_info.get("input")
            return {
                "event": "tool",
                "tool": tool_name,
                "args": args,
                "status": payload.get("status") or payload.get("state") or "executing",
            }

        ev_type = obj.get("event") or obj.get("type")

        if ev_type == "init":
            init_obj = obj.get("init")
            cid = obj.get("conversation_id") or (
                init_obj.get("conversation_id") if isinstance(init_obj, dict) else None
            )
            model_val = (
                init_obj.get("model") if isinstance(init_obj, dict) else None
            ) or obj.get("model")
            init_ev = {"event": "init", "conversation_id": cid, "_cli_init_cid": cid}
            if model_val:
                init_ev["model"] = model_val
            events.append(init_ev)

        elif ev_type == "thinking":
            thinking = obj.get("thinking") or obj.get("thought") or obj.get("reasoning_content") or obj.get("thinking_delta") or obj.get("delta")
            if thinking:
                events.append({"event": "thinking", "thinking": thinking})

        elif ev_type in ("tool_call", "tool"):
            events.append(parse_tool(obj))

        elif ev_type == "step_update":
            step = obj.get("step_update") or {}
            if not isinstance(step, dict):
                step = {}
            stype = step.get("step_type")
            if stype == "thinking":
                thinking = step.get("thinking") or step.get("thinking_delta") or step.get("text_delta") or step.get("delta")
                if thinking:
                    events.append({"event": "thinking", "thinking": thinking})
            elif stype == "agent_response":
                delta = step.get("text_delta") or step.get("delta")
                if delta:
                    events.append({"event": "delta", "delta": delta})
                thinking = (
                    step.get("thinking")
                    or step.get("thought")
                    or step.get("reasoning_content")
                    or step.get("thinking_delta")
                )
                if thinking:
                    events.append({"event": "thinking", "thinking": thinking})
                if step.get("usage"):
                    res_ev = {"event": "result", "usage": step["usage"], "status": step.get("state") or "completed"}
                    if step.get("duration_seconds") is not None:
                        res_ev["duration_seconds"] = step["duration_seconds"]
                    events.append(res_ev)
            elif stype in ("tool", "tool_call") or any(key in step for key in ("tool", "tool_name", "tool_info")):
                events.append(parse_tool(step))
            else:
                delta = step.get("text_delta") or step.get("delta")
                if delta:
                    events.append({"event": "delta", "delta": delta})
                thinking = step.get("thinking") or step.get("thought") or step.get("reasoning_content") or step.get("thinking_delta")
                if thinking:
                    events.append({"event": "thinking", "thinking": thinking})

        elif ev_type == "result":
            res = obj.get("result")
            if not isinstance(res, dict):
                res = {}
            usage = res.get("usage") or obj.get("usage")
            status = res.get("status") or obj.get("status") or "completed"
            response = res.get("response")
            cid = res.get("conversation_id") or obj.get("conversation_id")
            model_val = res.get("model") or obj.get("model")
            dur_val = res.get("duration_seconds") or obj.get("duration_seconds")
            thinking = next((payload[key] for payload in (res, obj)
                             for key in ("thinking", "thought", "reasoning_content", "thinking_delta")
                             if payload.get(key)), None)
            if thinking:
                events.append({"event": "thinking", "thinking": thinking})
            result_event = {"event": "result", "usage": usage, "status": status}
            if model_val:
                result_event["model"] = model_val
            if dur_val is not None:
                result_event["duration_seconds"] = dur_val
            if response:
                result_event["response"] = str(response)
            if cid:
                result_event["conversation_id"] = cid
            events.append(result_event)
            if str(status).upper() not in ("SUCCESS", "COMPLETED", "OK"):
                error = res.get("error") or obj.get("error") or res.get("response")
                if error:
                    events.append({"event": "error", "error": str(error), "_result_error": True})

        else:
            delta = obj.get("text_delta") or obj.get("delta") or obj.get("text")
            if delta:
                events.append({"event": "delta", "delta": delta})
            thinking = obj.get("thinking") or obj.get("thought") or obj.get("reasoning_content")
            if thinking:
                events.append({"event": "thinking", "thinking": thinking})
            cid = obj.get("conversation_id")
            if cid:
                events.append({"event": "init", "conversation_id": cid})
        records = _response_evidence(obj)
        index = (obj.get("step_update", {}).get("step_index") if ev_type == "step_update"
                 and isinstance(obj.get("step_update"), dict) else obj.get("step_index"))
        if ev_type == "result" and isinstance(obj.get("result"), dict):
            index = obj["result"].get("step_index", index)
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < (1 << 31):
            for event in events:
                if event.get("event") == "thinking":
                    event["step_index"] = index
        if ev_type == "step_update" and isinstance(obj.get("step_update"), dict):
            if not events:
                events.append({"event": "metadata"})
            events[0]["_step_index"] = obj["step_update"].get("step_index")
            events[0]["_step_type"] = obj["step_update"].get("step_type")
            index = obj["step_update"].get("step_index")
            if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
                for record in records:
                    if record["field"].startswith("step_update."):
                        record["step_indices"] = [index]
        if records:
            if not events:
                events.append({"event": "metadata"})
            events[0]["_response_evidence"] = records
    except json.JSONDecodeError:
        events.append({"event": "delta", "delta": line_str + "\n"})
    return events


async def _stream_agy(
    prompt: str,
    model: str | None,
    cwd: str | None = None,
    conversation_id: str | None = None,
    continue_last: bool = False,
    user_id: str = "default",
    effort: str | None = None,
    persist_conversation: bool = True,
):
    """Run agy and yield structured events (dict) as NDJSON or text chunks arrive."""
    target_cwd = _within_allowed(cwd) if cwd else None
    if not target_cwd:
        target_cwd = str(SCRATCH)

    argv = _build_argv(
        model,
        prompt=prompt,
        conversation_id=conversation_id,
        continue_last=continue_last,
        target_cwd=target_cwd,
        effort=effort,
    )
    route_info = _new_route_info(uuid.uuid4().hex, model)
    log_dir = tempfile.TemporaryDirectory(prefix="agy-route-", dir=str(SCRATCH))
    log_path = Path(log_dir.name) / "cli.log"
    argv.extend(["--log-file", str(log_path)])
    profile_proxy = None
    update_lease = None
    try:
        updater = globals().get('project_updater')
        if updater:
            update_lease = updater.acquire_turn()
        run_env = _agy_environment()
        if run_env.get("AGY_UPSTREAM_PROFILE", "native").lower() == "desktop":
            profile_proxy = DesktopProfileProxy(
                run_env.get("CLOUD_CODE_URL", "https://daily-cloudcode-pa.googleapis.com"),
                run_env.get("AGY_DESKTOP_VERSION", "2.22.0"), AGY_TIMEOUT,
            )
            run_env["CLOUD_CODE_URL"] = profile_proxy.url
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=target_cwd,
            env=run_env,
        )
    except Exception:
        if update_lease:
            update_lease.close()
        if profile_proxy:
            await asyncio.to_thread(profile_proxy.close)
        log_dir.cleanup()
        yield {"event": "metadata", "route_info": route_info}
        raise
    except BaseException:
        if update_lease:
            update_lease.close()
        if profile_proxy:
            await asyncio.to_thread(profile_proxy.close)
        log_dir.cleanup()
        raise

    assert proc.stdout is not None
    assert proc.stderr is not None
    buffered_line = ""
    stderr_tail = bytearray()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    deadline = asyncio.get_running_loop().time() + AGY_TIMEOUT if AGY_TIMEOUT > 0 else None

    async def _drain_stderr() -> None:
        # Must keep reading stderr concurrently with stdout — if the child fills the
        # pipe buffer and no one reads it, the child blocks on write() and the whole
        # process (including stdout) deadlocks.
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            stderr_tail.extend(chunk)
            del stderr_tail[:-4000]

    stderr_task = asyncio.create_task(_drain_stderr())

    async def _reap_process() -> None:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=1)
        except (asyncio.TimeoutError, ProcessLookupError):
            logger.warning("Timed out reaping agy process")

    active_cid, active_cid_ambiguous, observed_steps, response_steps = None, False, set(), set()
    coverage_unknown, current_response_seen = False, False
    pending_result_errors = []

    def _enrich_event(ev: dict) -> dict | None:
        nonlocal active_cid, active_cid_ambiguous, coverage_unknown, current_response_seen
        if ev.pop("_result_error", False):
            pending_result_errors.append(ev)
            return None
        cli_cid = ev.pop("_cli_init_cid", None)
        if isinstance(cli_cid, str):
            if active_cid is not None and active_cid != cli_cid:
                active_cid_ambiguous = True
            else:
                active_cid = cli_cid
        step_index, step_type = ev.pop("_step_index", None), ev.pop("_step_type", None)
        if (isinstance(step_index, int) and not isinstance(step_index, bool) and 0 <= step_index < (1 << 31)
                and isinstance(step_type, str) and step_type in ("agent_response", "tool", "tool_call")):
            observed_steps.add(step_index)
            if step_type == "agent_response":
                response_steps.add(step_index)
        elif step_type == "agent_response":
            coverage_unknown = True
        _merge_response_evidence(route_info, ev.pop("_response_evidence", []))
        ev_type = ev.get("event")
        if ev_type == "delta" and step_type == "agent_response" and str(ev.get("delta", "")).strip():
            current_response_seen = True
        if ev_type == "result":
            _merge_response_evidence(route_info, _request_log_evidence(log_path))
        if ev_type in ("init", "result", "metadata"):
            _update_route_coverage(route_info, response_steps, coverage_unknown)
            ev["route_info"] = json.loads(json.dumps(route_info))
        return ev

    stream_error, native_thinking = None, []
    try:
        while True:
            if deadline is None:
                chunk = await proc.stdout.read(1024)
            else:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                chunk = await asyncio.wait_for(proc.stdout.read(1024), timeout=remaining)
            if not chunk:
                break
            text = decoder.decode(chunk)

            if AGY_OUTPUT_FORMAT.strip().lower() == "stream-json":
                buffered_line += text
                lines = buffered_line.split("\n")
                buffered_line = lines.pop()
                for line in lines:
                    parsed_events = _parse_ndjson_line(line)
                    for ev in parsed_events:
                        enriched = _enrich_event(ev)
                        if enriched is None:
                            continue
                        if persist_conversation and enriched.get("event") == "init" and enriched.get("conversation_id") and AGY_PERSIST_CONVERSATIONS:
                            await save_conversation_async(user_id, enriched["conversation_id"])
                        yield enriched
            else:
                yield {"event": "delta", "delta": text}

        text = decoder.decode(b"", final=True)
        if text:
            buffered_line += text

        if buffered_line and buffered_line.strip():
            if AGY_OUTPUT_FORMAT.strip().lower() == "stream-json":
                parsed_events = _parse_ndjson_line(buffered_line)
                for ev in parsed_events:
                    enriched = _enrich_event(ev)
                    if enriched is None:
                        continue
                    if persist_conversation and enriched.get("event") == "init" and enriched.get("conversation_id") and AGY_PERSIST_CONVERSATIONS:
                        await save_conversation_async(user_id, enriched["conversation_id"])
                    yield enriched
            else:
                yield {"event": "delta", "delta": buffered_line}

        if proc.returncode is None:
            if deadline is None:
                await proc.wait()
            else:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                await asyncio.wait_for(proc.wait(), timeout=remaining)
        if proc.returncode not in (0, None):
            err_txt = stderr_tail.decode(errors="replace").strip()
            logger.warning(f"agy exited {proc.returncode}: {err_txt}")
            yield {"event": "error", "error": err_txt or f"antigravity-chat: agy exited with code {proc.returncode}"}

    except asyncio.TimeoutError:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        err_txt = stderr_tail.decode(errors="replace").strip()
        detail = f" — {err_txt}" if err_txt else ""
        yield {"event": "error", "error": f"antigravity-chat: timed out after {AGY_TIMEOUT}s{detail}"}
    except Exception as exc:
        stream_error = exc
    finally:
        stderr_task.cancel()
        try:
            await stderr_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await _reap_process()
            _merge_response_evidence(route_info, _request_log_evidence(log_path))
            if run_env.get("HOME") and active_cid and not active_cid_ambiguous and observed_steps:
                try:
                    records = await asyncio.to_thread(read_session_models, run_env["HOME"], active_cid, observed_steps)
                    _merge_response_evidence(route_info, records)
                except Exception:
                    logger.warning("Could not read agy response metadata")
                if response_steps:
                    try:
                        native_thinking = await asyncio.to_thread(read_session_thinking, run_env["HOME"],
                                                                  active_cid, response_steps, observed_steps)
                    except Exception:
                        logger.warning("Could not read agy thinking metadata")
        finally:
            try:
                if profile_proxy:
                    await asyncio.to_thread(profile_proxy.close)
            finally:
                if update_lease:
                    update_lease.close()
                log_dir.cleanup()
    for event in pending_result_errors:
        historical = None
        if (stream_error is None and proc.returncode == 0 and current_response_seen
                and run_env.get("HOME") and active_cid and not active_cid_ambiguous and response_steps):
            try:
                historical = await asyncio.to_thread(read_session_historical_error, run_env["HOME"],
                                                    active_cid, observed_steps, response_steps, event["error"])
            except Exception:
                logger.warning("Could not verify aggregate error scope")
        if historical:
            yield {"event": "notice", "scope": "historical", "step_index": historical["step_index"],
                   "detail": historical["error"]}
        else:
            yield event
    for event in native_thinking:
        yield event
    _update_route_coverage(route_info, response_steps, coverage_unknown)
    yield {"event": "metadata", "route_info": json.loads(json.dumps(route_info))}
    if stream_error is not None:
        raise stream_error


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "version": project_updater.running_version,
        "backend": "agy",
        "upstream_profile": os.getenv("AGY_UPSTREAM_PROFILE", "native"),
        "sandbox": AGY_SANDBOX,
        "auth": _AUTH_ENABLED,
        "output_format": AGY_OUTPUT_FORMAT,
        "persist_conversations": AGY_PERSIST_CONVERSATIONS,
    }


@app.get("/api/fs")
async def fs(path: str | None = None) -> dict:
    return browse_fs(path)


@app.get("/api/conversations")
async def conversations(user_id: str = "default") -> dict:
    uid = _sanitize_token(user_id, "default") or "default"
    items, active_id = await list_user_conversations_async(uid)
    return {"user_id": uid, "conversations": items, "active_id": active_id,
            "conversation_id": active_id}


@app.delete("/api/conversations/{cid}")
async def delete_conversation_ep(cid: str, user_id: str = "default") -> dict:
    uid = _sanitize_token(user_id, "default") or "default"
    ok = await delete_user_conversation_async(uid, cid)
    if not ok:
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"status": "deleted", "conversation_id": cid}


@app.post("/api/conversations/{cid}/activate")
async def activate_conversation_ep(cid: str, user_id: str = "default") -> dict:
    uid = _sanitize_token(user_id, "default") or "default"
    ok = await set_active_conversation_async(uid, cid)
    if not ok:
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"status": "activated", "conversation_id": cid}


class RenameRequest(BaseModel):
    title: str


@app.patch("/api/conversations/{cid}")
async def rename_conversation_ep(cid: str, user_id: str = "default", req: RenameRequest | None = None) -> dict:
    uid = _sanitize_token(user_id, "default") or "default"
    title = (req.title if req else "")
    ok = await rename_conversation_async(uid, cid, title)
    if not ok:
        raise HTTPException(status_code=404, detail="conversation not found or bad title")
    return {"status": "renamed", "conversation_id": cid, "title": title.strip()[:80]}


class PinRequest(BaseModel):
    pinned: StrictBool


@app.patch("/api/conversations/{cid}/pin")
async def pin_conversation_ep(cid: str, req: PinRequest, user_id: str = "default") -> dict:
    uid = _sanitize_token(user_id)
    clean_cid = _sanitize_token(cid)
    ok = await set_conversation_pinned_async(uid, clean_cid, req.pinned)
    if not ok:
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"status": "pinned" if req.pinned else "unpinned",
            "conversation_id": clean_cid, "pinned": req.pinned}


@app.delete("/api/conversations")
async def delete_conversation(user_id: str = "default") -> dict:
    uid = _sanitize_token(user_id, "default") or "default"
    await clear_conversation_async(uid)
    return {"status": "cleared", "user_id": uid}


class SaveMessagesRequest(BaseModel):
    messages: list[dict]


@app.get("/api/conversations/{cid}/messages")
async def get_conversation_messages_ep(cid: str) -> dict:
    clean_cid = _sanitize_token(cid)
    if not clean_cid:
        raise HTTPException(status_code=400, detail="Invalid conversation ID")

    msg_file = MESSAGES_DIR / f"{clean_cid}.json"
    if msg_file.exists():
        try:
            msgs = json.loads(msg_file.read_text())
            return {"conversation_id": clean_cid, "messages": msgs}
        except Exception as exc:
            logger.warning(f"Failed to read messages file {msg_file}: {exc}")

    # Fall back to transcript
    brain_log = Path(HOME) / ".gemini" / "antigravity-cli" / "brain" / clean_cid / ".system_generated" / "logs" / "transcript.jsonl"

    if brain_log.exists():
        parsed = parse_transcript_to_messages(brain_log)
        if parsed:
            try:
                msg_file.write_text(json.dumps(parsed, ensure_ascii=False, indent=2))
            except Exception:
                pass
            return {"conversation_id": clean_cid, "messages": parsed}

    return {"conversation_id": clean_cid, "messages": []}


@app.post("/api/conversations/{cid}/messages")
async def save_conversation_messages_ep(cid: str, req: SaveMessagesRequest) -> dict:
    clean_cid = _sanitize_token(cid)
    if not clean_cid:
        raise HTTPException(status_code=400, detail="Invalid conversation ID")

    msg_file = MESSAGES_DIR / f"{clean_cid}.json"
    tmp_file = msg_file.with_suffix(".tmp")
    try:
        tmp_file.write_text(json.dumps(req.messages, ensure_ascii=False, indent=2))
        tmp_file.replace(msg_file)
    except Exception as exc:
        logger.warning(f"Failed to save messages for {clean_cid}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "saved", "conversation_id": clean_cid, "count": len(req.messages)}


@app.get("/api/models")
async def models() -> dict:
    global _models_cache
    now = time.monotonic()
    if _models_cache and now - _models_cache[0] < _MODELS_TTL:
        return {"models": _models_cache[1]}
    
    max_retries = 3
    base_delay = 0.5
    names: list[dict] = []

    for attempt in range(max_retries):
        try:
            proc = await asyncio.create_subprocess_exec(
                AGY_BIN, "models",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_agy_environment(),
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            names = []
            for ln in out.decode().splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                model_id, _, label = ln.partition("\t")
                names.append({"id": model_id.strip(), "name": label.strip() or model_id.strip()})
            if names:
                break
        except Exception:
            names = []

        if attempt < max_retries - 1:
            await asyncio.sleep(base_delay * (2 ** attempt))

    if names:
        _models_cache = (now, names)
        for m in names:
            _models_label_map[m["id"]] = m["name"]
    return {"models": names}


# --------------------------------------------------------------------------- #
# Quota & Multi-Account Management
# --------------------------------------------------------------------------- #

class SwitchAccountRequest(BaseModel):
    id: str


class AddAccountRequest(BaseModel):
    token: str | None = None
    token_json: str | None = None
    alias: str | None = None
    switch_now: StrictBool = False


_quota_cache: dict[str, Any] = {
    "data": None,
    "timestamp": 0.0,
}
_quota_lock = asyncio.Lock()


def _decode_id_token(id_token: str | None) -> dict:
    if not id_token or not isinstance(id_token, str):
        return {}
    try:
        parts = id_token.split(".")
        if len(parts) >= 2:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            return json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))
    except Exception as exc:
        logger.debug("Failed to decode id_token JWT: %s", exc)
    return {}


def _get_token_account_info(token_data: dict) -> dict:
    id_token = token_data.get("id_token")
    claims = _decode_id_token(id_token) if id_token else {}
    email = claims.get("email") or token_data.get("email") or "unknown"
    name = claims.get("name") or token_data.get("name") or email
    exp = claims.get("exp")
    return {
        "email": email,
        "name": name,
        "exp": exp,
        "auth_method": token_data.get("auth_method", "oauth"),
    }


def _sync_active_account_to_storage() -> None:
    """Ensure the currently active OAuth token is saved in ACCOUNTS_DIR."""
    if not ACTIVE_TOKEN_FILE.exists():
        return
    try:
        content = ACTIVE_TOKEN_FILE.read_text()
        data = json.loads(content)
        active_token = data.get("token")
        info = _get_token_account_info(data)
        email = info.get("email")

        # Check if an account file already exists with this token or email
        if ACCOUNTS_DIR.exists():
            for f in ACCOUNTS_DIR.glob("*.json"):
                try:
                    f_data = json.loads(f.read_text())
                    if active_token and f_data.get("token") == active_token:
                        return
                    if email and email != "unknown" and _get_token_account_info(f_data).get("email") == email:
                        return
                except Exception:
                    pass

        if not email or email == "unknown":
            email = "default_account"
        safe_name = "".join(c for c in email if c.isalnum() or c in ("@", ".", "_", "-"))
        account_file = ACCOUNTS_DIR / f"{safe_name}.json"
        if not account_file.exists():
            account_file.write_text(json.dumps(data, indent=2) + "\n")
            account_file.chmod(0o600)
    except Exception as exc:
        logger.warning("Failed to auto-backup active account to %s: %s", ACCOUNTS_DIR, exc)


def _list_stored_accounts() -> list[dict]:
    _sync_active_account_to_storage()
    active_token_val = ""
    active_email = ""
    if ACTIVE_TOKEN_FILE.exists():
        try:
            active_data = json.loads(ACTIVE_TOKEN_FILE.read_text())
            active_token_val = active_data.get("token", "")
            active_email = _get_token_account_info(active_data).get("email", "")
        except Exception:
            pass

    accounts: list[dict] = []
    if ACCOUNTS_DIR.exists():
        for f in sorted(ACCOUNTS_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                info = _get_token_account_info(data)
                acc_token = data.get("token", "")
                is_active = False
                if active_token_val and acc_token and active_token_val == acc_token:
                    is_active = True
                elif active_email and info["email"] and active_email == info["email"]:
                    is_active = True
                accounts.append({
                    "id": f.stem,
                    "filename": f.name,
                    "email": info["email"],
                    "name": info["name"],
                    "exp": info.get("exp"),
                    "active": is_active,
                    "updated_at": int(f.stat().st_mtime),
                })
            except Exception as exc:
                logger.warning("Failed to parse account file %s: %s", f, exc)

    accounts.sort(key=lambda a: (not a["active"], a["email"]))
    return accounts


def _clear_quota_cache() -> None:
    _quota_cache["data"] = None
    _quota_cache["timestamp"] = 0.0


def _switch_to_account(account_id: str) -> dict:
    target_file = ACCOUNTS_DIR / f"{account_id}.json"
    if not target_file.exists():
        target_file = ACCOUNTS_DIR / account_id
    if not target_file.exists():
        for f in ACCOUNTS_DIR.glob("*.json"):
            if f.stem == account_id or f.name == account_id:
                target_file = f
                break
            try:
                d = json.loads(f.read_text())
                if _get_token_account_info(d).get("email") == account_id:
                    target_file = f
                    break
            except Exception:
                pass

    if not target_file.exists():
        raise HTTPException(status_code=404, detail=f"账号 '{account_id}' 不存在")

    content = target_file.read_text()
    data = json.loads(content)
    info = _get_token_account_info(data)

    ACTIVE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = ACTIVE_TOKEN_FILE.with_suffix(".tmp")
    tmp_file.write_text(content)
    tmp_file.chmod(0o600)
    tmp_file.replace(ACTIVE_TOKEN_FILE)

    # Sync runtime isolate token if configured
    runtime_home = Path(os.getenv("AGY_RUNTIME_HOME", str(SCRATCH / ".agy-home"))).expanduser().resolve()
    runtime_token = runtime_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    if runtime_token.exists() and not runtime_token.is_symlink():
        try:
            runtime_token.unlink()
            runtime_token.symlink_to(ACTIVE_TOKEN_FILE)
        except Exception:
            pass

    _clear_quota_cache()

    return {
        "ok": True,
        "message": f"成功切换到账号 {info.get('email', account_id)}",
        "active_account": info.get("email", account_id),
    }


def _save_new_account(token_data: dict | str, alias: str | None = None, switch_now: bool = False) -> dict:
    if isinstance(token_data, str):
        token_str = token_data.strip()
        try:
            data = json.loads(token_str)
        except Exception:
            data = {"token": token_str, "auth_method": "oauth"}
    elif isinstance(token_data, dict):
        data = token_data
    else:
        raise HTTPException(status_code=400, detail="无效的 Token 数据格式")

    if not isinstance(data, dict) or not data.get("token"):
        raise HTTPException(status_code=400, detail="Token 数据中缺少必要的 'token' 字段")

    info = _get_token_account_info(data)
    identifier = alias.strip() if alias and alias.strip() else info.get("email")
    if not identifier or identifier == "unknown":
        identifier = f"account_{uuid.uuid4().hex[:8]}"

    safe_name = "".join(c for c in identifier if c.isalnum() or c in ("@", ".", "_", "-"))
    if not safe_name:
        safe_name = f"account_{uuid.uuid4().hex[:8]}"

    target_file = ACCOUNTS_DIR / f"{safe_name}.json"
    target_file.write_text(json.dumps(data, indent=2) + "\n")
    target_file.chmod(0o600)

    if switch_now:
        _switch_to_account(safe_name)

    return {
        "ok": True,
        "id": safe_name,
        "email": info.get("email"),
        "name": info.get("name"),
        "switched": switch_now,
    }


def _delete_stored_account(account_id: str) -> dict:
    target_file = ACCOUNTS_DIR / f"{account_id}.json"
    if not target_file.exists():
        target_file = ACCOUNTS_DIR / account_id
    if not target_file.exists():
        raise HTTPException(status_code=404, detail="账号不存在")

    if ACTIVE_TOKEN_FILE.exists():
        try:
            active_data = json.loads(ACTIVE_TOKEN_FILE.read_text())
            target_data = json.loads(target_file.read_text())
            if active_data.get("token") and active_data.get("token") == target_data.get("token"):
                raise HTTPException(status_code=400, detail="无法删除当前正在使用的账号，请先切换到其他账号")
        except HTTPException:
            raise
        except Exception:
            pass

    target_file.unlink(missing_ok=True)
    return {"ok": True, "message": "账号已删除"}


def _parse_usage_output(usage_text: str) -> list[dict]:
    pattern = re.compile(
        r"^\s*(.+?)(?:\t+|\s{2,})((?:Weekly|Five Hour|Daily)\s+Limit\s+Remaining)(?:\t+|\s+)(\d+)%(?:\t+|\s+)(.+)$",
        re.M,
    )
    items: list[dict] = []
    for match in pattern.finditer(usage_text):
        m_name, l_type, pct_str, resets_at = match.groups()
        m_name = m_name.strip()
        l_type = l_type.strip()
        pct = int(pct_str)
        resets_at = resets_at.strip()

        is_5h = "Five Hour" in l_type
        is_weekly = "Weekly" in l_type
        is_daily = "Daily" in l_type

        type_code = "five_hour" if is_5h else ("weekly" if is_weekly else "daily")
        type_label = "5小时滑动额度 (消失额度)" if is_5h else ("周额度" if is_weekly else "每日额度")

        items.append({
            "model": m_name,
            "type": type_code,
            "type_name": l_type,
            "label": type_label,
            "percentage": pct,
            "resets_at": resets_at,
        })
    return items


def _parse_credits_output(credits_text: str) -> int:
    m = re.search(r"Remaining credits\s+(\d+)", credits_text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return 0


async def _fetch_quota_from_agy(force: bool = False) -> dict:
    async with _quota_lock:
        now = time.time()
        if not force and _quota_cache["data"] and (now - _quota_cache["timestamp"] < 45.0):
            res = dict(_quota_cache["data"])
            res["cached"] = True
            return res

        try:
            proc_usage = await asyncio.create_subprocess_exec(
                AGY_BIN, "-p=/usage",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(SCRATCH),
                env=_agy_environment(),
            )
            proc_credits = await asyncio.create_subprocess_exec(
                AGY_BIN, "-p=/credits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(SCRATCH),
                env=_agy_environment(),
            )

            out_usage, _ = await asyncio.wait_for(proc_usage.communicate(), timeout=25.0)
            out_credits, _ = await asyncio.wait_for(proc_credits.communicate(), timeout=25.0)
        except asyncio.TimeoutError:
            logger.warning("agy usage/credits timed out")
            raise HTTPException(status_code=504, detail="获取额度超时，请稍后刷新重试")
        except Exception as exc:
            logger.error("Error executing agy usage: %s", exc)
            raise HTTPException(status_code=500, detail=f"获取额度失败: {exc}")

        usage_text = out_usage.decode("utf-8", errors="replace")
        credits_text = out_credits.decode("utf-8", errors="replace")

        quotas = _parse_usage_output(usage_text)
        credits_val = _parse_credits_output(credits_text)

        current_email = "未知账号"
        if ACTIVE_TOKEN_FILE.exists():
            try:
                info = _get_token_account_info(json.loads(ACTIVE_TOKEN_FILE.read_text()))
                current_email = info.get("email") or current_email
            except Exception:
                pass

        data = {
            "ok": True,
            "email": current_email,
            "quotas": quotas,
            "credits": credits_val,
            "timestamp": now,
        }

        _quota_cache["data"] = data
        _quota_cache["timestamp"] = now
        res = dict(data)
        res["cached"] = False
        return res


@app.get("/api/quota")
async def get_quota_ep(force: bool = False) -> dict:
    return await _fetch_quota_from_agy(force=force)


@app.get("/api/accounts")
async def list_accounts_ep() -> dict:
    accs = _list_stored_accounts()
    active_email = next((a["email"] for a in accs if a["active"]), None)
    return {"ok": True, "active_account": active_email, "accounts": accs}


@app.post("/api/accounts/switch")
async def switch_account_ep(req: SwitchAccountRequest) -> dict:
    return _switch_to_account(req.id)


@app.post("/api/accounts/add")
async def add_account_ep(req: AddAccountRequest) -> dict:
    raw = req.token_json or req.token or ""
    return _save_new_account(raw, alias=req.alias, switch_now=req.switch_now)


@app.delete("/api/accounts/{account_id}")
async def delete_account_ep(account_id: str) -> dict:
    return _delete_stored_account(account_id)


_SSE_HEARTBEAT_SECONDS = 15


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """SSE stream of the assistant reply for the custom UI."""
    if not req.messages:
        raise HTTPException(status_code=422, detail="messages must be non-empty")
    
    prompt, cid, continue_last, user_id = await _resolve_prompt_and_session(req)

    async def event_gen():
        stream = _stream_agy(
            prompt=prompt, model=req.model, cwd=req.cwd, conversation_id=cid,
            continue_last=continue_last, user_id=user_id,
        )
        pending = None
        try:
            yield f"data: {json.dumps({'event': 'start'})}\n\n"
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(stream))
                # A heartbeat must not cancel the CLI read or start another turn.
                ready, _ = await asyncio.wait({pending}, timeout=_SSE_HEARTBEAT_SECONDS)
                if not ready:
                    yield ": keep-alive\n\n"
                    continue
                finished, pending = pending, None
                try:
                    ev = finished.result()
                except StopAsyncIteration:
                    break
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Chat stream failed (%s)", type(exc).__name__)
            yield f"data: {json.dumps({'event': 'error', 'error': str(exc)})}\n\n"
        finally:
            from anyio import CancelScope
            # The HTTP disconnect scope must not interrupt AGY's awaited cleanup.
            with CancelScope(shield=True):
                if pending is not None:
                    pending.cancel()
                    try:
                        await pending
                    except (asyncio.CancelledError, Exception):
                        pass
                await stream.aclose()
        yield f"data: {json.dumps({'event': 'done'})}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --- Bonus: OpenAI-compatible endpoint so other tools can reuse this too ----- #

@app.post("/v1/chat/completions")
async def openai_chat(req: ChatRequest) -> JSONResponse:
    if not req.messages:
        raise HTTPException(status_code=422, detail="messages must be non-empty")
    
    prompt, cid, continue_last, user_id = await _resolve_prompt_and_session(req)

    parts: list[str] = []
    error_msg: str | None = None
    response_metadata = _new_route_info(requested_model=req.model)
    async for ev in _stream_agy(
        prompt=prompt,
        model=req.model,
        cwd=req.cwd,
        conversation_id=cid,
        continue_last=continue_last,
        user_id=user_id,
    ):
        if isinstance(ev.get("route_info"), dict):
            response_metadata = ev["route_info"]
        if ev.get("event") == "delta" and ev.get("delta"):
            parts.append(ev["delta"])
        elif ev.get("event") == "result" and not parts and ev.get("response"):
            parts.append(ev["response"])
        elif ev.get("event") == "error" and ev.get("error"):
            error_msg = ev["error"]

    if error_msg:
        raise HTTPException(status_code=500, detail=error_msg)

    content = "".join(parts).strip()
    return JSONResponse({
        "id": f"agy-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model or "agy",
        "requested_model": req.model,
        "response_metadata": response_metadata,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
    })


@app.get("/v1/models")
async def openai_models() -> JSONResponse:
    m = await models()
    now = int(time.time())
    return JSONResponse({
        "object": "list",
        "data": [{"id": n["id"], "object": "model", "created": now, "owned_by": "antigravity"}
                 for n in m["models"]] or [{"id": "agy", "object": "model",
                                             "created": now, "owned_by": "antigravity"}],
    })


def main() -> None:
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)


if __name__ == "__main__":
    main()
