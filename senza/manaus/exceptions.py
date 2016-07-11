class ManausException(Exception):
    """
    Base class for Manaus exceptions
    """


class VPCError(ManausException, AttributeError):
    """
    Error raised when there are issues with VPCs configuration
    """

    def __init__(self, message: str, number_of_vpcs: int=None):
        super().__init__(message)
        self.number_of_vpcs = number_of_vpcs
