import pytest

from sniper.safety import SafetyService


class FakeRpc:
    def __init__(self, info):
        self.info = info

    async def get_mint_info(self, mint):
        return self.info


class FakePrices:
    async def usd_prices(self, mints):
        return {"So11111111111111111111111111111111111111112": 100.0}

    async def pair_liquidity_sol(self, mint, sol_usd):
        return 4.0


class FakeHttp:
    async def get(self, url, timeout=15):
        class Response:
            status_code = 503
            text = "down"

            def json(self):
                return {}

        return Response()


@pytest.mark.asyncio
async def test_autosnipe_fails_closed_on_freeze_and_missing_rugcheck():
    service = SafetyService(
        FakeHttp(),
        FakeRpc({"decimals": 6, "mint_authority": "MintAuth", "freeze_authority": "FreezeAuth", "supply": "1"}),
        FakePrices(),
        "https://api.rugcheck.xyz",
        require_rugcheck=True,
        require_mint_revoked=True,
    )
    report = await service.evaluate("Mint111111111111111111111111111111111111111", liquidity_sol=2, min_liquidity_sol=10)
    assert not report.ok_for_autosnipe
    text = " ".join(report.blockers)
    assert "freeze" in text
    assert "mint authority" in text
    assert "Risk check unavailable" in text
    assert "liquidity" in text


@pytest.mark.asyncio
async def test_clean_mint_can_pass_when_rugcheck_not_required():
    class OkHttp:
        async def get(self, url, timeout=15):
            class Response:
                status_code = 200

                def json(self):
                    return {"riskLevel": "Good", "lpLockedPct": 95, "risks": []}

            return Response()

    service = SafetyService(
        OkHttp(),
        FakeRpc({"decimals": 9, "mint_authority": None, "freeze_authority": None, "supply": "1"}),
        FakePrices(),
        "https://api.rugcheck.xyz",
        require_rugcheck=False,
        require_mint_revoked=True,
    )
    report = await service.evaluate("Mint111111111111111111111111111111111111111", liquidity_sol=20, min_liquidity_sol=10)
    assert report.ok_for_autosnipe
    assert report.decimals == 9
    assert report.lp_locked_pct == 95
