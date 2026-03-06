"""
Etsy API v3 integration for Ouroboros.

Handles digital product listings for printables on Etsy.
OAuth 2.0 with PKCE for secure authentication.

Required environment variables:
    ETSY_API_KEY        - App client ID from Etsy developer portal
    ETSY_SHARED_SECRET  - App shared secret from Etsy developer portal
    ETSY_SHOP_ID        - Your Etsy shop numeric ID
    ETSY_ACCESS_TOKEN   - OAuth 2.0 Bearer token (listings_w scope)
    ETSY_USER_ID        - Numeric Etsy user ID (embedded in access token)

Setup flow:
    1. Create an app at https://www.etsy.com/developers/your-apps
    2. Set redirect URI: http://localhost:8080/oauth/redirect
    3. Set ETSY_API_KEY and ETSY_SHARED_SECRET in environment
    4. Call etsy_oauth_setup → open the URL in browser → authorize
    5. Copy the `code` from the redirect URL
    6. Call etsy_oauth_exchange with the code
    7. Set ETSY_ACCESS_TOKEN and ETSY_USER_ID from the result
    8. Set ETSY_SHOP_ID from etsy_get_shop output
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_ETSY_BASE = "https://api.etsy.com/v3/application"
_ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
_ETSY_AUTH_URL = "https://www.etsy.com/oauth/connect"
_OAUTH_STATE_PATH = Path("/home/max2/ouroboros_data/etsy_oauth.json")

# Taxonomy: "Craft Supplies & Tools > Paper & Party Supplies > Paper > Printmaking"
# 2078 = "Prints" under Art & Collectibles (common for digital prints/printables)
_PRINTABLES_TAXONOMY_ID = 2078


# ─── Credentials & Auth ────────────────────────────────────────────────────────

def _creds() -> Dict[str, str]:
    """Gather Etsy credentials from environment variables."""
    return {
        "api_key": os.environ.get("ETSY_API_KEY", ""),
        "shared_secret": os.environ.get("ETSY_SHARED_SECRET", ""),
        "shop_id": os.environ.get("ETSY_SHOP_ID", ""),
        "access_token": os.environ.get("ETSY_ACCESS_TOKEN", ""),
        "user_id": os.environ.get("ETSY_USER_ID", ""),
    }


def _require_creds(creds: Dict[str, str], keys: List[str]) -> Optional[str]:
    """Return error string if any required credential is missing."""
    missing = [k for k in keys if not creds.get(k)]
    if missing:
        env_names = ", ".join(f"ETSY_{k.upper()}" for k in missing)
        return (
            f"❌ Missing Etsy credentials: {env_names}\n"
            "Run etsy_oauth_setup for setup instructions."
        )
    return None


def _auth_headers(creds: Dict[str, str]) -> Dict[str, str]:
    """Build required Etsy v3 authentication headers."""
    return {
        "Authorization": f"Bearer {creds['user_id']}.{creds['access_token']}",
        "x-api-key": f"{creds['api_key']}:{creds['shared_secret']}",
    }


def _api(
    method: str,
    path: str,
    *,
    data: Optional[Dict] = None,
    files: Optional[Dict] = None,
    creds: Optional[Dict[str, str]] = None,
) -> Any:
    """Make an authenticated Etsy v3 API request. Raises on non-2xx."""
    if creds is None:
        creds = _creds()

    url = f"{_ETSY_BASE}{path}"
    headers = _auth_headers(creds)

    if files:
        # Multipart upload — let requests set Content-Type
        resp = requests.request(method, url, headers=headers, data=data, files=files, timeout=120)
    else:
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
        resp = requests.request(method, url, headers=headers, data=data, timeout=30)

    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:300]
        raise RuntimeError(f"Etsy {resp.status_code} on {method} {path}: {detail}")

    return resp.json() if resp.content else {}


# ─── OAuth 2.0 + PKCE ──────────────────────────────────────────────────────────

def _etsy_oauth_setup(ctx: ToolContext) -> str:
    """
    Start Etsy OAuth 2.0 setup.
    Returns an authorization URL to open in your browser.
    After authorizing, call etsy_oauth_exchange with the code from the redirect URL.
    """
    creds = _creds()
    if not creds["api_key"]:
        return (
            "❌ ETSY_API_KEY not set.\n\n"
            "Setup steps:\n"
            "1. Go to https://www.etsy.com/developers/your-apps\n"
            "2. Create a new app\n"
            "3. Add redirect URI: http://localhost:8080/oauth/redirect\n"
            "4. Set ETSY_API_KEY and ETSY_SHARED_SECRET in your environment\n"
            "5. Call this tool again"
        )

    # PKCE: code_verifier → SHA-256 → base64url = code_challenge
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    state = secrets.token_hex(16)

    # Persist state for exchange step
    _OAUTH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_OAUTH_STATE_PATH, "w") as f:
        json.dump({"code_verifier": code_verifier, "state": state}, f)

    params = urllib.parse.urlencode({
        "response_type": "code",
        "redirect_uri": "http://localhost:8080/oauth/redirect",
        "scope": "listings_w listings_r shops_r",
        "client_id": creds["api_key"],
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    auth_url = f"{_ETSY_AUTH_URL}?{params}"

    return (
        "🔑 Etsy OAuth Setup\n\n"
        f"1. Open this URL in your browser:\n{auth_url}\n\n"
        "2. Log in to Etsy and authorize the app\n"
        "3. You'll be redirected to http://localhost:8080/oauth/redirect?code=XXX&state=...\n"
        "4. Copy the `code` value from that URL\n"
        "5. Call: etsy_oauth_exchange with code=YOUR_CODE\n\n"
        f"State saved to: {_OAUTH_STATE_PATH}"
    )


def _etsy_oauth_exchange(ctx: ToolContext, code: str) -> str:
    """
    Exchange OAuth authorization code for access token.
    Call after opening the URL from etsy_oauth_setup and copying the code.
    """
    creds = _creds()
    err = _require_creds(creds, ["api_key"])
    if err:
        return err

    if not _OAUTH_STATE_PATH.exists():
        return "❌ OAuth state not found. Run etsy_oauth_setup first."

    with open(_OAUTH_STATE_PATH) as f:
        oauth_state = json.load(f)

    resp = requests.post(
        _ETSY_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": creds["api_key"],
            "redirect_uri": "http://localhost:8080/oauth/redirect",
            "code": code.strip(),
            "code_verifier": oauth_state["code_verifier"],
        },
        timeout=30,
    )

    if not resp.ok:
        return f"❌ Token exchange failed: {resp.status_code} {resp.text}"

    token_data = resp.json()
    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")

    # Etsy embeds numeric user_id as prefix: "12345678.token_value"
    user_id = access_token.split(".")[0] if "." in access_token else ""

    # Save tokens to data dir
    token_path = Path("/home/max2/ouroboros_data/etsy_tokens.json")
    with open(token_path, "w") as f:
        json.dump({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "user_id": user_id,
        }, f, indent=2)

    return (
        "✅ Etsy OAuth complete!\n\n"
        f"User ID: {user_id}\n"
        f"Access Token: {access_token[:20]}...\n"
        f"Tokens saved to: {token_path}\n\n"
        "Add to your .env:\n"
        f"ETSY_USER_ID={user_id}\n"
        f"ETSY_ACCESS_TOKEN={access_token}\n\n"
        "Then call etsy_get_shop to get your ETSY_SHOP_ID."
    )


# ─── Shop info ─────────────────────────────────────────────────────────────────

def _etsy_get_shop(ctx: ToolContext) -> str:
    """
    Get Etsy shop info: name, shop ID, active listing count, URL.
    Also tells you what to set for ETSY_SHOP_ID.
    """
    creds = _creds()
    err = _require_creds(creds, ["api_key", "shared_secret", "access_token", "user_id"])
    if err:
        return err

    try:
        result = _api("GET", f"/users/{creds['user_id']}/shops", creds=creds)
        shops = result.get("results", [])
        if not shops:
            return "❌ No Etsy shop found for this account. Create one at https://www.etsy.com/sell"

        shop = shops[0]
        shop_id = shop.get("shop_id", "")
        shop_name = shop.get("shop_name", "")
        active = shop.get("listing_active_count", 0)
        on_vacation = shop.get("is_vacation", False)

        lines = [
            "🏪 Etsy Shop:",
            f"  Name: {shop_name}",
            f"  ID: {shop_id}",
            f"  Active listings: {active}",
            f"  Status: {'🏖️ On vacation' if on_vacation else '✅ Active'}",
            f"  URL: https://www.etsy.com/shop/{shop_name}",
        ]
        if not creds["shop_id"]:
            lines.append(f"\n💡 Add to .env: ETSY_SHOP_ID={shop_id}")
        return "\n".join(lines)

    except Exception as e:
        return f"❌ {e}"


# ─── Listings ──────────────────────────────────────────────────────────────────

def _etsy_list_listings(
    ctx: ToolContext,
    state: str = "active",
    limit: int = 25,
) -> str:
    """List Etsy shop listings (active, draft, or inactive)."""
    creds = _creds()
    err = _require_creds(creds, ["api_key", "shared_secret", "shop_id", "access_token", "user_id"])
    if err:
        return err

    try:
        result = _api(
            "GET",
            f"/shops/{creds['shop_id']}/listings",
            data={"state": state, "limit": min(limit, 100)},
            creds=creds,
        )
        listings = result.get("results", [])

        if not listings:
            return f"📭 No {state} listings in shop {creds['shop_id']}"

        lines = [f"📋 {state.capitalize()} listings ({len(listings)}):"]
        for item in listings:
            price = item.get("price", {})
            amount = price.get("amount", 0) / (price.get("divisor", 100) or 100)
            lines.append(
                f"  #{item.get('listing_id')} | {item.get('title', '')[:55]} "
                f"| ${amount:.2f} | {item.get('state', '')}"
            )
        return "\n".join(lines)

    except Exception as e:
        return f"❌ {e}"


def _etsy_create_listing(
    ctx: ToolContext,
    title: str,
    description: str,
    price_usd: float,
    pdf_path: str,
    tags: str = "",
    quantity: int = 999,
) -> str:
    """
    Create and publish a digital download listing on Etsy.

    Flow: create draft → upload PDF as digital file → publish to active.
    For printables (coloring pages, worksheets, etc.).

    Args:
        title: Listing title (max 140 chars)
        description: Listing description with details, usage rights, etc.
        price_usd: Price in USD (e.g. 2.99)
        pdf_path: Local path to the PDF file to sell as digital download
        tags: Comma-separated tags (max 13 tags, each max 20 chars)
        quantity: Quantity (default 999 — standard for unlimited digital)
    """
    creds = _creds()
    err = _require_creds(creds, ["api_key", "shared_secret", "shop_id", "access_token", "user_id"])
    if err:
        return err

    pdf = Path(pdf_path)
    if not pdf.exists():
        return f"❌ PDF not found: {pdf_path}"

    price_cents = int(round(price_usd * 100))
    tag_list = [t.strip()[:20] for t in tags.split(",") if t.strip()][:13]

    try:
        # Step 1: Create draft listing
        log.info("Creating Etsy draft: %s @ $%.2f", title[:60], price_usd)
        draft_data: Dict[str, Any] = {
            "quantity": quantity,
            "title": title[:140],
            "description": description,
            "price": price_cents,
            "who_made": "i_did",
            "when_made": "made_to_order",
            "taxonomy_id": _PRINTABLES_TAXONOMY_ID,
            "type": "download",
            "is_personalizable": "false",
        }
        if tag_list:
            draft_data["tags"] = ",".join(tag_list)

        draft = _api("POST", f"/shops/{creds['shop_id']}/listings", data=draft_data, creds=creds)
        listing_id = draft.get("listing_id")
        if not listing_id:
            return f"❌ Draft creation returned no listing_id: {draft}"
        log.info("Draft created: %s", listing_id)

        # Step 2: Upload digital file
        log.info("Uploading PDF (%d KB): %s", pdf.stat().st_size // 1024, pdf.name)
        with open(pdf, "rb") as fh:
            upload = _api(
                "POST",
                f"/shops/{creds['shop_id']}/listings/{listing_id}/files",
                data={"name": pdf.name, "rank": 1},
                files={"file": (pdf.name, fh, "application/pdf")},
                creds=creds,
            )
        file_id = upload.get("listing_file_id", "?")
        log.info("File uploaded: %s", file_id)

        # Step 3: Publish
        published = _api(
            "PATCH",
            f"/shops/{creds['shop_id']}/listings/{listing_id}",
            data={"state": "active"},
            creds=creds,
        )
        final_state = published.get("state", "unknown")

        return (
            f"✅ Etsy listing published!\n"
            f"  Title: {title[:70]}\n"
            f"  Listing ID: {listing_id}\n"
            f"  Price: ${price_usd:.2f}\n"
            f"  State: {final_state}\n"
            f"  File: {pdf.name} ({pdf.stat().st_size // 1024} KB)\n"
            f"  URL: https://www.etsy.com/listing/{listing_id}"
        )

    except Exception as e:
        log.error("Etsy listing creation failed: %s", e)
        return f"❌ Failed to create listing: {e}"


def _etsy_delete_listing(ctx: ToolContext, listing_id: str) -> str:
    """Delete a listing from Etsy shop by listing ID."""
    creds = _creds()
    err = _require_creds(creds, ["api_key", "shared_secret", "shop_id", "access_token", "user_id"])
    if err:
        return err

    try:
        _api("DELETE", f"/shops/{creds['shop_id']}/listings/{listing_id}", creds=creds)
        return f"✅ Listing {listing_id} deleted."
    except Exception as e:
        return f"❌ Failed to delete {listing_id}: {e}"


# ─── Tool registry ────────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="etsy_oauth_setup",
            schema={
                "name": "etsy_oauth_setup",
                "description": (
                    "Start Etsy OAuth 2.0 setup. Generates an authorization URL to open in browser. "
                    "Required first step before any other Etsy tools. "
                    "Needs ETSY_API_KEY and ETSY_SHARED_SECRET set in environment."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            handler=_etsy_oauth_setup,
            timeout_sec=10,
        ),
        ToolEntry(
            name="etsy_oauth_exchange",
            schema={
                "name": "etsy_oauth_exchange",
                "description": (
                    "Exchange Etsy OAuth authorization code for access token. "
                    "Call after opening the URL from etsy_oauth_setup and authorizing."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "Authorization code from the redirect URL (code= parameter)",
                        },
                    },
                    "required": ["code"],
                },
            },
            handler=_etsy_oauth_exchange,
            timeout_sec=30,
        ),
        ToolEntry(
            name="etsy_get_shop",
            schema={
                "name": "etsy_get_shop",
                "description": (
                    "Get Etsy shop info: name, shop ID, active listing count, status. "
                    "Use to get ETSY_SHOP_ID after OAuth setup."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            handler=_etsy_get_shop,
            timeout_sec=15,
        ),
        ToolEntry(
            name="etsy_list_listings",
            schema={
                "name": "etsy_list_listings",
                "description": "List Etsy shop listings by state (active, draft, inactive).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "description": "Listing state filter: active, draft, inactive (default: active)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max listings to return (default 25, max 100)",
                        },
                    },
                    "required": [],
                },
            },
            handler=_etsy_list_listings,
            timeout_sec=15,
        ),
        ToolEntry(
            name="etsy_create_listing",
            schema={
                "name": "etsy_create_listing",
                "description": (
                    "Create and publish a digital download listing on Etsy. "
                    "Uploads a PDF as the product file. Perfect for printables: "
                    "coloring pages, worksheets, activity packs. "
                    "Requires ETSY_API_KEY, ETSY_SHARED_SECRET, ETSY_SHOP_ID, "
                    "ETSY_ACCESS_TOKEN, ETSY_USER_ID."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Listing title (max 140 chars)",
                        },
                        "description": {
                            "type": "string",
                            "description": "Full listing description (include what's in the pack, file format, printing instructions)",
                        },
                        "price_usd": {
                            "type": "number",
                            "description": "Price in USD (e.g. 2.99, 4.99)",
                        },
                        "pdf_path": {
                            "type": "string",
                            "description": "Local path to the PDF file to sell as digital download",
                        },
                        "tags": {
                            "type": "string",
                            "description": "Comma-separated tags for search (max 13, each max 20 chars). E.g. 'coloring page,kids activity,printable'",
                        },
                        "quantity": {
                            "type": "integer",
                            "description": "Quantity available (default 999 — standard for unlimited digital downloads)",
                        },
                    },
                    "required": ["title", "description", "price_usd", "pdf_path"],
                },
            },
            handler=_etsy_create_listing,
            timeout_sec=120,
        ),
        ToolEntry(
            name="etsy_delete_listing",
            schema={
                "name": "etsy_delete_listing",
                "description": "Delete an Etsy listing by listing ID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "listing_id": {
                            "type": "string",
                            "description": "Etsy listing ID to delete",
                        },
                    },
                    "required": ["listing_id"],
                },
            },
            handler=_etsy_delete_listing,
            timeout_sec=15,
        ),
    ]
