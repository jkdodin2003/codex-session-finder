"""本机 Codex 项目查找。标准库实现；源数据库始终只读，索引独立保存。"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else ROOT
HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
OUTPUT = APP_DIR / "outputs"
DATABASE = OUTPUT / "search.sqlite"
RUNTIME = OUTPUT / "runtime.json"
PORT = 0 if FROZEN else 58974  # 便携包使用空闲端口，避免影响其他实例。
PAGE_SIZE = 50
ID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)


def redact(text: str) -> str:
    # 不读取登录配置；对消息中常见的凭证格式做显示脱敏。
    text = re.sub(r"<oai-mem-citation>.*?</oai-mem-citation>", "", text, flags=re.S)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b", "[密钥已隐藏]", text)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{16,}", "Bearer [已隐藏]", text)
    return text.strip()


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).lower().split())


def query_terms(text: str) -> list[str]:
    text = normalize(text)[:1600]
    if len(text) > 160:
        return [text]  # 粘贴长段原话时按整段查，不拆成几十个关键词。
    return list(dict.fromkeys(a or b for a, b in re.findall(r'"([^"]+)"|(\S+)', text)))[:12]


def timestamp(value) -> float:
    if isinstance(value, (int, float)):
        return value / 1000 if value > 100_000_000_000 else value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(path=DATABASE, readonly=False):
    path = Path(path).resolve()
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10, factory=ClosingConnection) if readonly else sqlite3.connect(path, timeout=10, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    if readonly:
        db.execute("PRAGMA query_only=ON")
    return db


def initialize(path=DATABASE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS threads(
            id TEXT PRIMARY KEY, title TEXT NOT NULL, cwd TEXT NOT NULL,
            project TEXT NOT NULL, updated REAL NOT NULL, archived INTEGER NOT NULL,
            favorite INTEGER NOT NULL DEFAULT 0, rollout TEXT NOT NULL,
            native_cursor INTEGER NOT NULL DEFAULT -1, file_offset INTEGER NOT NULL DEFAULT 0,
            file_signature TEXT NOT NULL DEFAULT '', indexed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS entries(
            id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, thread_id TEXT NOT NULL,
            role TEXT NOT NULL, phase TEXT NOT NULL, stamp REAL NOT NULL,
            text TEXT NOT NULL, norm TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS entries_thread ON entries(thread_id,stamp,id);
        CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
            thread_id UNINDEXED, norm, content='entries',content_rowid='id',tokenize='trigram');
        CREATE TRIGGER IF NOT EXISTS entry_insert AFTER INSERT ON entries BEGIN
            INSERT INTO search(rowid,thread_id,norm) VALUES(new.id,new.thread_id,new.norm);
        END;
        CREATE TRIGGER IF NOT EXISTS entry_delete AFTER DELETE ON entries BEGIN
            INSERT INTO search(search,rowid,thread_id,norm) VALUES('delete',old.id,old.thread_id,old.norm);
        END;
        CREATE TRIGGER IF NOT EXISTS entry_update AFTER UPDATE ON entries BEGIN
            INSERT INTO search(search,rowid,thread_id,norm) VALUES('delete',old.id,old.thread_id,old.norm);
            INSERT INTO search(rowid,thread_id,norm) VALUES(new.id,new.thread_id,new.norm);
        END;
        """)


def put_entry(db, thread_id, key, role, text, stamp=0, phase=""):
    text = redact(text)
    if not text or text.startswith(("# AGENTS.md instructions", "<environment_context>", "<permissions instructions>")):
        return
    db.execute("""INSERT INTO entries(key,thread_id,role,phase,stamp,text,norm) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(key) DO UPDATE SET text=excluded.text,norm=excluded.norm,
        role=excluded.role,phase=excluded.phase,stamp=excluded.stamp
        WHERE entries.text!=excluded.text OR entries.stamp!=excluded.stamp""",
        (thread_id + ":" + key, thread_id, role, phase, stamp, text, normalize(text)))


def message_text(item):
    if isinstance(item.get("text"), str):
        return item["text"]
    return "\n".join(c.get("text", "") for c in item.get("content", []) if isinstance(c, dict) and isinstance(c.get("text"), str))


