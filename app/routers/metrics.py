from fastapi import APIRouter

router = APIRouter()


@router.get("/")
def list_metrics():
    return {"metrics": [], "message": "Módulo en construcción"}
