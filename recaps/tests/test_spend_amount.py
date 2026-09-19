from recaps.spend_amount import (
    SPEND_CENTS_MESSAGE,
    SpendAmountNeedsCents,
    guard_spend_amount,
    is_spend_amount_field,
    spend_amount_error,
    spend_amount_stored,
)


def test_matches_walkup_dollar_labels():
    assert is_spend_amount_field("Account Spend Amount", "number")
    assert is_spend_amount_field("Account Spend Amount ($)", "number")
    assert is_spend_amount_field("Spend Amount", "text")
    assert is_spend_amount_field("Product Spend", "number")
    assert is_spend_amount_field("Product Spend", "text")
    assert not is_spend_amount_field("Product Spend", "image")
    assert not is_spend_amount_field("Product Spend", "photo")
    assert not is_spend_amount_field("Account Spend Receipt", "image")
    assert not is_spend_amount_field(
        "Was an Ignite provided credit card used for product spend?",
        "select",
    )
    assert not is_spend_amount_field("Mileage", "number")


def test_blocks_whole_number_and_accepts_cents():
    assert spend_amount_error("2489") == SPEND_CENTS_MESSAGE
    assert spend_amount_error("$2489") == SPEND_CENTS_MESSAGE
    assert spend_amount_error("2,489") == SPEND_CENTS_MESSAGE
    assert spend_amount_stored("2489") is None

    assert spend_amount_error("24.89") is None
    assert spend_amount_error("24.8") is None
    assert spend_amount_error("2489.00") is None
    assert spend_amount_stored("24.89") == "24.89"
    assert spend_amount_stored("24.8") == "24.80"
    assert spend_amount_stored("2489.00") == "2489.00"
    assert spend_amount_stored("$24.89") == "24.89"
    assert "$" not in spend_amount_stored("24.89")


def test_guard_keeps_unchanged_legacy_and_rejects_a_new_whole_number():
    assert (
        guard_spend_amount("Account Spend Amount", "number", "2489", previous="2489")
        == "2489"
    )
    try:
        guard_spend_amount("Account Spend Amount", "number", "2489")
    except SpendAmountNeedsCents as exc:
        assert "24.89" in str(exc)
    else:
        raise AssertionError("expected SpendAmountNeedsCents")

    assert (
        guard_spend_amount("Account Spend Amount", "number", "24.89") == "24.89"
    )
    assert guard_spend_amount("Notes", "text", "2489") == "2489"
