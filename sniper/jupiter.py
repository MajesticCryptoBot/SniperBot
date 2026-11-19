"""Jupiter Swap API v2: GET /order, sign locally, POST /execute."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from html import escape

from sniper.errors import UserFacingError

logger = logging.getLogger(__name__)


@dataclass
class SwapOrder:
    request_id: str
    transaction: str
    in_amount: int
    out_amount: int
    other_amount_threshold: int | None
    price_impact_pct: float
    slippage_bps: int | None
    in_usd: float | None
    out_usd: float | None
    route: str
    # Referral (integrator) fee, when requested: Jupiter picks the fee mint
    # (SOL has top priority) and reports what it will charge.
    fee_mint: str | None = None
    fee_bps: int | None = None
    fee_amount: int | None = None


@dataclass
class ExecuteResult:
    ok: bool
    signature: str | None
    error: str | None
    input_amount: int | None
    output_amount: int | None
    total_input_amount: int | None = None
    total_output_amount: int | None = None


def _impact_percent(data: dict) -> float:
    for key in ("priceImpactPct", "priceImpact"):
        if data.get(key) is None:
            continue
        try:
            value = float(data[key])
        except (TypeError, ValueError):
            continue
        if abs(value) <= 1:
            return abs(value) * 100
        return abs(value)
    return 0.0


def _route_label(data: dict) -> str:
    labels: list[str] = []
    router = data.get("router")
    if router:
        labels.append(str(router))
    for step in data.get("routePlan") or []:
        info = step.get("swapInfo") if isinstance(step, dict) else None
        info = info or (step if isinstance(step, dict) else {})
        label = info.get("label")
        if label and label not in labels:
            labels.append(str(label))
    return " -> ".join(labels) if labels else "auto"


def parse_order(data: dict) -> SwapOrder:
    if not isinstance(data, dict):
        raise UserFacingError("A live quote is temporarily unavailable. Please try again shortly.")
    error = data.get("error") or data.get("errorMessage")
    if error and not data.get("transaction"):
        raise UserFacingError(f"Quote unavailable: {escape(str(error)[:200])}")
    if not data.get("requestId") or not data.get("transaction"):
        raise UserFacingError("No executable swap route is currently available for this token.")
    try:
        in_amount = int(data["inAmount"])
        out_amount = int(data["outAmount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UserFacingError("The quote provider returned an invalid response. Please request a new quote.") from exc
    other = data.get("otherAmountThreshold")
    try:
        other_amount = int(other) if other is not None else None
    except (TypeError, ValueError):
        other_amount = None
    slippage = data.get("slippageBps")
    try:
        slippage_bps = int(slippage) if slippage is not None else None
    except (TypeError, ValueError):
        slippage_bps = None
    return SwapOrder(
        request_id=str(data["requestId"]),
        transaction=str(data["transaction"]),
        in_amount=in_amount,
        out_amount=out_amount,
        other_amount_threshold=other_amount,
        price_impact_pct=_impact_percent(data),
        slippage_bps=slippage_bps,
        in_usd=_optional_float(data.get("inUsdValue")),
        out_usd=_optional_float(data.get("outUsdValue")),
        route=_route_label(data),
        fee_mint=_optional_str(data.get("feeMint")),
        fee_bps=_optional_int(data.get("feeBps")),
        fee_amount=_optional_int(data.get("platformFee") or data.get("feeAmount")),
    )


def parse_execute(data: dict) -> ExecuteResult:
    status = str(data.get("status") or "").lower()
    signature = data.get("signature") or None
    error = data.get("error") or data.get("errorMessage")
    code = data.get("code")
    if error and code not in (None, 0):
        error = f"{error} (code {code})"
    ok = status == "success" and bool(signature)
    return ExecuteResult(
        ok=ok,
        signature=str(signature) if signature else None,
        error=None if ok else (str(error)[:400] if error else f"Jupiter execute status: {data.get('status')}"),
        input_amount=_optional_int(data.get("inputAmountResult") or data.get("totalInputAmount")),
        output_amount=_optional_int(data.get("outputAmountResult") or data.get("totalOutputAmount")),
        total_input_amount=_optional_int(data.get("totalInputAmount")),
        total_output_amount=_optional_int(data.get("totalOutputAmount")),
    )


def actual_fee_amount(order: SwapOrder, result: ExecuteResult, input_mint: str, output_mint: str) -> int | None:
    """Best-known integrator fee taken by an executed swap, in fee-mint raw units.

    Prefers the executed totals (total minus the net that reached the taker) when
    the response exposes both and feeMint matches a side; otherwise falls back to
    the quote's fee estimate. None when nothing about the fee is known."""
    if order.fee_mint == output_mint and result.total_output_amount is not None and result.output_amount is not None:
        fee = abs(result.total_output_amount - result.output_amount)
        if fee:
            return fee
    if order.fee_mint == input_mint and result.total_input_amount is not None and result.input_amount is not None:
        fee = abs(result.total_input_amount - result.input_amount)
        if fee:
            return fee
    return order.fee_amount


