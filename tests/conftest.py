import os
import shutil
import tempfile

import pytest

import nicotine_source

from harness import NicotineHarness


def pytest_report_header(config):
    path, reason = nicotine_source.find_source()
    detail = f"{reason} ({nicotine_source.describe(path)})" if path else reason
    return f"Nicotine+ source: {detail}"


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def no_real_mpd(monkeypatch):
    """Tests never talk to a real MPD: off unless a test points FLACLI_MPD at its own fake server."""
    monkeypatch.setenv("FLACLI_MPD", "off")


@pytest.fixture(scope="session")
def nicotine():
    """A running headless Nicotine+ core with the bridge plugin installed (not yet enabled)."""
    path, reason = nicotine_source.find_source()

    if path is None:
        pytest.skip(f"Nicotine+ source unavailable: {reason}")

    data_dir = tempfile.mkdtemp(prefix="nicotine-mcp-test-")
    harness = NicotineHarness(path, data_dir)
    harness.start()

    yield harness

    harness.stop()
    shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture
def bridge(nicotine):
    """The bridge plugin enabled and the core reset to an empty state before and after the test."""
    if not nicotine.bridge_loaded:
        assert nicotine.enable_bridge(), "bridge plugin failed to load"

    nicotine.reset()
    nicotine.set_plugin_setting("allow_downloads", True)

    yield nicotine

    if nicotine.bridge_loaded:
        nicotine.reset()

    assert nicotine.pump_alive, "Nicotine+ main loop died during the test"
    assert not nicotine.pump_errors, "exception escaped to the Nicotine+ event bus:\n" + "\n".join(nicotine.pump_errors)
    assert nicotine.quit_events == 0, "Nicotine+ quit during the test"


@pytest.fixture
def search(bridge):
    """A started search: returns (token, harness)."""
    token = bridge.rpc("search", query="test artist album")["search_id"]
    return token