def display_title(title, first):
    title = redact(title or "")
    if title and len(title) <= 120 and "\n" not in title:
        return title
    source = title or first
    attachment = re.search(r"^## (.+?\.(?:docx?|pdf|zip|rar|xlsx?|pptx?|mp4|png|jpg))(?::|$)", source, re.M | re.I)
    if attachment:
        return redact(attachment.group(1))[:100]
    if "## My request:" in source:
        source = source.split("## My request:", 1)[1]
    source = redact(source).splitlines()
    return (next((s.strip() for s in source if s.strip()), "未命名聊天"))[:100]


def load_metadata(home):
    names = {}
    index = home / "session_index.jsonl"
    if index.exists():
        with index.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if row.get("thread_name"):
                        names[row["id"]] = row["thread_name"]
                except (ValueError, KeyError):
                    continue
    with connect(home / "state_5.sqlite", readonly=True) as db:
        projects = {r["id"]: r["name"] for r in db.execute("SELECT id,name FROM projects")}
        columns = {r["name"] for r in db.execute("PRAGMA table_info(threads)")}
        updated = "coalesce(nullif(updated_at_ms,0), updated_at * 1000) / 1000.0" if "updated_at_ms" in columns else "updated_at"
        rows = list(db.execute(f"""SELECT id,title,cwd,rollout_path,{updated} AS updated_at,archived,first_user_message,
            project_id,preview,source FROM threads WHERE source NOT LIKE '%subagent%'
            AND coalesce(agent_path,'') IN ('','/root')"""))
    return sorted((dict(r, title=display_title(names.get(r["id"], r["title"]), r["first_user_message"]),
                        project=projects.get(r["project_id"], Path(r["cwd"]).name or "其他聊天"),
                        updated=timestamp(r["updated_at"])) for r in rows), key=lambda r: r["updated"], reverse=True)


def resolve_rollout(value, home):
    p = Path(value)
    if p.exists():
        return p
    name = p.name
    date = re.search(r"rollout-(\d{4})-(\d{2})-(\d{2})T", name)
    for root in (home / "sessions", home / "archived_sessions"):
        if date:
            candidate = root.joinpath(*date.groups(), name)
            if candidate.exists():
                return candidate
        candidate = root / name
        if candidate.exists():
            return candidate
    return None


def legacy_messages(path, offset=0):
    """忽略工具输出和推理，只解析实际用户/助手消息；支持追加中的不完整末行。"""
    with path.open("rb") as stream:
        stream.seek(offset)
        while True:
            start = stream.tell()
            line = stream.readline(8 * 1024 * 1024)
            if not line:
                break
            if not line.endswith(b"\n"):
                if len(line) < 8 * 1024 * 1024:
                    break  # 下次从这条尚未写完的记录重读。
                # ponytail: 超过8MiB的单条工具记录只跳过，不在内存里展开。
                while line and not line.endswith(b"\n"):
                    line = stream.readline(8 * 1024 * 1024)
                yield stream.tell(), None
                continue
            kind = re.search(rb'"type"\s*:\s*"([^"]+)"', line[:250])
            kind = kind.group(1) if kind else b""
            if kind == b"response_item" and re.search(rb'"type"\s*:\s*"message"', line[:600]):
                try:
                    obj = json.loads(line)
                    item = obj["payload"]
                    role = item.get("role")
                    if role in ("user", "assistant"):
                        text = message_text(item)
                        key = item.get("id") or hashlib.sha1((role + str(start) + text).encode()).hexdigest()
                        yield stream.tell(), (key, role, text, timestamp(obj.get("timestamp")), item.get("phase") or "")
                        continue
                except (ValueError, KeyError):
                    pass
            yield stream.tell(), None


