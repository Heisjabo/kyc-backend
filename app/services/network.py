"""Device and Network Intelligence: what the address and the browser say about a person.

Shufti's report has a block of this, and it is what turns "the check passed" into
"the check passed from a data centre in another country through a proxy". Every
field in it comes from one of three places:

- **The request.** The IP (nginx's X-Real-IP, which the client cannot set), the
  user agent, and any proxy headers that arrived with it.
- **The browser.** The session page sends a fingerprint, its timezone and the few
  signs of an automated or emulated browser that a web page can see
  (`DeviceSignals`), with ipapi.co's answer about its own address and the
  addresses its WebRTC left from. ipapi's location is used when its IP is the
  one that reached us; a different IP from either is itself a finding.
- **Lookups we hold ourselves.** DB-IP's free City and ASN databases (CC BY 4.0,
  hence the credit on the report), the Tor Project's list of exit nodes, a
  list of ASNs that belong to hosting and VPN providers (brianhama/bad-asn-list,
  MIT), and the addresses of consumer VPN servers (az0/vpn_ip, GPL-3.0). All
  are downloaded into NETWORK_DATA_DIR and refreshed in the background, so our
  server never sends an applicant's address to somebody else's API.

A rooted or jailbroken phone cannot be seen from a web page, so that line stays
unanswered rather than claiming "No".
"""

import csv
import gzip
import ipaddress
import logging
import math
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import maxminddb
import pycountry
from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import DeviceSession, Step, Verification
from app.reports.verification import _browser
from app.schemas.device import DeviceSignals

log = logging.getLogger(__name__)

# What is downloaded, from where, and how long a copy is trusted for.
_DBIP = "https://download.db-ip.com/free/dbip-{kind}-lite-{month}.mmdb.gz"
_TOR = "https://check.torproject.org/torbulkexitlist"
_HOSTING = "https://raw.githubusercontent.com/brianhama/bad-asn-list/master/bad-asn-list.csv"
# az0/vpn_ip (GPL-3.0): the addresses consumer VPN servers actually answer on
# (NordVPN, PIA, CyberGhost, Windscribe, ProtonVPN, Hola and others), resolved
# daily from the providers' own server lists. Downloaded and read here, never
# shipped with the service.
_VPN = "https://az0-vpnip-public.oooninja.com/ip.txt"
_CITY, _ASN, _TOR_FILE, _HOSTING_FILE = "dbip-city-lite.mmdb", "dbip-asn-lite.mmdb", "tor-exits.txt", "hosting-asns.csv"
_VPN_FILE = "vpn-servers.txt"
_MAX_AGE = {_CITY: 35 * 86400, _ASN: 35 * 86400, _TOR_FILE: 6 * 3600, _HOSTING_FILE: 7 * 86400,
            _VPN_FILE: 24 * 3600}

# Hosting providers that are mostly VPN exits rather than servers somebody runs:
# an address in one of these is read as a proxy, not only as a data centre.
_VPN_ASNS = {
    9009,     # M247, behind most of the large consumer VPNs
    60068,    # Datacamp / CDN77
    212238,   # Datacamp
    147049,   # PacketHub
    62240,    # Clouvider
    136787,   # TEFINCOM (NordVPN)
    207137,   # PacketHub
    209854,   # Surfshark
    203020,   # HostRoyale
    199218,   # ProtonVPN
    209103,   # Proton
}

# Words in an ASN's name that say what kind of network it is.
_HOSTING_WORDS = ("hosting", "host ", "datacenter", "data center", "data centre", "cloud", "server", "vps", "colocation")
_MOBILE_WORDS = ("mobile", "cellular", "wireless", "lte", "4g", "5g", "gsm", "mtn", "airtel", "safaricom",
                 "vodacom", "vodafone", "orange", "tigo", "glo ", "globacom", "9mobile", "telkom kenya",
                 "t-mobile", "verizon wireless", "ethio telecom")

# Graphics that only a virtual machine, an emulator or a headless browser draws with.
_SOFTWARE_RENDERERS = ("swiftshader", "llvmpipe", "softpipe", "virtualbox", "vmware", "android emulator",
                       "mesa offscreen", "microsoft basic render")

_lock = threading.Lock()
_refreshing = threading.Event()
_last_check = 0.0


