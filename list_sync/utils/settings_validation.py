"""
Validation for settings written through the API.

The setup wizard checks what it is given: it refuses a Seerr URL that points at
cloud metadata, a Discord webhook that isn't on Discord, a sync interval outside
the supported range, a timezone that doesn't exist. POST /api/settings/config
wrote the same settings with none of those checks, so anything the wizard
refused could be stored by posting it to the settings endpoint instead - and
several of those values are later fetched by the server, which turns a stored
setting into an outbound request to wherever the value points.

Both paths validate through here now, so a check added once applies to every way
a setting can be written.

Only the settings that are used as URLs, or that have a range the rest of the
code assumes, are checked. Everything else (list IDs, API keys, flags) is passed
through: this is validation, not an allowlist of setting names, and rejecting an
unknown key would break any caller that saves one.
"""

from typing import Any, Dict

from .url_safety import DISCORD_WEBHOOK_HOSTS, validate_outbound_url

# Settings that name a service this server will request. Each says whether a
# private address is a legitimate target for that particular sink.
_FETCHED_URL_SETTINGS = {
    # A self-hosted Seerr normally lives on a private address, so those are
    # allowed here. Cloud metadata never is, whatever the sink.
    "overseerr_url": {"allow_private": True, "label": "Seerr URL"},
    "seerr_url": {"allow_private": True, "label": "Seerr URL"},
}

# Settings that are only ever displayed or linked to, never fetched. They still
# need a scheme check, because a 'javascript:' value reaching the dashboard as a
# link would run in the browser of whoever clicks it.
_DISPLAY_URL_SETTINGS = {
    "frontend_domain": "Frontend domain",
    "backend_domain": "Backend domain",
    "nuxt_public_api_url": "API URL",
}

# The rest of the code stores intervals in hours and schedules from them, so a
# zero or negative value means a sync loop with no delay in it.
MIN_SYNC_INTERVAL_HOURS = 1
MAX_SYNC_INTERVAL_HOURS = 168  # a week


def validate_discord_webhook(value: str) -> str:
    """
    Check that a Discord webhook URL is one.

    Args:
        value (str): The webhook URL to check

    Returns:
        str: An error message, or "" if the URL is acceptable
    """
    allowed, reason = validate_outbound_url(
        value, allow_private=False, allowed_hosts=DISCORD_WEBHOOK_HOSTS,
    )
    if not allowed:
        return f"Invalid Discord webhook URL: {reason}"

    # Discord's own webhook path. A URL on discord.com that isn't a webhook is
    # still somewhere this server should not be POSTing sync summaries.
    if "/api/webhooks/" not in value:
        return (
            "Invalid Discord webhook URL: it should look like "
            "https://discord.com/api/webhooks/<id>/<token>"
        )

    return ""


def validate_settings(settings: Dict[str, Any]) -> Dict[str, str]:
    """
    Check a batch of settings before any of them are saved.

    Args:
        settings (Dict[str, Any]): The settings to check, keyed by setting name

    Returns:
        Dict[str, str]: One message per rejected setting, keyed by setting name.
            Empty when everything is acceptable.
    """
    errors: Dict[str, str] = {}

    for key, sink in _FETCHED_URL_SETTINGS.items():
        value = settings.get(key)
        if not value or not str(value).strip():
            continue
        allowed, reason = validate_outbound_url(
            str(value).strip(), allow_private=sink["allow_private"],
        )
        if not allowed:
            errors[key] = f"{sink['label']} rejected: {reason}"

    for key, label in _DISPLAY_URL_SETTINGS.items():
        value = settings.get(key)
        if not value or not str(value).strip():
            continue
        text = str(value).strip()
        if not text.lower().startswith(("http://", "https://")):
            errors[key] = f"{label} must start with http:// or https://"

    # Checked whenever a webhook is supplied, not only when notifications are
    # switched on: a webhook saved while disabled is still there to be enabled
    # later, and enabling it is a separate request that sees no URL to check.
    webhook = settings.get("discord_webhook")
    if webhook and str(webhook).strip():
        error = validate_discord_webhook(str(webhook).strip())
        if error:
            errors["discord_webhook"] = error

    if "sync_interval" in settings and settings["sync_interval"] not in (None, ""):
        try:
            interval = float(settings["sync_interval"])
        except (TypeError, ValueError):
            errors["sync_interval"] = "Sync interval must be a number"
        else:
            if not MIN_SYNC_INTERVAL_HOURS <= interval <= MAX_SYNC_INTERVAL_HOURS:
                errors["sync_interval"] = (
                    f"Sync interval must be between {MIN_SYNC_INTERVAL_HOURS} and "
                    f"{MAX_SYNC_INTERVAL_HOURS} hours"
                )

    timezone = settings.get("timezone")
    if timezone and str(timezone).strip():
        from .timezone_utils import normalize_timezone_input

        try:
            if not normalize_timezone_input(str(timezone).strip()):
                errors["timezone"] = "Invalid timezone"
        except Exception as e:
            errors["timezone"] = f"Invalid timezone: {e}"

    return errors
