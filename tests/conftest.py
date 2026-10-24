from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url: str, enable_custom_integrations: None) -> None:
    """
    Make the meshtastic custom integration loadable in every test.

    `recorder_db_url` is requested first and then ignored, purely to pin fixture ordering.
    Enabling custom integrations builds `hass`, and the recorder refuses to choose a database
    once that has happened — so without this, no test could pull in the recorder while an
    autouse fixture got there first.
    """
    return
