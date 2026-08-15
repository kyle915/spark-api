"""Public /approve payload: products + BA count from the request."""

from events.views import _public_ba_count, _public_product_names


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
    def __init__(self, *, notes="", rows=None):
        self.id = 1
        self.notes = notes
        self.request_product = _Manager(rows or [])


def test_ba_count_from_notes():
    req = _Request(notes="Please staff Saturday.\nBA count: 2")
    assert _public_ba_count(req) == 2


def test_ba_count_missing_is_none():
    assert _public_ba_count(_Request(notes="just notes")) is None


def test_products_match_approval_email_helper():
    req = _Request(
        rows=[
            _RP("Liquid Death Mountain Water 19.2oz"),
            _RP("Severed Lime Sparkling"),
        ]
    )
    assert _public_product_names(req) == [
        "Liquid Death Mountain Water 19.2oz",
        "Severed Lime Sparkling",
    ]
