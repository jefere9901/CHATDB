import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_env()


def data_directory():
    value = Path(os.getenv("APP_DATA_DIR", "data"))
    return (value if value.is_absolute() else ROOT / value).resolve()


def save_database_url(value, database_type=None, schemas=None):
    """只修改数据库配置，保留模型密钥与其他用户设置。"""
    if "\n" in value or "\r" in value:
        raise ValueError("数据库连接配置不能包含换行符。")
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    updates = {"DATABASE_URL": value}
    if database_type is not None:
        updates["DATABASE_TYPE"] = database_type
    if schemas is not None:
        updates["DATABASE_SCHEMAS"] = schemas
    if any("\n" in v or "\r" in v for v in updates.values()):
        raise ValueError("数据库连接配置不能包含换行符。")
    rewritten = []
    for line in lines:
        if line.partition("=")[0].strip() not in updates:
            rewritten.append(line)
    rewritten.extend(key + "=" + val for key, val in updates.items())
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix=".env-", dir=ROOT)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(rewritten) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass
class Settings:
    api_key: str = os.getenv("LLM_API_KEY", "")
    base_url: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")
    access_token: str = os.getenv("APP_ACCESS_TOKEN", "")
    database_url: str = os.getenv("DATABASE_URL", "")
    database_type: str = os.getenv("DATABASE_TYPE", "")
    schemas: str = os.getenv("DATABASE_SCHEMAS", "public")
    query_timeout: int = max(1, min(60, int(os.getenv("QUERY_TIMEOUT_SECONDS", "10"))))
    row_limit: int = max(1, min(1000, int(os.getenv("QUERY_ROW_LIMIT", "200"))))
    data_dir: Path = data_directory()


settings = Settings()
