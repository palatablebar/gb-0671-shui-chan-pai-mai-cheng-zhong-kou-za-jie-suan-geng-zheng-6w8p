import sqlite3, json
from pathlib import Path
from datetime import datetime, timezone
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("""CREATE TABLE IF NOT EXISTS recall_publications (
        run_id TEXT NOT NULL,
        order_no TEXT NOT NULL,
        revision INTEGER NOT NULL,
        result TEXT NOT NULL,
        published_at TEXT NOT NULL,
        PRIMARY KEY (run_id, order_no)
    )""")
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    publication=_publication(con,run_id)
    if publication: result['publication']=publication
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def _publication(con, run_id):
    rows=con.execute('SELECT order_no,revision,published_at,result FROM recall_publications WHERE run_id=? ORDER BY rowid DESC',(run_id,)).fetchall()
    if not rows: return None
    latest=dict(rows[0])
    latest['versions']=[dict(r) for r in rows]
    return latest

def publish_recall(path, run_id, revision, recall):
    """原子发布：乐观锁校验 revision，单据与时间戳同事务提交；中断只留下旧版。"""
    order_no=recall['order']['order_no']
    published_at=datetime.now(timezone.utc).isoformat()
    payload=json.dumps({'order':recall['order'],'tickets':recall['tickets'],
                        'voyage_controls':recall['voyage_controls']},
                       ensure_ascii=False,sort_keys=True)
    with connect(path) as con:
        run=con.execute('SELECT revision FROM runs WHERE id=?',(run_id,)).fetchone()
        if not run: raise BusinessError('NOT_FOUND','批次不存在',404)
        if run['revision']!=revision:
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
        existing=con.execute('SELECT 1 FROM recall_publications WHERE run_id=? AND order_no=?',
                             (run_id,order_no)).fetchone()
        if existing:
            return get(path,run_id)
        con.execute('INSERT INTO recall_publications(run_id,order_no,revision,result,published_at) VALUES(?,?,?,?,?)',
                    (run_id,order_no,revision,payload,published_at))
        con.execute("UPDATE runs SET status='召回已发布' WHERE id=?",(run_id,))
    return get(path,run_id)
