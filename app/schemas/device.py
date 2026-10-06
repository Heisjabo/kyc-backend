"""What the session page tells us about the browser it is running in."""

import json

from pydantic import BaseModel, Field, field_validator

# The components are the browser's word and go into a JSONB column: capped so a
# tampered page cannot send megabytes.
MAX_COMPONENTS_BYTES = 16_000


class IpLookup(BaseModel):
    """What ipapi.co told the person's browser about its own address.

    Fetched by the page, not by us, so the applicant's address only goes to
    ipapi.co from their own browser. It is the browser's word, so the API checks
    its IP against the address the request actually came from before using it.
    Unknown fields in ipapi's answer are ignored.
    """

    ip: str | None = Field(default=None, max_length=64)
    network: str | None = Field(default=None, max_length=64)
    city: str | None = Field(default=None, max_length=120)
    region: str | None = Field(default=None, max_length=120)
    country_code: str | None = Field(default=None, max_length=8)
    country_name: str | None = Field(default=None, max_length=80)
    continent_code: str | None = Field(default=None, max_length=4)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    timezone: str | None = Field(default=None, max_length=64)
    utc_offset: str | None = Field(default=None, max_length=8)
    asn: str | None = Field(default=None, max_length=32)
    org: str | None = Field(default=None, max_length=160)


class DeviceSignals(BaseModel):
    """Read by the page itself, because none of it arrives with a request.

    Every field is optional: a browser that blocks one of them still gets
    through, and the report shows what it had.
    """

    # FingerprintJS's visitor ID (the open-source library, run in the browser):
    # a hash of about forty browser traits, so one machine is recognised across
    # sessions even when its address changes.
    fingerprint: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9]*$")
    # How sure FingerprintJS is of that ID, 0 to 1.
    fingerprint_confidence: float | None = Field(default=None, ge=0, le=1)
    # The small traits behind the ID (screen, timezone, platform, audio and so
    # on), kept for a reviewer. Large ones (canvas images, font lists) are left
    # out by the page.
    fingerprint_components: dict | None = None
    # The IANA name the browser's clock is set to, e.g. "Africa/Kigali".
    timezone: str | None = Field(default=None, max_length=64)
    languages: list[str] = Field(default_factory=list, max_length=10)
    platform: str | None = Field(default=None, max_length=64)
    screen: str | None = Field(default=None, max_length=32)
    touch_points: int | None = Field(default=None, ge=0, le=64)
    hardware_concurrency: int | None = Field(default=None, ge=0, le=1024)
    device_memory: float | None = Field(default=None, ge=0, le=1024)
    webgl_renderer: str | None = Field(default=None, max_length=200)
    # navigator.webdriver: true when a browser is being driven by automation.
    webdriver: bool = False
    # The public addresses the browser's WebRTC (a STUN request) left from. A
    # browser-extension VPN or a proxy does not carry this traffic, so the real
    # address shows up here.
    webrtc_ips: list[str] = Field(default_factory=list, max_length=8)
    # ipapi.co's answer to the browser, when it gave one.
    ip_lookup: IpLookup | None = None

    @field_validator("fingerprint_components")
    @classmethod
    def _small_enough(cls, value: dict | None) -> dict | None:
        if value is not None and len(json.dumps(value)) > MAX_COMPONENTS_BYTES:
            raise ValueError(f"fingerprint_components is larger than {MAX_COMPONENTS_BYTES} bytes")
        return value
