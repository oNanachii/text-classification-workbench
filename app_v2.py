"""Versioned SMS workbench. Public inference requires no PyTorch or pickle loading."""
from collections import defaultdict,deque
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
import threading
import time
from typing import Literal
from fastapi import FastAPI,Request,HTTPException,Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse,RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,ConfigDict,Field
from dataflow import DataStore,Conflict,MAX_CSV_BYTES,read_json
import experiments
from decision import DecisionRequest, DecisionService

ROOT=Path(__file__).resolve().parent
CSP="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"

class Predict(BaseModel):
    model_config=ConfigDict(extra='forbid')
    text:str=Field(strict=True,min_length=1,max_length=2000)

class CSVPreview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    csv:str=Field(strict=True,max_length=MAX_CSV_BYTES)
    source:str=Field(strict=True,min_length=1,max_length=200)

class Confirm(BaseModel):
    model_config=ConfigDict(extra='forbid')
    preview_id:str=Field(pattern=r'^import-[a-f0-9]{64}$')

class Review(BaseModel):
    model_config=ConfigDict(extra='forbid')
    label:Literal['ham','spam']
    note:str=Field(strict=True,max_length=500)
    expected_revision:int=Field(strict=True,ge=0)

class Selection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    run_id:str=Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,95}$')
    kind:Literal['lr','mlp16']
    threshold:float=Field(strict=True,ge=0,le=1,allow_inf_nan=False)
    expected_revision:int=Field(strict=True,ge=0)

