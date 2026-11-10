import pytest


@pytest.fixture
def anyio_backend() -> str:
    # Run async tests on asyncio only (anyio's pytest plugin ships with httpx/starlette).
    return "asyncio"
