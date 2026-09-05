"""The forge port: typed failures shared by every provider adapter.

Step 1 of the port extraction (issue #131) declares only the six failure
types every adapter translates its provider-specific errors into. The port
surface itself (`RepositoryId`, `Capability`, `ForgeOperation`,
`ForgeReader`/`ForgeWriter`) is added once a caller exists for it.
"""

from __future__ import annotations

from .protocol import ClaimError


class ForgeError(ClaimError):
    """An unclassified forge failure."""


class ForgeUnsupportedError(ForgeError):
    """The forge cannot perform this operation at all."""


class ForgePermissionDeniedError(ForgeError):
    """The forge refused the operation as an authorization failure."""


class ForgeNotFoundError(ForgeError):
    """The forge reports that the named subject does not exist."""


class ForgeTransientError(ForgeError):
    """The forge failed in a way a retry might not."""


class ForgeMalformedResponseError(ForgeError):
    """The forge's response could not be parsed into the expected shape."""
