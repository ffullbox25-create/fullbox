class FbsError(Exception):
    pass


class FbsFeatureDisabled(FbsError):
    pass


class FbsStorageError(FbsError):
    pass


class FbsReplenishmentError(FbsError):
    pass


class FbsPickingError(FbsError):
    pass


class FbsScanMismatchError(FbsPickingError):
    """A retryable scanner input mismatch that must not quarantine the order."""


class FbsMarkingAlreadyUsedError(FbsPickingError):
    def __init__(
        self,
        *,
        marking_code: str,
        owner_order_id: str = "",
        same_order: bool = False,
    ):
        self.marking_code = str(marking_code or "").strip()
        self.owner_order_id = str(owner_order_id or "").strip()
        self.same_order = bool(same_order)
        if self.same_order:
            message = "Данный КИЗ уже отобран для другой единицы этого заказа."
        elif self.owner_order_id:
            message = (
                "Данный КИЗ уже отобран или использован в другом заказе "
                f"№{self.owner_order_id}."
            )
        else:
            message = "Данный КИЗ уже отобран или использован в другом заказе."
        super().__init__(message)


class FbsLabelError(FbsError):
    pass


class FbsEquipmentError(FbsError):
    pass


class FbsInventoryError(FbsError):
    pass


class FbsMovementError(FbsError):
    pass


class FbsHandoverError(FbsError):
    pass


class FbsIntegrationError(FbsError):
    pass
