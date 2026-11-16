import asyncio
import re
import sqlite3
import time
from functools import partial
from contextlib import contextmanager
from datetime import date, timedelta

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope
from sqlalchemy import text, create_engine, inspect, event
from sqlalchemy.pool import NullPool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from .config import settings, save_database_url
from .connections import DATABASE_TYPES, connection_url, infer_type, public_connection


class QueryRejected(ValueError):
    pass


async def in_thread(function, *args):
    return await asyncio.get_running_loop().run_in_executor(None, partial(function, *args))


@contextmanager
def sqlite_connect(path, uri=False):
    conn = sqlite3.connect(path, uri=uri)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# SQL 函数使用白名单；查询权限最终仍由数据库只读账号和事务兜底。
SAFE_FUNCTIONS = {
    "ABS", "AVG", "CAST", "CEIL", "CEILING", "COALESCE", "CONCAT", "COUNT",
    "CURRENT_DATE", "CURRENT_TIMESTAMP", "DATE", "DATE_TRUNC", "DATEDIFF",
    "EXTRACT", "FLOOR", "GREATEST", "IF", "IIF", "IFNULL", "LEAST", "LENGTH",
    "LOWER", "LTRIM", "MAX", "MIN", "NULLIF", "ROUND", "RTRIM", "STRFTIME",
    "SUBSTR", "SUBSTRING", "SUM", "TRIM", "UPPER", "ROW_NUMBER", "RANK",
    "DENSE_RANK", "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE", "REPLACE",
    "TIME", "DATETIME", "JULIANDAY", "TO_CHAR", "TO_DATE", "TO_TIMESTAMP",
    "STRING_AGG", "GROUP_CONCAT", "ARRAY_AGG", "BOOL_AND", "BOOL_OR",
    "DATE_ADD", "DATE_SUB", "CURDATE", "NOW", "YEAR", "MONTH", "DAY",
    "DAYOFMONTH", "DATE_FORMAT", "TIMESTAMPDIFF", "TIMESTAMPADD",
}


def validate_sql(sql, dialect, allowed_tables, default_schema=None):
    if not sql.strip() or len(sql) > 20000:
        raise QueryRejected("SQL 不能为空，长度不能超过 20,000 字符。")
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.ParseError as exc:
        raise QueryRejected("SQL 语法无法解析，请检查后重试。") from exc
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise QueryRejected("只允许单条 SELECT 或只读集合查询。")
    tree = statements[0]
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop,
                 exp.Alter, exp.Command, exp.Into, exp.Lock, exp.Merge)
    if any(isinstance(node, forbidden) for node in tree.walk()):
        raise QueryRejected("查询包含写入、高危命令或锁定操作，已拦截。")
    if any(isinstance(node, (exp.Parameter, exp.SessionParameter, exp.Hint, exp.WithTableHint,
                             exp.QueryOption, exp.NextValueFor))
           for node in tree.walk()):
        raise QueryRejected("不允许读写会话变量或使用优化器提示覆盖查询限制。")
    if any(col.name.upper() in {"NEXTVAL", "CURRVAL"} for col in tree.find_all(exp.Column)):
        raise QueryRejected("不允许访问或递增数据库序列。")
    for node in tree.find_all(exp.Func):
        if isinstance(node.parent, exp.Dot):
            raise QueryRejected("不允许调用自定义 Schema 下的函数。")
        name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name().upper()
        if name not in SAFE_FUNCTIONS and name not in {"CASE", "AND", "OR"}:
            raise QueryRejected("函数 {} 不在只读白名单中。".format(name))
    # scope 区分真实表与 CTE，避免把 CTE 名称当成越权表。
    for scope in traverse_scope(tree):
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                if not isinstance(source.this, exp.Identifier) or source.catalog:
                    raise QueryRejected("不允许跨数据库或表函数访问。")
                table_key = (source.db or default_schema or ("main" if dialect == "sqlite" else "public"), source.name)
                if dialect == "oracle" and not source.db and source.name.upper() == "DUAL":
                    continue
                permitted = table_key in allowed_tables
                if dialect == "oracle":
                    table_name = source.name if source.this.args.get("quoted") else source.name.upper()
                    db_identifier = source.args.get("db")
                    schema_name = table_key[0] if db_identifier and db_identifier.args.get("quoted") else table_key[0].upper()
                    permitted = (schema_name, table_name) in allowed_tables
                if not permitted:
                    raise QueryRejected("无权访问表 {}.{}。".format(*table_key))
                if dialect in {"oracle", "tsql"} and not source.db:
                    source.set("db", exp.to_identifier(default_schema))
    return tree.sql(dialect=dialect)


