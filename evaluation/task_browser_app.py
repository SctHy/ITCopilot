"""Isolated browser smoke fixture: temporary data, fake Redis and deterministic Agent.

Run with uvicorn evaluation.task_browser_app:app --host 127.0.0.1 --port 8127.
No production directory, notification destinations or model credentials are used.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.main import app as api_app
from evaluation.application import application
from evaluation.shared_tasks import offline_handler


@asynccontextmanager
async def lifespan(app):
    async with application() as (_, state):
        state.task_worker.handle = offline_handler
        state.task_capabilities.notifications.contacts = {}
        state.task_capabilities.notifications.transports = {}
        yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=['http://127.0.0.1:5199'], allow_methods=['*'], allow_headers=['*'])
app.mount('/', api_app)
