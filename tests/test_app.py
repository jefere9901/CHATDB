"""验证查询边界、知识入库和会话流式闭环。使用临时目录，不修改业务数据。"""
import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.config import settings
from app.database import QueryRejected, database, validate_sql, mysql_limited_sql
from app.main import app, chat, ChatRequest, active_sessions
from app.store import store


class SQLSafetyTests(unittest.TestCase):
    def test_read_queries(self):
        for sql in [
            "SELECT * FROM sales",
            "WITH x AS (SELECT amount FROM sales) SELECT SUM(amount) FROM x",
            "SELECT id FROM sales UNION ALL SELECT id FROM sales",
            "SELECT COUNT(*) FROM sales WHERE status='completed'",
        ]:
            with self.subTest(sql=sql):
                validate_sql(sql, "sqlite", {("main", "sales")})

    def test_reject_writes_and_escape_routes(self):
        for sql in [
            "DELETE FROM sales",
            "DROP TABLE sales",
            "SELECT * FROM sales; DELETE FROM sales",
            "WITH x AS (DELETE FROM sales RETURNING *) SELECT * FROM x",
            "SELECT * INTO stolen FROM sales",
            "SELECT * FROM sales FOR UPDATE",
            "SELECT load_extension('bad')",
            "SELECT readfile('secret')",
            "SELECT * FROM sqlite_master",
            "SELECT * FROM private.sales",
            "ATTACH DATABASE 'other.db' AS other",
            "PRAGMA writable_schema=ON",
            "SELECT * FROM read_csv('file')",
            "SELECT 1 UNION SELECT pg_read_file('/etc/passwd')",
        ]:
            with self.subTest(sql=sql):
                with self.assertRaises(QueryRejected):
                    validate_sql(sql, "sqlite", {("main", "sales")})

    def test_postgres_schema_and_functions(self):
        validate_sql("SELECT COUNT(*) FROM reporting.orders", "postgres", {("reporting", "orders")})
        for sql in ["SELECT pg_sleep(5)", "SELECT evil.count(*) FROM reporting.orders",
                    "SELECT * FROM private.orders", "SELECT * FROM reporting.orders FOR SHARE"]:
            with self.subTest(sql=sql):
                with self.assertRaises(QueryRejected):
                    validate_sql(sql, "postgres", {("reporting", "orders")})

    def test_mysql_schema_functions_and_guard(self):
        allowed = {("business", "orders")}
        for sql in [
            "SELECT COUNT(*) FROM orders",
            "SELECT * FROM business.orders WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 6 DAY)",
            "WITH x AS (SELECT id FROM orders) SELECT * FROM x",
        ]:
            validate_sql(sql, "mysql", allowed, "business")
        for sql in [
            "SELECT * FROM other.orders", "SELECT * FROM mysql.user",
            "SELECT @secret", "SELECT @@version", "SELECT @x := 1",
            "SELECT SLEEP(20)", "SELECT GET_LOCK('x', 20)",
            "SELECT LOAD_FILE('/secret')", "SELECT * FROM orders INTO OUTFILE '/tmp/leak'",
            "SELECT /*+ MAX_EXECUTION_TIME(999999) */ * FROM orders",
            "SELECT * FROM orders FOR UPDATE", "DELETE FROM orders",
        ]:
            with self.subTest(sql=sql):
                with self.assertRaises(QueryRejected):
                    validate_sql(sql, "mysql", allowed, "business")

    def test_mysql_limit_preserves_small_limit(self):
        self.assertIn("LIMIT 5", mysql_limited_sql("SELECT 1 LIMIT 5"))
        self.assertIn("LIMIT {}".format(settings.row_limit + 1), mysql_limited_sql("SELECT 1 LIMIT 99999"))
        self.assertIn("LIMIT {}".format(settings.row_limit + 1), mysql_limited_sql("SELECT 1"))


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.original = (store.path, database.demo_path, settings.data_dir, settings.database_url,
                        settings.api_key, settings.access_token)
        settings.data_dir = Path(cls.tmp.name)
        settings.database_url = ""
        settings.api_key = ""
        settings.access_token = ""
        store.path = settings.data_dir / "app.sqlite3"
        database.demo_path = settings.data_dir / "demo.sqlite3"
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        (store.path, database.demo_path, settings.data_dir, settings.database_url,
         settings.api_key, settings.access_token) = cls.original
        cls.tmp.cleanup()

    def new_session(self):
        response = self.client.post("/api/sessions")
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]

    def test_page_and_status(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/status").json()["mode"], "demo")

    def test_connection_form_catalog_and_invalid_request_privacy(self):
        response = self.client.get("/api/database/connection")
        self.assertEqual(response.status_code, 200)
        self.assertEqual({t["type"] for t in response.json()["types"]},
                         {"mysql", "mariadb", "postgresql", "mssql", "oracle", "sqlite"})
        response = self.client.post("/api/database/connection/test", json={
            "type": "mysql", "port": "bad", "password": "sensitive-password"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("sensitive-password", response.text)
        self.assertEqual(self.client.post("/api/database/connection/test", json={
            "type": "sqlite", "database": str(database.demo_path)}).status_code, 200)

    def test_connection_changes_blocked_during_generation(self):
        active_sessions.add("test-connection-guard")
        try:
            response = self.client.post("/api/database/connection", json={
                "type": "sqlite", "database": str(database.demo_path)})
            self.assertEqual(response.status_code, 409)
        finally:
            active_sessions.discard("test-connection-guard")
        session_id = self.new_session()
        with patch("app.main.database_switching", True):
            self.assertEqual(self.client.post("/api/sessions/" + session_id + "/chat",
                                             json={"content": "hello"}).status_code, 409)
            self.assertEqual(self.client.post("/api/database/connection", json={
                "type": "sqlite", "database": str(database.demo_path)}).status_code, 409)

    def test_schema_and_real_query(self):
        tables = self.client.get("/api/database/schema").json()["tables"]
        self.assertEqual({t["name"] for t in tables}, {"departments", "sales"})
        result = self.client.post("/api/database/query", json={"sql": "SELECT COUNT(*) AS total FROM sales"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["rows"][0][0], 360)

    def test_limit_and_plan(self):
        result = self.client.post("/api/database/query", json={"sql": "SELECT * FROM sales"}).json()
        self.assertEqual(len(result["rows"]), settings.row_limit)
        self.assertTrue(result["truncated"])
        result = self.client.post("/api/database/query", json={
            "sql": "SELECT * FROM sales WHERE status='completed' AND created_at >= '2026-01-01'", "explain": True})
        self.assertEqual(result.status_code, 200)
        self.assertIn("idx_sales_status_date", json.dumps(result.json()))

    def test_block_and_audit(self):
        sql = "DELETE FROM sales"
        self.assertEqual(self.client.post("/api/database/query", json={"sql": sql}).status_code, 400)
        records = self.client.get("/api/audit").json()
        self.assertTrue(any(r["action"] == "query_blocked" and r["payload"]["sql"] == sql for r in records))

    def test_chat_end_to_end(self):
        session_id = self.new_session()
        response = self.client.post("/api/sessions/{}/chat".format(session_id),
                                    json={"content": "帮我查询最近7天各部门销售额", "agent": "auto"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: context", response.text)
        self.assertIn("event: done", response.text)
        messages = self.client.get("/api/sessions/" + session_id).json()["messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["metadata"]["agent"], "sql")
        self.assertEqual(messages[1]["metadata"]["status"], "complete")
        sql = messages[1]["content"].split("```sql\n")[1].split("```")[0]
        result = self.client.post("/api/database/query", json={"sql": sql, "session_id": session_id}).json()
        self.assertEqual(len(result["rows"]), 4)
        self.assertTrue(all(row[2] > 0 for row in result["rows"]))
        records = self.client.get("/api/audit").json()
        self.assertTrue(any(r["action"] == "answer" and r["payload"]["sql"] for r in records))
        exported = self.client.get("/api/sessions/{}/export".format(session_id))
        self.assertIn("SELECT", exported.text)

    def test_knowledge_upload_and_retrieve(self):
        response = self.client.post("/api/knowledge", files={"file": ("规范.md", "火星订单采用分区归档，保留火星订单唯一编号。".encode("utf-8"), "text/markdown")})
        self.assertEqual(response.status_code, 201)
        document_id = response.json()["id"]
        session_id = self.new_session()
        response = self.client.post("/api/sessions/{}/chat".format(session_id), json={"content": "火星订单如何归档？"})
        self.assertIn("event: done", response.text)
        meta = self.client.get("/api/sessions/" + session_id).json()["messages"][1]["metadata"]
        self.assertTrue(any(r["name"] == "规范.md" for r in meta["references"]))
        self.assertEqual(self.client.delete("/api/knowledge/" + document_id).status_code, 200)

    def test_attachment_and_bad_upload(self):
        response = self.client.post("/api/attachments", files={"file": ("error.log", b"ERROR: timeout waiting for lock", "text/plain")})
        self.assertEqual(response.status_code, 201)
        session_id = self.new_session()
        result = self.client.post("/api/sessions/{}/chat".format(session_id), json={
            "content": "帮我分析这个日志", "attachment_ids": [response.json()["id"]]})
        self.assertIn("event: done", result.text)
        messages = self.client.get("/api/sessions/" + session_id).json()["messages"]
        self.assertEqual(messages[0]["metadata"]["attachments"][0]["name"], "error.log")
        self.assertEqual(messages[1]["metadata"]["agent"], "diagnosis")
        self.assertEqual(self.client.post("/api/attachments", files={"file": ("x.exe", b"bad")}).status_code, 400)
        self.assertEqual(self.client.post("/api/attachments", files={"file": ("x.txt", b"")}).status_code, 400)

    def test_unknown_session_and_validation(self):
        self.assertEqual(self.client.post("/api/sessions/missing/chat", json={"content": "hi"}).status_code, 404)
        session_id = self.new_session()
        self.assertEqual(self.client.post("/api/sessions/{}/chat".format(session_id), json={"content": "   "}).status_code, 422)
        self.assertEqual(self.client.post("/api/sessions/{}/chat".format(session_id), json={"content": "hello", "agent": "bad"}).status_code, 422)

    def test_stream_cancel_releases_session(self):
        session_id = self.new_session()
        async def consume_partial():
            response = await chat(session_id, ChatRequest(content="索引设计原则", agent="knowledge"))
            iterator = response.body_iterator
            async for event in iterator:
                if "event: delta" in event:
                    break
            await iterator.aclose()
        asyncio.run(consume_partial())
        self.assertNotIn(session_id, active_sessions)
        messages = store.messages(session_id)
        self.assertEqual(messages[1]["metadata"]["status"], "interrupted")
        self.assertTrue(messages[1]["content"])
        self.assertIn("event: done", self.client.post("/api/sessions/{}/chat".format(session_id),
                                                     json={"content": "继续讲索引"}).text)

    def test_oversize_and_html_attachment(self):
        self.assertEqual(self.client.post("/api/attachments", files={
            "file": ("large.txt", b"x" * (10 * 1024 * 1024 + 1), "text/plain")}).status_code, 413)
        response = self.client.post("/api/attachments", files={
            "file": ("awr.html", b"<h1>AWR report</h1><script>steal-secret()</script><p>CPU 90%</p>", "text/html")})
        self.assertEqual(response.status_code, 201)
        content = store.attachment(response.json()["id"])["content"]
        self.assertIn("CPU 90%", content)
        self.assertNotIn("steal-secret", content)

    def test_authentication(self):
        settings.access_token = "test-secret"
        try:
            self.assertEqual(self.client.get("/api/sessions").status_code, 401)
            self.assertEqual(self.client.get("/api/sessions", headers={"Authorization": "Bearer test-secret"}).status_code, 200)
        finally:
            settings.access_token = ""

    def test_live_model_stream(self):
        original_client = httpx.AsyncClient
        received = []
        def handle(request):
            body = json.loads(request.content)
            received.append(body)
            self.assertIn("Schema", body["messages"][0]["content"])
            self.assertIn("SELECT", body["messages"][0]["content"])
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"模型测试回答"}}]}\n\ndata: [DONE]\n\n',
                                  headers={"Content-Type": "text/event-stream"})
        def make_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            return original_client(*args, **kwargs)
        settings.api_key = "test-key"
        try:
            session_id = self.new_session()
            with patch("app.agents.httpx.AsyncClient", side_effect=make_client):
                result = self.client.post("/api/sessions/{}/chat".format(session_id), json={"content": "写 SQL", "agent": "sql"})
            self.assertIn("event: done", result.text)
            messages = self.client.get("/api/sessions/" + session_id).json()["messages"]
            self.assertEqual(messages[1]["content"], "模型测试回答")
            self.assertEqual(messages[1]["metadata"]["mode"], "live")
            self.assertEqual(len(received), 1)
        finally:
            settings.api_key = ""

    def test_model_failure_saved(self):
        original_client = httpx.AsyncClient
        def make_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "bad key"}))
            return original_client(*args, **kwargs)
        settings.api_key = "bad-key"
        try:
            session_id = self.new_session()
            with patch("app.agents.httpx.AsyncClient", side_effect=make_client):
                result = self.client.post("/api/sessions/{}/chat".format(session_id), json={"content": "写 SQL", "agent": "sql"})
            self.assertIn("event: error", result.text)
            messages = self.client.get("/api/sessions/" + session_id).json()["messages"]
            self.assertEqual(messages[1]["metadata"]["status"], "error")
            self.assertNotIn("bad-key", messages[1]["content"])
        finally:
            settings.api_key = ""

    def test_delete_preserves_audit(self):
        session_id = self.new_session()
        self.client.post("/api/sessions/{}/chat".format(session_id), json={"content": "索引知识"})
        self.assertEqual(self.client.delete("/api/sessions/" + session_id).status_code, 200)
        self.assertEqual(self.client.get("/api/sessions/" + session_id).status_code, 404)
        self.assertTrue(any(r["session_id"] == session_id for r in self.client.get("/api/audit").json()))


if __name__ == "__main__":
    unittest.main()
