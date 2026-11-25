import pytest

from sniper.subscription import SubscriptionGate, is_joined


def test_membership_statuses():
    assert is_joined("member", None)
    assert is_joined("administrator", False)
    assert is_joined("creator", None)
    assert is_joined("ChatMemberStatus.OWNER", None)
    assert is_joined("restricted", True)
    assert not is_joined("restricted", False)
    assert not is_joined("left", None)
    assert not is_joined("kicked", None)


@pytest.mark.asyncio
async def test_both_channels_required_and_cached():
    calls = {"n": 0}

    async def fetch(bot, channel, user_id):
        calls["n"] += 1
        return channel == "@a", None

    now = {"t": 0.0}
    gate = SubscriptionGate(["@a", "@b"], ttl_seconds=60, fetch=fetch, clock=lambda: now["t"])
    result = await gate.check(None, 7)
    assert not result.ok
    assert result.admin_error is False
    assert result.missing == ["@b"]
    assert "@b" in result.user_message
    again = await gate.check(None, 7)
    assert again.missing == ["@b"]
    assert calls["n"] == 2  # both channels once; second check is cached

    now["t"] = 61
    await gate.check(None, 7)
    assert calls["n"] == 4


@pytest.mark.asyncio
async def test_force_bypasses_cache_and_errors_are_not_cached():
    calls = {"n": 0}

    async def fetch(bot, channel, user_id):
        calls["n"] += 1
        if channel == "@b":
            return False, "@b: bot is not an admin"
        return True, None

    gate = SubscriptionGate(["@a", "@b"], ttl_seconds=60, fetch=fetch, clock=lambda: 0)
    result = await gate.check(None, 1)
    assert result.admin_error
    assert "not an admin" in result.error_detail
    assert "Membership could not be verified" in result.user_message
    assert not result.ok
    await gate.check(None, 1)
    assert calls["n"] == 4  # error was not cached

    async def ok_fetch(bot, channel, user_id):
        return True, None

    gate._fetch = ok_fetch
    forced_gate_calls = {"n": 0}

    async def counting(bot, channel, user_id):
        forced_gate_calls["n"] += 1
        return True, None

    gate._fetch = counting
    # prime cache
    good = await gate.check(None, 2)
    assert good.ok
    assert forced_gate_calls["n"] == 2
    cached = await gate.check(None, 2)
    assert cached.ok
    assert forced_gate_calls["n"] == 2
    forced = await gate.check(None, 2, force=True)
    assert forced.ok
    assert forced_gate_calls["n"] == 4
