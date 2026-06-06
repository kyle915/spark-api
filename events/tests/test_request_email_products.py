"""Unit tests for events.envelopes._request_product_names — the helper that
feeds selected SKUs into the request notification emails (approval, requestor
confirmation, admin, approved/declined). Dedups, skips null/blank product
names, and never raises (best-effort)."""

from events.envelopes import _request_product_names


class _Product:
    def __init__(self, name):
        self.name = name


class _RP:
    def __init__(self, name):
        self.product = _Product(name) if name is not None else None


class _Manager:
    def __init__(self, rows):
        self._rows = rows

    def select_related(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _Request:
    def __init__(self, rows):
        self.id = 1
        self.request_product = _Manager(rows)


def test_returns_distinct_names_in_order():
    req = _Request(
        [
            _RP("Liquid Death Mountain Water 19.2oz"),
            _RP("Severed Lime Sparkling"),
            _RP("Liquid Death Mountain Water 19.2oz"),  # dup → collapsed
        ]
    )
    assert _request_product_names(req) == [
        "Liquid Death Mountain Water 19.2oz",
        "Severed Lime Sparkling",
    ]


def test_skips_null_and_blank_products():
    req = _Request([_RP(None), _RP("   "), _RP("Convicted Melon")])
    assert _request_product_names(req) == ["Convicted Melon"]


def test_empty_request_product_set():
    assert _request_product_names(_Request([])) == []


def test_none_request_is_safe():
    assert _request_product_names(None) == []


def test_never_raises_on_broken_relation():
    class Boom:
        id = 1

        @property
        def request_product(self):
            raise RuntimeError("db down")

    assert _request_product_names(Boom()) == []