class Indexer:
    def __init__(self, home=HOME, database=DATABASE):
        self.home, self.database = Path(home), Path(database)
        initialize(self.database)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        with connect(self.database, readonly=True) as db:
            count = db.execute("SELECT count(*) FROM entries WHERE role!='meta'").fetchone()[0]
        self.status = {"running": False, "done": 0, "total": 0, "messages": count, "errors": [], "current": "", "last_update": 0}

    def start(self):
        if not self.lock.acquire(blocking=False):
            return False
        self.status.update(running=True, done=0, errors=[], current="读取聊天目录")
        threading.Thread(target=self.run, daemon=True).start()
        return True

    def run(self):
        try:
            metadata = load_metadata(self.home)
            self.status["total"] = len(metadata)
            with connect(self.database) as db:
                for r in metadata:
                    db.execute("""INSERT INTO threads(id,title,cwd,project,updated,archived,rollout) VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET title=excluded.title,cwd=excluded.cwd,project=excluded.project,
                        updated=excluded.updated,archived=excluded.archived,rollout=excluded.rollout""",
                        (r["id"], r["title"], r["cwd"], r["project"], r["updated"], r["archived"], r["rollout_path"]))
                    put_entry(db, r["id"], "metadata", "meta", "\n".join([r["title"], r["project"], r["cwd"], r["first_user_message"], r["preview"]]))
            history_path = self.home / "thread_history_1.sqlite"
            history = connect(history_path, readonly=True) if history_path.exists() else None
            try:
                projections = {r["thread_id"]: r["next_rollout_byte_offset"] for r in history.execute("SELECT thread_id,next_rollout_byte_offset FROM thread_history_projection_state")} if history else {}
                with connect(self.database) as db:
                    for r in metadata:
                        if self.stop.is_set():
                            break
                        self.status["current"] = r["title"]
                        try:
                            saved = db.execute("SELECT * FROM threads WHERE id=?", (r["id"],)).fetchone()
                            cursor = saved["native_cursor"]
                            if history and r["id"] in projections:
                                highest = history.execute("SELECT coalesce(max(updated_at_ordinal),-1) FROM thread_items WHERE thread_id=?", (r["id"],)).fetchone()[0]
                                for item in history.execute("""SELECT item_id,item_type,item_json,created_at_ms FROM thread_items
                                    WHERE thread_id=? AND updated_at_ordinal>? AND item_type IN ('userMessage','agentMessage')
                                    ORDER BY updated_at_ordinal""", (r["id"], cursor)):
                                    obj = json.loads(item["item_json"])
                                    put_entry(db, r["id"], item["item_id"], "user" if item["item_type"] == "userMessage" else "assistant",
                                              message_text(obj), timestamp(item["created_at_ms"]), obj.get("phase") or "")
                                cursor = highest
                            path = resolve_rollout(r["rollout_path"], self.home)
                            offset, signature = saved["file_offset"], saved["file_signature"]
                            if path:
                                stat = path.stat()
                                new_signature = str(path.resolve()) + ":" + str(stat.st_ino)
                                if stat.st_size < offset or (signature and signature != new_signature):
                                    offset = 0
                                offset = max(offset, min(projections.get(r["id"], 0), stat.st_size))
                                if offset < stat.st_size:
                                    last_commit = time.monotonic()
                                    for end, message in legacy_messages(path, offset):
                                        offset = end
                                        if message:
                                            put_entry(db, r["id"], *message)
                                        if time.monotonic() - last_commit > 2:
                                            db.execute("UPDATE threads SET file_offset=?,file_signature=?,native_cursor=? WHERE id=?", (offset, new_signature, cursor, r["id"]))
                                            db.commit()
                                            last_commit = time.monotonic()
                                            if self.stop.is_set():
                                                break
                                signature = new_signature
                            elif r["id"] not in projections:
                                raise FileNotFoundError("旧聊天文件已不在原位置，暂只索引名称和首条消息")
                            db.execute("UPDATE threads SET native_cursor=?,file_offset=?,file_signature=?,indexed=1 WHERE id=?", (cursor, offset, signature, r["id"]))
                            db.commit()
                        except (OSError, sqlite3.Error, ValueError) as exc:
                            db.rollback()
                            self.status["errors"].append({"title": r["title"], "reason": str(exc)[:180]})
                            logging.error("index thread %s: %s", r["id"], type(exc).__name__)
                        self.status["done"] += 1
                    self.status["messages"] = db.execute("SELECT count(*) FROM entries WHERE role!='meta'").fetchone()[0]
                self.status["last_update"] = time.time()
            finally:
                if history:
                    history.close()
        except Exception as exc:
            logging.exception("index refresh failed")
            self.status["errors"].append({"title": "索引更新", "reason": str(exc)[:180]})
        finally:
            self.status.update(running=False, current="")
            self.lock.release()


def contains_sql(column, term):
    if any(c in term for c in "%_\\"):
        return f"instr({column},?)>0", term
    return f"{column} LIKE ?", "%" + term + "%"


