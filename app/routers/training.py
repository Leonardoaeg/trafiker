from fastapi import APIRouter

router = APIRouter()


@router.get("/")
def training_status():
    return {"message": "Módulo en construcción"}
