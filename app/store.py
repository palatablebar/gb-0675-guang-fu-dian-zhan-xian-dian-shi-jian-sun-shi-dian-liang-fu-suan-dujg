import sqlite3, json, hashlib
from pathlib import Path
from datetime import datetime, timezone
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("CREATE TABLE IF NOT EXISTS declarations (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, basis_digest TEXT NOT NULL, basis TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(run_id,basis_digest))")
    con.execute("CREATE TABLE IF NOT EXISTS declare_current (run_id TEXT PRIMARY KEY, declaration_id TEXT NOT NULL REFERENCES declarations(id))")
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

def _declaration_id(run_id,digest):
    return hashlib.sha256(f'{run_id}:{digest}'.encode()).hexdigest()[:16]

def _declaration_json(row):
    return {'id':row['id'],'run_id':row['run_id'],'basis_digest':row['basis_digest'],
            'basis':json.loads(row['basis']),'result':json.loads(row['result']),'created_at':row['created_at']}

def _current(con,run_id):
    return con.execute('SELECT d.* FROM declarations d JOIN declare_current c ON c.declaration_id=d.id WHERE c.run_id=?',(run_id,)).fetchone()

def _require_run(con,run_id):
    if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
        raise BusinessError('NOT_FOUND','批次不存在',404)

def _insert_declaration(con,run_id,digest,basis,result):
    row_id=_declaration_id(run_id,digest)
    con.execute('INSERT INTO declarations(id,run_id,basis_digest,basis,result,created_at) VALUES(?,?,?,?,?,?)',
                (row_id,run_id,digest,json.dumps(basis,ensure_ascii=False),json.dumps(result,ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat()))
    return con.execute('SELECT * FROM declarations WHERE id=?',(row_id,)).fetchone()

def get_declaration(path,run_id):
    with connect(path) as con:
        _require_run(con,run_id)
        row=_current(con,run_id)
        if not row: raise BusinessError('DECLARATION_MISSING','尚未形成申报',404)
        history=[{'id':r['id'],'basis_digest':r['basis_digest'],'created_at':r['created_at'],
                  'decision':json.loads(r['result'])['ruling']['decision']}
                 for r in con.execute('SELECT * FROM declarations WHERE run_id=? ORDER BY rowid',(run_id,))]
        return {'current':_declaration_json(row),'versions':len(history),'history':history}

def form_declaration(path,run_id,basis,digest,result):
    """形成申报：同批次同依据重试直接返回原申报，不新增也不覆盖原始凭据。"""
    with connect(path) as con:
        _require_run(con,run_id)
        row=_current(con,run_id)
        if row:
            if row['basis_digest']==digest:
                return {**_declaration_json(row),'created':False}
            raise BusinessError('BASIS_CONFLICT','已存在申报，请通过依据更新变更依据',409)
        row=_insert_declaration(con,run_id,digest,basis,result)
        con.execute('INSERT INTO declare_current(run_id,declaration_id) VALUES(?,?)',(run_id,row['id']))
        return {**_declaration_json(row),'created':True}

def update_basis(path,run_id,basis,digest,result):
    """依据更新：同一事务写入新版本并切换当前指针；任何一步失败整体回滚，旧版保持当前。
    同一依据重试或回切旧依据时不新增申报，历史版本与原始凭据全部保留。"""
    with connect(path) as con:
        _require_run(con,run_id)
        row=_current(con,run_id)
        if row and row['basis_digest']==digest:
            return {**_declaration_json(row),'created':False}
        existing=con.execute('SELECT * FROM declarations WHERE run_id=? AND basis_digest=?',(run_id,digest)).fetchone()
        target=existing or _insert_declaration(con,run_id,digest,basis,result)
        if row: con.execute('UPDATE declare_current SET declaration_id=? WHERE run_id=?',(target['id'],run_id))
        else: con.execute('INSERT INTO declare_current(run_id,declaration_id) VALUES(?,?)',(run_id,target['id']))
        return {**_declaration_json(target),'created':existing is None}
