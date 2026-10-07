import json

from recaps.drop_off_locations import (
    drop_off_summary_lines,
    looks_like_drop_off_locations,
    parse_drop_off_locations,
)
from recaps.excel import _format_field_value as excel_format_field_value
from recaps.pdf import _format_field_value as pdf_format_field_value

STOPS = [
    {
        "placeName": "Gateway Wine & Spirits",
        "address": "3120 S Grand Blvd, St. Louis, MO 63118",
        "lat": 38.60412,
        "lng": -90.24321,
        "source": "places",
        "skus": [
            {"productId": "1", "productName": "Black Cherry 10mg 4-Pack", "cases": 2},
            {"productId": "2", "productName": "Strawberry Lemonade 10mg 4-Pack", "cases": 1},
        ],
    },
    {
        "placeName": "The Corner Tap",
        "lat": 38.62679,
        "lng": -90.25863,
        "source": "gps",
        "skus": [{"productId": "3", "cases": "1"}, {"productId": "4", "cases": 0}],
    },
]


def test_summary_lines_match_web_readout():
    assert drop_off_summary_lines(parse_drop_off_locations(json.dumps(STOPS))) == [
        "Gateway Wine & Spirits · 3120 S Grand Blvd, St. Louis, MO 63118 · 3 cases"
        " · Black Cherry 10mg 4-Pack × 2, Strawberry Lemonade 10mg 4-Pack × 1",
        "The Corner Tap · 38.62679, -90.25863 · 1 case · SKU 3 × 1",
    ]


def test_parse_tolerates_legacy_text_and_snake_case():
    assert parse_drop_off_locations("") == []
    [legacy] = parse_drop_off_locations("Southside Bottle Shop")
    assert legacy.place_name == "Southside Bottle Shop"
    [snake] = parse_drop_off_locations(
        json.dumps({"place_name": "Bar", "products": [{"product_id": "9", "quantity": 4}]})
    )
    assert snake.place_name == "Bar"
    assert snake.cases == 4


def test_looks_like_drop_off_locations():
    assert looks_like_drop_off_locations(STOPS)
    assert not looks_like_drop_off_locations(["Full can", "4oz pour"])
    assert not looks_like_drop_off_locations([])


def test_pdf_and_excel_never_print_dict_reprs():
    raw = json.dumps(STOPS)
    for formatted in (pdf_format_field_value(raw), excel_format_field_value(raw)):
        assert "Gateway Wine & Spirits" in formatted
        assert "{" not in formatted and "placeName" not in formatted
    assert pdf_format_field_value('["Full can", "4oz pour"]') == "Full can, 4oz pour"
    assert excel_format_field_value('["Full can", "4oz pour"]') == "Full can, 4oz pour"
    assert pdf_format_field_value('[{"label": "Booth", "count": 3}]') == "label: Booth, count: 3"
