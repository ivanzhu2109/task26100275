"""Exception types raised by the content receiver library."""


class ReceiverError(Exception):
    """Base class for all receiver errors."""


class ManifestError(ReceiverError):
    """The supplied manifest is malformed or inconsistent."""


class ManifestConflictError(ReceiverError):
    """A different manifest was already registered for this version."""


class UnknownVersionError(ReceiverError):
    """The referenced version has not been registered."""


class UnknownFileError(ReceiverError):
    """The referenced file path is not part of the version's manifest."""
