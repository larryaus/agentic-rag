from typing import Annotated, cast

from fastapi import Depends, Request

from kb_assistant.container import Container
from kb_assistant.core.ports import AuthContext
from kb_assistant.errors import UnauthorizedError
from kb_assistant.logging import set_log_user


def container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


Dependencies = Annotated[Container, Depends(container)]


async def authenticate(request: Request, deps: Dependencies) -> AuthContext:
    header = request.headers.get("authorization")
    if not header or not header.startswith("Bearer ") or not header[7:].strip():
        raise UnauthorizedError()
    try:
        auth = await deps.verifier.verify(header)
    except Exception as exc:
        raise UnauthorizedError() from exc
    set_log_user(auth.sub)
    return auth


Auth = Annotated[AuthContext, Depends(authenticate)]