def excerpt(text, terms, width=260):
    lower = unicodedata.normalize("NFKC", text).lower()
    pos = min((lower.find(t) for t in terms if lower.find(t) >= 0), default=0)
    start = max(0, pos - 60)
    return ("…" if start else "") + text[start:start + width] + ("…" if start + width < len(text) else "")


def indexed_last_chat(db, thread_id):
    row = db.execute("SELECT stamp FROM entries WHERE thread_id=? AND role IN ('user','assistant') ORDER BY stamp DESC LIMIT 1", (thread_id,)).fetchone()
    return row[0] if row else 0


def recent_threads(home, database, query="", project="", archived=False, favorites=False, page=0):
    # Metadata comes from Codex now, not from the last full-text refresh. Read
    # only message timestamps for the visible page, never the large message JSON.
    rows = [r for r in load_metadata(home) if r["source"] != "exec"]
    projects = sorted({r["project"] for r in rows})
    terms = query_terms(query)
    results = []
    with connect(database, readonly=True) as db:
        flags = {r["id"]: (r["favorite"], r["indexed"]) for r in db.execute("SELECT id,favorite,indexed FROM threads")}
        rows = [r for r in rows if (archived or not r["archived"]) and (not project or r["project"] == project)
                and (not favorites or flags.get(r["id"], (0, 0))[0])
                and all(t in normalize(r["title"]) for t in terms)]
        history_path = home / "thread_history_1.sqlite"
        history = None
        try:
            if history_path.is_file():
                history = connect(history_path, readonly=True)
        except sqlite3.Error:
            pass  # Keep the live list available if message projection is unavailable.
        try:
            for row in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
                item = {k: row[k] for k in ("id", "title", "cwd", "project", "updated", "archived")}
                item["favorite"], item["indexed"] = flags.get(row["id"], (0, 0))
                item["last_chat"] = indexed_last_chat(db, row["id"])
                if history:
                    try:
                        msg = history.execute("""SELECT coalesce(nullif(completed_at_ms,0),created_at_ms) AS stamp
                            FROM thread_items WHERE thread_id=? AND item_type IN ('userMessage','agentMessage')
                            ORDER BY rollout_ordinal DESC LIMIT 1""", (row["id"],)).fetchone()
                        if msg:
                            item["last_chat"] = max(item["last_chat"], timestamp(msg["stamp"]))
                    except sqlite3.Error:
                        history.close()
                        history = None
                results.append(item)
        finally:
            if history:
                history.close()
    return {"total": len(rows), "results": results, "terms": terms, "projects": projects}


def search_threads(database, query="", project="", archived=True, favorites=False, page=0):
    terms = query_terms(query)
    where, params = [], []
    for term in sorted(terms, key=len, reverse=True):
        if len(term) < 3:
            condition, value = contains_sql("e.norm", term)
            # trigram 不支持两字词：先用长词缩小聊天，再在该聊天的消息里找。
            where.append(f"EXISTS (SELECT 1 FROM entries e WHERE e.thread_id=t.id AND {condition})")
        else:
            condition, value = contains_sql("norm", term)
            where.append(f"t.id IN (SELECT thread_id FROM search WHERE {condition})")
        params.append(value)
    if project:
        where.append("t.project=?")
        params.append(project)
    if not archived:
        where.append("t.archived=0")
    if favorites:
        where.append("t.favorite=1")
    clause = " AND ".join(where) or "1=1"
    with connect(database, readonly=True) as db:
        total = db.execute("SELECT count(*) FROM threads t WHERE " + clause, params).fetchone()[0]
        # ponytail: 桌面个人库在当前候选中排序，超过1000个命中时改为SQL分页相关度排序。
        limit = " LIMIT 1000" if terms else " LIMIT 50 OFFSET " + str(page * 50)
        rows = db.execute("SELECT t.* FROM threads t WHERE " + clause + " ORDER BY updated DESC" + limit, params).fetchall()
        results = []
        for row in rows:
            result = {k: row[k] for k in ("id", "title", "cwd", "project", "updated", "archived", "favorite", "indexed")}
            result["last_chat"] = indexed_last_chat(db, row["id"])
            if terms:
                # 旧命中不应被最近300条消息盖住。
                clauses, values = zip(*(contains_sql("norm", t) for t in terms))
                candidates = list(db.execute("SELECT role,phase,stamp,text,norm FROM entries WHERE thread_id=? AND (" + " OR ".join(clauses) + ") ORDER BY stamp DESC LIMIT 80", (row["id"], *values)))
            else:
                candidates = list(db.execute("SELECT role,phase,stamp,text,norm FROM entries WHERE thread_id=? ORDER BY stamp DESC LIMIT 1", (row["id"],)))
            def score(c):
                hits = sum(t in c["norm"] for t in terms)
                return hits * 10 + (3 if c["role"] == "assistant" and c["phase"] == "final_answer" else 0) + (1 if c["role"] != "meta" else 0)
            best = max(candidates, key=score) if candidates else None
            result["snippet"] = excerpt(best["text"], terms) if best else "正在整理这条聊天的历史消息…"
            result["role"] = best["role"] if best else "meta"
            title_hits = sum(t in normalize(row["title"]) for t in terms)
            result["score"] = title_hits * 30 + (score(best) if best and terms else 0)
            results.append(result)
    results.sort(key=lambda r: (r["score"], r["updated"]), reverse=True)
    return {"total": total, "results": results[page * 50:(page + 1) * 50] if terms else results, "terms": terms, "candidate_cap": 1000}


