from typing import Protocol

from novel_agent.auth.models import Principal


class PrincipalVerifier(Protocol):
    async def verify(self, token: str) -> Principal: ...
