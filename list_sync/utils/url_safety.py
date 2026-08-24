"""
Validation for URLs the server is about to fetch.

Several endpoints take a URL from the caller and make the server request it.
Without a check that turns the API into a proxy for whatever the caller can
reach, which on a host with a private network means internal services and
cloud metadata endpoints the caller could never reach directly.

Policy differs by sink, so the caller says what it needs:

  * a Discord webhook only ever lives on Discord, so pass allowed_hosts
  * poster images only ever come from public CDNs, so private addresses are
    rejected outright
  * Overseerr legitimately runs on a private address, so private is allowed
    there - but cloud metadata never is, whatever the sink

Known limitation: the hostname is resolved here and resolved again by the HTTP
client, so a name that changes its answer between the two calls can slip past
(DNS rebinding). Closing that needs the connection pinned to the address that
was checked, which is beyond what this helper does. It still removes the
straightforward attacks.
"""

import ipaddress
import logging
import socket
from typing import Iterable, Optional, Tuple
from urllib.parse import urlparse

# Addresses that hand out cloud credentials. Never a legitimate target, so
# these are refused even when a sink otherwise permits private addresses.
_METADATA_ADDRESSES = frozenset({
    "169.254.169.254",     # AWS IMDS, Azure IMDS, DigitalOcean, Oracle
    "169.254.170.2",       # AWS ECS task metadata
    "100.100.100.200",     # Alibaba Cloud
    "192.0.0.192",         # Oracle Cloud legacy
    "fd00:ec2::254",       # AWS IMDS over IPv6
})

_METADATA_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
})

_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Discord's webhook hosts. Anything else claiming to be a Discord webhook
# isn't one.
DISCORD_WEBHOOK_HOSTS = frozenset({
    "discord.com",
    "discordapp.com",
    "ptb.discord.com",
    "canary.discord.com",
})


def _address_is_private(ip: ipaddress._BaseAddress) -> bool:
    """Whether an address belongs to a range that isn't publicly routable."""
    # An IPv4 address wrapped in IPv6 is still that IPv4 address.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped

    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve(hostname: str) -> Tuple[list, Optional[str]]:
    """Resolve a hostname to every address it answers with."""
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return [], f"could not resolve '{hostname}' ({e.strerror or e})"
    except Exception as e:
        return [], f"could not resolve '{hostname}' ({type(e).__name__})"

    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue

    if not addresses:
        return [], f"'{hostname}' resolved to no usable address"

    return addresses, None


def validate_outbound_url(
    raw_url: str,
    *,
    allow_private: bool = False,
    allowed_hosts: Optional[Iterable[str]] = None,
) -> Tuple[bool, str]:
    """
    Decide whether the server should fetch a caller-supplied URL.

    Args:
        raw_url (str): The URL to check
        allow_private (bool): Permit addresses on private networks. Set this
            only where a private target is expected, such as a self-hosted
            Overseerr. Cloud metadata stays blocked either way.
        allowed_hosts (Optional[Iterable[str]]): If given, the hostname must be
            one of these (or a subdomain of one).

    Returns:
        Tuple[bool, str]: (allowed, reason). The reason explains a refusal, or
            describes what was allowed.
    """
    if not raw_url or not isinstance(raw_url, str):
        return False, "No URL supplied"

    url = raw_url.strip()

    try:
        parsed = urlparse(url)
    except ValueError as e:
        return False, f"Malformed URL ({e})"

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False, (
            f"URL scheme '{parsed.scheme}' is not allowed - only http and https are"
        )

    # user:password@host can be used to disguise the real host from a reader,
    # and no legitimate caller here needs it.
    if parsed.username or parsed.password:
        return False, "URLs with embedded credentials are not allowed"

    hostname = (parsed.hostname or "").strip().rstrip(".")
    if not hostname:
        return False, "URL has no host"

    lowered = hostname.lower()

    if lowered in _METADATA_HOSTNAMES:
        return False, f"'{hostname}' is a cloud metadata endpoint"

    if allowed_hosts is not None:
        permitted = {h.lower() for h in allowed_hosts}
        if not any(lowered == h or lowered.endswith("." + h) for h in permitted):
            return False, (
                f"Host '{hostname}' is not permitted here. "
                f"Allowed: {', '.join(sorted(permitted))}"
            )

    # A literal address needs no lookup; a name needs every answer checked,
    # since one bad address among several is enough.
    try:
        addresses = [ipaddress.ip_address(lowered)]
    except ValueError:
        addresses, error = _resolve(hostname)
        if error:
            return False, error

    for address in addresses:
        text = str(address)
        mapped = getattr(address, "ipv4_mapped", None)
        if text in _METADATA_ADDRESSES or (mapped and str(mapped) in _METADATA_ADDRESSES):
            return False, f"'{hostname}' resolves to cloud metadata address {text}"

        if not allow_private and _address_is_private(address):
            return False, (
                f"'{hostname}' resolves to {text}, which is on a private or "
                f"reserved network"
            )

    return True, f"'{hostname}' is allowed"


def assert_safe_url(
    raw_url: str,
    *,
    allow_private: bool = False,
    allowed_hosts: Optional[Iterable[str]] = None,
    what: str = "URL",
) -> str:
    """
    Validate a URL, raising ValueError with the reason if it isn't allowed.

    Args:
        raw_url (str): The URL to check
        allow_private (bool): See validate_outbound_url
        allowed_hosts (Optional[Iterable[str]]): See validate_outbound_url
        what (str): Name for the URL in the error message

    Returns:
        str: The URL, stripped

    Raises:
        ValueError: If the URL must not be fetched
    """
    allowed, reason = validate_outbound_url(
        raw_url, allow_private=allow_private, allowed_hosts=allowed_hosts,
    )
    if not allowed:
        logging.warning(f"Refused to fetch {what}: {reason}")
        raise ValueError(f"{what} rejected: {reason}")
    return raw_url.strip()