def detail(database, thread_id, query="", all_messages=False, before=0, home=None):
    terms = query_terms(query)
    with connect(database, readonly=True) as db:
        thread = db.execute("SELECT id,title,cwd,project,updated,archived,favorite FROM threads WHERE id=?", (thread_id,)).fetchone()
        if not thread:
            current = next((r for r in load_metadata(home) if r["id"] == thread_id), None) if home and ID_RE.fullmatch(thread_id) else None
            if not current:
                raise ValueError("找不到这条聊天，请更新记录后再试")
            thread = {k: current[k] for k in ("id", "title", "cwd", "project", "updated", "archived")}
            thread["favorite"] = 0
        where, values = ["thread_id=?", "role!='meta'"], [thread_id]
        if terms and not all_messages:
            conditions, args = zip(*(contains_sql("norm", t) for t in terms))
            where.append("(" + " OR ".join(conditions) + ")")
            values.extend(args)
        if before:
            where.append("id<?")
            values.append(before)
        messages = [dict(r) for r in db.execute("SELECT id,role,phase,stamp,text FROM entries WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT 51", values)]
        more = len(messages) > 50
        messages = messages[:50]
        for message in messages:
            if len(message["text"]) > 30_000:
                message["text"] = excerpt(message["text"], terms, 30_000) + "\n[此条很长，完整内容请打开原聊天]"
        messages.reverse()
        return {"thread": dict(thread), "messages": messages, "terms": terms, "more": more, "before": messages[0]["id"] if messages else 0}


def open_chat(thread_id):
    if not ID_RE.fullmatch(thread_id):
        raise ValueError("聊天ID格式不正确")
    os.startfile("codex://threads/" + thread_id)


def local_project_folder(value):
    # Codex commonly records an extended-length drive path (\\?\F:\...).
    # Normalize only this drive form; never convert UNC or device paths.
    def plain_drive(text):
        if text.startswith("\\\\?\\") and re.match(r"^[A-Za-z]:[\\/]", text[4:]):
            text = text[4:]
        return text if re.match(r"^[A-Za-z]:[\\/]", text) else ""

    text = plain_drive(value or "")
    if not text or not Path(text).is_dir():
        raise ValueError("对话原目录不在本机或暂不可用；没有改用其他目录。可尝试从桌面端打开。")
    folder = Path(text)
    resolved = plain_drive(str(folder.resolve(strict=True)))
    if not resolved:
        raise ValueError("原目录指向非本机磁盘；未打开")
    return folder


