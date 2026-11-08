import os
import json
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel
from groq import Groq

load_dotenv()

app = FastAPI(title="F.R.I.D.A.Y. // Private Vault", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
DB_PATH = os.path.join(BASE_DIR, "friday_vault.db")
os.makedirs(WEB_DIR, exist_ok=True)

MASTER_CODE = os.environ.get("MASTER_PASSCODE", "MAYANK77")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

AI_MODELS = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            password TEXT NOT NULL,
            bio TEXT DEFAULT '',
            avatar_color TEXT DEFAULT '#ec4899',
            last_seen TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS stories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            text_content TEXT NOT NULL,
            bg_gradient TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS follows (
            follower TEXT NOT NULL,
            following TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            PRIMARY KEY (follower, following)
        )
    """)
    c.execute(
        "INSERT OR IGNORE INTO users (username, display_name, password, bio, avatar_color, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
        ("friday", "F.R.I.D.A.Y. AI", "SYSTEM_BOT", "Private Neural Co-Pilot", "#db2777", now_iso())
    )
    conn.commit()
    conn.close()


init_db()


class AuthPayload(BaseModel):
    master_code: str
    username: str
    password: str
    display_name: Optional[str] = ""
    bio: Optional[str] = ""
    avatar_color: Optional[str] = "#ec4899"


class StoryPayload(BaseModel):
    username: str
    text_content: str
    bg_gradient: str = "linear-gradient(135deg, #be185d, #ec4899)"


class FollowAction(BaseModel):
    follower: str
    following: str


class ConnectionManager:
    def __init__(self):
        self.active: Dict[str, List[WebSocket]] = {}

    async def connect(self, username: str, ws: WebSocket):
        await ws.accept()
        self.active.setdefault(username, []).append(ws)
        self.update_last_seen(username)
        await self.broadcast_presence(username, True)

    def disconnect(self, username: str, ws: WebSocket):
        if username in self.active and ws in self.active[username]:
            self.active[username].remove(ws)
            if not self.active[username]:
                del self.active[username]
        self.update_last_seen(username)

    def is_online(self, username: str) -> bool:
        return username == "friday" or (username in self.active and len(self.active[username]) > 0)

    def update_last_seen(self, username: str):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE users SET last_seen = ? WHERE username = ?", (now_iso(), username))
        conn.commit()
        conn.close()

    async def send_to(self, username: str, payload: dict):
        for ws in list(self.active.get(username, [])):
            try:
                await ws.send_json(payload)
            except Exception:
                pass

    async def broadcast_presence(self, username: str, online: bool):
        msg = {"type": "presence", "username": username, "online": online, "last_seen": now_iso()}
        for user_sockets in list(self.active.values()):
            for ws in list(user_sockets):
                try:
                    await ws.send_json(msg)
                except Exception:
                    pass


manager = ConnectionManager()


def can_chat(u1: str, u2: str) -> bool:
    if u1 == "friday" or u2 == "friday" or u1 == u2:
        return True
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT 1 FROM follows WHERE ((follower=? AND following=?) OR (follower=? AND following=?)) AND status='accepted'",
        (u1, u2, u2, u1)
    )
    ok = c.fetchone() is not None
    conn.close()
    return ok


def get_follow_state(me: str, peer: str) -> str:
    if peer == "friday" or me == peer:
        return "accepted"
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Check if already accepted in either direction
    c.execute(
        "SELECT 1 FROM follows WHERE ((follower=? AND following=?) OR (follower=? AND following=?)) AND status='accepted'",
        (me, peer, peer, me)
    )
    if c.fetchone():
        conn.close()
        return "accepted"
    # Check outgoing pending
    c.execute("SELECT status FROM follows WHERE follower=? AND following=?", (me, peer))
    row = c.fetchone()
    if row and row[0] == "pending":
        conn.close()
        return "outgoing_pending"
    # Check incoming pending
    c.execute("SELECT status FROM follows WHERE follower=? AND following=?", (peer, me))
    row2 = c.fetchone()
    if row2 and row2[0] == "pending":
        conn.close()
        return "incoming_pending"
    conn.close()
    return "none"


async def ask_friday_ai(prompt: str) -> str:
    if not client:
        return "F.R.I.D.A.Y. core offline: GROQ_API_KEY missing."
    for model in AI_MODELS:
        try:
            res = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are F.R.I.D.A.Y., an intelligent, loyal, concise personal AI assistant."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.65,
                max_tokens=900,
            )
            return res.choices[0].message.content.strip()
        except Exception:
            continue
    return "Neural link busy, try again in a moment."


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    path = os.path.join(WEB_DIR, "index.html")
    if not os.path.exists(path):
        return HTMLResponse("<h3>F.R.I.D.A.Y. UI Missing</h3>")
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/friday_logo.png")
async def serve_logo():
    p = os.path.join(WEB_DIR, "friday_logo.png")
    if os.path.exists(p):
        return FileResponse(p)
    raise HTTPException(status_code=404)


@app.get("/manifest.json")
async def serve_manifest():
    return JSONResponse({
        "name": "JEE Physics Study Portal",
        "short_name": "PhysicsNotes",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#030305",
        "theme_color": "#ec4899"
    })


@app.post("/api/register")
async def register(payload: AuthPayload):
    if payload.master_code != MASTER_CODE:
        raise HTTPException(status_code=403, detail="Invalid Secret Gatekeeper Code")
    uname = payload.username.strip().lower().lstrip("@")
    if not uname or len(uname) < 2:
        raise HTTPException(status_code=400, detail="Username too short")
    dname = payload.display_name.strip() or uname.capitalize()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO users (username, display_name, password, bio, avatar_color, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
            (uname, dname, payload.password, payload.bio or "", payload.avatar_color or "#ec4899", now_iso())
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=409, detail="Username already exists")
    conn.close()
    return {"username": uname, "display_name": dname, "bio": payload.bio or "", "avatar_color": "#ec4899"}


@app.post("/api/login")
async def login(payload: AuthPayload):
    if payload.master_code != MASTER_CODE:
        raise HTTPException(status_code=403, detail="Invalid Secret Gatekeeper Code")
    uname = payload.username.strip().lower().lstrip("@")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT username, display_name, password, bio, avatar_color FROM users WHERE username=?", (uname,))
    row = c.fetchone()
    conn.close()
    if not row or row[2] != payload.password:
        raise HTTPException(status_code=401, detail="Wrong username or password")
    return {"username": row[0], "display_name": row[1], "bio": row[3], "avatar_color": row[4]}


@app.get("/api/users/{me}")
async def list_users(me: str, q: str = ""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT username, display_name, bio, avatar_color, last_seen FROM users WHERE username != ?", (me,))
    rows = c.fetchall()
    conn.close()
    ql = q.strip().lower().lstrip("@")
    out = []
    for r in rows:
        if ql and ql not in r[0] and ql not in r[1].lower():
            continue
        f_state = get_follow_state(me, r[0])
        out.append({
            "username": r[0],
            "display_name": r[1],
            "bio": r[2],
            "avatar_color": r[3],
            "last_seen": r[4],
            "online": manager.is_online(r[0]),
            "follow_state": f_state
        })
    return {"users": out}


@app.post("/api/follow/request")
async def send_follow_request(act: FollowAction):
    if act.follower == act.following or act.following == "friday":
        return {"status": "accepted"}
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO follows (follower, following, status, created_at) VALUES (?, ?, 'pending', ?)",
        (act.follower, act.following, now_iso())
    )
    conn.commit()
    conn.close()
    await manager.send_to(act.following, {"type": "follow_update", "from": act.follower, "status": "incoming_pending"})
    return {"status": "outgoing_pending"}


@app.post("/api/follow/accept")
async def accept_follow_request(act: FollowAction):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE follows SET status='accepted' WHERE follower=? AND following=?",
        (act.follower, act.following)
    )
    conn.commit()
    conn.close()
    await manager.send_to(act.follower, {"type": "follow_update", "from": act.following, "status": "accepted"})
    await manager.send_to(act.following, {"type": "follow_update", "from": act.follower, "status": "accepted"})
    return {"status": "accepted"}


@app.post("/api/follow/reject")
async def reject_follow_request(act: FollowAction):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "DELETE FROM follows WHERE (follower=? AND following=?) OR (follower=? AND following=?)",
        (act.follower, act.following, act.following, act.follower)
    )
    conn.commit()
    conn.close()
    await manager.send_to(act.follower, {"type": "follow_update", "from": act.following, "status": "none"})
    await manager.send_to(act.following, {"type": "follow_update", "from": act.follower, "status": "none"})
    return {"status": "none"}


@app.get("/api/messages/{me}/{peer}")
async def get_messages(me: str, peer: str):
    f_state = get_follow_state(me, peer)
    if f_state != "accepted":
        return {"messages": [], "follow_state": f_state}
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, sender, receiver, content, reply_to, reaction, status, is_ai, timestamp
        FROM messages
        WHERE (sender=? AND receiver=?) OR (sender=? AND receiver=?)
        ORDER BY id ASC LIMIT 200
    """, (me, peer, peer, me))
    rows = c.fetchall()
    conn.close()
    msgs = [
        {
            "id": r[0], "sender": r[1], "receiver": r[2], "content": r[3],
            "reply_to": r[4], "reaction": r[5], "status": r[6], "is_ai": r[7], "timestamp": r[8]
        }
        for r in rows
    ]
    return {"messages": msgs, "follow_state": "accepted"}


