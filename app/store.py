import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager

from .config import settings


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


class Store:
    """每次操作独立连接，事务提交后才返回结果。"""

    def __init__(self, path=None):
        self.path = path or settings.data_dir / "chatdb.sqlite3"

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(str(self.path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL
                    REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL, content TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id);
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL,
                    content TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL
                    REFERENCES documents(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL, content TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id TEXT PRIMARY KEY, session_id TEXT, action TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)
            # 服务异常退出时，保留未完成回答并标记状态。
            rows = conn.execute("SELECT id, metadata FROM messages WHERE role='assistant'").fetchall()
            for row in rows:
                meta = json.loads(row["metadata"])
                if meta.get("status") == "streaming":
                    meta["status"] = "interrupted"
                    conn.execute("UPDATE messages SET metadata=? WHERE id=?", (json.dumps(meta), row["id"]))
        if not self.documents():
            self.add_document("数据库设计与查询规范", "ChatDB 内置示例规范", """
# 只读查询规范
查询必须使用只读账号。禁止 INSERT、UPDATE、DELETE、DROP、ALTER、TRUNCATE、
COPY、CALL、SELECT INTO、SELECT FOR UPDATE 和多条语句。限制结果行数并设置查询超时。
生成 DDL 仅作为建议展示，不在助手中执行。

# 索引设计
联合索引遵循最左前缀原则。等值筛选列通常置于范围筛选列之前，但必须结合选择性和执行计划验证。
避免对索引列使用函数或隐式类型转换。索引会增加写入和存储成本，不应给每个字段建索引。
LIKE '%关键词' 通常不能利用普通 B-tree 索引。PostgreSQL 可评估 pg_trgm。

# 表结构设计
表和字段使用 snake_case，必须有主键。金额使用 DECIMAL 或 NUMERIC，避免浮点误差。
时间字段明确时区；业务约束优先用 NOT NULL、UNIQUE、CHECK、外键表达。
先满足合理的第三范式，再根据实际查询考虑有依据的反范式。

# 慢 SQL 排查
先收集 SQL、数据库版本、表行数、现有索引、执行计划和时间范围。
PostgreSQL EXPLAIN 展示估算计划；EXPLAIN ANALYZE 会实际执行，必须谨慎。
检查扫描方式、连接算法、估算行数偏差、排序与临时文件、锁等待和 I/O。
没有执行计划和监控证据时，只能给出待验证假设，不能断言 CPU 飙高根因。

# 示例业务口径
departments 为部门维表；sales 为销售订单事实表。
销售额取 status='completed' 的 amount 总和；最近 7 天按当天及之前 6 个自然日统计。
部门通过 sales.department_id = departments.id 关联。
数据是动态生成的本地演示数据，不能代表真实业务。
""")
            self.add_document("PostgreSQL 索引文档入口", "https://www.postgresql.org/docs/current/indexes.html",
                              "PostgreSQL 官方文档介绍 B-tree、Hash、GiST、SP-GiST、GIN、BRIN 索引类型。"
                              "具体索引选择应根据操作符、数据分布和执行计划确定。此条为导航摘要，完整原文请访问来源链接。")

    def sessions(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC")]

    def session(self, session_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            return dict(row) if row else None

    def create_session(self):
        item = dict(id=uid(), title="新对话", created_at=now(), updated_at=now())
        with self.connect() as conn:
            conn.execute("INSERT INTO sessions VALUES (:id,:title,:created_at,:updated_at)", item)
        return item

    def delete_session(self, session_id):
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        self.log(session_id, "session_deleted", {})

    def messages(self, session_id):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM messages WHERE session_id=? ORDER BY rowid", (session_id,))
            items = [dict(r) for r in rows]
        for item in items:
            item["metadata"] = json.loads(item["metadata"])
        return items

    def add_message(self, session_id, role, content, metadata=None):
        item = dict(id=uid(), session_id=session_id, role=role, content=content,
                    metadata=json.dumps(metadata or {}, ensure_ascii=False), created_at=now())
        with self.connect() as conn:
            conn.execute("INSERT INTO messages VALUES (:id,:session_id,:role,:content,:metadata,:created_at)", item)
            conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now(), session_id))
            if role == "user":
                conn.execute("UPDATE sessions SET title=? WHERE id=? AND title='新对话'", (content[:32], session_id))
        return item["id"]

    def update_message(self, message_id, content, metadata):
        with self.connect() as conn:
            conn.execute("UPDATE messages SET content=?, metadata=? WHERE id=?",
                         (content, json.dumps(metadata, ensure_ascii=False), message_id))

    def add_document(self, name, source, content):
        document_id = uid()
        chunks = []
        for start in range(0, len(content), 800):
            chunks.append((uid(), document_id, len(chunks) + 1, content[start:start + 1000]))
        with self.connect() as conn:
            conn.execute("INSERT INTO documents VALUES (?,?,?,?,?)", (document_id, name, source, content, now()))
            conn.executemany("INSERT INTO chunks VALUES (?,?,?,?)", chunks)
        return document_id

    def documents(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("""
                SELECT d.id,d.name,d.source,d.created_at,length(d.content) AS characters,
                count(c.id) AS chunks FROM documents d LEFT JOIN chunks c ON c.document_id=d.id
                GROUP BY d.id ORDER BY d.created_at DESC
            """)]

    def delete_document(self, document_id):
        with self.connect() as conn:
            return conn.execute("DELETE FROM documents WHERE id=?", (document_id,)).rowcount

    def search(self, query, limit=4):
        # 中文二元词与英文单词的加权检索；本地模式不依赖 embedding 服务。
        words = re.findall(r"[a-zA-Z_][a-zA-Z_0-9]{1,}", query.lower())
        for segment in re.findall(r"[\u4e00-\u9fff]+", query):
            words.extend(segment[i:i + 2] for i in range(len(segment) - 1))
        words = set(words)
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT c.id,c.position,c.content,d.id AS document_id,d.name,d.source
                FROM chunks c JOIN documents d ON c.document_id=d.id
            """).fetchall()
        ranked = []
        for row in rows:
            item = dict(row)
            text = (item["name"] + " " + item["content"]).lower()
            score = sum((2 if len(word) > 2 else 1) for word in words if word in text)
            if score:
                item["score"] = score
                ranked.append(item)
        return sorted(ranked, key=lambda r: r["score"], reverse=True)[:limit]

    def add_attachment(self, name, content):
        attachment_id = uid()
        with self.connect() as conn:
            conn.execute("INSERT INTO attachments VALUES (?,?,?,?)", (attachment_id, name, content, now()))
        return attachment_id

    def attachment(self, attachment_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
            return dict(row) if row else None

    def log(self, session_id, action, payload):
        with self.connect() as conn:
            conn.execute("INSERT INTO audit VALUES (?,?,?,?,?)",
                         (uid(), session_id, action, json.dumps(payload, ensure_ascii=False, default=str), now()))

    def audit(self, limit=100):
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM audit ORDER BY rowid DESC LIMIT ?", (limit,))]
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows


store = Store()
