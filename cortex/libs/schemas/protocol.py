"""Version negotiation payloads for authenticated external transports."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CURRENT_PROTOCOL_VERSION: Final = "2.0"
LEGACY_PROTOCOL_VERSION: Final = "1.0"
SUPPORTED_PROTOCOL_VERSIONS: Final = (
    LEGACY_PROTOCOL_VERSION,
    CURRENT_PROTOCOL_VERSION,
)


def _supported_versions() -> list[Literal["1.0", "2.0"]]:
    return ["1.0", "2.0"]


def parse_protocol_version(value: str) -> tuple[int, int]:
    """Parse a strict ``major.minor`` protocol version."""

    parts = value.split(".")
    if len(parts) != 2 or any(not part.isdigit() for part in parts):
        raise ValueError("protocol version must use major.minor decimal form")
    return int(parts[0]), int(parts[1])


def negotiate_protocol(offers: list[str] | tuple[str, ...]) -> str | None:
    """Select the newest mutually supported version without crossing majors."""

    parsed_offers: list[tuple[int, int, str]] = []
    for raw in offers:
        try:
            major, minor = parse_protocol_version(raw)
        except ValueError:
            continue
        parsed_offers.append((major, minor, raw))

    server = {
        parse_protocol_version(version): version
        for version in SUPPORTED_PROTOCOL_VERSIONS
    }
    compatible: list[tuple[int, int, str]] = []
    for major, minor, _raw in parsed_offers:
        same_major = [
            (srv_major, srv_minor, version)
            for (srv_major, srv_minor), version in server.items()
            if srv_major == major and srv_minor <= minor
        ]
        compatible.extend(same_major)
    if not compatible:
        return None
    return max(compatible, key=lambda item: (item[0], item[1]))[2]


class AuthRequestPayload(BaseModel):
    """Authenticated protocol offer sent as the first WebSocket payload."""

    model_config = ConfigDict(extra="ignore")

    auth_token: str = Field(..., min_length=1)
    protocol_version: str | None = Field(
        None,
        description="Preferred major.minor version; absent means legacy 1.0",
    )
    supported_protocol_versions: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Versions the client can decode, newest preference optional",
    )

    @field_validator("protocol_version")
    @classmethod
    def _validate_preferred(cls, value: str | None) -> str | None:
        if value is not None:
            parse_protocol_version(value)
        return value

    @field_validator("supported_protocol_versions")
    @classmethod
    def _validate_supported(cls, values: list[str]) -> list[str]:
        for value in values:
            parse_protocol_version(value)
        return values

    def offers(self) -> list[str]:
        """Return the ordered offer set, defaulting missing legacy clients."""

        offers: list[str] = []
        if self.protocol_version is not None:
            offers.append(self.protocol_version)
        offers.extend(
            version
            for version in self.supported_protocol_versions
            if version not in offers
        )
        return offers or [LEGACY_PROTOCOL_VERSION]


class AuthOkPayload(BaseModel):
    """Daemon protocol selection returned only after token validation."""

    model_config = ConfigDict(extra="forbid")

    selected_protocol_version: Literal["1.0", "2.0"]
    server_protocol_version: Literal["2.0"] = "2.0"
    supported_protocol_versions: list[Literal["1.0", "2.0"]] = Field(
        default_factory=_supported_versions
    )
    capabilities: list[str] = Field(
        default_factory=lambda: [
            "dual_clock_metadata",
            "event_identity",
            "causation_ids",
        ]
    )


class ProtocolErrorPayload(BaseModel):
    """Machine-readable negotiation error sent before protocol close."""

    model_config = ConfigDict(extra="forbid")

    code: Literal["unsupported_protocol", "malformed_protocol"]
    offered_protocol_versions: list[str] = Field(default_factory=list)
    supported_protocol_versions: list[Literal["1.0", "2.0"]] = Field(
        default_factory=_supported_versions
    )
