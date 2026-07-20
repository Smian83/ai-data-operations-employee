"""
Internal/operational endpoints -- not part of the public product API.
Currently just Prometheus metrics for the execution engine. Gated behind
get_current_superuser rather than a separate worker-auth mechanism: this
endpoint only exposes aggregate operational counters (never TaskRun content,
never credentials), and the existing superuser concept is sufficient for
that. See README for the production recommendation to also restrict this
route at the network layer (internal-only ingress) once deployed.
"""
from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_superuser
from app.db.session import get_db
from app.models.enums import TaskRunStatus
from app.models.task_run import TaskRun
from app.models.user import User
from app.worker import metrics

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/metrics")
def get_metrics(
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_superuser),
) -> Response:
    # queue_depth is refreshed on demand here because the FastAPI process
    # does not run the claim/complete loop that otherwise updates this
    # gauge (that only happens inside the worker process) -- a request to
    # this endpoint should never show a stale value just because no worker
    # activity has happened recently.
    depth = db.execute(
        select(func.count()).select_from(TaskRun).where(TaskRun.status == TaskRunStatus.PENDING)
    ).scalar_one()
    metrics.queue_depth.set(depth)

    return Response(content=generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)