class Rollback(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision:int=Field(strict=True,ge=0)


def create_app(root=None,admin_token=None,public=None,decision_service=None):
    directory=Path(root or os.environ.get('SMS_LAB_ROOT',ROOT/'var')).resolve()
    token=admin_token if admin_token is not None else os.environ.get('SMS_LAB_ADMIN_TOKEN','')
    public=(os.environ.get('SMS_LAB_PUBLIC','false').lower()=='true') if public is None else public
    store=None if public else DataStore(directory)
    @asynccontextmanager
    async def lifespan(app):
        selected,model=experiments.load_selected(directory)
        app.state.loaded=selected;app.state.model=model
        yield
        app.state.model=None
    app=FastAPI(title='Text Model Lab',version='2.0.0',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url='/api/openapi.json')
    app.state.loaded=None;app.state.model=None
    decisions=decision_service or DecisionService()
    rates=defaultdict(deque);rate_lock=threading.Lock();predict_slots=threading.BoundedSemaphore(2)
    def is_operator(request):
        return bool(token) and hmac.compare_digest(request.headers.get('authorization','').encode(),('Bearer '+token).encode())
    def operator(request):
        if not token:raise HTTPException(503,'Operator token is not configured')
        if not is_operator(request):raise HTTPException(401,'Operator authentication required')
    def writable(request):
        if public:raise HTTPException(403,'Public deployment is read-only')
        operator(request)
        if not store.prepared():raise HTTPException(503,'Prepare the local dataset first')
    @app.middleware('http')
    async def boundary(request,call_next):
        def secured(response):
            response.headers['Content-Security-Policy']=CSP
            response.headers['X-Content-Type-Options']='nosniff';response.headers['Referrer-Policy']='no-referrer';response.headers['X-Robots-Tag']='noindex, nofollow'
            if request.url.path.startswith('/api/'):response.headers['Cache-Control']='no-store'
            return response
        maximum=16*1024 if request.url.path=='/api/decision' else 10*1024*1024
        try:
            length=int(request.headers.get('content-length','0'))
        except ValueError:return secured(JSONResponse({'error':'Invalid request length'},400))
        if length<0:return secured(JSONResponse({'error':'Invalid request length'},400))
        if length>maximum:return secured(JSONResponse({'error':'Request too large'},413))
        if request.method in {'POST','PATCH'}:
            if request.url.path not in {'/api/predict','/api/decision'} and not is_operator(request):
                return secured(JSONResponse({'error':'Operator authentication required'},401))
            if request.url.path=='/api/decision' and request.headers.get('content-type','').split(';')[0].strip().lower()!='application/json':
                return secured(JSONResponse({'error':'JSON content type required'},415))
            chunks=[];size=0
            async for part in request.stream():
                size+=len(part)
                if size>maximum:return secured(JSONResponse({'error':'Request too large'},413))
                chunks.append(part)
            request._body=b''.join(chunks)
        response=await call_next(request)
        return secured(response)
    @app.exception_handler(RequestValidationError)
    async def validation_error(_r,_e):return JSONResponse({'error':'Fields do not match the request contract'},422)
    @app.exception_handler(Conflict)
    async def conflict(_r,e):return JSONResponse({'error':str(e)},409)
    @app.exception_handler(ValueError)
    async def invalid(_r,e):return JSONResponse({'error':str(e)[:240]},400)
    @app.exception_handler(LookupError)
    async def missing(_r,_e):return JSONResponse({'error':'Record not found'},404)
    @app.get('/api/config')
    def config(request:Request):
        return {'public':public,'operator_authenticated':is_operator(request),'operator_configured':bool(token),'dataset_prepared':bool(store and store.prepared()),'scope':'English SMS ham/spam; local CPU training; public inference only','version':'2.1.0','decision':decisions.config(public=public,authenticated=is_operator(request))}
    @app.get('/api/health')
    def health():return {'status':'ok' if app.state.model is not None else 'model_not_selected','model_ready':app.state.model is not None,'public':public,'loaded':app.state.loaded,'runtime':'numpy numeric format; no torch or pickle inference'}
    @app.get('/api/service')
    def service(request:Request):
        r=experiments.registry(directory)
        return {'loaded':app.state.loaded,'selected':r['selected'],'revision':r['revision'],'restart_required':r['selected']!=app.state.loaded,'history':r['history'] if is_operator(request) else []}
    @app.post('/api/predict')
    def predict(body:Predict,request:Request):
        if app.state.model is None:raise HTTPException(503,'No model is loaded')
        key=request.client.host if request.client else 'local'
        with rate_lock:
            q=rates[key];stamp=time.monotonic()
            while q and q[0]<stamp-60:q.popleft()
            if len(q)>=30:raise HTTPException(429,'Please wait before predicting again')
            q.append(stamp)
        if not predict_slots.acquire(blocking=False):raise HTTPException(429,'Prediction capacity is busy')
        try:
            return app.state.model.predict(body.text,app.state.loaded['threshold'])|{'model_version':app.state.loaded['run_id'],'model':app.state.loaded['kind'],'snapshot_id':app.state.loaded['snapshot_id'],'dataset_id':app.state.loaded['dataset_id'],'scope':'English SMS example; no live-traffic accuracy guarantee'}
        finally:predict_slots.release()
    @app.post('/api/decision')
    async def decision(body:DecisionRequest,request:Request):
        key=decisions.authorize(body,request,public=public,authenticated=is_operator(request))
        decisions.acquire(request.client.host if request.client else 'unknown')
        try:
            return await decisions.decide(body,app.state.model,app.state.loaded,key)
        finally:
            key=None
            decisions.release()
    @app.get('/api/final-report')
    def final_report(request:Request):
        if not public:operator(request)
        path=directory/'final-test-report.json'
        return {'report':read_json(path) if path.is_file() else None}
    @app.get('/api/runs')
    def runs(request:Request):
        if not public:operator(request)
        return {'runs':experiments.list_runs(directory)}
    @app.get('/api/dataset')
    def dataset(request:Request):
        writable(request)
        return {'manifest':store.require_prepared()['manifest'],'samples':store.samples(limit=1)['total'],'snapshots':store.snapshots()}
    @app.get('/api/samples')
    def samples(request:Request,limit:int=Query(20,ge=1,le=100),offset:int=Query(0,ge=0),q:str=Query('',max_length=200),label:Literal['','ham','spam']='',pending:bool=False):
        writable(request);return store.samples(limit,offset,q,label,pending)
    @app.get('/api/samples/{identity}')
    def sample(identity:str,request:Request):writable(request);return store.sample(identity)
    @app.patch('/api/samples/{identity}')
    def review(identity:str,body:Review,request:Request):writable(request);return store.review(identity,body.label,body.note,body.expected_revision)
    @app.post('/api/imports/preview')
    def preview(body:CSVPreview,request:Request):writable(request);return store.preview_csv(body.csv,body.source)
    @app.post('/api/imports/confirm')
    def confirm(body:Confirm,request:Request):writable(request);return store.confirm_import(body.preview_id)
    @app.post('/api/snapshots')
    def snapshot(request:Request):writable(request);return store.snapshot()
    @app.post('/api/service/select')
    def select(body:Selection,request:Request):
        writable(request);return experiments.select(directory,body.run_id,body.kind,body.threshold,body.expected_revision)
    @app.post('/api/service/rollback')
    def rollback(body:Rollback,request:Request):writable(request);return experiments.rollback(directory,body.expected_revision)
    web=ROOT/'frontend'/'dist'
    if web.is_dir():
        app.mount('/workbench',StaticFiles(directory=web,html=True),name='workbench')
        @app.get('/')
        def index():return RedirectResponse('/workbench/')
    return app