# The data files


def _dir() -> Path:
    return Path(settings.NETWORK_DATA_DIR)


def _stale(name: str) -> bool:
    path = _dir() / name
    return not path.exists() or time.time() - path.stat().st_mtime > _MAX_AGE[name]


def _fetch(url: str, target: Path, unzip: bool = False) -> None:
    """Download to a temporary name and move it into place, so a reader never sees half a file."""
    partial = target.with_suffix(target.suffix + ".part")
    with httpx.stream("GET", url, timeout=120, follow_redirects=True) as response:
        response.raise_for_status()
        with open(partial, "wb") as out:
            for chunk in response.iter_bytes():
                out.write(chunk)
    if unzip:
        unpacked = target.with_suffix(target.suffix + ".unpacked")
        with gzip.open(partial, "rb") as src, open(unpacked, "wb") as out:
            shutil.copyfileobj(src, out)
        partial.unlink()
        partial = unpacked
    partial.replace(target)


def refresh() -> None:
    """Bring every data file up to date. Each one that fails keeps its old copy."""
    _dir().mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    # DB-IP names its files by month and publishes at the start of it, so the
    # previous month's is the fallback for the first days.
    months = [now.strftime("%Y-%m"), (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")]
    for name, kind in ((_ASN, "asn"), (_CITY, "city")):
        if not _stale(name):
            continue
        for month in months:
            try:
                _fetch(_DBIP.format(kind=kind, month=month), _dir() / name, unzip=True)
                break
            except Exception as error:  # noqa: BLE001, the next month or the old copy
                log.warning("DB-IP %s %s not downloaded: %s", kind, month, error)
    for name, url in ((_TOR_FILE, _TOR), (_HOSTING_FILE, _HOSTING), (_VPN_FILE, _VPN)):
        if _stale(name):
            try:
                _fetch(url, _dir() / name)
            except Exception as error:  # noqa: BLE001, the old copy stays
                log.warning("%s not downloaded: %s", name, error)
    _readers.cache_clear()
    _tor_exits.cache_clear()
    _hosting_asns.cache_clear()
    _vpn_servers.cache_clear()


def refresh_in_background() -> None:
    """Start a refresh if one is due and none is running. Checks at most every ten minutes."""
    global _last_check
    if not settings.NETWORK_DATA_DOWNLOAD:
        return
    with _lock:
        if _refreshing.is_set() or time.time() - _last_check < 600:
            return
        _last_check = time.time()
        if not any(_stale(name) for name in _MAX_AGE):
            return
        _refreshing.set()

    def run():
        try:
            refresh()
        finally:
            _refreshing.clear()

    threading.Thread(target=run, name="network-data", daemon=True).start()


@lru_cache(maxsize=1)
def _readers() -> tuple:
    def open_db(name):
        path = _dir() / name
        return maxminddb.open_database(str(path)) if path.exists() else None

    return open_db(_CITY), open_db(_ASN)


@lru_cache(maxsize=1)
def _tor_exits() -> frozenset[str]:
    path = _dir() / _TOR_FILE
    if not path.exists():
        return frozenset()
    return frozenset(line.strip() for line in path.read_text().splitlines() if line.strip())


@lru_cache(maxsize=1)
def _hosting_asns() -> frozenset[int]:
    path = _dir() / _HOSTING_FILE
    if not path.exists():
        return frozenset()
    with open(path, newline="") as f:
        return frozenset(int(row["ASN"]) for row in csv.DictReader(f) if row.get("ASN", "").isdigit())


@lru_cache(maxsize=1)
def _vpn_servers() -> dict[str, str]:
    """Each VPN server address and the provider it belongs to ("1.2.3.4 # nordvpn")."""
    path = _dir() / _VPN_FILE
    if not path.exists():
        return {}
    servers = {}
    for line in path.read_text().splitlines():
        address, _, provider = line.partition("#")
        address = address.strip()
        if address and not line.startswith("#"):
            servers[address] = provider.strip() or "VPN"
    return servers


def ready() -> dict[str, bool]:
    """Which of the lookups have data, for the report's footnote and for health."""
    city, asn = _readers()
    return {"geo": city is not None, "asn": asn is not None, "tor": bool(_tor_exits()),
            "hosting": bool(_hosting_asns()), "vpn": bool(_vpn_servers())}


# Timezones


@lru_cache(maxsize=1)
def _zones() -> list[tuple[list[str], float, float, str]]:
    """zone.tab: each zone's countries and the coordinates of its principal city."""
    def degrees(text: str, width: int) -> float:
        sign = -1 if text[0] == "-" else 1
        body = text[1:]
        d, rest = int(body[:width]), body[width:]
        m = int(rest[:2])
        s = int(rest[2:4]) if len(rest) >= 4 else 0
        return sign * (d + m / 60 + s / 3600)

    table = (resources.files("tzdata") / "zoneinfo" / "zone.tab").read_text()
    zones = []
    for line in table.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        codes, coordinates, name = line.split("\t")[:3]
        split = max(coordinates.rfind("+"), coordinates.rfind("-"))
        lat, lon = coordinates[:split], coordinates[split:]
        zones.append((codes.split(","), degrees(lat, 2), degrees(lon, 3), name))
    return zones


def ip_timezone(country: str | None, latitude: float | None, longitude: float | None) -> str | None:
    """The zone of the nearest principal city in the same country as the address."""
    if not country:
        return None
    candidates = [z for z in _zones() if country.upper() in z[0]]
    if not candidates:
        return None
    if latitude is None or longitude is None or len(candidates) == 1:
        return candidates[0][3]

    def distance(zone):
        _, lat, lon, _ = zone
        return math.hypot(lat - latitude, (lon - longitude) * math.cos(math.radians(latitude)))

    return min(candidates, key=distance)[3]


def _offset(zone: str | None) -> int | None:
    """Minutes ahead of UTC right now, so two names for one offset are not a mismatch."""
    if not zone:
        return None
    try:
        delta = datetime.now(ZoneInfo(zone)).utcoffset()
    except Exception:  # noqa: BLE001, an unknown name from a browser
        return None
    return int(delta.total_seconds() // 60) if delta is not None else None


# The address


def client_ip(request: Request) -> str | None:
    """The address the person connected from.

    nginx sets X-Real-IP to the address it saw and overwrites any the client sent.
    X-Forwarded-For is only read without nginx in front (a laptop), and then its
    last entry, the one added by whoever spoke to us, not the first, which anyone
    can write.
    """
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real
    forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
    if forwarded:
        return forwarded[-1]
    return request.client.host if request.client else None


def _proxy_headers(request: Request) -> bool:
    """Did the request pass through another proxy before nginx?

    nginx appends the address it saw to X-Forwarded-For, so a single entry is the
    person. More than one, or a Via or Forwarded header, means something in
    between added its own.
    """
    forwarded = [p for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    return len(forwarded) > 1 or "via" in request.headers or "forwarded" in request.headers


@dataclass
class Address:
    ip: str | None
    private: bool = False
    continent: str | None = None
    country: str | None = None
    country_name: str | None = None
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    asn: int | None = None
    asn_name: str | None = None
    timezone: str | None = None
    is_tor: bool | None = None
    is_datacenter: bool | None = None
    vpn_network: bool = False
    # The provider, when the address is a known VPN server.
    vpn_provider: str | None = None
    mobile_network: bool = False


def lookup(ip: str | None) -> Address:
    """Everything the held data says about one address."""
    address = Address(ip=ip)
    try:
        parsed = ipaddress.ip_address(ip) if ip else None
    except ValueError:
        parsed = None
    if parsed is None or not parsed.is_global:
        # Not a public address (a private range, or a name such as a test
        # client's): nothing to look up.
        address.private = True
        return address

    city_db, asn_db = _readers()
    if city_db is not None:
        record = city_db.get(ip) or {}
        address.continent = (record.get("continent") or {}).get("names", {}).get("en")
        country = record.get("country") or {}
        address.country = country.get("iso_code")
        address.country_name = country.get("names", {}).get("en")
        address.city = (record.get("city") or {}).get("names", {}).get("en")
        location = record.get("location") or {}
        address.latitude, address.longitude = location.get("latitude"), location.get("longitude")
        address.timezone = location.get("time_zone") or ip_timezone(
            address.country, address.latitude, address.longitude
        )
    if asn_db is not None:
        record = asn_db.get(ip) or {}
        address.asn = record.get("autonomous_system_number")
        address.asn_name = record.get("autonomous_system_organization")

    exits = _tor_exits()
    address.is_tor = (ip in exits) if exits else None
    address.vpn_provider = _vpn_servers().get(ip)
    hosting = _hosting_asns()
    name = (address.asn_name or "").lower()
    if address.asn is not None:
        address.vpn_network = address.asn in _VPN_ASNS or address.vpn_provider is not None
        address.is_datacenter = (address.asn in hosting or address.vpn_network
                                 or any(word in name for word in _HOSTING_WORDS))
        address.mobile_network = any(word in name for word in _MOBILE_WORDS)
    return address


# The browser


def _emulated(signals: DeviceSignals, user_agent: str | None) -> tuple[bool, list[str]]:
    """Signs that this is not a real phone or computer in somebody's hand."""
    reasons = []
    agent = (user_agent or "").lower()
    if signals.webdriver:
        reasons.append("the browser is driven by automation (navigator.webdriver)")
    if "headless" in agent:
        reasons.append("the browser is headless")
    renderer = (signals.webgl_renderer or "").lower()
    if any(word in renderer for word in _SOFTWARE_RENDERERS):
        reasons.append(f"graphics are drawn in software ({signals.webgl_renderer})")
    phone = "android" in agent or "iphone" in agent
    platform = (signals.platform or "").lower()
    if phone and signals.touch_points == 0:
        reasons.append("a phone's user agent on a screen with no touch")
    elif phone and "android" in agent and ("x86" in platform or "win" in platform or "mac" in platform):
        reasons.append(f"an Android user agent on a {signals.platform} platform")
    return bool(reasons), reasons


# Putting it together


def _country_code(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    try:
        if len(value) == 2:
            found = pycountry.countries.get(alpha_2=value.upper())
        elif len(value) == 3:
            found = pycountry.countries.get(alpha_3=value.upper())
        else:
            found = pycountry.countries.lookup(value)
    except LookupError:
        found = None
    return found.alpha_2 if found else None


def _expected_country(verification: Verification) -> str | None:
    """Where the person is meant to be: the client's country for the session, else the one they claimed."""
    return _country_code(verification.country) or _country_code((verification.claimed or {}).get("country"))


def _addresses_seen(db: Session, verification: Verification, ip: str | None) -> list[str]:
    """Every address this verification has been reached from, in the order they appeared."""
    seen: list[str] = []
    rows = db.scalars(
        select(DeviceSession.ip).where(DeviceSession.verification_id == verification.id).order_by(DeviceSession.id)
    ).all()
    steps = db.scalars(
        select(Step).where(Step.verification_id == verification.id).order_by(Step.started_at, Step.id)
    ).all()
    for candidate in [*rows, *((s.device or {}).get("ip") for s in steps), ip]:
        if candidate and candidate not in seen:
            seen.append(candidate)
    return seen


def _assess(row: DeviceSession, address: Address, emulated: list[str], proxied_headers: bool,
            frequent: bool, browser_ip_differs: bool | None, leaked: list[str]) -> tuple[int, str, str, str]:
    """Score, risk level, threat level and a one-line summary, from what was found."""
    findings: list[tuple[int, str]] = []
    if row.is_tor:
        findings.append((60, "the address is a Tor exit node"))
    if address.vpn_provider:
        findings.append((40, f"the address is a known VPN server ({address.vpn_provider})"))
    elif row.is_proxy:
        findings.append((35, "the connection comes through a proxy or VPN"
                         + (" (proxy headers on the request)" if proxied_headers else "")))
    if row.is_datacenter and not row.is_proxy:
        findings.append((25, f"the address belongs to a hosting provider ({address.asn_name})"))
    if emulated:
        findings.append((30, "the device looks emulated: " + "; ".join(emulated)))
    if row.country_mismatch:
        findings.append((15, "the address is in a different country from the one given for the person"))
    if row.timezone_mismatch:
        findings.append((15, "the browser's timezone differs from the address's"))
    if frequent:
        findings.append((10, "the address changed several times during the verification"))
    if browser_ip_differs:
        findings.append((20, "the browser's own lookup saw a different address from the one that reached us"))
    if leaked:
        findings.append((25, "the browser's direct (WebRTC) connection came from another address: "
                         + ", ".join(leaked)))

    score = min(100, sum(points for points, _ in findings))
    risk = "low" if score < 25 else "medium" if score < 60 else "high"
    threat = "high" if row.is_tor else "medium" if (row.is_proxy or row.is_datacenter) else "low"
    if findings:
        summary = "; ".join(text for _, text in findings)
        summary = summary[0].upper() + summary[1:] + "."
    else:
        summary = "No risk signals: a consumer network in the expected place, from an ordinary browser."
    return score, risk, threat, summary


_CONTINENTS = {"AF": "Africa", "AN": "Antarctica", "AS": "Asia", "EU": "Europe", "NA": "North America",
               "OC": "Oceania", "SA": "South America"}


def _same_family(a: str | None, b: str | None) -> bool:
    try:
        return ipaddress.ip_address(a).version == ipaddress.ip_address(b).version
    except ValueError:
        return False


def _use_browser_lookup(address: Address, found) -> tuple[Address, str, bool | None]:
    """Fold ipapi.co's answer to the browser into what we hold.

    Returns the address to keep, where its location came from, and whether the
    browser saw a different IP from the one that reached us (None when that
    cannot be compared).

    ipapi's location is used only for the address the request actually came
    from: it is the browser's word, and a page someone has tampered with could
    send any city. Our own ASN, Tor and hosting checks stand either way. A
    browser that reached ipapi over IPv6 and us over IPv4 is the same person on
    two protocols, so the addresses are only compared within one family.
    """
    if found is None or not found.ip:
        return address, "db-ip", None
    if address.private:
        # No nginx in front (a laptop): the browser's view is the only public
        # address there is.
        address = lookup(found.ip)
        if address.private:
            return address, "db-ip", None
    elif found.ip != address.ip:
        return address, "db-ip", (True if _same_family(found.ip, address.ip) else None)

    address.continent = _CONTINENTS.get((found.continent_code or "").upper(), address.continent)
    address.country = (found.country_code or address.country or "").upper() or None
    address.country_name = found.country_name or address.country_name
    address.city = found.city or address.city
    if found.latitude is not None and found.longitude is not None:
        address.latitude, address.longitude = found.latitude, found.longitude
    address.timezone = found.timezone or address.timezone
    if address.asn is None and found.asn and found.asn.upper().removeprefix("AS").isdigit():
        address.asn = int(found.asn.upper().removeprefix("AS"))
        address.asn_name = found.org
    return address, "ipapi.co", False


def _webrtc_leaks(ip: str | None, found: list[str]) -> list[str]:
    """Public addresses WebRTC reached the internet from that are not the one that reached us.

    A VPN app carries all of the machine's traffic, so WebRTC's STUN request
    leaves from the same address. A browser extension or a proxy only carries
    web requests, and WebRTC's UDP goes around it from the real connection:
    that address shows up here. Compared within one IP family, because a
    browser can reach STUN over IPv6 and us over IPv4 on the same line.
    """
    leaks = []
    for candidate in found:
        try:
            parsed = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if parsed.is_global and candidate != ip and _same_family(candidate, ip) and candidate not in leaks:
            leaks.append(candidate)
    return leaks


def record(db: Session, verification: Verification, request: Request, signals: DeviceSignals) -> DeviceSession:
    """Look at this request and this browser, and keep what they say against the verification.

    One row per address and fingerprint: the same browser on the same network
    coming back is the same row seen again, a new network is a new row.
    """
    refresh_in_background()
    seen_ip = client_ip(request)
    address, location_source, browser_ip_differs = _use_browser_lookup(lookup(seen_ip), signals.ip_lookup)
    ip = address.ip
    user_agent = request.headers.get("user-agent")
    fingerprint = (signals.fingerprint or "")[:64] or None

    # Two pages of one session can report at the same moment. Held until the
    # commit, so the second finds the first's row instead of adding its own.
    db.execute(select(func.pg_advisory_xact_lock(verification.id)))
    row = db.scalar(
        select(DeviceSession).where(
            DeviceSession.verification_id == verification.id,
            DeviceSession.ip == ip,
            DeviceSession.fingerprint == fingerprint,
        )
    )
    if row is None:
        row = DeviceSession(verification_id=verification.id, ip=ip, fingerprint=fingerprint)
        db.add(row)

    browser, system = _browser(user_agent)
    is_emulated, emulated = _emulated(signals, user_agent)
    proxied_headers = _proxy_headers(request)

    row.country = address.country
    row.city = address.city
    row.latitude, row.longitude = address.latitude, address.longitude
    row.asn = f"AS{address.asn}" if address.asn is not None else None
    row.asn_name = address.asn_name
    row.timezone = address.timezone
    row.operating_system = system if user_agent else None
    row.user_agent = user_agent
    row.is_tor = address.is_tor
    row.is_datacenter = address.is_datacenter
    if address.private:
        row.is_proxy = proxied_headers
    elif address.is_datacenter is None and not proxied_headers:
        row.is_proxy = None if address.is_tor is None else bool(address.is_tor)
    else:
        row.is_proxy = bool(proxied_headers or address.vpn_network or address.is_tor)

    expected = _expected_country(verification)
    row.country_mismatch = (address.country != expected) if (address.country and expected) else None
    ip_offset, browser_offset = _offset(address.timezone), _offset(signals.timezone)
    row.timezone_mismatch = (ip_offset != browser_offset) if (ip_offset is not None and browser_offset is not None) else None
    # A data centre address whose clock disagrees with the browser's is almost
    # always somebody at home behind a VPN.
    if row.is_datacenter and row.timezone_mismatch:
        row.is_proxy = True
    # The browser reaching ipapi from one address and us from another means one
    # of the two connections went through something else.
    if browser_ip_differs:
        row.is_proxy = True
    leaked = _webrtc_leaks(ip, signals.webrtc_ips)
    if leaked:
        row.is_proxy = True
    row.emulated_device = is_emulated
    row.rooted_device = None

    seen = _addresses_seen(db, verification, ip)
    frequent = len(seen) >= 3
    score, risk, threat, summary = _assess(row, address, emulated, proxied_headers, frequent, browser_ip_differs,
                                         leaked)
    row.fraud_score, row.risk_level, row.threat_level = score, risk, threat

    if address.private:
        routing = "private network"
    elif row.is_tor:
        routing = "anonymizer"
    elif address.vpn_network:
        routing = "vpn"
    elif row.is_proxy:
        routing = "proxy"
    elif row.is_datacenter:
        routing = "hosting"
    elif address.mobile_network:
        routing = "mobile gateway"
    elif address.asn is not None:
        routing = "fixed"
    else:
        routing = None

    row.signals = {
        "continent": address.continent,
        "country_name": address.country_name,
        "browser": browser if user_agent else None,
        "browser_timezone": signals.timezone,
        "fingerprint_confidence": signals.fingerprint_confidence,
        "fingerprint_components": signals.fingerprint_components,
        "languages": signals.languages,
        "screen": signals.screen,
        "platform": signals.platform,
        "touch_points": signals.touch_points,
        "webgl_renderer": signals.webgl_renderer,
        "hardware_concurrency": signals.hardware_concurrency,
        "device_memory": signals.device_memory,
        "webdriver": signals.webdriver,
        "emulation_reasons": emulated,
        "proxy_headers": proxied_headers,
        "expected_country": expected,
        "ip_routing_type": routing,
        "addresses_seen": seen,
        "frequent_ip_changes": frequent,
        "stable_ip": len(seen) == 1,
        "private_address": address.private,
        "summary": summary,
        "location_source": location_source,
        "server_ip": seen_ip,
        "browser_ip": signals.ip_lookup.ip if signals.ip_lookup else None,
        "browser_ip_differs": browser_ip_differs,
        "browser_lookup": signals.ip_lookup.model_dump() if signals.ip_lookup else None,
        "vpn_provider": address.vpn_provider,
        "webrtc_ips": signals.webrtc_ips,
        "webrtc_leaks": [
            {"ip": leak, "country": (found := lookup(leak)).country, "city": found.city,
             "asn": f"AS{found.asn}" if found.asn is not None else None, "asn_name": found.asn_name}
            for leak in leaked
        ],
        "sources": ready(),
    }
    db.commit()
    db.refresh(row)
    return row
