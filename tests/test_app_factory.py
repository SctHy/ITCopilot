from fastapi.testclient import TestClient

from it_copilot.api.app import create_app
from it_copilot.infra.protocols import APPLICATION


def test_dependencies_and_streams_belong_to_one_app():
    first, second = create_app(), create_app()
    a, b = first.state.container, second.state.container
    a.lifecycle = APPLICATION.start()
    assert TestClient(first).get("/health").json()["lifecycle"] == a.lifecycle.state
    assert TestClient(second).get("/health").json()["lifecycle"] == "uninitialized"
    assert a.events is not b.events
    assert a.background_tasks is not b.background_tasks
    assert first.state.mcp_mount is not second.state.mcp_mount
