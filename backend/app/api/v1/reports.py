"""/reports: predefined reports (the same ones the AI butler runs)."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import DB, CurrentUser
from app.services import reports
from app.services.reports import GroupBy

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/sales-summary")
def sales_summary(
    p: CurrentUser,
    db: DB,
    date_from: date,
    date_to: date,
    group_by: GroupBy = "day",
    location_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    return reports.sales_summary(
        db,
        p,
        date_from=date_from,
        date_to=date_to,
        group_by=group_by,
        location_id=location_id,
        limit=limit,
    )


@router.get("/low-stock")
def low_stock(
    p: CurrentUser,
    db: DB,
    threshold: Decimal = Decimal(5),
    location_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    return reports.low_stock(db, p, threshold=threshold, location_id=location_id, limit=limit)