def _optional_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class JupiterClient:
    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.http = http

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    async def order(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount: int,
        taker: str,
        slippage_bps: int,
        priority_fee_lamports: int | None = None,
        referral_account: str | None = None,
        referral_fee_bps: int | None = None,
    ) -> SwapOrder:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "taker": taker,
            "slippageBps": str(int(slippage_bps)),
        }
        if priority_fee_lamports:
            # Documented GET /order query parameter — overrides Jupiter's
            # dynamic priority fee for this swap.
            params["prioritizationFeeLamports"] = str(int(priority_fee_lamports))
        if referral_account and referral_fee_bps:
            # Integrator (bot) fee, charged atomically inside the swap by
            # Jupiter — no second transaction, and the fee mint follows
            # Jupiter's priority list (SOL first).
            params["referralAccount"] = referral_account
            params["referralFee"] = str(int(referral_fee_bps))
        try:
            response = await self.http.get(f"{self.base}/order", params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise UserFacingError("The market service is not responding. Please try again shortly.") from exc
        return self._parse_response(response, parse_order, "Jupiter order")

    async def execute(self, signed_b64: str, request_id: str) -> ExecuteResult:
        body = {"signedTransaction": signed_b64, "requestId": request_id}
        headers = {**self._headers(), "Content-Type": "application/json"}
        try:
            response = await self.http.post(f"{self.base}/execute", json=body, headers=headers, timeout=45)
        except httpx.HTTPError as exc:
            raise UserFacingError("The swap could not be submitted. Verify the token and request a fresh quote before retrying.") from exc
        if response.status_code >= 400:
            raise UserFacingError(_http_error("Swap", response))
        try:
            data = response.json()
        except Exception as exc:
            raise UserFacingError("The swap could not be submitted. Please try again shortly.") from exc
        return parse_execute(data if isinstance(data, dict) else {})

    def _parse_response(self, response: httpx.Response, parser, label: str):
        if response.status_code >= 400:
            raise UserFacingError(_http_error(label, response))
        try:
            data = response.json()
        except Exception as exc:
            raise UserFacingError("A live quote is temporarily unavailable. Please try again shortly.") from exc
        return parser(data)


def _http_error(label: str, response: httpx.Response) -> str:
    body = (response.text or "").replace("\n", " ")
    if len(body) > 200:
        body = body[:200] + "..."
    logger.warning("%s failed (HTTP %s): %s", label, response.status_code, body)
    if response.status_code == 429:
        return f"{label} is busy right now (rate limited). Please wait a moment and try again."
    if response.status_code in {401, 403}:
        return f"{label} is temporarily unavailable. Please try again shortly."
    return f"{label} was rejected (HTTP {response.status_code}). Please try again shortly."