def cli_command(home, thread_id, terminal, codex):
    if not isinstance(thread_id, str) or not ID_RE.fullmatch(thread_id):
        raise ValueError("聊天ID格式不正确")
    with connect(home / "state_5.sqlite", readonly=True) as db:
        row = db.execute("""SELECT cwd FROM threads WHERE id=? AND source NOT LIKE '%subagent%'
            AND coalesce(agent_path,'') IN ('','/root')""", (thread_id,)).fetchone()
    if not row:
        raise ValueError("这条对话不在当前电脑的 Codex 记录里")
    folder = local_project_folder(row["cwd"])
    if ";" in str(folder) or "%" in str(folder):
        raise ValueError("原目录不适合直接传给 Windows 终端；未启动 CLI")
    if not terminal.is_file() or not codex.is_file():
        raise ValueError("找不到已安装的 Windows 终端或 Codex CLI")
    return [str(terminal), "-w", "CodexChats", "new-tab", "-d", str(folder), "--title", "Codex CLI",
            str(codex), "resume", "-C", str(folder), thread_id]


def open_cli_chat(home, thread_id):
    local = Path(os.environ["LOCALAPPDATA"])
    argv = cli_command(home, thread_id, local / "Microsoft/WindowsApps/wt.exe", local / "Programs/OpenAI/Codex/bin/codex.exe")
    # No shell, prompt, or client-supplied path. Only native resume by exact ID.
    subprocess.Popen(argv, env=dict(os.environ, CODEX_HOME=str(home)))


