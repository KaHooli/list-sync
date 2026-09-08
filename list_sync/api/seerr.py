"""
Seerr API client for the ListSync application.
"""

import json
import logging
import re
import requests
from typing import Dict, Any, Tuple, Optional
from urllib.parse import quote

from ..books import (  # noqa: F401  (re-exported for callers of this module)
    BOOK_FORMAT_AUDIOBOOK,
    BOOK_FORMAT_BOTH,
    BOOK_FORMAT_EBOOK,
    BOOK_FORMAT_OVERLAP,
    BOOK_FORMATS,
    DEFAULT_BOOK_FORMAT,
    normalize_book_format,
)
from ..utils.helpers import calculate_title_similarity, custom_input, color_gradient

# Seerr permission bits (server/lib/permissions.ts).
# ADMIN implies every other permission.
PERMISSION_ADMIN = 2
PERMISSION_REQUEST = 32
PERMISSION_REQUEST_MOVIE = 262144
PERMISSION_REQUEST_TV = 524288


class SeerrClient:
    """Client for interacting with the Seerr API."""
    
    def __init__(self, seerr_url: str, api_key: str, requester_user_id: str = "1"):
        """
        Initialize the Seerr API client.
        
        Args:
            seerr_url (str): Seerr server URL
            api_key (str): API key
            requester_user_id (str, optional): Requester user ID. Defaults to "1".
        """
        self.seerr_url = seerr_url.rstrip('/')
        self.api_key = api_key
        self.requester_user_id = requester_user_id
        self.headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
        self.request_headers = {"X-Api-Key": api_key, "X-Api-User": requester_user_id, "Content-Type": "application/json"}
        self._users_cache = None
        self._capabilities = None

    def _headers_for_user(self, requester_user_id: Optional[str] = None) -> Dict[str, str]:
        """
        Build request headers for a specific Seerr user without mutating defaults.
        """
        user_id = requester_user_id or self.requester_user_id or "1"
        return {
            "X-Api-Key": self.api_key,
            "X-Api-User": str(user_id),
            "Content-Type": "application/json"
        }

    def _submit_request(self, payload: Dict[str, Any], description: str,
                        requester_user_id: Optional[str] = None) -> str:
        """
        POST a request to Seerr as a specific user and classify the outcome.

        Seerr answers with distinct signals that all look like "it didn't
        work" unless they're read carefully:
          401 - the X-Api-User ID doesn't exist on the server
          403 - the user exists but may not request, or has hit their quota
          409 - this user already has a request for this media

        Args:
            payload (Dict[str, Any]): Request body to POST
            description (str): Human-readable description used in log lines
            requester_user_id (Optional[str]): Seerr user to request as

        Returns:
            str: "success", "already_requested", or "error"
        """
        user_id = str(requester_user_id or self.requester_user_id or "1")
        request_url = f"{self.seerr_url}/api/v1/request"

        # Name the requester in the body as well as the X-Api-User header.
        # SeerrNG never reads that header - an API key authenticates as the
        # owner account and nothing else - so the header alone silently
        # attributed every list's requests to the admin. The body field is what
        # both it and Overseerr honour.
        body = dict(payload)
        if user_id.isdigit():
            body["userId"] = int(user_id)

        try:
            response = requests.post(
                request_url,
                headers=self._headers_for_user(user_id),
                json=body,
                timeout=30
            )
            response.raise_for_status()
            logging.debug(f"Request successful for {description} as user {user_id}")
            return "success"

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            server_message = self._extract_error_message(e.response)

            # Naming another requester needs Manage Users or Manage Requests.
            # A key without it could request perfectly well before this, so
            # fall back rather than turning a working sync into a wall of 403s.
            if (status_code == 403 and "userId" in body
                    and "permission to modify the request user" in server_message.lower()):
                logging.warning(
                    f"⚠️  {description}: this API key may not request on behalf of other users, "
                    f"so the request was made without naming user {user_id}. Seerr will attribute "
                    f"it to the key's own account. Grant that account Manage Requests in Seerr "
                    f"to keep per-list users working."
                )
                return self._submit_request_without_requester(payload, description, user_id)

            # 409 is Seerr's canonical "already requested" answer.
            if status_code == 409:
                logging.info(f"📌 {description}: already requested by user {user_id} ({server_message})")
                return "already_requested"

            if status_code == 401:
                logging.error(
                    f"❌ {description}: Seerr rejected user_id {user_id} (401). "
                    f"No user with that ID exists on {self.seerr_url}. "
                    f"Check the user assigned to this list against Settings → Users in Seerr."
                )
                return "error"

            if status_code == 403:
                lowered = server_message.lower()
                if "quota" in lowered:
                    logging.error(
                        f"❌ {description}: user {user_id} has hit their request quota ({server_message}). "
                        f"Raise or clear the quota in Seerr under Users → {user_id} → Permissions."
                    )
                else:
                    logging.error(
                        f"❌ {description}: user {user_id} is not allowed to make this request ({server_message}). "
                        f"Grant that user the Request permission in Seerr under Users → {user_id} → Permissions "
                        f"(4K requests need the separate 4K permission)."
                    )
                return "error"

            if status_code == 400:
                # Older builds answered 400 for duplicates; keep detecting that.
                if any(phrase in server_message.lower()
                       for phrase in ["already", "duplicate", "exists"]):
                    logging.info(f"📌 {description}: already requested (400 response)")
                    return "already_requested"
                logging.error(f"❌ {description}: bad request (400) - {server_message}")
                return "error"

            logging.error(f"❌ {description}: HTTP {status_code} as user {user_id} - {server_message}")
            return "error"

        except requests.exceptions.RequestException as e:
            logging.error(f"❌ {description}: could not reach Seerr - {str(e)}")
            return "error"

    def _submit_request_without_requester(self, payload: Dict[str, Any], description: str,
                                          user_id: str) -> str:
        """
        Re-send a request without naming the requester, after Seerr refused it.

        Args:
            payload (Dict[str, Any]): The original body, with no userId in it
            description (str): Human-readable description used in log lines
            user_id (str): The user the request was meant for, for the headers

        Returns:
            str: "success", "already_requested", or "error"
        """
        try:
            response = requests.post(
                f"{self.seerr_url}/api/v1/request",
                headers=self._headers_for_user(user_id),
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            return "success"
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 409:
                return "already_requested"
            logging.error(
                f"❌ {description}: HTTP {e.response.status_code} without a named requester - "
                f"{self._extract_error_message(e.response)}"
            )
            return "error"
        except requests.exceptions.RequestException as e:
            logging.error(f"❌ {description}: could not reach Seerr - {str(e)}")
            return "error"

    @staticmethod
    def _extract_error_message(response) -> str:
        """Pull Seerr's error message out of a failed response body."""
        try:
            data = response.json()
            if isinstance(data, dict):
                message = data.get("message")
                if message:
                    return str(message)
        except (ValueError, AttributeError):
            pass
        text = getattr(response, "text", "") or ""
        return text.strip()[:200] or "no detail returned"

    def get_users(self, use_cache: bool = True) -> Optional[list]:
        """
        Fetch the users configured on the Seerr server.

        Args:
            use_cache (bool): Reuse the list fetched earlier in this run. A sync
                validates one requester per list, and the user list won't change
                underneath it.

        Returns:
            Optional[list]: List of user dicts, or None if the lookup failed
        """
        if use_cache and self._users_cache is not None:
            return self._users_cache

        users_url = f"{self.seerr_url}/api/v1/user"
        try:
            # take=0 is rejected by some builds; ask for a large page instead
            response = requests.get(
                users_url,
                headers=self.headers,
                params={"take": 200},
                timeout=15
            )
            response.raise_for_status()
            self._users_cache = response.json().get("results", [])
            return self._users_cache
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to fetch Seerr users: {str(e)}")
            return None

    def validate_requester(self, requester_user_id: str) -> Tuple[bool, str]:
        """
        Check up front that a list's assigned user can actually make requests.

        Catching this before a sync turns a run of opaque per-item failures into
        one clear message naming the user and what's wrong with it.

        Args:
            requester_user_id (str): Seerr user ID to validate

        Returns:
            Tuple[bool, str]: (usable, human-readable explanation)
        """
        user_id = str(requester_user_id)
        users = self.get_users()

        if users is None:
            # Couldn't reach the user endpoint - don't block the sync over it.
            return True, f"Could not verify user {user_id} (user lookup failed); continuing anyway"

        match = next((u for u in users if str(u.get("id")) == user_id), None)
        if not match:
            known = ", ".join(f"{u.get('id')}={u.get('displayName')}" for u in users[:20]) or "none"
            return False, (
                f"Seerr has no user with ID {user_id}. "
                f"Known users: {known}"
            )

        display_name = match.get("displayName") or match.get("username") or f"user {user_id}"

        permissions = match.get("permissions")
        if isinstance(permissions, int):
            is_admin = bool(permissions & PERMISSION_ADMIN)
            can_request = any(
                permissions & bit
                for bit in (PERMISSION_REQUEST, PERMISSION_REQUEST_MOVIE, PERMISSION_REQUEST_TV)
            )
            if not is_admin and not can_request:
                return False, (
                    f"'{display_name}' (ID {user_id}) does not have the Request permission in Seerr. "
                    f"Grant it under Users → {display_name} → Permissions."
                )

        return True, f"Requests will be made as '{display_name}' (ID {user_id})"


    def test_connection(self):
        """
        Test the connection to the Seerr API.
        
        Raises:
            Exception: If the connection test fails
        """
        test_url = f"{self.seerr_url}/api/v1/status"
        try:
            response = requests.get(test_url, headers=self.headers)
            response.raise_for_status()
            logging.info("Seerr API connection successful!")
            return True
        except Exception as e:
            logging.error(f"Seerr API connection failed. Error: {str(e)}")
            raise
    
    def set_requester_user(self) -> str:
        """
        Set the requester user based on available users.
        
        Returns:
            str: The requester user ID
        """
        users_url = f"{self.seerr_url}/api/v1/user"
        try:
            requester_user_id = "1"
            response = requests.get(users_url, headers=self.headers)
            response.raise_for_status()
            jsonResult = response.json()
            
            if jsonResult['pageInfo']['results'] > 1:
                print(color_gradient("\n📋 Multiple users detected, you can choose which user will make the requests on ListSync behalf.\n", "#00aaff", "#00ffaa"))
                for result in jsonResult['results']:
                    print(color_gradient(f"{result['id']}. {result['displayName']}", "#ffaa00", "#ff5500"))
                requester_user_id = custom_input(color_gradient("\nEnter the number of the user to use as requester: ", "#ffaa00", "#ff5500"))
                if not next((x for x in jsonResult['results'] if str(x['id']) == requester_user_id), None):
                    requester_user_id = "1"
                    print(color_gradient("\n❌  Invalid option, using admin as requester user.", "#ff0000", "#aa0000"))
                
            logging.info("Requester user set!")
            return requester_user_id
        except Exception as e:
            logging.error(f"Failed to set requester user. Error: {str(e)}")
            return "1"  # Default fallback
    
    def get_media_by_tmdb_id(self, tmdb_id: int, media_type: str) -> Optional[Dict[str, Any]]:
        """
        Get media details directly by TMDB ID (no search needed).
        
        Args:
            tmdb_id (int): TMDB ID
            media_type (str): Media type (movie or tv)
            
        Returns:
            Optional[Dict[str, Any]]: Media details with ID and status or None if not found
        """
        media_url = f"{self.seerr_url}/api/v1/{media_type}/{tmdb_id}"
        
        try:
            logging.info(f"🎯 Seerr API: Direct lookup by TMDB ID: {tmdb_id} [{media_type}]")
            logging.debug(f"Request URL: {media_url}")
            
            response = requests.get(media_url, headers=self.headers, timeout=10)
            
            if response.status_code == 404:
                logging.info(f"❌ Seerr API: TMDB ID {tmdb_id} not found in Seerr")
                return None
            
            if response.status_code == 403:
                logging.error(f"❌ Seerr API: 403 Forbidden - API key does not have permission to access /api/v1/{media_type}/{tmdb_id}")
                logging.error(f"   Please check your API key permissions in Seerr settings. The key needs 'Read' permission for media endpoints.")
                return None
            
            response.raise_for_status()
            media_data = response.json()
            
            # Extract the title for logging
            media_title = media_data.get('title') if media_type == 'movie' else media_data.get('name')
            media_year = None
            try:
                if media_type == 'movie' and 'releaseDate' in media_data:
                    media_year = media_data['releaseDate'][:4]
                elif media_type == 'tv' and 'firstAirDate' in media_data:
                    media_year = media_data['firstAirDate'][:4]
            except (ValueError, TypeError):
                pass
            
            logging.info(f"✅ Seerr API: Found '{media_title}' ({media_year}) via TMDB ID {tmdb_id}")
            # Removed verbose media data logging to reduce log size
            
            # Ensure tmdb_id is an integer (may be string from collections)
            overseerr_id = int(tmdb_id) if tmdb_id else None
            
            return {
                "id": overseerr_id,  # Use TMDB ID as the identifier (converted to int)
                "mediaType": media_type,
                "title": media_title,
                "year": media_year
            }
            
        except requests.exceptions.RequestException as e:
            logging.error(f"❌ Seerr API error for TMDB ID {tmdb_id}: {str(e)}")
            return None
    
    def search_media(self, media_title: str, media_type: str, release_year: int = None) -> Optional[Dict[str, Any]]:
        """
        Search for media in Seerr (fallback method when no TMDB ID available).
        
        Args:
            media_title (str): Title to search for
            media_type (str): Media type (movie or tv)
            release_year (int, optional): Release year. Defaults to None.
            
        Returns:
            Optional[Dict[str, Any]]: Search result or None if not found
        """
        logging.info(f"🔍 Seerr API: Fallback search by title: '{media_title}' ({release_year}) [{media_type}]")
        search_url = f"{self.seerr_url}/api/v1/search"
        search_title = media_title  # Use the provided title
        
        page = 1
        best_match = None
        best_score = 0
        
        while True:
            try:
                # Use quote to properly encode all special characters including forward slashes
                # quote() encodes spaces as %20 and slashes as %2F (unlike requests.utils.quote which doesn't encode /)
                encoded_query = quote(search_title, safe='')
                url = f"{search_url}?query={encoded_query}&page={page}&language=en"
                
                logging.info(f"  📄 Seerr API: Searching page {page} for '{search_title}' (Year: {release_year})")
                logging.debug(f"  Request URL: {url}")
                response = requests.get(url, headers=self.headers, timeout=10)
                
                if response.status_code == 429:
                    logging.warning("Rate limited, waiting 5 seconds...")
                    import time
                    time.sleep(5)
                    continue
                
                if response.status_code == 403:
                    logging.error(f"❌ Seerr API: 403 Forbidden - API key does not have permission to access /api/v1/search")
                    logging.error(f"   Please check your API key permissions in Seerr settings. The key needs 'Read' permission for search endpoints.")
                    return None
                    
                response.raise_for_status()
                search_results = response.json()
                
                if not search_results.get("results"):
                    break
                    
                for result in search_results["results"]:
                    result_type = result.get("mediaType")
                    if result_type != media_type:
                        continue
                    
                    # Get the title based on media type
                    result_title = result.get("title") if media_type == "movie" else result.get("name")
                    if not result_title:
                        continue
                    
                    # Get year
                    result_year = None
                    try:
                        if media_type == "movie" and "releaseDate" in result:
                            result_year = int(result["releaseDate"][:4])
                        elif media_type == "tv" and "firstAirDate" in result:
                            result_year = int(result["firstAirDate"][:4])
                    except (ValueError, TypeError):
                        pass
                    
                    # Calculate title similarity
                    similarity = calculate_title_similarity(search_title, result_title)
                    
                    # Calculate final score
                    score = similarity
                    
                    # Year matching
                    if release_year and result_year:
                        if release_year == result_year:
                            score *= 2  # Double score for exact year match
                            logging.debug(f"  ✓ Exact year match for '{result_title}' ({result_year}) - Base similarity: {similarity}")
                        elif abs(release_year - result_year) <= 1:
                            score *= 1.5  # 1.5x score for off-by-one year
                            logging.debug(f"  ≈ Close year match for '{result_title}' ({result_year}) - Base similarity: {similarity}")
                    
                    logging.debug(f"  🔍 Match candidate: '{result_title}' ({result_year}) - Score: {score}")
                    
                    # Update best match if we have a better score
                    # For exact year matches, require a lower similarity threshold
                    min_similarity = 0.5 if (release_year and result_year and release_year == result_year) else 0.7
                    
                    if score > best_score and similarity >= min_similarity:
                        best_score = score
                        best_match = result
                        logging.info(f"  ⭐ New best match: '{result_title}' ({result_year}) - Score: {score}")
                
                # Only continue to next page if we haven't found a good match
                if best_score > 1.5 or page >= search_results.get("totalPages", 1):
                    break
                
                page += 1
                
            except requests.exceptions.RequestException as e:
                logging.error(f'Error searching for "{search_title}": {str(e)}')
                if "429" in str(e):
                    import time
                    time.sleep(5)
                    continue
                raise

        if best_match:
            result_title = best_match.get("title") if media_type == "movie" else best_match.get("name")
            result_year = None
            try:
                if media_type == "movie" and "releaseDate" in best_match:
                    result_year = best_match["releaseDate"][:4]
                elif media_type == "tv" and "firstAirDate" in best_match:
                    result_year = best_match["firstAirDate"][:4]
            except (ValueError, TypeError):
                pass
            
            logging.info(f"✅ Seerr API: Final match for '{media_title}' ({release_year}): '{result_title}' ({result_year}) - Score: {best_score}")
            return {
                "id": best_match["id"],
                "mediaType": best_match["mediaType"],
            }
        
        logging.warning(f'❌ Seerr API: No matching results found for "{media_title}" ({release_year}) of type "{media_type}"')
        return None
    
    def get_media_state(self, media_id: int, media_type: str, is_4k: bool = False) -> Dict[str, Any]:
        """
        Get the full state of a media item in Seerr, including who requested it.

        The library-wide status alone can't answer "does *this* user already have
        a request?", which is what per-list users need to know before deciding
        whether to submit one.

        Args:
            media_id (int): Media ID (must be integer, not string)
            media_type (str): Media type (movie or tv)
            is_4k (bool): Which resolution's requests to count, since Seerr
                tracks 4K and non-4K requests separately

        Returns:
            Dict[str, Any]: is_available, is_requested, number_of_seasons and
                requested_by_user_ids (a set of user IDs as strings)
        """
        # Ensure media_id is an integer (may be string from API responses)
        try:
            media_id = int(media_id)
        except (ValueError, TypeError):
            logging.error(f"Invalid media_id type in get_media_status: {type(media_id)} = {media_id}")
            raise ValueError(f"media_id must be an integer, got {type(media_id)}: {media_id}")

        media_url = f"{self.seerr_url}/api/v1/{media_type}/{media_id}"

        try:
            response = requests.get(media_url, headers=self.headers, timeout=15)
            response.raise_for_status()
            media_data = response.json()

            media_info = media_data.get("mediaInfo") or {}
            status = media_info.get("status")
            number_of_seasons = self.extract_number_of_seasons(media_data)
            requested_by = self._extract_requester_ids(media_info, is_4k)

            logging.debug(
                f"Seerr {media_type} ID {media_id}: status={status}, "
                f"seasons={number_of_seasons}, requested_by={sorted(requested_by) or 'nobody'}"
            )

            # Status codes:
            # None: not in Seerr's database yet
            # 0: NOT REQUESTED (available to request)
            # 1: REQUESTED (pending approval)
            # 2: PENDING (approved, waiting for download)
            # 3: PROCESSING (downloading/importing)
            # 4: PARTIALLY_AVAILABLE (some content available)
            # 5: AVAILABLE (fully available)
            if status is None or status == 0:
                return {
                    "is_available": False,
                    "is_requested": False,
                    "number_of_seasons": number_of_seasons,
                    "requested_by_user_ids": requested_by,
                }

            return {
                "is_available": status in [4, 5],
                "is_requested": status in [1, 2, 3],
                "number_of_seasons": number_of_seasons,
                "requested_by_user_ids": requested_by,
            }
        except Exception as e:
            logging.error(f"Error confirming status for {media_type} ID {media_id}: {str(e)}")
            raise

    @staticmethod
    def _extract_requester_ids(media_info: Dict[str, Any], is_4k: bool = False) -> set:
        """
        Collect the IDs of users who already have a request on this media.

        Only requests at the resolution being synced count, since Seerr
        treats 4K and non-4K as separate requests. A declined request counts
        too: re-submitting it every sync would fight the admin who declined it.
        """
        requester_ids = set()
        for request in (media_info.get("requests") or []):
            if not isinstance(request, dict):
                continue
            if bool(request.get("is4k", False)) != bool(is_4k):
                continue
            requested_by = request.get("requestedBy") or {}
            user_id = requested_by.get("id") if isinstance(requested_by, dict) else None
            if user_id is not None:
                requester_ids.add(str(user_id))
        return requester_ids

    def get_media_status(self, media_id: int, media_type: str) -> Tuple[bool, bool, int]:
        """
        Get the status of media in Seerr.

        Args:
            media_id (int): Media ID (must be integer, not string)
            media_type (str): Media type (movie or tv)

        Returns:
            Tuple[bool, bool, int]: Availability, requested status, and number of seasons
        """
        state = self.get_media_state(media_id, media_type)
        return state["is_available"], state["is_requested"], state["number_of_seasons"]


    def extract_number_of_seasons(self, media_data):
        """
        Extract the number of seasons from media data.
        
        Args:
            media_data (dict): Media data from Seerr
            
        Returns:
            int: Number of seasons (defaults to 1)
        """
        number_of_seasons = media_data.get("numberOfSeasons")
        logging.debug(f"Extracted number of seasons: {number_of_seasons}")
        return number_of_seasons if number_of_seasons is not None else 1
    
    def request_media(self, media_id: int, media_type: str, is_4k: bool = False, requester_user_id: Optional[str] = None) -> str:
        """
        Request media in Seerr.
        
        Args:
            media_id (int): Media ID (must be integer, not string)
            media_type (str): Media type (movie or tv)
            is_4k (bool, optional): Whether to request 4K. Defaults to False.
            
        Returns:
            str: Status of the request ("success", "already_requested", or "error")
        """
        # Ensure media_id is an integer (may be string from API responses)
        try:
            media_id = int(media_id)
        except (ValueError, TypeError):
            logging.error(f"Invalid media_id type: {type(media_id)} = {media_id}")
            return "error"
        
        payload = {
            "mediaId": media_id,  # Ensure it's an integer, not string
            "mediaType": media_type,
            "is4k": is_4k
        }

        return self._submit_request(
            payload,
            f"{media_type} ID {media_id}",
            requester_user_id
        )

    def request_tv_series(self, tv_id: int, number_of_seasons: int, is_4k: bool = False, requester_user_id: Optional[str] = None) -> str:
        """
        Request TV series in Seerr with specific seasons.
        
        Args:
            tv_id (int): TV series ID (must be integer, not string)
            number_of_seasons (int): Number of seasons to request
            is_4k (bool, optional): Whether to request 4K. Defaults to False.
            
        Returns:
            str: Status of the request ("success" or "error")
        """
        # Ensure tv_id is an integer (may be string from API responses)
        try:
            tv_id = int(tv_id)
        except (ValueError, TypeError):
            logging.error(f"Invalid tv_id type: {type(tv_id)} = {tv_id}")
            return "error"
        
        seasons_list = [i for i in range(1, number_of_seasons + 1)]
        logging.debug(f"Seasons list for TV series ID {tv_id}: {seasons_list}")

        payload = {
            "mediaId": tv_id,  # Ensure it's an integer, not string
            "mediaType": "tv",
            "is4k": is_4k,
            "seasons": seasons_list
        }

        logging.debug(f"Requesting TV series ID {tv_id}: {number_of_seasons} seasons")

        return self._submit_request(
            payload,
            f"TV series ID {tv_id} ({number_of_seasons} season(s))",
            requester_user_id
        )

    def request_specific_season(self, tv_id: int, season_number: int, is_4k: bool = False, requester_user_id: Optional[str] = None) -> str:
        """
        Request a specific season of a TV series in Seerr.
        
        Args:
            tv_id (int): TV series TMDB ID (must be integer, not string)
            season_number (int): Season number to request
            is_4k (bool, optional): Whether to request 4K. Defaults to False.
            
        Returns:
            str: Status of the request ("success" or "error")
        """
        # Ensure tv_id is an integer (may be string from API responses)
        try:
            tv_id = int(tv_id)
        except (ValueError, TypeError):
            logging.error(f"Invalid tv_id type: {type(tv_id)} = {tv_id}")
            return "error"
        
        payload = {
            "mediaId": tv_id,  # Ensure it's an integer, not string
            "mediaType": "tv",
            "is4k": is_4k,
            "seasons": [season_number]  # Request only the specific season
        }

        logging.info(f"📺 Requesting Season {season_number} for TV series TMDB ID {tv_id}")

        return self._submit_request(
            payload,
            f"Season {season_number} of TV series ID {tv_id}",
            requester_user_id
        )

    # ------------------------------------------------------------------
    # Books
    #
    # Book support is not part of Overseerr or Jellyseerr - it arrived with
    # SeerrNG, which requests books through a Readarr-compatible "Bookshelf"
    # service (Chaptarr). Everything below therefore probes for the feature
    # before using it, so a list-sync pointed at a bookless server degrades to
    # a clear message instead of a wall of 404s.
    # ------------------------------------------------------------------

    def get_capabilities(self, refresh: bool = False) -> Dict[str, Any]:
        """
        Discover which media types the connected Seerr can actually request.

        Args:
            refresh (bool): Re-probe instead of reusing the cached answer. The
                answer only changes when the server is reconfigured, so a sync
                probes once and reuses it for every list.

        Returns:
            Dict[str, Any]: {"books": {supported, ebook, audiobook, known, reason}}
        """
        if self._capabilities is not None and not refresh:
            return self._capabilities

        self._capabilities = {"books": self._probe_book_support()}
        return self._capabilities

    def supports_books(self, refresh: bool = False) -> bool:
        """
        Whether this server has book support at all (i.e. is a SeerrNG build).

        Returns:
            bool: True when book endpoints exist on the connected server
        """
        return bool(self.get_capabilities(refresh)["books"]["supported"])

    def _probe_book_support(self) -> Dict[str, Any]:
        """
        Ask the server about its Bookshelf services to settle three questions
        at once: does this build know about books, is a service configured, and
        which formats can be requested.

        The Bookshelf settings endpoint is the cheapest honest probe - it is
        served by the app itself, unlike /book/search which would go out to
        Open Library just to tell us the route exists.

        Returns:
            Dict[str, Any]: supported/ebook/audiobook flags, whether the answer
                is trustworthy ("known"), and a human-readable reason
        """
        no_books = {"supported": False, "ebook": False, "audiobook": False}
        url = f"{self.seerr_url}/api/v1/settings/readarr"

        try:
            response = requests.get(url, headers=self.headers, timeout=15)
        except requests.exceptions.RequestException as e:
            return {
                **no_books,
                "known": False,
                "reason": f"Could not reach {self.seerr_url} to check for book support: {e}",
            }

        if response.status_code in (404, 405):
            # The Bookshelf *settings* route is newer than book support itself,
            # so its absence is not proof: ask the book endpoint directly
            # before concluding this server cannot do books at all.
            return self._probe_book_endpoint()

        if response.status_code in (401, 403):
            return {
                **no_books,
                "known": False,
                "reason": (
                    f"Seerr rejected the API key when checking for book support "
                    f"(HTTP {response.status_code}). Book lists stay hidden until a key "
                    f"with admin access is configured."
                ),
            }

        if response.status_code >= 400:
            return {
                **no_books,
                "known": False,
                "reason": f"Unexpected HTTP {response.status_code} while checking for book support.",
            }

        try:
            services = response.json()
        except ValueError:
            return {
                **no_books,
                "known": False,
                "reason": "Seerr returned a non-JSON answer when checking for book support.",
            }

        if not isinstance(services, list):
            return {
                **no_books,
                "known": False,
                "reason": "Seerr returned an unexpected Bookshelf settings payload.",
            }

        # SeerrNG only accepts a request for a format that has a *default*
        # server of that kind, so that - not merely "a server exists" - is what
        # decides which formats we may offer.
        has_ebook = any(
            service.get("isDefault") and (service.get("serviceType") or BOOK_FORMAT_EBOOK) == BOOK_FORMAT_EBOOK
            for service in services if isinstance(service, dict)
        )
        has_audiobook = any(
            service.get("isDefault") and service.get("serviceType") == BOOK_FORMAT_AUDIOBOOK
            for service in services if isinstance(service, dict)
        )

        if has_ebook and has_audiobook:
            reason = "Book requests are available for ebooks and audiobooks."
        elif has_ebook:
            reason = "Book requests are available for ebooks only (no default audiobook Bookshelf server)."
        elif has_audiobook:
            reason = "Book requests are available for audiobooks only (no default ebook Bookshelf server)."
        else:
            reason = (
                "This Seerr server supports books, but no default Bookshelf server is "
                "configured, so book requests would be rejected. Set one under "
                "Settings → Services in Seerr."
            )

        return {
            "supported": True,
            "ebook": has_ebook,
            "audiobook": has_audiobook,
            "known": True,
            "reason": reason,
        }

    def _probe_book_endpoint(self) -> Dict[str, Any]:
        """
        Fall back to the book endpoint when the Bookshelf settings are absent.

        /book/search is the book feature itself rather than a setting for it,
        and needs only an authenticated caller, so it answers the one question
        that matters - can this server do books at all - on builds where the
        settings route is missing or has moved.

        Returns:
            Dict[str, Any]: The same shape as _probe_book_support()
        """
        no_books = {"supported": False, "ebook": False, "audiobook": False}
        url = f"{self.seerr_url}/api/v1/book/search"

        try:
            response = requests.get(
                url, headers=self.headers, params={"query": "test", "page": 1}, timeout=20
            )
        except requests.exceptions.RequestException as e:
            return {
                **no_books,
                "known": False,
                "reason": f"Could not reach {self.seerr_url} to check for book support: {e}",
            }

        if response.status_code in (404, 405):
            return {
                **no_books,
                "known": True,
                "reason": (
                    "This Seerr server has no book support. Book lists need a build that "
                    "can request books, such as SeerrNG with a Chaptarr/Readarr-compatible "
                    "Bookshelf service."
                ),
            }

        if response.status_code >= 400:
            return {
                **no_books,
                "known": False,
                "reason": (
                    f"Could not tell whether this server supports books: the Bookshelf "
                    f"settings are unavailable and /book/search answered "
                    f"HTTP {response.status_code}."
                ),
            }

        # Books work, but without the Bookshelf settings there is no way to say
        # which formats have a default service. Left unsettled on purpose, so
        # neither format is refused on a guess.
        return {
            "supported": True,
            "ebook": True,
            "audiobook": True,
            "known": False,
            "reason": (
                "This server can request books, but its Bookshelf settings could not be "
                "read, so which formats have a default service is unverified."
            ),
        }

    def get_book(self, book_id: str) -> Optional[Dict[str, Any]]:
        """
        Look a book up directly by its Open Library work ID.

        Args:
            book_id (str): Open Library work ID (e.g. "OL27448W")

        Returns:
            Optional[Dict[str, Any]]: Book details, or None if not found
        """
        book_id = str(book_id or "").strip()
        if not book_id:
            return None

        book_url = f"{self.seerr_url}/api/v1/book/{quote(book_id, safe='')}"
        try:
            logging.info(f"📚 Seerr API: Direct lookup by Open Library ID: {book_id}")
            response = requests.get(book_url, headers=self.headers, timeout=20)

            if response.status_code == 404:
                logging.info(f"❌ Seerr API: Open Library ID {book_id} not found")
                return None

            response.raise_for_status()
            return self._as_book_result(response.json())
        except requests.exceptions.RequestException as e:
            logging.error(f"❌ Seerr API error for Open Library ID {book_id}: {str(e)}")
            return None

    @staticmethod
    def _as_book_result(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalise a Seerr book payload into the fields a request needs."""
        if not isinstance(data, dict) or not data.get("id"):
            return None
        return {
            "id": str(data.get("id")),
            "title": data.get("title"),
            "author": data.get("author"),
            "author_id": data.get("authorId"),
            "year": data.get("firstPublishYear"),
            "isbn13": data.get("isbn13"),
            "edition_id": data.get("editionId"),
            "media_info": data.get("mediaInfo") or {},
        }

    def search_book(self, title: str, author: Optional[str] = None,
                    year: Optional[int] = None, isbn: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Find a book on the connected Seerr server.

        Open Library's search is passed through verbatim by Seerr, so an ISBN
        can be matched exactly with a field query before falling back to the
        fuzzy title/author match that a Goodreads shelf usually needs.

        Args:
            title (str): Book title
            author (Optional[str]): Author name, used both to narrow the query
                and to score candidates
            year (Optional[int]): First publication year, used for scoring
            isbn (Optional[str]): ISBN-10/13 from the source list, if any

        Returns:
            Optional[Dict[str, Any]]: Best matching book, or None
        """
        if isbn:
            digits = re.sub(r"[^0-9Xx]", "", str(isbn))
            if digits:
                exact = self._book_search_request(f"isbn:{digits}")
                if exact:
                    # An ISBN identifies one edition, so the top hit is the book.
                    logging.info(f"✅ Seerr API: Matched '{title}' by ISBN {digits}")
                    return self._as_book_result(exact[0])

        query = f"{title} {author}".strip() if author else str(title or "").strip()
        if not query:
            return None

        results = self._book_search_request(query)
        if not results:
            logging.warning(f'❌ Seerr API: No book results for "{title}" by {author or "unknown author"}')
            return None

        best, best_score = None, 0.0
        for result in results:
            if not isinstance(result, dict) or not result.get("id"):
                continue

            candidate_title = result.get("title") or ""
            score = calculate_title_similarity(title, candidate_title)

            candidate_author = result.get("author") or ""
            if author and candidate_author:
                author_similarity = calculate_title_similarity(author, candidate_author)
                # The author is the strongest signal a shelf gives us: two books
                # share a title far more often than they share title and author.
                # The middle band leaves room for the same person spelled
                # differently ("J.R.R. Tolkien" / "John Ronald Reuel Tolkien").
                if author_similarity >= 0.8:
                    score *= 1.5
                elif author_similarity < 0.5:
                    score *= 0.5

            candidate_year = result.get("firstPublishYear")
            if year and candidate_year:
                try:
                    if abs(int(year) - int(candidate_year)) <= 1:
                        score *= 1.2
                except (TypeError, ValueError):
                    pass

            logging.debug(f"  📖 Candidate: '{candidate_title}' by {candidate_author} - Score: {score:.2f}")
            if score > best_score:
                best, best_score = result, score

        # Below this the "match" is usually a different book by a similar name.
        if not best or best_score < 0.6:
            logging.warning(
                f'❌ Seerr API: No confident book match for "{title}" by {author or "unknown author"} '
                f'(best score {best_score:.2f})'
            )
            return None

        logging.info(
            f"✅ Seerr API: Matched '{title}' → '{best.get('title')}' by "
            f"{best.get('author') or 'unknown author'} (score {best_score:.2f})"
        )
        return self._as_book_result(best)

    def _book_search_request(self, query: str) -> list:
        """Run one book search and return its raw results."""
        search_url = f"{self.seerr_url}/api/v1/book/search"
        try:
            logging.info(f"🔍 Seerr API: Book search for '{query}'")
            response = requests.get(
                search_url,
                headers=self.headers,
                params={"query": query, "page": 1},
                timeout=30
            )

            if response.status_code == 404:
                logging.error(
                    "❌ Seerr API: /book/search is not available on this server. "
                    "Book lists need a Seerr build with book support (SeerrNG)."
                )
                return []

            response.raise_for_status()
            results = response.json().get("results") or []
            return results if isinstance(results, list) else []
        except requests.exceptions.RequestException as e:
            logging.error(f'❌ Seerr API: Book search failed for "{query}": {str(e)}')
            return []

    @staticmethod
    def _extract_book_requesters(media_info: Dict[str, Any], book_format: str) -> set:
        """
        Collect the users whose open requests already cover this format.

        Seerr only returns requests that are still pending or approved here, so
        a declined or completed one correctly leaves the book requestable
        again. A "both" request covers either single format, and is in turn
        blocked by either - the same overlap Seerr enforces server-side.
        """
        overlapping = BOOK_FORMAT_OVERLAP.get(book_format, {book_format})
        requester_ids = set()
        for request in ((media_info or {}).get("requests") or []):
            if not isinstance(request, dict):
                continue
            stored_format = normalize_book_format(request.get("bookFormat"), BOOK_FORMAT_EBOOK)
            if stored_format not in overlapping:
                continue
            requested_by = request.get("requestedBy") or {}
            user_id = requested_by.get("id") if isinstance(requested_by, dict) else None
            requester_ids.add(str(user_id) if user_id is not None else "unknown")
        return requester_ids

    @staticmethod
    def book_state_from_media_info(media_info: Optional[Dict[str, Any]],
                                   book_format: str = BOOK_FORMAT_EBOOK) -> Dict[str, Any]:
        """
        Work out what still needs requesting for one book, in one format.

        A book is "available" per format: Seerr links the ebook and the
        audiobook to their own Bookshelf service, so a shelf synced as
        audiobooks should still be requested when only the ebook is on hand.

        Args:
            media_info (Optional[Dict[str, Any]]): mediaInfo from a book payload
            book_format (str): Format this list asks for

        Returns:
            Dict[str, Any]: is_available, is_requested, is_blocklisted and
                requested_by_user_ids
        """
        media_info = media_info or {}
        book_format = normalize_book_format(book_format)

        has_ebook = media_info.get("externalServiceId") is not None
        has_audiobook = media_info.get("audiobookExternalServiceId") is not None
        if book_format == BOOK_FORMAT_EBOOK:
            is_available = has_ebook
        elif book_format == BOOK_FORMAT_AUDIOBOOK:
            is_available = has_audiobook
        else:
            # Seerr refuses a "both" request as soon as either half is covered.
            is_available = has_ebook or has_audiobook

        requested_by = SeerrClient._extract_book_requesters(media_info, book_format)

        return {
            "is_available": is_available,
            "is_requested": bool(requested_by),
            # MediaStatus.BLOCKLISTED (6) - Seerr will refuse the request.
            "is_blocklisted": media_info.get("status") == 6,
            "requested_by_user_ids": requested_by,
        }

    def get_book_state(self, book_id: str, book_format: str = BOOK_FORMAT_EBOOK) -> Dict[str, Any]:
        """
        Fetch a book and report what still needs requesting for one format.

        Args:
            book_id (str): Open Library work ID
            book_format (str): Format this list asks for

        Returns:
            Dict[str, Any]: Same shape as book_state_from_media_info()
        """
        book = self.get_book(book_id)
        return self.book_state_from_media_info((book or {}).get("media_info"), book_format)

    def request_book(self, book_id: str, book_format: str = BOOK_FORMAT_EBOOK,
                     edition_id: Optional[str] = None, author_id: Optional[str] = None,
                     isbn13: Optional[str] = None,
                     requester_user_id: Optional[str] = None) -> str:
        """
        Request a book in Seerr as a specific user.

        Args:
            book_id (str): Open Library work ID (e.g. "OL27448W")
            book_format (str): "ebook", "audiobook" or "both"
            edition_id (Optional[str]): Open Library edition ID, when known
            author_id (Optional[str]): Open Library author ID, when known
            isbn13 (Optional[str]): ISBN-13 of the matched edition, when known
            requester_user_id (Optional[str]): Seerr user to request as

        Returns:
            str: "success", "already_requested", or "error"
        """
        book_id = str(book_id or "").strip()
        if not book_id:
            logging.error("Cannot request a book without an Open Library ID")
            return "error"

        book_format = normalize_book_format(book_format)
        payload: Dict[str, Any] = {
            "mediaType": "book",
            "mediaId": book_id,
            "format": book_format,
        }
        # Seerr matches an existing library row on any of these, so passing the
        # ones the list gave us keeps a request from creating a duplicate book.
        if edition_id:
            payload["editionId"] = str(edition_id)
        if author_id:
            payload["authorId"] = str(author_id)
        if isbn13:
            payload["isbn13"] = str(isbn13)

        return self._submit_request(
            payload,
            f"book {book_id} ({book_format})",
            requester_user_id
        )
