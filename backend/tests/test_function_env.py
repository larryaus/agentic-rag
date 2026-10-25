import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FUNCTIONS = json.loads((Path(__file__).parent / "fixtures" / "function-env.json").read_text())
ROUTES = {
    "chat": ("POST", "/v1/chat"),
    "uploads": ("POST", "/v1/uploads"),
    "documents": ("GET", "/v1/documents"),
    "sessions": ("GET", "/v1/sessions"),
}
# Variables the Lambda runtime itself provides alongside the function's own environment.
RUNTIME = {
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
}

BOOT = """
import asyncio, json, socket, sys, urllib.request
def blocked(*args, **kwargs):
    raise AssertionError('network attempted during startup')
socket.socket.connect = blocked
urllib.request.OpenerDirector.open = blocked
spec = json.loads(sys.argv[1])
if spec['handler'] == 'run.sh':
    import httpx
    from kb_assistant.app import create_app
    app = create_app()
    async def check():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                assert (await client.get('/healthz')).status_code == 200
                for method, path in spec['own']:
                    assert (await client.request(method, path)).status_code == 401, path
                for method, path in spec['other']:
                    assert (await client.request(method, path)).status_code == 404, path
    asyncio.run(check())
else:
    from kb_assistant.adapters.aws import lambda_events
    module, name = spec['handler'].rsplit('.', 1)
    assert module == 'kb_assistant.adapters.aws.lambda_events' and callable(getattr(lambda_events, name))
    container = lambda_events.runtime()[1]
    for prop in ['store', 'blobs', 'indexer']:
        assert getattr(container, prop) is not None
print('booted')
"""


@pytest.mark.parametrize("name", sorted(FUNCTIONS))
def test_function_boots_with_exactly_its_cdk_environment(name):
    spec = FUNCTIONS[name]
    env = {
        # Deploy-time references get placeholders of the right shape.
        key: value
        if value is not None
        else f"https://{key.lower()}.invalid"
        if key.endswith(("_ISSUER", "_URL"))
        else f"dummy-{key.lower()}"
        for key, value in spec["environment"].items()
    }
    own = [
        ROUTES[group]
        for group in env.get("KB_ROUTES", "").split(",")
        if group
    ]
    other = [route for route in ROUTES.values() if route not in own]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            BOOT,
            json.dumps(dict(handler=spec["handler"], own=own, other=other)),
        ],
        env={**env, **RUNTIME, "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[1],
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("booted")
