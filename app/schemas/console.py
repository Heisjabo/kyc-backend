"""What the admin console's sign-in endpoints take and return.

The console is pangolin-admin, used by Pangolin staff for both products. Its
sign-in reuses the account's email and password; only the response differs, in
saying what the person may do there.
"""

from datetime import datetime

from pydantic import BaseModel


class ConsoleUserResponse(BaseModel):
    id: str
    email: str
    first_name: str
    last_name: str
    role: str
    products: list[str]
    last_login_at: datetime | None


class ConsoleTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: ConsoleUserResponse