def limited_sql(sql, dialect):
    tree = sqlglot.parse_one(sql, read=dialect)
    if dialect == "oracle":
        for select in tree.find_all(exp.Select):
            if not select.args.get("from"):
                select.set("from", exp.From(this=exp.Table(this=exp.to_identifier("DUAL"))))
    limit = tree.args.get("limit")
    count = limit.expression if limit else None
    cap = settings.row_limit + 1
    if not isinstance(count, exp.Literal) or not count.is_int or int(count.this) > cap:
        tree = tree.limit(cap)
    return tree.sql(dialect=dialect)


def mysql_limited_sql(sql):
    return limited_sql(sql, "mysql")


class Database:
    def __init__(self):
        self.demo_path = settings.data_dir / "demo.sqlite3"
        self.engine = None
        self.dialect = "postgres" if settings.database_url else "sqlite"
        self.mysql_database = ""
        self.kind = "sqlite"
        self.url = None
        self.schemas = settings.schemas
        self._switch_lock = asyncio.Lock()

    @property
    def label(self):
        name = (self.url.query.get("service_name") or self.url.database) if self.url else "示例业务库"
        return DATABASE_TYPES[self.kind]["label"] + " · " + (name or "请选择业务数据库")

    @property
    def is_demo(self):
        return self.dialect == "sqlite" and self.url is None

    def connection_config(self):
        return public_connection(self.url, self.kind, self.schemas)

    def init_demo(self):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        with sqlite_connect(str(self.demo_path)) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS departments (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS sales (
                    id INTEGER PRIMARY KEY, department_id INTEGER NOT NULL
                    REFERENCES departments(id), amount NUMERIC NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sales_status_date ON sales(status, created_at);
            """)
            if conn.execute("SELECT count(*) FROM departments").fetchone()[0] == 0:
                conn.executemany("INSERT INTO departments VALUES (?,?)",
                                 [(1, "华东销售部"), (2, "华南销售部"), (3, "华北销售部"), (4, "线上业务部")])
                orders = []
                for day in range(30):
                    for department in range(1, 5):
                        for n in range(3):
                            orders.append((department, 580 + (day * 137 + department * 421 + n * 89) % 5000,
                                           "cancelled" if (day + n + department) % 9 == 0 else "completed",
                                           str(date.today() - timedelta(days=day))))
                conn.executemany("INSERT INTO sales(department_id,amount,status,created_at) VALUES (?,?,?,?)", orders)

    @contextmanager
    def sqlite_connection(self):
        conn = sqlite3.connect(self.demo_path.as_uri() + "?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
        try:
            yield conn
        finally:
            conn.close()

    async def startup(self):
        self.engine = None
        self.mysql_database = ""
        self.url = None
        self.schemas = settings.schemas
        if settings.database_url:
            url = make_url(settings.database_url)
            kind = settings.database_type or infer_type(url)
            self._configure(url, kind, settings.schemas)
        else:
            self.dialect = "sqlite"
            self.kind = "sqlite"
            self.schemas = "main"
            self.init_demo()

    def _configure(self, url, kind, schemas):
        if kind not in DATABASE_TYPES:
            raise ValueError("不支持此 DATABASE_TYPE。")
        self.kind = kind
        self.dialect = DATABASE_TYPES[kind]["dialect"]
        self.url = url.set(drivername=DATABASE_TYPES[kind]["driver"])
        self.schemas = schemas or DATABASE_TYPES[kind]["schema"]
        if kind == "oracle" and not self.schemas:
            self.schemas = (url.username or "").upper()
        if kind == "oracle":
            self.schemas = ",".join(s.strip().upper() for s in self.schemas.split(","))
        if kind == "sqlite":
            from pathlib import Path
            path = Path(url.database)
            self.demo_path = (path if path.is_absolute() else settings.data_dir.parent / path).resolve()
            if not self.demo_path.is_file():
                raise ValueError("SQLite 数据库文件不存在。")
            self.url = self.url.set(database=str(self.demo_path))
            return
        if self.dialect == "mysql":
            self.mysql_database = url.database or ""
            args = {"connect_timeout": settings.query_timeout}
        elif self.dialect == "postgres":
            if not all(re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", s.strip()) for s in self.schemas.split(",")):
                raise ValueError("DATABASE_SCHEMAS 包含无效 Schema 名称。")
            args = {"command_timeout": settings.query_timeout, "timeout": settings.query_timeout}
        elif kind == "mssql":
            args = {"login_timeout": settings.query_timeout, "timeout": settings.query_timeout,
                    "readonly": True, "disable_connect_retry": True}
        else:
            args = {"tcp_connect_timeout": settings.query_timeout}
        if kind in {"mssql", "oracle"}:
            self.engine = create_engine(self.url, poolclass=NullPool, connect_args=args, hide_parameters=True)
            if kind == "oracle":
                @event.listens_for(self.engine, "connect")
                def set_call_timeout(dbapi_connection, record):
                    dbapi_connection.call_timeout = settings.query_timeout * 1000
        else:
            self.engine = create_async_engine(self.url, pool_pre_ping=True, connect_args=args, hide_parameters=True)

    async def _probe(self):
        if not self.engine:
            return await in_thread(self._sqlite_schema)
        if self.kind in {"mssql", "oracle"}:
            def probe():
                with self.engine.connect() as conn:
                    conn.execute(text("SELECT 1 FROM DUAL" if self.kind == "oracle" else "SELECT 1"))
            await in_thread(probe)
        else:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return await self._schema()

    async def connect(self, body, persist=False):
        # 候选连接和元数据读取成功之后才替换当前连接。
        async with self._switch_lock:
            url = connection_url(body, self.url, self.kind)
            candidate = Database()
            try:
                candidate._configure(url, body.type, body.schemas)
                tables = await candidate._probe()
                if persist:
                    save_database_url(candidate.url.render_as_string(hide_password=False),
                                      candidate.kind, candidate.schemas)
            except BaseException:
                await candidate.close()
                raise
            if not persist:
                await candidate.close()
                return {"ok": True, "message": "连接与表结构读取成功。", "table_count": len(tables)}
            previous = self.engine
            previous_kind = self.kind
            for field in ("engine", "dialect", "kind", "url", "schemas", "mysql_database", "demo_path"):
                setattr(self, field, getattr(candidate, field))
            settings.database_url = self.url.render_as_string(hide_password=False)
            settings.database_type = self.kind
            settings.schemas = self.schemas
            if previous:
                if previous_kind in {"mssql", "oracle"}:
                    await in_thread(previous.dispose)
                else:
                    await previous.dispose()
            return {"ok": True, "message": "已保存并连接。", "table_count": len(tables),
                    "connection": self.connection_config()}

    async def databases(self):
        if self.dialect != "mysql" or not self.engine:
            return []
        async with self.engine.connect() as conn:
            result = await conn.execute(text("SHOW DATABASES"))
            names = [row[0] for row in result]
        system = {"information_schema", "mysql", "performance_schema", "sys"}
        return sorted(name for name in names if name.lower() not in system)

    async def select_mysql_database(self, name, persist=False):
        async with self._switch_lock:
            await self._select_mysql_database(name, persist)

    async def _select_mysql_database(self, name, persist):
        if self.dialect != "mysql" or not self.engine:
            raise QueryRejected("数据库选择目前仅适用于 MySQL。")
        if name not in await self.databases():
            raise QueryRejected("数据库不存在或账号没有访问权限。")
        previous = self.engine
        url = previous.url.set(database=name)
        candidate = create_async_engine(url, pool_pre_ping=True,
                                       connect_args={"connect_timeout": settings.query_timeout}, hide_parameters=True)
        try:
            async with candidate.connect() as conn:
                await conn.execute(text("SELECT 1"))
            if persist:
                save_database_url(url.render_as_string(hide_password=False))
        except BaseException:
            await candidate.dispose()
            raise
        self.engine = candidate
        self.mysql_database = name
        self.url = url
        if persist:
            settings.database_url = url.render_as_string(hide_password=False)
        await previous.dispose()

    async def schema(self):
        async with self._switch_lock:
            return await self._schema()

    async def _schema(self):
        if not self.engine:
            return await in_thread(self._sqlite_schema)
        if self.dialect == "mysql":
            return await self._mysql_schema()
        if self.kind in {"mssql", "oracle"}:
            return await in_thread(self._inspected_schema)
        schemas = [s.strip() for s in self.schemas.split(",")]
        async with self.engine.connect() as conn:
            columns = (await conn.execute(text("""
                SELECT table_schema,table_name,column_name,data_type,is_nullable
                FROM information_schema.columns WHERE table_schema = ANY(:schemas)
                ORDER BY table_schema,table_name,ordinal_position
            """), {"schemas": schemas})).mappings().all()
            indexes = (await conn.execute(text("""
                SELECT schemaname,tablename,indexname,indexdef FROM pg_indexes
                WHERE schemaname = ANY(:schemas)
            """), {"schemas": schemas})).mappings().all()
        tables = {}
        for col in columns:
            key = (col["table_schema"], col["table_name"])
            tables.setdefault(key, {"schema": key[0], "name": key[1], "columns": [], "indexes": []})
            tables[key]["columns"].append({"name": col["column_name"], "type": col["data_type"],
                                           "nullable": col["is_nullable"] == "YES"})
        for index in indexes:
            key = (index["schemaname"], index["tablename"])
            if key in tables:
                tables[key]["indexes"].append({"name": index["indexname"], "definition": index["indexdef"]})
        return list(tables.values())

    def _inspected_schema(self):
        tables = []
        with self.engine.connect() as conn:
            inspector = inspect(conn)
            for schema in self.schemas.split(","):
                schema = schema.strip()
                for name in inspector.get_table_names(schema=schema):
                    primary = set(inspector.get_pk_constraint(name, schema=schema).get("constrained_columns") or [])
                    def actual_name(value):
                        return str(conn.dialect.denormalize_name(value)) if self.kind == "oracle" else value
                    columns = [{"name": actual_name(c["name"]), "type": str(c["type"]),
                                "nullable": c.get("nullable", True), "primary_key": c["name"] in primary,
                                "comment": c.get("comment") or ""}
                               for c in inspector.get_columns(name, schema=schema)]
                    indexes = [{"name": i["name"], "definition": "{} ({})".format(
                        i["name"], ", ".join(i.get("column_names") or []))}
                               for i in inspector.get_indexes(name, schema=schema)]
                    tables.append({"schema": schema, "name": actual_name(name), "columns": columns, "indexes": indexes})
        return tables

    async def _mysql_schema(self):
        if not self.mysql_database:
            return []
        async with self.engine.connect() as conn:
            columns = (await conn.execute(text("""
                SELECT TABLE_SCHEMA AS table_schema, TABLE_NAME AS table_name,
                       COLUMN_NAME AS column_name, COLUMN_TYPE AS data_type,
                       IS_NULLABLE AS is_nullable, COLUMN_KEY AS column_key,
                       COLUMN_COMMENT AS column_comment
                FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=:database
                ORDER BY TABLE_NAME, ORDINAL_POSITION
            """), {"database": self.mysql_database})).mappings().all()
            indexes = (await conn.execute(text("""
                SELECT TABLE_NAME AS table_name, INDEX_NAME AS index_name,
                       COLUMN_NAME AS column_name, NON_UNIQUE AS non_unique
                FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=:database
                ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
            """), {"database": self.mysql_database})).mappings().all()
        tables = {}
        for column in columns:
            name = column["table_name"]
            tables.setdefault(name, {"schema": self.mysql_database, "name": name, "columns": [], "indexes": []})
            tables[name]["columns"].append({
                "name": column["column_name"], "type": column["data_type"],
                "nullable": column["is_nullable"] == "YES",
                "primary_key": column["column_key"] == "PRI", "comment": column["column_comment"],
            })
        groups = {}
        for index in indexes:
            key = (index["table_name"], index["index_name"])
            groups.setdefault(key, {"columns": [], "unique": not index["non_unique"]})
            groups[key]["columns"].append(index["column_name"] or "(表达式)")
        for (table_name, index_name), index in groups.items():
            if table_name in tables:
                tables[table_name]["indexes"].append({
                    "name": index_name,
                    "definition": "{}{} ({})".format("UNIQUE " if index["unique"] else "",
                                                     index_name, ", ".join(index["columns"])),
                })
        return list(tables.values())

    def _sqlite_schema(self):
        with self.sqlite_connection() as conn:
            result = []
            names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for name in names:
                safe = name.replace('"', '""')
                columns = [{"name": r[1], "type": r[2], "nullable": not bool(r[3] or r[5]), "primary_key": bool(r[5])}
                           for r in conn.execute('PRAGMA table_info("{}")'.format(safe))]
                indexes = [{"name": r[0], "definition": r[1]} for r in
                           conn.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (name,))]
                result.append({"schema": "main", "name": name, "columns": columns, "indexes": indexes})
            return result

    async def execute(self, sql, explain=False):
        async with self._switch_lock:
            return await self._execute(sql, explain)

    async def _execute(self, sql, explain=False):
        # Schema 校验、执行与切库互斥，整个查询绑定同一个数据源。
        engine = self.engine
        dialect = self.dialect
        mysql_database = self.mysql_database
        started = time.monotonic()
        if dialect == "mysql" and not mysql_database:
            raise QueryRejected("请先在「数据库 & 查询」中选择一个业务数据库。")
        tables = await self._schema()
        allowed = {(t["schema"], t["name"]) for t in tables}
        default_schema = mysql_database if dialect == "mysql" else self.schemas.split(",")[0].strip()
        normalized = validate_sql(sql, dialect, allowed, default_schema or None)
        if engine and dialect == "mysql":
            output = await asyncio.wait_for(self._mysql_execute(engine, normalized, explain),
                                            timeout=settings.query_timeout + 1)
        elif engine and self.kind in {"mssql", "oracle"}:
            output = await in_thread(self._sync_execute, normalized, explain)
        elif engine:
            prefix = "EXPLAIN (FORMAT TEXT) " if explain else ""
            async with engine.connect() as conn:
                async with conn.begin():
                    await conn.execute(text("SET TRANSACTION READ ONLY"))
                    await conn.execute(text("SELECT set_config('statement_timeout', :timeout, true)"),
                                       {"timeout": str(settings.query_timeout * 1000)})
                    await conn.execute(text("SELECT set_config('search_path', :schemas, true)"),
                                       {"schemas": self.schemas})
                    # 流式游标避免将整个结果集载入内存。
                    async with conn.stream(text(prefix + normalized)) as result:
                        keys = list(result.keys())
                        rows = []
                        async for row in result:
                            rows.append(list(row))
                            if len(rows) > settings.row_limit:
                                break
                    output = {"columns": keys, "rows": rows[:settings.row_limit],
                              "truncated": len(rows) > settings.row_limit}
        else:
            output = await asyncio.wait_for(in_thread(self._sqlite_execute, normalized, explain),
                                            timeout=settings.query_timeout + 1)
        output.update({"elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                       "limit": settings.row_limit, "sql": normalized, "explain": explain})
        return output

    async def _mysql_execute(self, engine, sql, explain):
        statement = ("EXPLAIN FORMAT=JSON " + sql) if explain else mysql_limited_sql(sql)
        async with engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text("SET TRANSACTION READ ONLY"))
                timeout_statement = ("SET SESSION max_statement_time = {}".format(settings.query_timeout)
                                     if self.kind == "mariadb" else
                                     "SET SESSION MAX_EXECUTION_TIME = {}".format(settings.query_timeout * 1000))
                await conn.execute(text(timeout_statement))
                async with conn.stream(text(statement)) as result:
                    keys = list(result.keys())
                    rows = []
                    async for row in result:
                        rows.append(list(row))
                        if len(rows) > settings.row_limit:
                            break
                return {"columns": keys, "rows": rows[:settings.row_limit],
                        "truncated": len(rows) > settings.row_limit}

    def _sync_execute(self, sql, explain):
        if explain and self.kind == "oracle":
            raise QueryRejected("Oracle EXPLAIN PLAN 会写入计划表，当前只读工具暂不提供；可上传执行计划供分析。")
        # NullPool 使每次查询使用独立连接；异常和计划模式均不会遗留会话状态。
        with self.engine.connect() as conn:
            if self.kind == "oracle":
                conn.connection.driver_connection.call_timeout = settings.query_timeout * 1000
                conn.execute(text("SET TRANSACTION READ ONLY"))
            if explain:
                # SHOWPLAN 必须独立批次，且不执行用户查询。
                conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                conn.execute(text("SET SHOWPLAN_XML ON"))
                try:
                    result = conn.execute(text(sql))
                    rows = result.fetchmany(settings.row_limit + 1)
                    columns = list(result.keys())
                finally:
                    conn.execute(text("SET SHOWPLAN_XML OFF"))
            else:
                result = conn.execute(text(limited_sql(sql, self.dialect)))
                rows = result.fetchmany(settings.row_limit + 1)
                columns = list(result.keys())
            return {"columns": columns, "rows": [list(row) for row in rows[:settings.row_limit]],
                    "truncated": len(rows) > settings.row_limit}

    def _sqlite_execute(self, sql, explain):
        started = time.monotonic()
        with self.sqlite_connection() as conn:
            conn.set_progress_handler(lambda: int(time.monotonic() - started > settings.query_timeout), 1000)
            # 账号/只读连接、AST 与 SQLite authorizer 三层防护。
            def authorize(action, arg1, arg2, database_name, trigger):
                if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE}:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").upper() in SAFE_FUNCTIONS:
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY
            conn.set_authorizer(authorize)
            cursor = conn.execute(("EXPLAIN QUERY PLAN " if explain else "") + sql)
            rows = cursor.fetchmany(settings.row_limit + 1)
            return {"columns": [d[0] for d in cursor.description], "rows": [list(r) for r in rows[:settings.row_limit]],
                    "truncated": len(rows) > settings.row_limit}

    async def close(self):
        if self.engine:
            if self.kind in {"mssql", "oracle"}:
                await in_thread(self.engine.dispose)
            else:
                await self.engine.dispose()
            self.engine = None


database = Database()
