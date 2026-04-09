from pathlib import Path

import yaml


def _load_branch_contract() -> dict:
    path = Path(__file__).resolve().parent.parent / "branch-bank.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _operation_map(spec: dict) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for route, item in spec["paths"].items():
        for method, operation in item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                out[(route, method.lower())] = operation
    return out


def _with_api_prefix(path: str) -> str:
    if path.startswith("/api/v1/"):
        return path
    return f"/api/v1{path}"


def test_openapi_routes_methods_security_and_status_codes(client):
    app_spec = client.get("/openapi.json").json()
    branch_spec = _load_branch_contract()

    app_ops = _operation_map(app_spec)
    branch_ops = _operation_map(branch_spec)

    for (route, method), branch_op in branch_ops.items():
        app_key = (route, method)
        if app_key not in app_ops:
            app_key = (_with_api_prefix(route), method)
        assert app_key in app_ops, f"Missing operation: {app_key}"
        app_op = app_ops[app_key]

        branch_security = branch_op.get("security")
        app_security = app_op.get("security")
        if branch_security == []:
            assert app_security in (None, []), f"Operation {app_key} should be unauthenticated"
        else:
            assert app_security is not None, f"Operation {app_key} should require auth"
            assert {"BearerAuth": []} in app_security, f"Operation {app_key} must use BearerAuth"

        branch_codes = set(branch_op.get("responses", {}).keys())
        app_codes = set(app_op.get("responses", {}).keys())
        missing = branch_codes - app_codes
        assert not missing, f"Operation {app_key} missing response codes: {missing}"


def test_openapi_contains_bearer_auth_security_scheme(client):
    app_spec = client.get("/openapi.json").json()
    schemes = app_spec["components"]["securitySchemes"]
    assert "BearerAuth" in schemes
    assert schemes["BearerAuth"]["type"] == "http"
    assert schemes["BearerAuth"]["scheme"] == "bearer"


def test_user_registration_response_schema_matches_contract(client):
    app_spec = client.get("/openapi.json").json()
    app_user_schema = app_spec["components"]["schemas"]["UserRegistrationResponse"]
    properties = set(app_user_schema.get("properties", {}).keys())

    assert "userId" in properties
    assert "fullName" in properties
    assert "createdAt" in properties
    assert "authToken" not in properties
    assert "apiKey" not in properties
