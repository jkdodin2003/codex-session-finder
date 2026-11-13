"""最小可运行回归检查。所有测试文件在本项目 outputs 内，原聊天数据库不参与写入。"""
import json
from pathlib import Path
import tempfile
import sqlite3
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch
import app


def run():
    app.OUTPUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="selfcheck-", dir=app.OUTPUT) as directory:
        root = Path(directory).resolve()
        assert root.is_relative_to(app.OUTPUT.resolve())
        database = root / "search.sqlite"
        app.initialize(database)
        a = "11111111-1111-4111-8111-111111111111"
        b = "22222222-2222-4222-8222-222222222222"
        quote = "示例文字\n\n测试 8500 元 压力参数设置"
        with app.connect(database) as db:
            for id_, title in ((a, "示例项目甲"), (b, "示例项目乙")):
                db.execute("INSERT INTO threads(id,title,cwd,project,updated,archived,rollout) VALUES(?,?,?,?,?,?,?)", (id_, title, str(root), "测试项目", 1, int(id_ == b), ""))
                app.put_entry(db, id_, "metadata", "meta", title)
            app.put_entry(db, a, "reply", "assistant", quote, 2, "final_answer")
            app.put_entry(db, a, "user", "user", "USB / RS485 通讯 50% 参数_表", 1)
            app.put_entry(db, b, "reply", "assistant", "8500 只是历史参考", 3)
            app.put_entry(db, b, "tool", "assistant", "安全显示 sk-123456789012345678901234", 4)
        assert app.search_threads(database, "8500 压力")["results"][0]["id"] == a
        assert app.search_threads(database, "示例项目乙")["results"][0]["id"] == b
        assert not app.search_threads(database, "示例项目乙", archived=False)["results"]
        assert app.search_threads(database, "USB 8500")["total"] == 1  # 关键词可以分别命中不同消息。
        assert app.search_threads(database, "U")["total"] >= 1
        assert app.search_threads(database, "压")["total"] >= 1
        assert app.search_threads(database, "50%")["total"] == 1
        assert app.search_threads(database, "参数_表")["total"] == 1
        assert not app.search_threads(database, "' OR 1=1 --")["results"]
        assert app.detail(database, a, "压力")["messages"][0]["text"] == quote
        assert app.search_threads(database, "SK-123456789012345678901234")["total"] == 0
        with app.connect(database) as db:
            db.execute("UPDATE threads SET favorite=1 WHERE id=?", (a,))
            app.put_entry(db, a, "reply", "assistant", quote + " 支持回放", 3, "final_answer")
        assert app.search_threads(database, favorites=True)["total"] == 1
        assert app.search_threads(database, "回放")["total"] == 1
        assert app.normalize("25\n ms") == "25 ms"
        assert app.display_title("# Files mentioned by the user:\n\n## 控制器需求.docx: D:/x.docx", "") == "控制器需求.docx"
        long_quote = "完整原话 " * 40
        assert app.query_terms(long_quote) == [app.normalize(long_quote)]
        logfile = root / "rollout.jsonl"
        objects = [
            {"timestamp": "2026-10-05T10:00:00Z", "type": "response_item", "payload": {"type": "function_call_output", "output": "不要索引工具输出8500"}},
            {"timestamp": "2026-10-05T10:00:01Z", "type": "response_item", "payload": {"type": "message", "id": "real", "role": "assistant", "content": [{"type": "output_text", "text": "真实回复 压力8500"}]}}
        ]
        logfile.write_bytes(b"".join(json.dumps(obj, ensure_ascii=False).encode() + b"\n" for obj in objects) + b'{"type":"response_item"')
        items = list(app.legacy_messages(logfile))
        assert len([message for _, message in items if message]) == 1
        offset = items[-1][0]
        assert offset < logfile.stat().st_size
        assert not list(app.legacy_messages(logfile, offset))  # 不完整末行不误吞。
        readonly = app.connect(database, readonly=True)
        try:
            readonly.execute("DELETE FROM threads")
            raise AssertionError("只读连接不应允许删除")
        except sqlite3.OperationalError:
            pass
        finally:
            readonly.close()
        # 最小源库验证：首次同步、最新改名、增量消息、收藏保留。
        source = root / "home"
        source.mkdir()
        state = sqlite3.connect(source / "state_5.sqlite")
        state.executescript("""CREATE TABLE projects(id TEXT,name TEXT);
            CREATE TABLE threads(id TEXT,title TEXT,cwd TEXT,rollout_path TEXT,updated_at INTEGER,
            archived INTEGER,first_user_message TEXT,project_id TEXT,preview TEXT,source TEXT,agent_path TEXT);""")
        state.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)", (a, "旧名称", str(root), str(logfile), 1, 0, "测试首条", None, "", "vscode", None))
        state.commit()
        (source / "session_index.jsonl").write_text(json.dumps({"id": a, "thread_name": "示例项目甲"}, ensure_ascii=False) + "\n", "utf-8")
        native = sqlite3.connect(source / "thread_history_1.sqlite")
        native.executescript("""CREATE TABLE thread_history_projection_state(thread_id TEXT,next_rollout_byte_offset INTEGER);
            CREATE TABLE thread_items(thread_id TEXT,item_id TEXT,item_type TEXT,item_json TEXT,created_at_ms INTEGER,updated_at_ordinal INTEGER);""")
        native.execute("INSERT INTO thread_history_projection_state VALUES(?,0)", (a,))
        native.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?)", (a, "real", "agentMessage", json.dumps({"text": "真实回复 压力8500", "phase": "final_answer"}, ensure_ascii=False), 1791194401000, 10))
        native.commit()
        isolated = root / "indexed.sqlite"
        indexer = app.Indexer(source, isolated)
        indexer.lock.acquire()
        indexer.run()
        assert not indexer.status["errors"], indexer.status["errors"]
        assert app.search_threads(isolated, "示例项目甲 压力")["total"] == 1
        with app.connect(isolated) as db:
            db.execute("UPDATE threads SET favorite=1 WHERE id=?", (a,))
            assert db.execute("SELECT count(*) FROM entries WHERE role='assistant'").fetchone()[0] == 1
        state.execute("UPDATE threads SET updated_at=2")
        state.commit()
        native.execute("UPDATE thread_items SET updated_at_ordinal=20,item_json=?", (json.dumps({"text": "真实回复 压力8500 增量PID", "phase": "final_answer"}, ensure_ascii=False),))
        native.commit()
        # 补全上一轮末行后，增量解析不能丢这条新消息。
        logfile.write_bytes(logfile.read_bytes() + b',"payload":{"type":"message","id":"new","role":"user","content":[{"text":"NEW-INCREMENTAL"}]}}\n')
        indexer.lock.acquire()
        indexer.run()
        assert app.search_threads(isolated, "new-incremental", favorites=True)["total"] == 1
        assert app.search_threads(isolated, "增量PID")["total"] == 1
        # A brand-new title should show up before the full-text index is refreshed.
        fresh = "33333333-3333-4333-8333-333333333333"
        folder = root / "中文项目 With Spaces"
        folder.mkdir()
        state.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (fresh, "刚刚新开的会话", "\\\\?\\" + str(folder), str(logfile), 1791194500, 0, "", None, "", "vscode", None))
        state.commit()
        native.execute("ALTER TABLE thread_items ADD rollout_ordinal INTEGER DEFAULT 0")
        native.execute("ALTER TABLE thread_items ADD completed_at_ms INTEGER")
        native.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?,?)",
                       (fresh, "latest", "agentMessage", "{}", 1791194499000, 10, 10, 1791194500123))
        native.commit()
        result = app.recent_threads(source, isolated, "新开的会话")["results"]
        assert len(result) == 1 and result[0]["id"] == fresh
        assert result[0]["last_chat"] == 1791194500.123 and result[0]["indexed"] == 0
        assert app.recent_threads(source, isolated)["results"][0]["id"] == fresh
        assert app.detail(isolated, fresh, home=source)["thread"]["title"] == "刚刚新开的会话"
        terminal = root / "mock-wt.exe"
        cli = root / "mock-codex.exe"
        terminal.touch()
        cli.touch()
        argv = app.cli_command(source, fresh, terminal, cli)
        assert argv == [str(terminal), "-w", "CodexChats", "new-tab", "-d", str(folder), "--title", "Codex CLI",
                        str(cli), "resume", "-C", str(folder), fresh]
        with app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler) as server:
            server.token = "fixture-local-token"
            server.indexer = SimpleNamespace(home=source)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            def request(token=None, origin=None):
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["X-Project-Finder-Token"] = token
                if origin:
                    headers["Origin"] = origin
                payload = json.dumps({"id": fresh, "cwd": "IGNORED CLIENT INPUT"}).encode()
                url = f"http://127.0.0.1:{server.server_port}/api/open-cli"
                try:
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(urllib.request.Request(url, payload, headers), timeout=3) as r:
                        return r.status
                except urllib.error.HTTPError as e:
                    return e.code
            try:
                with patch.object(app, "open_cli_chat") as launch:
                    assert request() == 403
                    assert request(server.token, "https://example.invalid") == 403
                    launch.assert_not_called()
                    assert request(server.token) == 200
                    launch.assert_called_once_with(source, fresh)
            finally:
                server.shutdown()
                worker.join(3)
        for bad in ("not-a-uuid", b):
            try:
                app.cli_command(source, bad, terminal, cli)
            except ValueError:
                pass
            else:
                raise AssertionError("不应为未知或错误 ID 打开 CLI")
        state.execute("UPDATE threads SET cwd=? WHERE id=?", (str(root / 'not-found'), fresh))
        state.commit()
        try:
            app.cli_command(source, fresh, terminal, cli)
        except ValueError:
            pass
        else:
            raise AssertionError("不应为缺失项目目录打开 CLI")
        native.close()
        state.close()
    print("PASS: 原有搜索/收藏/增量索引；实时标题及最后聊天时间；CLI 精确 ID、中文目录和错误输入拒绝")


if __name__ == "__main__":
    run()
