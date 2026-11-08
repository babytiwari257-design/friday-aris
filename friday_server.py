import os
import json
import uuid
import sqlite3
import hashlib
import datetime
from typing import Dict, List, Optional, Any
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

load_dotenv()

from friday_brain import ask_friday

app = FastAPI(title="F.R.I.D.A.Y. // Social AI Core", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
DB_PATH = os.path.join(BASE_DIR, "friday_social.db")
os.makedirs(WEB_DIR, exist_ok=True)


# ==========================================
# DATABASE INITIALIZATION (SQLite)
# ==========================================
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            bio TEXT DEFAULT 'Available on F.R.I.D.A.Y.',
            avatar_color TEXT DEFAULT '#f59e0b',
            last_seen TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            chat_key TEXT NOT NULL,
            sender TEXT NOT NULL,
            receiver TEXT NOT NULL,
            content TEXT NOT NULL,
            reply_to TEXT DEFAULT '',
            reaction TEXT DEFAULT '',
            status TEXT DEFAULT 'sent',
            is_ai INTEGER DEFAULT 0,
            timestamp TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stories (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            display_name TEXT NOT NULL,
            text_content TEXT NOT NULL,
            bg_gradient TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    # Seed F.R.I.D.A.Y. Official AI Account
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cur.execute("""
        INSERT OR IGNORE INTO users (username, display_name, password_hash, bio, avatar_color, last_seen, created_at)
        VALUES ('friday', 'F.R.I.D.A.Y. AI', 'SYSTEM_CORE', 'Aris Industries Flagship Social AI', '#06b6d4', ?, ?)
    """, (now_iso, now_iso))
    conn.commit()
    conn.close()


init_db()


# ==========================================
# HELPERS & SCHEMAS
# ==========================================
def hash_password(password: str) -> str:
    salt = "aris_friday_salt_2026"
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def make_chat_key(u1: str, u2: str) -> str:
    pair = sorted([u1.lower().strip(), u2.lower().strip()])
    return f"{pair[0]}__{pair[1]}"


class RegisterRequest(BaseModel):
    username: str
    display_name: str
    password: str
    bio: Optional[str] = "Hey there! Using F.R.I.D.A.Y."
    avatar_color: Optional[str] = "#8b5cf6"


class LoginRequest(BaseModel):
    username: str
    password: str


class StoryRequest(BaseModel):
    username: str
    text_content: str
    bg_gradient: Optional[str] = "linear-gradient(135deg, #6366f1, #ec4899)"


# ==========================================
# REAL-TIME WEBSOCKET CONNECTION MANAGER
# ==========================================
class ConnectionManager:
    def __init__(self):
        self.active: Dict[str, WebSocket] = {}

    async def connect(self, username: str, ws: WebSocket):
        await ws.accept()
        self.active[username] = ws
        self.update_last_seen(username)
        await self.broadcast_presence(username, True)

    def disconnect(self, username: str):
        if username in self.active:
            del self.active[username]
        self.update_last_seen(username)

    def update_last_seen(self, username: str):
        conn = get_db()
        conn.execute("UPDATE users SET last_seen = ? WHERE username = ?", (utc_now_iso(), username))
        conn.commit()
        conn.close()

    def is_online(self, username: str) -> bool:
        return username == "friday" or username in self.active

    async def send_to(self, username: str, payload: dict):
        ws = self.active.get(username)
        if ws:
            try:
                await ws.send_json(payload)
            except Exception:
                pass

    async def broadcast_presence(self, username: str, online: bool):
        conn = get_db()
        row = conn.execute("SELECT last_seen FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        last_seen = row["last_seen"] if row else utc_now_iso()
        event = {
            "type": "presence",
            "username": username,
            "online": online,
            "last_seen": last_seen,
        }
        for user, ws in list(self.active.items()):
            if user != username:
                try:
                    await ws.send_json(event)
                except Exception:
                    pass


manager = ConnectionManager()


# ==========================================
# FRONTEND & PWA ROUTES
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def serve_app():
    index_path = os.path.join(WEB_DIR, "index.html")
    if not os.path.exists(index_path):
        return HTMLResponse("<h1>F.R.I.D.A.Y. // Upload web/index.html</h1>", status_code=404)
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/manifest.json")
async def serve_manifest():
    return FileResponse(os.path.join(WEB_DIR, "manifest.json"), media_type="application/manifest+json")


@app.get("/api/health")
async def health():
    return {"status": "ONLINE", "system": "F.R.I.D.A.Y.", "active_users": len(manager.active)}


# ==========================================
# INSTAGRAM-STYLE AUTH & USER DISCOVERY
# ==========================================
@app.post("/api/register")
async def register(req: RegisterRequest):
    uname = req.username.lower().strip().lstrip("@")
    if not uname or len(uname) < 3 or not uname.isalnum():
        raise HTTPException(status_code=400, detail="Username must be 3+ alphanumeric characters.")
    if uname == "friday":
        raise HTTPException(status_code=400, detail="Username 'friday' is reserved.")
    if len(req.password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters.")

    conn = get_db()
    existing = conn.execute("SELECT username FROM users WHERE username = ?", (uname,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=409, detail="Username already taken. Choose another!")

    now_iso = utc_now_iso()
    conn.execute(
        """INSERT INTO users (username, display_name, password_hash, bio, avatar_color, last_seen, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (uname, req.display_name.strip() or uname, hash_password(req.password), req.bio, req.avatar_color, now_iso, now_iso),
    )
    # Welcome DM from F.R.I.D.A.Y.
    welcome_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO messages (id, chat_key, sender, receiver, content, status, is_ai, timestamp)
           VALUES (?, ?, 'friday', ?, ?, 'delivered', 1, ?)""",
        (
            welcome_id,
            make_chat_key("friday", uname),
            uname,
            f"Hey {req.display_name.strip() or uname}! 👋 Welcome to F.R.I.D.A.Y. — your AI-powered social hub. Search any @username to start a DM, post a 24h Story, or summon me inside any chat using @friday!",
            now_iso,
        ),
    )
    conn.commit()
    conn.close()
    return {
        "username": uname,
        "display_name": req.display_name.strip() or uname,
        "bio": req.bio,
        "avatar_color": req.avatar_color,
    }


@app.post("/api/login")
async def login(req: LoginRequest):
    uname = req.username.lower().strip().lstrip("@")
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (uname,)).fetchone()
    conn.close()
    if not user or user["password_hash"] != hash_password(req.password):
        raise HTTPException(status_code=401, detail="Invalid @username or password.")
    return {
        "username": user["username"],
        "display_name": user["display_name"],
        "bio": user["bio"],
        "avatar_color": user["avatar_color"],
    }


@app.get("/api/users/{current_user}")
async def list_users(current_user: str, q: str = ""):
    conn = get_db()
    query = f"%{q.lower().strip()}%"
    rows = conn.execute(
        """SELECT username, display_name, bio, avatar_color, last_seen FROM users
           WHERE username != ? AND (username LIKE ? OR LOWER(display_name) LIKE ?)""",
        (current_user.lower(), query, query),
    ).fetchall()
    conn.close()

    users_list = []
    for r in rows:
        u = dict(r)
        u["online"] = manager.is_online(u["username"])
        users_list.append(u)
    # Pin F.R.I.D.A.Y. at the top
    users_list.sort(key=lambda x: (0 if x["username"] == "friday" else 1, x["display_name"].lower()))
    return {"users": users_list}


@app.get("/api/messages/{u1}/{u2}")
async def get_messages(u1: str, u2: str):
    ckey = make_chat_key(u1, u2)
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM messages WHERE chat_key = ? ORDER BY timestamp ASC LIMIT 120",
        (ckey,),
    ).fetchall()
    conn.close()
    return {"messages": [dict(r) for r in rows]}


# ==========================================
# 24-HOUR STORIES / STATUS API
# ==========================================
@app.get("/api/stories")
async def get_stories():
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).isoformat()
    conn = get_db()
    conn.execute("DELETE FROM stories WHERE created_at < ?", (cutoff,))
    conn.commit()
    rows = conn.execute("SELECT * FROM stories ORDER BY created_at DESC").fetchall()
    conn.close()
    return {"stories": [dict(r) for r in rows]}


@app.post("/api/stories")
async def post_story(req: StoryRequest):
    conn = get_db()
    user = conn.execute("SELECT display_name FROM users WHERE username = ?", (req.username.lower(),)).fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found.")
    sid = str(uuid.uuid4())
    now_iso = utc_now_iso()
    conn.execute(
        "INSERT INTO stories (id, username, display_name, text_content, bg_gradient, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (sid, req.username.lower(), user["display_name"], req.text_content[:220], req.bg_gradient, now_iso),
    )
    conn.commit()
    conn.close()
    return {"status": "posted", "id": sid}


# ==========================================
# REAL-TIME WEBSOCKET ENGINE (DMs, SEEN, TYPING, REACTIONS, @FRIDAY)
# ==========================================
@app.websocket("/ws/{username}")
async def websocket_endpoint(ws: WebSocket, username: str):
    uname = username.lower().strip()
    await manager.connect(uname, ws)
    try:
        while True:
            data = await ws.receive_json()
            evt_type = data.get("type")

            # 1. Typing Indicator
            if evt_type == "typing":
                target = data.get("to", "").lower()
                await manager.send_to(target, {
                    "type": "typing",
                    "from": uname,
                    "is_typing": bool(data.get("is_typing")),
                })

            # 2. Blue Ticks / Seen Receipt
            elif evt_type == "seen":
                peer = data.get("peer", "").lower()
                ckey = make_chat_key(uname, peer)
                conn = get_db()
                conn.execute(
                    "UPDATE messages SET status = 'seen' WHERE chat_key = ? AND receiver = ? AND status != 'seen'",
                    (ckey, uname),
                )
                conn.commit()
                conn.close()
                await manager.send_to(peer, {"type": "seen_ack", "by": uname})

            # 3. Instagram Heart / Emoji Reaction
            elif evt_type == "react":
                msg_id = data.get("msg_id")
                emoji = data.get("emoji", "❤️")
                peer = data.get("peer", "").lower()
                conn = get_db()
                conn.execute("UPDATE messages SET reaction = ? WHERE id = ?", (emoji, msg_id))
                conn.commit()
                conn.close()
                payload = {"type": "reaction", "msg_id": msg_id, "emoji": emoji, "by": uname}
                await manager.send_to(uname, payload)
                await manager.send_to(peer, payload)

            # 4. Direct Message + @FRIDAY Co-Pilot
            elif evt_type == "message":
                target = data.get("to", "").lower().strip()
                content = data.get("content", "").strip()
                reply_to = data.get("reply_to", "").strip()
                if not target or not content:
                    continue

                ckey = make_chat_key(uname, target)
                msg_id = str(uuid.uuid4())
                now_iso = utc_now_iso()
                status = "delivered" if manager.is_online(target) else "sent"
                if target == "friday":
                    status = "seen"

                conn = get_db()
                conn.execute(
                    """INSERT INTO messages (id, chat_key, sender, receiver, content, reply_to, status, is_ai, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                    (msg_id, ckey, uname, target, content, reply_to, status, now_iso),
                )
                conn.commit()

                msg_obj = {
                    "type": "new_message",
                    "id": msg_id,
                    "chat_key": ckey,
                    "sender": uname,
                    "receiver": target,
                    "content": content,
                    "reply_to": reply_to,
                    "reaction": "",
                    "status": status,
                    "is_ai": 0,
                    "timestamp": now_iso,
                }
                await manager.send_to(uname, msg_obj)
                if target != "friday":
                    await manager.send_to(target, msg_obj)

                # Trigger F.R.I.D.A.Y. AI Brain (either 1-on-1 DM or @friday mention)
                if target == "friday" or "@friday" in content.lower():
                    await manager.send_to(uname, {"type": "typing", "from": "friday", "is_typing": True})
                    if target != "friday":
                        await manager.send_to(target, {"type": "typing", "from": "friday", "is_typing": True})

                    hist_rows = conn.execute(
                        "SELECT sender, content FROM messages WHERE chat_key = ? ORDER BY timestamp DESC LIMIT 10",
                        (ckey,),
                    ).fetchall()
                    history = [
                        {"role": "assistant" if r["sender"] == "friday" else "user", "content": r["content"]}
                        for r in reversed(hist_rows)
                    ]

                    ai_res = ask_friday(
                        prompt=content,
                        sender_name=uname,
                        chat_context=history,
                        is_group_mention=(target != "friday"),
                    )

                    ai_id = str(uuid.uuid4())
                    ai_time = utc_now_iso()
                    ai_sender = "friday"
                    ai_receiver = uname if target == "friday" else target

                    conn.execute(
                        """INSERT INTO messages (id, chat_key, sender, receiver, content, reply_to, status, is_ai, timestamp)
                           VALUES (?, ?, ?, ?, ?, ?, 'delivered', 1, ?)""",
                        (ai_id, ckey, ai_sender, ai_receiver, ai_res["reply"], content[:80], ai_time),
                    )
                    conn.commit()

                    ai_msg = {
                        "type": "new_message",
                        "id": ai_id,
                        "chat_key": ckey,
                        "sender": ai_sender,
                        "receiver": ai_receiver,
                        "content": ai_res["reply"],
                        "reply_to": content[:80],
                        "reaction": "",
                        "status": "delivered",
                        "is_ai": 1,
                        "timestamp": ai_time,
                    }
                    await manager.send_to(uname, {"type": "typing", "from": "friday", "is_typing": False})
                    await manager.send_to(uname, ai_msg)
                    if target != "friday":
                        await manager.send_to(target, {"type": "typing", "from": "friday", "is_typing": False})
                        await manager.send_to(target, ai_msg)

                conn.close()

    except WebSocketDisconnect:
        manager.disconnect(uname)
        await manager.broadcast_presence(uname, False)
    except Exception:
        manager.disconnect(uname)
        await manager.broadcast_presence(uname, False)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("friday_server:app", host="0.0.0.0", port=port, reload=False)
