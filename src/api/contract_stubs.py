"""The frozen contract — routes whose shape is agreed before their logic exists.

Wave 0 of the beta push freezes every cross-repo contract before any parallel
work starts, because the alternative is three repos negotiating interfaces with
each other mid-build. Lyra generates its typed client from `openapi.json`,
which is generated from this app, so a route absent here cannot be built
against — and a UI team blocked on a backend team is exactly the serialisation
the freeze exists to avoid.

Every route below returns RFC 9457 `not-implemented` with 501. That is a
deliberate, visible placeholder, not a silent one: a client that calls it gets
a machine-readable answer saying so, and the endpoint appears in the schema
with its real request and response models.

**How to retire a stub.** Move the route into the module that owns its concern,
implement it, and delete it from here. This file should shrink to nothing. If
it stops shrinking, the freeze has become a backlog.

Already retired: B.15 (waitlist) — see growth.py / growth_routes.py.
B.17 (moving-markets feed) — see discovery.py / discovery_routes.py.
B.7 (TOTP) — see mfa.py / mfa_routes.py.
B.19 (UI-mode telemetry) — see growth.py / growth_routes.py.
B.11 (explanation calibration) — see evaluation/calibration.py / calibration_routes.py.
B.10 (referrals, quota, intent) — see accounts.py / routes.py (referrals,
quota) and growth.py / growth_routes.py (billing intent). Both routes had
real implementations from the start; this file just never dropped the
now-shadowed stub definitions, so every request to them matched the stub
first and the real handler was unreachable.

No stub remains as of this writing — the router below is empty and kept only
so app.py's `include_router(contract_stub_router)` has nothing to break when
the next stub is added.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .access import get_current_user
from .errors import ErrorType, PmieError

# Same fail-closed default as routes.py and sharing_routes.py: every stub
# below will be a real, identity-scoped endpoint once it's built, so it
# inherits get_current_user now rather than gaining it as an afterthought
# when the 501 is replaced with real logic. A genuinely public stub opts out
# via access.PUBLIC_ROUTES, the only way to do so.
router = APIRouter(prefix="/v1", dependencies=[Depends(get_current_user)])

_RESPONSES: dict[int | str, dict] = {
    501: {
        "description": "Not implemented yet — the contract is frozen, the logic is not."
    }
}


def _stub(task: str) -> None:
    """Fail loudly and machine-readably, naming the task that will land it."""
    raise PmieError(
        ErrorType.NOT_IMPLEMENTED,
        f"This endpoint's contract is frozen but its logic is not built yet ({task}).",
        501,
    )
