from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def alembic_config(database: Path) -> Config:
    project_root = Path(__file__).parents[1]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database.as_posix()}")
    return config


def test_alembic_creates_fresh_database(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"
    command.upgrade(alembic_config(database), "head")

    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert {
            "alembic_version",
            "ff_chat_threads",
            "ff_workspaces",
            "ff_code_runs",
            "ff_tool_calls",
            "ff_desktop_runs",
            "ff_desktop_actions",
            "ff_desktop_approvals",
        } <= tables
        tool_columns = {
            item["name"] for item in inspect(engine).get_columns("ff_tool_calls")
        }
        assert {"before_hashes", "after_hashes", "unified_diff"} <= tool_columns
    finally:
        engine.dispose()


def test_alembic_preserves_existing_chat_data(tmp_path: Path) -> None:
    database = tmp_path / "existing.db"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE ff_chat_threads ("
            "chat_id VARCHAR(64) PRIMARY KEY, tenant_id VARCHAR(100) NOT NULL, "
            "user_id VARCHAR(100) NOT NULL, title VARCHAR(200) NOT NULL, "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE ff_chat_messages ("
            "message_id VARCHAR(64) PRIMARY KEY, chat_id VARCHAR(64) NOT NULL, "
            "role VARCHAR(20) NOT NULL, content TEXT NOT NULL, mode VARCHAR(20) NOT NULL, "
            "task_id VARCHAR(64), metadata JSON NOT NULL, created_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO ff_chat_threads VALUES "
            "('chat_existing','default','local-user','保留的数据',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO ff_chat_messages VALUES "
            "('msg_existing','chat_existing','user','hello','chat',NULL,'{}',CURRENT_TIMESTAMP)"
        )
    engine.dispose()

    command.upgrade(alembic_config(database), "head")

    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        inspector = inspect(engine)
        assert "workspace_id" in {
            item["name"] for item in inspector.get_columns("ff_chat_threads")
        }
        assert "code_run_id" in {
            item["name"] for item in inspector.get_columns("ff_chat_messages")
        }
        assert "desktop_run_id" in {
            item["name"] for item in inspector.get_columns("ff_chat_messages")
        }
        with engine.connect() as connection:
            title = connection.exec_driver_sql(
                "SELECT title FROM ff_chat_threads WHERE chat_id='chat_existing'"
            ).scalar_one()
            message = connection.exec_driver_sql(
                "SELECT content FROM ff_chat_messages WHERE message_id='msg_existing'"
            ).scalar_one()
        assert title == "保留的数据"
        assert message == "hello"
    finally:
        engine.dispose()
