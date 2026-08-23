"""Read-only mandate endpoints — confirms the DB layer works through the API."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Mandate, MandateStatus, Rail

router = APIRouter(prefix="/mandates", tags=["mandates"])


class MandateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_ref: str
    rail: Rail
    amount: int
    status: MandateStatus
    mandate_expiry: datetime
    created_at: datetime


@router.get("", response_model=list[MandateOut])
def list_mandates(db: Session = Depends(get_db)) -> list[Mandate]:
    return db.query(Mandate).order_by(Mandate.id).all()


@router.get("/{mandate_id}", response_model=MandateOut)
def get_mandate(mandate_id: int, db: Session = Depends(get_db)) -> Mandate:
    mandate = db.get(Mandate, mandate_id)
    if mandate is None:
        raise HTTPException(status_code=404, detail="Mandate not found")
    return mandate
