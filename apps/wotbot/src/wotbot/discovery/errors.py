"""The discovery error taxonomy.

Every expected failure in this package inherits from :class:`DiscoveryError`,
and the ones an external source can cause inherit from :class:`ProviderError`.
Orchestration code catches ``ProviderError`` and reports a degraded result;
anything else is a defect and is allowed to reach the error handler with its
traceback intact.

Input that the caller controls is still rejected with a plain ``ValueError`` so
the API layer keeps mapping it to 422. The distinction is deliberate: a bad
request is the caller's fault, a ``ProviderError`` is the source's fault, and
everything else is ours.
"""

from __future__ import annotations


class DiscoveryError(Exception):
    """Any expected discovery failure."""


class ProviderError(DiscoveryError):
    """A failure caused by an external source rather than by wotbot."""


class SourceUnavailableError(ProviderError):
    """A source could not be reached, or answered with an error status."""


class SourceAuthenticationError(ProviderError):
    """A source rejected its stored credential without exposing response details."""


class SourceProtocolError(ProviderError):
    """A source answered, but with content this provider cannot use."""


class SourceResponseTooLargeError(SourceProtocolError):
    """A response exceeded the HTTP client's byte budget."""


class SourceConfigurationError(ProviderError):
    """A stored source record cannot be turned into a usable runtime source.

    This is a registry data problem rather than a defect, so it is reported as
    a degraded result and logged. Its message may name internal configuration,
    so callers must not forward it to the user.
    """


class UnsafeUrlError(ProviderError):
    """A source referenced a URL that the public-network policy refuses."""


class StaleCandidateError(ProviderError):
    """A candidate was derived from source content that has since changed."""


class SourceConflictError(DiscoveryError):
    """A source cannot be changed while other records depend on it."""


class RefreshConflictError(DiscoveryError):
    """A Thing or its source changed after a refresh was previewed."""


class CredentialChallengeError(DiscoveryError):
    """A source needs credentials before the requested work can continue.

    This is control flow rather than a failure: the caller turns it into a
    credential prompt and retries once the secret has been stored.
    """

    def __init__(
        self,
        *,
        status: str,
        source_id: str,
        security_name: str,
        scheme: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.source_id = source_id
        self.security_name = security_name
        self.scheme = scheme

    def public(self) -> dict[str, str]:
        return {
            "status": self.status,
            "owner_kind": "source",
            "source_id": self.source_id,
            "security_name": self.security_name,
            "scheme": self.scheme,
            "message": str(self),
        }


__all__ = [
    "CredentialChallengeError",
    "DiscoveryError",
    "ProviderError",
    "RefreshConflictError",
    "SourceAuthenticationError",
    "SourceConfigurationError",
    "SourceConflictError",
    "SourceProtocolError",
    "SourceResponseTooLargeError",
    "SourceUnavailableError",
    "StaleCandidateError",
    "UnsafeUrlError",
]
