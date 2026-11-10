from decimal import Decimal

import pytest

from cardforge.evaluate.api_budget import Ledger, token_cost


def test_reservations_survive_restart_and_block_unfunded_or_duplicate_calls(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = Ledger(path, 5)
    ledger.reserve("ambiguous-network-call", Decimal("4.9"), {})
    restarted = Ledger(path, 5)
    assert restarted.summary()["unsettled"] == 1
    with pytest.raises(ValueError, match="exhausted"):
        restarted.reserve("another-call", Decimal("0.11"), {})
    with pytest.raises(ValueError, match="already accounted"):
        restarted.reserve("ambiguous-network-call", Decimal("0.01"), {})
    assert restarted.summary()["accounted_upper_usd"] == "4.9"


def test_token_usage_releases_only_unused_reservation_and_cannot_be_rewritten(tmp_path):
    ledger = Ledger(tmp_path / "ledger.json", 5)
    ledger.reserve("call", Decimal("0.10"), {"charged_upper_usd": "0"})
    assert ledger.summary()["accounted_upper_usd"] == "0.10"
    cost = token_cost(1000, 2000, 0.75, 3.75)
    assert cost == Decimal("0.00825")
    with pytest.raises(ValueError, match="outside reserved"):
        ledger.settle("call", Decimal("0.11"), {})
    ledger.settle("call", cost, {"input": 1000, "output_including_thoughts": 2000})
    assert ledger.summary()["remaining_usd"] == "4.99175"
    with pytest.raises(ValueError, match="cannot be rewritten"):
        ledger.settle("call", Decimal("0"), {})


def test_zero_budget_or_nonfinite_reservation_stops_before_any_provider(tmp_path):
    with pytest.raises(ValueError):
        Ledger(tmp_path / "ledger.json", 0)
    with pytest.raises(ValueError):
        Ledger(tmp_path / "ledger.json", 9)
    ledger = Ledger(tmp_path / "ledger.json", 5)
    with pytest.raises(ValueError):
        ledger.reserve("call", Decimal("NaN"), {})


def test_explicit_extra_dollar_preserves_prior_spend_and_enforces_six_total(tmp_path):
    path = tmp_path / "ledger.json"
    Ledger(path, 5).reserve("old-call", Decimal("4.9"), {})
    expanded = Ledger(path, 6)
    assert expanded.summary()["remaining_usd"] == "1.1"
    expanded.reserve("new-call", Decimal("1.1"), {})
    with pytest.raises(ValueError, match="exhausted"):
        expanded.reserve("over-cap", Decimal("0.0001"), {})


def test_parallel_clients_cannot_overbook_the_shared_project_budget(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    def reserve(i):
        try:
            Ledger(tmp_path / "ledger.json", 5).reserve(str(i), Decimal("0.60"), {})
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(reserve, range(20)))
    assert sum(results) == 8
    assert Ledger(tmp_path / "ledger.json", 5).summary()["accounted_upper_usd"] == "4.80"


def test_eight_dollar_total_retains_old_entries_and_never_grants_eight_more(tmp_path):
    path = tmp_path / 'ledger.json'
    Ledger(path, 6).reserve('prior-history', Decimal('5.8'), {})
    enlarged = Ledger(path, 8)
    assert enlarged.summary()['accounted_upper_usd'] == '5.8'
    assert enlarged.summary()['remaining_usd'] == '2.2'
    enlarged.reserve('last-funded-request', Decimal('2.2'), {})
    with pytest.raises(ValueError, match='exhausted'):
        enlarged.reserve('unfunded', Decimal('0.000001'), {})
