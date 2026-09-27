"""FastAPI application and lifespan management."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from code_executor.models import Settings
from code_executor.session_pool import SessionPool


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    pool = SessionPool(settings)
    app.state.settings = settings
    app.state.pool = pool

    async def _reaper():
        while True:
            await asyncio.sleep(60)
            await pool.cleanup_idle()
            pool.cleanup_old_artifacts()

    task = asyncio.create_task(_reaper())
    yield
    task.cancel()
    await pool.shutdown_all()


app = FastAPI(lifespan=lifespan)

# Import routes so they are registered on the app
from code_executor.api import routes as _routes  # noqa: E402, F401
