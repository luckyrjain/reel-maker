from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from api.db import get_db
from api import models
from api.oauth import InstagramOAuth, SUPPORTED_PROVIDERS, consume_state, get_oauth_provider, new_state

router = APIRouter()
templates = Jinja2Templates(directory="ui/templates")


def _require_known_provider(provider: str) -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")


@router.get("/credentials", response_class=HTMLResponse)
def list_credentials(request: Request, db: Session = Depends(get_db)):
    connected = {
        c.provider: c
        for c in db.query(models.Credential).filter(models.Credential.provider.in_(SUPPORTED_PROVIDERS)).all()
    }
    return templates.TemplateResponse(
        request, "credentials.html",
        {"connected": connected, "providers": SUPPORTED_PROVIDERS},
    )


@router.get("/credentials/{provider}/authorize")
def authorize(provider: str):
    _require_known_provider(provider)
    oauth = get_oauth_provider(provider)
    if not oauth.client_id:
        raise HTTPException(
            status_code=422,
            detail=f"{provider} OAuth is not configured — set the client id/secret in .env",
        )
    return RedirectResponse(oauth.authorize_redirect_url(new_state(provider)))


@router.get("/credentials/{provider}/callback", response_class=HTMLResponse)
def oauth_callback(
    provider: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    _require_known_provider(provider)
    if error:
        raise HTTPException(status_code=400, detail=f"{provider} denied the connection: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code/state in OAuth callback")
    if consume_state(state) != provider:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state — try connecting again")

    oauth = get_oauth_provider(provider)
    try:
        tokens = oauth.exchange_code(code)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Token exchange with {provider} failed: {exc}") from exc

    access_token = tokens.get("access_token")
    if not access_token:
        raise HTTPException(status_code=502, detail=f"{provider} did not return an access token")

    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in")
    provider_account_id = None
    account_label = None
    scope_str = tokens.get("scope")

    if isinstance(oauth, InstagramOAuth):
        # The token exchange_code() returns is short-lived (~1-2h) — swap it for
        # a long-lived (~60 day) one before discovering the Page, since the Page
        # token we ultimately store inherits its lifetime from this one.
        try:
            long_lived = oauth.exchange_long_lived_token(access_token)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Long-lived token exchange failed: {exc}") from exc
        access_token = long_lived.get("access_token", access_token)
        expires_in = long_lived.get("expires_in", expires_in)

        # Instagram publishing rides on the Facebook Page's token, not the
        # user token itself — swap to it here.
        try:
            account = oauth.discover_account(access_token)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        provider_account_id = account["ig_user_id"]
        access_token = account["page_access_token"]
        account_label = f"Instagram business account {provider_account_id}"
        # Graph API's /me/accounts response has no expires_in for the Page token
        # itself — `expires_in` here is still the long-lived *user* token's, kept
        # as the best available approximation. A Page token derived from a
        # long-lived user token inherits that token's effective lifetime in
        # practice (it stops working when the user token does), so this is a
        # reasonable "you may need to reconnect around this date" estimate, not
        # an authoritative expiry for the exact bytes stored below.

    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=int(expires_in)) if expires_in else None
    )

    cred = db.query(models.Credential).filter(models.Credential.provider == provider).first()
    is_new = cred is None
    cred = cred or models.Credential(provider=provider)
    cred.token_blob = access_token
    cred.refresh_token_blob = refresh_token
    cred.provider_account_id = provider_account_id
    cred.account_label = account_label or cred.account_label
    cred.expires_at = expires_at
    cred.scopes = scope_str.split(" ") if scope_str else None
    if is_new:
        db.add(cred)
    db.commit()

    return RedirectResponse("/api/credentials", status_code=303)


@router.post("/credentials/{provider}/disconnect")
def disconnect(provider: str, db: Session = Depends(get_db)):
    _require_known_provider(provider)
    cred = db.query(models.Credential).filter(models.Credential.provider == provider).first()
    if cred:
        db.delete(cred)
        db.commit()
    return RedirectResponse("/api/credentials", status_code=303)
