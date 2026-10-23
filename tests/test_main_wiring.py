"""Smoke on the main.py MCP wiring (Task 4 review lesson).

The suite never executes main(), so a missing import in the wiring
slips through 96 green tests. This smoke imports main and actually
BUILDS the wrapped MCP app (no uvicorn serving) — the class of
NameErrors on the wiring must fail here, not in the container.
"""


def test_main_wiring_builds_bearer_wrapped_mcp_app():
    import main as main_module

    # F-1: the middleware must be imported at module level, not just used
    assert main_module.BearerAuthMiddleware is not None

    app = main_module.BearerAuthMiddleware(
        main_module.mcp_server.streamable_http_app(
            stateless_http=True, json_response=True, host="0.0.0.0"
        )
    )

    assert callable(app)


def test_main_wiring_builds_master_wrapped_ui_app():
    """UI plane gate (PLAN_UI_MASTER_TOKEN): same NameError class as F-1 —
    the wrapper must be imported and must compose with ui_app."""
    import main as main_module

    assert main_module.MasterTokenMiddleware is not None

    app = main_module.MasterTokenMiddleware(main_module.ui_app)

    assert callable(app)
