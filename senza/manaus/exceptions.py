class ManausException(Exception):
    """
    Base class for Manaus exceptions
    """


class VPCError(ManausException, AttributeError):
    """
    Error raised when there are issues with VPCs configuration
    """

    def __init__(self, message):
        super().__init__(message)
