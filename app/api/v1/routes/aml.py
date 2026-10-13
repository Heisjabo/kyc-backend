"""AML screening results, for whoever is asking.

A client gets the screenings of its own verifications that found someone or
could not screen. Pangolin staff get those of every client and the searches
staff ran from the console, as the console shows them. Running a search is a
console action (POST /v1/console/aml/search).
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import Caller, current_caller
from app.api.v1.routes import console_aml
from app.db.session import get_session
from app.schemas.console import ConsoleAmlEntity, ConsoleAmlEntryList

router = APIRouter(prefix="/aml", tags=["aml"])


@router.get("/results", response_model=ConsoleAmlEntryList)
def results(
    outcome: str | None = Query(default=None, description="no_match, possible_match, strong_match or unavailable"),
    source: str | None = Query(default=None, description="Pangolin staff only: manual or verification"),
    q: str | None = Query(default=None, max_length=120, description="Name, reference or email"),
    client_id: int | None = Query(default=None, description="Pangolin staff only: one client's screenings"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    caller: Caller = Depends(current_caller),
    db: Session = Depends(get_session),
) -> ConsoleAmlEntryList:
    """Screenings that found someone or could not screen, newest first."""
    return console_aml.aml_results(
        db,
        source=source if caller.staff else "verification",
        outcome=outcome,
        q=q,
        page=page,
        page_size=page_size,
        account_id=caller.scope_for(client_id),
    )


@router.get("/entities/{entity_id}", response_model=ConsoleAmlEntity)
def entity(entity_id: str, caller: Caller = Depends(current_caller)) -> ConsoleAmlEntity:
    """One watchlist entry in full, as it is publicly listed."""
    return console_aml.read_entity(entity_id, caller.account)