def show_window(url):
    edges = [Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
             Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe"]
    edge = next((p for p in edges if p.exists()), None)
    if edge:
        subprocess.Popen([str(edge), "--app=" + url, "--window-size=1260,840"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, data, status=200, content_type="application/json; charset=utf-8"):
        raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        port = self.server.server_port
        expected = f"127.0.0.1:{port}"
        if self.headers.get("Host") != expected:
            self.send({"error": "仅允许本机地址"}, 403)
            return False
        if self.headers.get("Origin") not in (None, "http://" + expected):
            self.send({"error": "请求来源不匹配"}, 403)
            return False
        if not secrets.compare_digest(self.headers.get("X-Project-Finder-Token", "").encode(), self.server.token.encode()):
            self.send({"error": "请从CodeX 项目查找器启动器打开"}, 403)
            return False
        self.server.last_active = time.monotonic()
        return True

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/":
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                return self.send({"error": "地址不匹配"}, 403)
            html = (ROOT / "index.html").read_text("utf-8").replace("<!-- SESSION_TOKEN -->", '<meta name="finder-token" content="' + self.server.token + '">')
            return self.send(html.encode("utf-8"), content_type="text/html; charset=utf-8")
        if not self.authorized():
            return
        query = urllib.parse.parse_qs(parsed.query)
        get = lambda key, default="": query.get(key, [default])[0]
        try:
            if parsed.path == "/api/status":
                with connect(readonly=True) as db:
                    projects = [r[0] for r in db.execute("SELECT DISTINCT project FROM threads ORDER BY project")]
                    count = db.execute("SELECT count(*) FROM threads").fetchone()[0]
                self.send(dict(self.server.indexer.status, projects=projects, chats=count, version="1.0", index_path=str(DATABASE)))
            elif parsed.path == "/api/recent":
                self.send(recent_threads(self.server.indexer.home, DATABASE, get("q"), get("project"), get("archived") == "1", get("favorites") == "1", min(99, max(0, int(get("page", "0"))))))
            elif parsed.path == "/api/search":
                self.send(search_threads(DATABASE, get("q"), get("project"), get("archived", "1") == "1", get("favorites") == "1", min(19, max(0, int(get("page", "0"))))))
            elif parsed.path == "/api/thread":
                self.send(detail(DATABASE, get("id"), get("q"), get("all") == "1", max(0, int(get("before", "0"))), home=self.server.indexer.home))
            else:
                self.send({"error": "没有这个接口"}, 404)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.send({"error": str(exc)[:250]}, 400)

    def do_POST(self):
        if not self.authorized():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 4096:
                raise ValueError("请求过大")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("请求格式必须是对象")
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/refresh":
                self.send({"started": self.server.indexer.start()})
                return
            if path == "/api/quit":
                self.send({"ok": True})
                self.server.indexer.stop.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            thread_id = data.get("id", "")
            if not isinstance(thread_id, str) or not ID_RE.fullmatch(thread_id):
                raise ValueError("请选择一条聊天")
            if path == "/api/open-cli":
                open_cli_chat(self.server.indexer.home, thread_id)
                return self.send({"ok": True})
            if path == "/api/open":
                detail(DATABASE, thread_id, home=self.server.indexer.home)
                open_chat(thread_id)
                return self.send({"ok": True})
            if path == "/api/folder":
                data = detail(DATABASE, thread_id, home=self.server.indexer.home)
                os.startfile(str(local_project_folder(data["thread"]["cwd"])))
                return self.send({"ok": True})
            with connect() as db:
                row = db.execute("SELECT cwd FROM threads WHERE id=?", (thread_id,)).fetchone()
                if not row:
                    raise ValueError("这条聊天不存在")
                if path == "/api/favorite":
                    db.execute("UPDATE threads SET favorite=? WHERE id=?", (int(bool(data.get("value"))), thread_id))
                else:
                    self.send({"error": "没有这个接口"}, 404)
                    return
            self.send({"ok": True})
        except (ValueError, OSError, sqlite3.Error, TypeError) as exc:
            self.send({"error": str(exc)[:250]}, 400)


def running_instance():
    try:
        info = json.loads(RUNTIME.read_text("utf-8"))
        parsed = urllib.parse.urlsplit(info["url"])
        if parsed.hostname != "127.0.0.1" or parsed.scheme != "http":
            return None
        req = urllib.request.Request(info["url"] + "api/status", headers={"X-Project-Finder-Token": info["token"]})
        with urllib.request.urlopen(req, timeout=1) as response:
            if json.load(response).get("version") == "1.0":
                return info
    except (OSError, ValueError, KeyError):
        pass
    return None


def acquire_instance():
    """命名互斥量只属于本程序；防止连点启动造成两次全库扫描。"""
    if os.name != "nt":
        return True
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    name = "Local\\CodeXProjectFinder-" + hashlib.sha1(str(APP_DIR).casefold().encode()).hexdigest()[:16]
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise OSError("无法创建程序运行锁")
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(handle)
        return False
    # 句柄保留到进程退出，Windows 自动释放；不修改系统服务/注册表。
    global _instance_handle
    _instance_handle = handle
    return True


def main():
    parser = argparse.ArgumentParser()
    launch = parser.add_mutually_exclusive_group()
    launch.add_argument("--open", dest="open", action="store_true")
    launch.add_argument("--no-open", dest="open", action="store_false")
    parser.set_defaults(open=FROZEN)
    parser.add_argument("--index-only", action="store_true")
    args = parser.parse_args()
    OUTPUT.mkdir(exist_ok=True)
    logging.basicConfig(filename=OUTPUT / "app.log", level=logging.INFO, encoding="utf-8", format="%(asctime)s %(levelname)s %(message)s")
    if not acquire_instance():
        for _ in range(30):
            existing = running_instance()
            if existing:
                if args.open:
                    show_window(existing["url"])
                elif args.index_only:
                    print("程序已经运行，请在界面中点击更新记录。")
                return
            time.sleep(0.2)
        logging.error("existing instance did not respond within startup wait")
        return
    indexer = Indexer()
    if args.index_only:
        indexer.lock.acquire()
        indexer.run()
        print(json.dumps(indexer.status, ensure_ascii=False))
        return
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        logging.error("local port %s could not be bound: %s", PORT, type(exc).__name__)
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f"本机端口 {PORT} 当前不可用。未改动其他程序，请稍后重新启动CodeX 项目查找器。", "CodeX 项目查找器", 0)
        return
    server.daemon_threads = True
    server.indexer, server.token = indexer, secrets.token_urlsafe(32)
    server.last_active = time.monotonic()
    url = f"http://127.0.0.1:{server.server_port}/"
    logging.info("service listening at %s", url)
    info = {"url": url, "token": server.token, "pid": os.getpid()}
    RUNTIME.write_text(json.dumps(info), encoding="utf-8")
    indexer.start()
    if args.open:
        show_window(url)
    def idle_watch():
        while not indexer.stop.wait(30):
            if time.monotonic() - server.last_active > 300 and not indexer.status["running"]:
                indexer.stop.set()
                server.shutdown()
                return
    threading.Thread(target=idle_watch, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        indexer.stop.set()
        server.server_close()
        if RUNTIME.exists() and json.loads(RUNTIME.read_text("utf-8")).get("pid") == os.getpid():
            RUNTIME.unlink()  # 只清理本程序自己的运行标记。


if __name__ == "__main__":
    main()
