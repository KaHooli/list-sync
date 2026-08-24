"""
TMDB API client for resolving external IDs to TMDB IDs.

IMDb lists give us IMDb IDs, but Overseerr is addressed by TMDB ID, so
something has to bridge the two. ListSync has always used Trakt for that, but
Trakt now requires a paid VIP subscription to register an API application,
which puts exact ID resolution behind a paywall.

TMDB's own /find endpoint does the same job from a free API key, and in one
hop rather than two. Without either, matching falls back to fuzzy title search,
which mismatches remakes, sequels and common titles.
"""

import logging
from typing import Any, Dict, Optional

import requests

TMDB_API_BASE = "https://api.themoviedb.org/3"

# TMDB returns results grouped by media type; these are the groups we care
# about, in the order we prefer them for a given requested type.
_RESULT_KEYS = {
    "movie": ("movie_results", "tv_results"),
    "tv": ("tv_results", "movie_results"),
}


def is_available() -> bool:
    """Whether a TMDB API key is configured."""
    from ..config import get_tmdb_api_key
    return bool(get_tmdb_api_key())


def resolve_imdb_id(imdb_id: str, media_type: str = "movie") -> Optional[Dict[str, Any]]:
    """
    Resolve an IMDb ID to a TMDB ID using TMDB's /find endpoint.

    This is an exact lookup on the external ID, not a search, so a result is
    always the right title.

    Args:
        imdb_id (str): IMDb ID, e.g. "tt0111161"
        media_type (str): "movie" or "tv" - decides which result group is
            preferred when a title appears in both

    Returns:
        Optional[Dict[str, Any]]: {"tmdb_id": int, "media_type": str, "title": str}
            or None if unresolved or no API key is configured
    """
    from ..config import get_tmdb_api_key

    api_key = get_tmdb_api_key()
    if not api_key:
        return None

    if not imdb_id or not imdb_id.startswith("tt"):
        logging.debug(f"Not an IMDb ID, skipping TMDB lookup: {imdb_id}")
        return None

    url = f"{TMDB_API_BASE}/find/{imdb_id}"
    params = {"api_key": api_key, "external_source": "imdb_id"}

    try:
        response = requests.get(url, params=params, timeout=15)

        if response.status_code == 401:
            logging.error(
                "TMDB rejected the API key (401). Check TMDB_KEY - "
                "a free key is available at https://www.themoviedb.org/settings/api"
            )
            return None

        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        logging.warning(f"TMDB lookup failed for {imdb_id}: {e}")
        return None
    except ValueError as e:
        logging.warning(f"TMDB returned invalid JSON for {imdb_id}: {e}")
        return None

    # Prefer the group matching the type we're looking for, then fall back to
    # the other - IMDb sometimes classifies a title differently from TMDB.
    for key in _RESULT_KEYS.get(media_type, _RESULT_KEYS["movie"]):
        results = data.get(key) or []
        if not results:
            continue

        match = results[0]
        tmdb_id = match.get("id")
        if tmdb_id is None:
            continue

        resolved_type = "tv" if key == "tv_results" else "movie"
        title = match.get("title") or match.get("name")

        if resolved_type != media_type:
            logging.info(
                f"TMDB resolved {imdb_id} as {resolved_type}, not {media_type} - using {resolved_type}"
            )

        return {
            "tmdb_id": int(tmdb_id),
            "media_type": resolved_type,
            "title": title,
        }

    logging.info(f"TMDB has no entry for IMDb ID {imdb_id}")
    return None
