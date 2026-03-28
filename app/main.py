from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.routers import agent, campaigns, metrics, alerts, training, v1_ai
from app.scheduler.jobs import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Arranque: iniciar scheduler de métricas y alertas
    start_scheduler()
    yield
    # Cierre limpio
    stop_scheduler()


app = FastAPI(
    title="Trafiker Agent API",
    description="Agente de IA especialista en marketing digital y Meta Ads",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agent.router, prefix="/agent", tags=["Agente"])
app.include_router(campaigns.router, prefix="/campaigns", tags=["Campañas"])
app.include_router(metrics.router, prefix="/metrics", tags=["Métricas"])
app.include_router(alerts.router, prefix="/alerts", tags=["Alertas"])
app.include_router(training.router, prefix="/training", tags=["Entrenamiento"])
app.include_router(v1_ai.router, prefix="/v1/ai", tags=["v1 AI"])


@app.get("/", tags=["Health"])
def root():
    return {"status": "ok", "agent": "Trafiker v1.0"}


@app.get("/health", tags=["Health"])
def health():
    return {"status": "healthy", "version": "1.2.0", "sdk": "google-genai"}
