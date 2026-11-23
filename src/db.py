"""存储层：SQLite(WAL)，遵循 docs/技术方案_v0.4.md §3.2

要点：
- 双时间轴：t_valid（事情发生/发布） 与 t_txn（我们得知/入库），永不 UPDATE
- probe_run 四态：ok / zero_hit / unavailable / parse_broken
- snapshot：每次运行冻结一个快照，预测挂在快照上防后视偏差
"""
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone

APP_NAME = "LLMIntelAgent"


def app_dir() -> str:
    """按部署方案：数据放 ~/Library/Application Support/，不写应用目录"""
    override = os.environ.get("LLMINTEL_HOME")
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    base = os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP_NAME)
    os.makedirs(base, exist_ok=True)
    return base


def db_path() -> str:
    return os.path.join(app_dir(), "data.db")


def seed_path() -> str:
    """随包预置的历史数据（首次启动时装到数据目录）"""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "seed.db")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed.db")


_SEED_CACHE: dict | None = None


def seed_status() -> dict:
    """给 UI 用：这份种子数据里有什么、截止到哪天。

    采集期间数据库有写锁，所以：① 带 busy_timeout ② 结果缓存（每次安装不变）。
    """
    global _SEED_CACHE
    if _SEED_CACHE is not None:
        return _SEED_CACHE
    p = db_path()
    if not os.path.exists(p):
        return {"seeded": False}
    conn = sqlite3.connect(p, timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        n = conn.execute("SELECT count(*) FROM doc").fetchone()[0]
        last = conn.execute("SELECT max(substr(t_valid,1,10)) FROM doc").fetchone()[0]
        first = conn.execute("SELECT min(substr(t_valid,1,10)) FROM doc").fetchone()[0]
        _SEED_CACHE = {"seeded": True, "docs": n, "from": first, "to": last}
        return _SEED_CACHE
    except Exception:
        return {"seeded": False}
    finally:
        conn.close()


def config_path() -> str:
    return os.path.join(app_dir(), "config.json")


def now_iso() -> str:
    """统一 UTC ISO8601，字符串排序等价时间序"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 运行记录
CREATE TABLE IF NOT EXISTS run (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT NOT NULL DEFAULT 'running',
  new_items   INTEGER NOT NULL DEFAULT 0,
  sources_ok  INTEGER NOT NULL DEFAULT 0,
  sources_bad INTEGER NOT NULL DEFAULT 0,
  note        TEXT,
  cost_tokens INTEGER NOT NULL DEFAULT 0
);

-- 采集健康度（沉默也是数据点，四态）
CREATE TABLE IF NOT EXISTS probe_run (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       INTEGER NOT NULL REFERENCES run(id),
  source_id    TEXT NOT NULL,
  source_name  TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('ok','zero_hit','unavailable','parse_broken')),
  hit_count    INTEGER NOT NULL DEFAULT 0,
  http_status  INTEGER,
  latency_ms   INTEGER,
  error        TEXT,
  checked_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_probe_run ON probe_run (run_id, source_id);

-- 文档（双时间轴）
CREATE TABLE IF NOT EXISTS doc (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  guid         TEXT NOT NULL UNIQUE,
  url          TEXT NOT NULL,
  source_id    TEXT NOT NULL,
  source_name  TEXT NOT NULL,
  title        TEXT NOT NULL,
  author       TEXT,
  t_valid      TEXT NOT NULL,
  t_txn        TEXT NOT NULL,
  content      TEXT,
  content_hash TEXT,
  fetched_ok   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_doc_valid ON doc (t_valid);

-- 条目（分类 + 打分 + 摘要）
CREATE TABLE IF NOT EXISTS item (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id     INTEGER NOT NULL UNIQUE REFERENCES doc(id),
  category   TEXT NOT NULL,
  score      REAL NOT NULL DEFAULT 0,
  keywords   TEXT NOT NULL DEFAULT '[]',
  summary    TEXT NOT NULL DEFAULT '',
  entities   TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_item_cat ON item (category, score);

-- 快照
CREATE TABLE IF NOT EXISTS snapshot (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  taken_at     TEXT NOT NULL,
  max_item_id  INTEGER NOT NULL,
  run_id       INTEGER NOT NULL
);

-- 周报（按公司归类，每周一份；payload 是成品 JSON，避免每次重算）
CREATE TABLE IF NOT EXISTS weekly (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  week         TEXT NOT NULL UNIQUE,
  start_date   TEXT NOT NULL,
  end_date     TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  mode         TEXT NOT NULL DEFAULT 'rule',
  tokens       INTEGER NOT NULL DEFAULT 0,
  payload      TEXT NOT NULL
);

-- 预测台账（一等公民；本版先建表，结构按方案 §3.2）
CREATE TABLE IF NOT EXISTS prediction (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  claim_text    TEXT NOT NULL,
  deadline      TEXT NOT NULL,
  p             REAL NOT NULL CHECK (p BETWEEN 0.01 AND 0.99),
  snapshot_id   INTEGER REFERENCES snapshot(id),
  basis         TEXT NOT NULL DEFAULT '{}',
  state         TEXT NOT NULL DEFAULT 'open'
                CHECK (state IN ('proposed','open','resolved_hit','resolved_miss','void','unresolvable')),
  resolved_at   TEXT,
  resolved_by   TEXT
);
"""


MIGRATIONS = [
    "ALTER TABLE doc ADD COLUMN coverage_count INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE doc ADD COLUMN fingerprint TEXT",
    "ALTER TABLE doc ADD COLUMN dupe_of INTEGER",
    "ALTER TABLE item ADD COLUMN evidence_n INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE item ADD COLUMN evidence_kind TEXT NOT NULL DEFAULT 'proxy'",
    "ALTER TABLE doc ADD COLUMN read_num INTEGER",
    "ALTER TABLE doc ADD COLUMN like_num INTEGER",
    "ALTER TABLE doc ADD COLUMN group_name TEXT",
    "ALTER TABLE doc ADD COLUMN merge_sim REAL",
    "ALTER TABLE doc ADD COLUMN merged_at TEXT",
    "ALTER TABLE item ADD COLUMN story_company TEXT",
]


def _migrate(conn) -> None:
    for sql in MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_fp ON doc (fingerprint)")
    # 用量表加列：缓存命中量 + 推理思考量（预算按全额算，这两个只作展示/对账）
    for sql in ("ALTER TABLE usage_day ADD COLUMN cached INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE usage_day ADD COLUMN reasoning INTEGER NOT NULL DEFAULT 0"):
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass
    conn.commit()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_usage(conn) -> None:
    """建用量表并补齐后加的列（缓存命中/思考量）。幂等。"""
    conn.execute(USAGE_SCHEMA)
    for sql in ("ALTER TABLE usage_day ADD COLUMN cached INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE usage_day ADD COLUMN reasoning INTEGER NOT NULL DEFAULT 0"):
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass


USAGE_SCHEMA = ("CREATE TABLE IF NOT EXISTS usage_day ("
                "day TEXT NOT NULL, kind TEXT NOT NULL, tokens INTEGER NOT NULL DEFAULT 0,"
                "calls INTEGER NOT NULL DEFAULT 0, cached INTEGER NOT NULL DEFAULT 0,"
                "reasoning INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, kind))")


def add_tokens(n: int, kind: str = "run", cached: int = 0, reasoning: int = 0) -> None:
    """按天累计模型 token 用量（早报/晚报/周报/对话共用一份预算）。

    n 是**全额**：prompt（含缓存命中）+ completion。cached / reasoning 只作对账展示，
    不参与扣减——客户要的口径就是「不管有没有命中缓存」。
    """
    if not n or n <= 0:
        return
    day = time.strftime("%Y-%m-%d")
    conn = connect()
    try:
        _ensure_usage(conn)
        conn.execute(
            "INSERT INTO usage_day (day,kind,tokens,calls,cached,reasoning) VALUES (?,?,?,1,?,?) "
            "ON CONFLICT(day,kind) DO UPDATE SET tokens=tokens+excluded.tokens, "
            "calls=calls+1, cached=cached+excluded.cached, reasoning=reasoning+excluded.reasoning",
            (day, kind, int(n), int(cached), int(reasoning)))
        conn.commit()
    finally:
        conn.close()


def usage_detail() -> dict:
    """今天的分项用量（给系统页/接口用）：总 token、调用次数、缓存命中、思考 token。"""
    day = time.strftime("%Y-%m-%d")
    conn = connect()
    try:
        _ensure_usage(conn)
        r = conn.execute(
            "SELECT COALESCE(SUM(tokens),0) t, COALESCE(SUM(calls),0) c, "
            "COALESCE(SUM(cached),0) ca, COALESCE(SUM(reasoning),0) re "
            "FROM usage_day WHERE day=?", (day,)).fetchone()
        by_kind = {x["kind"]: {"tokens": x["tokens"], "calls": x["calls"]}
                   for x in conn.execute(
                       "SELECT kind, tokens, calls FROM usage_day WHERE day=?", (day,))}
        return {"tokens": int(r["t"] or 0), "calls": int(r["c"] or 0),
                "cached": int(r["ca"] or 0), "reasoning": int(r["re"] or 0),
                "by_kind": by_kind}
    except Exception:
        return {"tokens": 0, "calls": 0, "cached": 0, "reasoning": 0, "by_kind": {}}
    finally:
        conn.close()


def today_tokens(kind: str | None = None) -> int:
    day = time.strftime("%Y-%m-%d")
    conn = connect()
    try:
        conn.execute(USAGE_SCHEMA)
        if kind:
            r = conn.execute("SELECT COALESCE(SUM(tokens),0) FROM usage_day WHERE day=? AND kind=?",
                             (day, kind)).fetchone()
        else:
            r = conn.execute("SELECT COALESCE(SUM(tokens),0) FROM usage_day WHERE day=?", (day,)).fetchone()
        return int(r[0] or 0)
    finally:
        conn.close()


def init_db() -> None:
    # 首次启动：把随包的历史数据装进来，避免客户打开看到空白
    dst = db_path()
    if not os.path.exists(dst):
        sp = seed_path()
        if os.path.exists(sp):
            try:
                shutil.copyfile(sp, dst)
            except Exception:
                pass
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate(conn)
        # 进程被强杀/断电时，run 会永远停在 running，UI 会一直显示"采集进行中"。
        # 启动时统一认领为 interrupted（数据本身不受影响，只是状态收尾）。
        try:
            n = conn.execute(
                "UPDATE run SET status='interrupted', "
                "finished_at=COALESCE(finished_at, ?), note='进程中断，启动时自动收尾' "
                "WHERE status='running'", (now_iso(),)).rowcount
            if n:
                print(f"[db] 收尾 {n} 条中断的运行记录", file=sys.stderr)
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()


def start_run() -> int:
    conn = connect()
    try:
        cur = conn.execute("INSERT INTO run (started_at) VALUES (?)", (now_iso(),))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def finish_run(run_id: int, *, status="ok", new_items=0, ok=0, bad=0, note=None, cost=0) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE run SET finished_at=?, status=?, new_items=?, sources_ok=?, sources_bad=?, note=?, cost_tokens=? WHERE id=?",
            (now_iso(), status, new_items, ok, bad, note, cost, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def log_probe(run_id, source_id, source_name, status, hit_count=0,
              http_status=None, latency_ms=None, error=None) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO probe_run (run_id, source_id, source_name, status, hit_count, http_status, latency_ms, error, checked_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, source_id, source_name, status, hit_count, http_status,
             latency_ms, error, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def last_run() -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def freshness() -> dict:
    """距上次成功采集多久。20h/72h 两档给启动补跑与 UI 提示用。"""
    conn = connect()
    try:
        okrow = conn.execute(
            "SELECT id,started_at,finished_at,status FROM run "
            "WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone()
        anyrow = conn.execute(
            "SELECT id,started_at,finished_at,status FROM run ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    def hours(ts):
        if not ts:
            return None
        try:
            d = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return round((datetime.now(timezone.utc) - d).total_seconds() / 3600, 1)
        except Exception:
            return None

    last_ok = (okrow["finished_at"] or okrow["started_at"]) if okrow else None
    last_any = (anyrow["finished_at"] or anyrow["started_at"]) if anyrow else None
    h = hours(last_ok)
    return {
        "last_ok_at": last_ok,
        "last_run_at": last_any,
        "last_status": (anyrow["status"] if anyrow else None),
        "hours": h,
        "stale_20h": bool(h is None or h >= 20),
        "stale_72h": bool(h is None or h >= 72),
    }