@app.get("/api/stories")
async def get_stories():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT s.id, s.username, u.display_name, s.text_content, s.bg_gradient, s.created_at
        FROM stories s JOIN users u ON s.username = u.username
        ORDER BY s.id DESC LIMIT 30
    """)
    rows = c.fetchall()
    conn.close()
    return {
        "stories": [
            {"id": r[0], "username": r[1], "display_name": r[2], "text_content": r[3], "bg_gradient": r[4], "created_at": r[5]}
            for r in rows
        ]
    }


@app.post("/api/stories")
async def post_story(payload: StoryPayload):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO stories (username, text_content, bg_gradient, created_at) VALUES (?, ?, ?, ?)",
        (payload.username, payload.text_content, payload.bg_gradient, now_iso())
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.websocket("/ws/{username}")
async def websocket_endpoint(ws: WebSocket, username: str):
    await manager.connect(username, ws)
    try:
        while True:
            data = await ws.receive_json()
            mtype = data.get("type")

            if mtype == "ping":
                await ws.send_json({"type": "pong"})
                continue

            if mtype == "typing":
                peer = data.get("to")
                if peer and can_chat(username, peer):
                    await manager.send_to(peer, {"type": "typing", "from": username, "is_typing": data.get("is_typing", False)})

            elif mtype == "seen":
                peer = data.get("peer")
                if peer:
                    conn = sqlite3.connect(DB_PATH)
                    conn.execute("UPDATE messages SET status='seen' WHERE sender=? AND receiver=? AND status!='seen'", (peer, username))
                    conn.commit()
                    conn.close()
                    await manager.send_to(peer, {"type": "seen_ack", "by": username})

            elif mtype == "react":
                msg_id = data.get("msg_id")
                emoji = data.get("emoji", "")
                peer = data.get("peer")
                conn = sqlite3.connect(DB_PATH)
                conn.execute("UPDATE messages SET reaction=? WHERE id=?", (emoji, msg_id))
                conn.commit()
                conn.close()
                payload = {"type": "reaction", "msg_id": msg_id, "emoji": emoji}
                await manager.send_to(username, payload)
                if peer:
                    await manager.send_to(peer, payload)

            elif mtype == "message":
                to_user = data.get("to")
                content = (data.get("content") or "").strip()
                reply_to = data.get("reply_to", "")
                if not to_user or not content:
                    continue

                if not can_chat(username, to_user):
                    await manager.send_to(username, {"type": "blocked", "reason": "Follow request must be accepted first."})
                    continue

                ts = now_iso()
                status = "delivered" if manager.is_online(to_user) else "sent"
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute(
                    "INSERT INTO messages (sender, receiver, content, reply_to, status, is_ai, timestamp) VALUES (?, ?, ?, ?, ?, 0, ?)",
                    (username, to_user, content, reply_to, status, ts)
                )
                mid = c.lastrowid
                conn.commit()
                conn.close()

                msg_obj = {
                    "type": "new_message", "id": mid, "sender": username, "receiver": to_user,
                    "content": content, "reply_to": reply_to, "reaction": "", "status": status, "is_ai": 0, "timestamp": ts
                }
                await manager.send_to(username, msg_obj)
                if to_user != username:
                    await manager.send_to(to_user, msg_obj)

                if to_user == "friday" or "@friday" in content.lower():
                    prompt_txt = content.replace("@friday", "").replace("@FRIDAY", "").strip() or content
                    await manager.send_to(username, {"type": "typing", "from": "friday", "is_typing": True})
                    ai_reply = await ask_friday_ai(prompt_txt)
                    await manager.send_to(username, {"type": "typing", "from": "friday", "is_typing": False})

                    ats = now_iso()
                    target_peer = username if to_user == "friday" else to_user
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute(
                        "INSERT INTO messages (sender, receiver, content, reply_to, status, is_ai, timestamp) VALUES ('friday', ?, ?, '', 'seen', 1, ?)",
                        (target_peer, ai_reply, ats)
                    )
                    aid = c.lastrowid
                    conn.commit()
                    conn.close()

                    ai_msg = {
                        "type": "new_message", "id": aid, "sender": "friday", "receiver": target_peer,
                        "content": ai_reply, "reply_to": "", "reaction": "", "status": "seen", "is_ai": 1, "timestamp": ats
                    }
                    await manager.send_to(username, ai_msg)
                    if to_user != "friday":
                        await manager.send_to(to_user, ai_msg)

    except WebSocketDisconnect:
        manager.disconnect(username, ws)
        await manager.broadcast_presence(username, False)
    except Exception:
        manager.disconnect(username, ws)
        await manager.broadcast_presence(username, False)
