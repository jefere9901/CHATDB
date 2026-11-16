"""Connection catalog and URL construction; credentials never enter public responses."""
from pathlib import Path

from sqlalchemy.engine import URL, make_url
from pydantic import BaseModel, Field, SecretStr

from .config import ROOT


DATABASE_TYPES = {
    "mysql": {"label": "MySQL", "port": 3306, "driver": "mysql+aiomysql", "dialect": "mysql", "schema": ""},
    "mariadb": {"label": "MariaDB", "port": 3306, "driver": "mysql+aiomysql", "dialect": "mysql", "schema": ""},
    "postgresql": {"label": "PostgreSQL", "port": 5432, "driver": "postgresql+asyncpg", "dialect": "postgres", "schema": "public"},
    "mssql": {"label": "SQL Server", "port": 1433, "driver": "mssql+pytds", "dialect": "tsql", "schema": "dbo"},
    "oracle": {"label": "Oracle", "port": 1521, "driver": "oracle+oracledb", "dialect": "oracle", "schema": ""},
    "sqlite": {"label": "SQLite", "port": None, "driver": "sqlite", "dialect": "sqlite", "schema": "main"},
}


class ConnectionRequest(BaseModel):
    type: str = Field(max_length=32)
    host: str = Field(default="", max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str = Field(default="", max_length=128)
    password: SecretStr = Field(default_factory=lambda: SecretStr(""))
    reuse_password: bool = False
    database: str = Field(default="", max_length=512)
    schemas: str = Field(default="", max_length=512)
    oracle_mode: str = "service_name"


def connection_url(body, current_url=None, current_type=None):
    if body.type not in DATABASE_TYPES:
        raise ValueError("请选择支持的数据库类型。")
    kind = DATABASE_TYPES[body.type]
    if body.type == "sqlite":
        if not body.database.strip():
            raise ValueError("请填写服务器上的 SQLite 文件路径。")
        path = Path(body.database.strip())
        path = (path if path.is_absolute() else ROOT / path).resolve()
        if not path.is_file():
            raise ValueError("SQLite 文件不存在；请选择已有数据库文件。")
        return URL.create("sqlite", database=str(path))
    if not body.host.strip() or not body.username.strip():
        raise ValueError("请填写主机 IP / 地址与用户名。")
    if body.type not in {"mysql", "mariadb"} and not body.database.strip():
        raise ValueError("请填写数据库名或 Oracle 服务名 / SID。")
    schemas = [s.strip() for s in body.schemas.split(",") if s.strip()]
    if any(not s.replace("_", "").isalnum() or s[0].isdigit() for s in schemas):
        raise ValueError("Schema 仅支持字母、数字和下划线，多个 Schema 用逗号分隔。")
    password = body.password.get_secret_value()
    if body.reuse_password:
        old = make_url(current_url) if current_url else None
        if not old or current_type != body.type or (
            old.host, old.port or kind["port"], old.username
        ) != (body.host.strip(), body.port or kind["port"], body.username.strip()):
            raise ValueError("连接主机或账号已变更，请重新输入密码。")
        password = old.password or ""
    query = {}
    database_name = body.database.strip() or None
    if body.type == "oracle":
        if body.oracle_mode not in {"service_name", "sid"}:
            raise ValueError("Oracle 连接方式必须是服务名或 SID。")
        if body.oracle_mode == "service_name":
            query["service_name"] = database_name
            database_name = None
    return URL.create(kind["driver"], username=body.username.strip(), password=password,
                      host=body.host.strip(), port=body.port or kind["port"],
                      database=database_name, query=query)


def infer_type(url):
    backend = url.get_backend_name()
    return {"postgres": "postgresql", "mariadb": "mariadb"}.get(backend, backend)


def public_connection(url, kind, schemas):
    parsed = make_url(url) if url else None
    return {
        "type": kind, "host": parsed.host or "" if parsed else "",
        "port": parsed.port if parsed else None,
        "username": parsed.username or "" if parsed else "",
        "database": (parsed.query.get("service_name") or parsed.database or "") if parsed else "",
        "schemas": schemas, "has_password": bool(parsed and parsed.password),
        "oracle_mode": "service_name" if parsed and parsed.query.get("service_name") else "sid",
    }


def catalog():
    return [{"type": key, **{k: v for k, v in value.items() if k != "driver"}}
            for key, value in DATABASE_TYPES.items()]
