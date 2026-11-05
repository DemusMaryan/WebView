"""Read-only, role-protected monitoring. Job ownership routes remain unchanged."""
from fastapi import APIRouter,Depends
from fastapi.responses import FileResponse
from .auth import require_admin
from .config import DATA
from .monitoring import cached_snapshot

router=APIRouter(dependencies=[Depends(require_admin)])


@router.get('/api/admin/monitoring')
def monitoring():
    return cached_snapshot()


@router.get('/admin',include_in_schema=False)
@router.get('/admin/',include_in_schema=False)
def page():
    return FileResponse(DATA/'ui/index.html',headers={'Cache-Control':'private, no-store'})
