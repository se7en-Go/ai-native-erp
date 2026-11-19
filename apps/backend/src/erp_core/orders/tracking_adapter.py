from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

import httpx

MAIN_STATUS_MAP = {
    "0": "not_found",
    "NotFound": "not_found",
    "10": "info_received",
    "InfoReceived": "info_received",
    "20": "in_transit",
    "InTransit": "in_transit",
    "30": "expired",
    "Expired": "expired",
    "35": "available_for_pickup",
    "AvailableForPickup": "available_for_pickup",
    "40": "out_for_delivery",
    "OutForDelivery": "out_for_delivery",
    "50": "undelivered",
    "DeliveryFailure": "undelivered",
    "60": "delivered",
    "Delivered": "delivered",
    "70": "alert",
    "Exception": "alert",
}

SUB_STATUS_MAP = {
    "NotFound_Other": "not_found_other",
    "NotFound_InvalidCode": "invalid_code",
    "InfoReceived_NoTrackingYet": "no_tracking_yet",
    "InTransit_PickedUp": "picked_up",
    "InTransit_Other": "in_transit_other",
    "InTransit_Departure": "departure",
    "InTransit_Arrival": "arrival",
    "InTransit_CustomsProcessing": "customs_processing",
    "InTransit_CustomsReleased": "customs_released",
    "InTransit_CustomsRequiringInformation": "customs_info_required",
    "Expired_Other": "expired_other",
    "AvailableForPickup_Other": "pickup_other",
    "AvailableForPickup_Ready": "ready_for_pickup",
    "AvailableForPickup_Overdue": "pickup_overdue",
    "OutForDelivery_Other": "out_for_delivery_other",
    "OutForDelivery_Delay": "out_for_delivery_delay",
    "DeliveryFailure_Other": "delivery_failure_other",
    "DeliveryFailure_NoBody": "no_recipient",
    "DeliveryFailure_Security": "security_issue",
    "DeliveryFailure_Rejected": "rejected_by_recipient",
    "DeliveryFailure_InvalidAddress": "invalid_address",
    "Delivered_Other": "delivered_other",
    "Delivered_Signed": "signed",
    "Exception_Other": "exception_other",
    "Exception_Returning": "returning_to_sender",
    "Exception_Returned": "returned_to_sender",
    "Exception_NoBody": "undeliverable_no_body",
    "Exception_Lost": "lost_in_transit",
    "Exception_Damaged": "damaged",
    "Exception_CustomsHold": "customs_hold",
}


def verify_17track_signature(raw_body: bytes, received_sign: str, webhook_secret: str) -> bool:
    """Verify the 17TRACK v2.2 `sign` header without decoding the body."""
    signature = received_sign.strip().lower()
    if not webhook_secret or len(signature) != 64 or not signature.isascii():
        return False
    expected = hashlib.sha256(raw_body + b"/" + webhook_secret.encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, signature)


def map_17track_status(
    main_status: str | int | None, sub_status: str | None = None
) -> tuple[str, str | None]:
    status = MAIN_STATUS_MAP.get(str(main_status), "not_found")
    sub = SUB_STATUS_MAP.get(sub_status, sub_status.lower()) if sub_status else None
    return status, sub


class Tracking17TrackAdapter:
    verify_webhook_signature = staticmethod(verify_17track_signature)

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.17track.net/track/v2.2",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("TRACK17_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self._client = http_client

    async def _post(self, path: str, item: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise ValueError("TRACK17_API_KEY is not configured")
        url = f"{self.base_url}{path}"
        headers = {"17token": self.api_key}
        if self._client is not None:
            response = await self._client.post(url, headers=headers, json=[item])
        else:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(url, headers=headers, json=[item])
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("17TRACK response is not a JSON object")
        return result

    async def register_tracking(
        self, tracking_number: str, carrier_code: str | None = None
    ) -> dict[str, Any]:
        item: dict[str, Any] = {"number": tracking_number.strip()}
        if carrier_code and carrier_code.isascii() and carrier_code.isdecimal():
            item["carrier"] = int(carrier_code)
        else:
            item["auto_detection"] = True
        return await self._post("/register", item)

    async def fetch_status(self, tracking_number: str, cache_level: int = 0) -> dict[str, Any]:
        if type(cache_level) is not int or cache_level != 0:
            raise ValueError("17TRACK requires cacheLevel=0 to avoid consuming 10x quota")
        item = {"number": tracking_number.strip(), "cacheLevel": 0}
        return await self._post("/gettrackinfo", item)
