from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.web import render
from app import store
from app.errors import BusinessError
from app.modules.declare.rules import normalize_basis, basis_from_context, basis_digest, build_declaration

router=APIRouter()

def _db():
    from app.main import DB_PATH
    return DB_PATH

def _events(run):
    return {r['id']:r for r in run['records'] if r['kind']=='EVENT'}

def _context_basis(run):
    raw=basis_from_context(run['context'])
    if raw is None: raise BusinessError('BASIS_MISSING','该批上下文缺少申报依据')
    return normalize_basis(raw,set(_events(run)))

@router.get('/declare')
def page(request:Request):
    record=None
    run_id=request.query_params.get('run_id')
    if run_id:
        try: record=store.get_declaration(_db(),run_id)
        except BusinessError: record=None
    return render(request,'declare',{'record':record})

@router.post('/api/declare')
def calculate(payload:dict=Body(default={})):
    return evaluate(payload,until=8)

@router.post('/api/runs/{run_id}/declare')
def form(run_id:str):
    run=store.get(_db(),run_id)
    basis=_context_basis(run)
    result=build_declaration(run['energy_rows'],basis,_events(run))
    return store.form_declaration(_db(),run_id,basis,basis_digest(basis),result)

@router.put('/api/runs/{run_id}/declare/basis')
def update(run_id:str,payload:dict=Body(...)):
    run=store.get(_db(),run_id)
    # 先整体校验新依据，失败直接报错，不触碰库中旧版。
    basis=normalize_basis(payload.get('basis'),set(_events(run)),strict_events=True)
    result=build_declaration(run['energy_rows'],basis,_events(run))
    return store.update_basis(_db(),run_id,basis,basis_digest(basis),result)

@router.get('/api/runs/{run_id}/declare')
def current(run_id:str):
    return store.get_declaration(_db(),run_id)
