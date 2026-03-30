"""
数据库连接管理和常用 CRUD 操作。
"""
from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from src.config import settings
from src.logger import get_logger
from src.models.schema import Base, SourceCredibility
from src.config import SOURCE_CREDIBILITY

logger = get_logger(__name__)

engine = create_engine(
    settings.db_url,
    connect_args={"check_same_thread": False},  # SQLite 多线程
    echo=False,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    """初始化数据库：建表 + 启用 WAL 模式 + 种入初始数据源可信度"""
    Base.metadata.create_all(bind=engine)

    # 启用 WAL 模式，防止意外断开损坏数据库（移动硬盘场景）
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.commit()

    _seed_source_credibility()
    logger.info("数据库初始化完成: [bold]%s[/bold]", settings.db_path)


def _seed_source_credibility() -> None:
    """将 config.py 中定义的初始可信度写入 source_credibility 表（跳过已存在的）"""
    with get_db() as db:
        for source_name, score in SOURCE_CREDIBILITY.items():
            existing = db.query(SourceCredibility).filter_by(source_name=source_name).first()
            if not existing:
                db.add(SourceCredibility(source_name=source_name, base_score=score))
        db.commit()


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """获取数据库 Session 上下文管理器"""
    db: Session = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_disk_available() -> bool:
    """检查数据库所在路径是否可访问（防止移动硬盘未插入时启动）"""
    from pathlib import Path
    db_path = Path(settings.db_path)
    if not db_path.parent.exists():
        logger.error(
            "数据库目录不存在: [red]%s[/red]\n"
            "请检查移动硬盘是否已连接，或修改 .env 中的 DB_PATH",
            db_path.parent,
        )
        return False
    return True
