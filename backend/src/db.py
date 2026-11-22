from contextlib import contextmanager
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from .config import DATABASE_URL

@contextmanager
def get_conn():
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    migrations_dir = Path(__file__).resolve().parents[1] / "migrations"
    with get_conn() as conn:
        for migration in sorted(migrations_dir.glob("*.sql")):
            conn.execute(migration.read_text())
