from types import SimpleNamespace

import pytest

from events.torch_markets import torch_market_for_event


@pytest.mark.parametrize(
    ("address", "market"),
    [
        ("1 N Beach Rd, Dania Beach, FL 33004", "miami"),
        ("250 NW 24th St, Miami, FL 33127", "miami"),
        ("8001 S Orange Blossom Trl, Orlando, FL 32809", "orlando"),
        ("Kissimmee, FL", "orlando"),
        ("1500 McKinney St, Houston, TX 77010", "houston"),
        ("Katy, TX", "houston"),
        ("Clutch City Cruisers, Houston", "houston"),
        ("2100 Ross Ave, Dallas, TX 75201", "austin-dallas"),
        ("500 E Washington St, Austin TX 78701", "austin-dallas"),
        ("401 Channelside Dr, Tampa, FL 33602", None),
        ("1 Main St, Austin, MN 55912", None),
        ("", None),
    ],
)
def test_market_from_the_events_own_address(address, market):
    event = SimpleNamespace(address=address, location=None, state=None)
    assert torch_market_for_event(event) == market


def test_no_event_no_market():
    assert torch_market_for_event(None) is None
