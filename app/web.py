from fastapi import Request
from fastapi.templating import Jinja2Templates
from app.config import ROOT, META
from app.engine import evaluate
from app import store

templates=Jinja2Templates(directory=str(ROOT/'app/templates'))

def render(request:Request,key,extra=None):
    from app.main import DB_PATH
    run_id=request.query_params.get('run_id')
    if run_id:
        state=store.get(DB_PATH,run_id)
    else:
        state=store.save(DB_PATH,evaluate({'dataset':request.query_params.get('dataset','dirty')}))
    view=state['views'].get(key,{'title':'','rows':[],'metrics':{}})
    context={'meta':META,'key':key,'state':state,'view':view}
    if extra: context.update(extra)
    return templates.TemplateResponse(request=request,name=f'{key}.html',context=context)
