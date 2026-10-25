import ast
import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path


def test_aws_only_imports_in_fresh_interpreter():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import asyncio
import base64
import json
import pkgutil
import socket
import sys
import urllib.request
class BlockLocal(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'asyncpg', 'pgvector', 'openai', 'pypdf', 'bs4'}:
            raise ImportError('local dependency blocked: ' + fullname)
sys.meta_path.insert(0, BlockLocal())
from kb_assistant.app import create_app
from kb_assistant.settings import Settings
from kb_assistant.adapters import aws, oidc
from kb_assistant.adapters.aws import lambda_events
for module in pkgutil.iter_modules(aws.__path__, aws.__name__ + '.'):
    importlib.import_module(module.name)
assert 'boto3' in sys.modules and 'jwt' in sys.modules
calls = []
def blocked(*args, **kwargs):
    calls.append(True)
    raise OSError('network disabled')
socket.socket.connect = blocked
urllib.request.OpenerDirector.open = blocked
app = create_app(Settings(table_name='table', docs_bucket='docs', knowledge_base_id='kb',
    data_source_id='ds', chat_model_id='model', guardrail_id='guard', guardrail_version='1',
    oidc_issuer='https://issuer.invalid', oidc_client_id='client'))
for name in ['store', 'blobs', 'model', 'retriever', 'indexer', 'guardrail', 'verifier']:
    assert getattr(app.state.container, name) is not None
assert not calls, 'startup must not use the network'
async def check():
    import httpx
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            response = await client.get('/v1/documents', headers={'Authorization': 'Bearer x'})
            assert response.status_code == 401 and response.json() == {'message': 'Unauthorized'}
            assert not calls, 'malformed JWT should not fetch keys'
            parts = [base64.urlsafe_b64encode(json.dumps(v).encode()).decode().rstrip('=')
                     for v in [{'alg': 'RS256', 'kid': 'missing'}, {}]]
            token = '.'.join([*parts, 'AA'])
            response = await client.get('/v1/documents', headers={'Authorization': 'Bearer ' + token})
            assert response.status_code == 401
            assert calls == [True], 'JWKS failure must fail closed'
asyncio.run(check())
lambda_events.ingest_handler({'detail': {'object': {'key': 'doc.metadata.json'}}}, None)
print('all AWS adapters, properties, JWT failure and sidecar event succeeded')
""",
        ],
        env={**os.environ, "DEPLOY_TARGET": "aws"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "all AWS adapters, properties, JWT failure and sidecar event succeeded" in result.stdout


def test_development_runtime_versions_match_lambda_lock():
    lock = Path(__file__).parents[1] / "requirements-aws.lock"
    for line in lock.read_text().splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            name, version = line.split("==")
            assert importlib.metadata.version(name) == version, name


def test_no_boto_imports_outside_aws_adapters():
    root = Path(__file__).parents[1] / "src" / "kb_assistant"
    for path in root.rglob("*.py"):
        if "adapters/aws/" in path.as_posix():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(name.split(".")[0] in ["boto3", "botocore"] for name in names), str(path)


def test_local_only_imports_in_fresh_interpreter(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib, importlib.abc, pkgutil, sys
class BlockAws(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'boto3', 'botocore', 's3transfer'}:
            raise ImportError('AWS dependency blocked: ' + fullname)
sys.meta_path.insert(0, BlockAws())
from kb_assistant.app import create_app
from kb_assistant.settings import Settings
from kb_assistant.adapters import local
for module in pkgutil.iter_modules(local.__path__, local.__name__ + '.'):
    importlib.import_module(module.name)
app = create_app(Settings(deploy_target='local', blob_root=sys.argv[1], llm_api_key='k',
    oidc_issuer='http://keycloak.invalid/realms/kb', oidc_client_id='kb-web'))
container = app.state.container
for name in ['blobs', 'model', 'guardrail', 'verifier', 'embedder']:
    assert getattr(container, name) is not None
assert 'boto3' not in sys.modules and 'botocore' not in sys.modules
print('local adapters built without AWS dependencies')
""",
            str(tmp_path),
        ],
        env={**os.environ, "DEPLOY_TARGET": "local"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "local adapters built without AWS dependencies" in result.stdout
