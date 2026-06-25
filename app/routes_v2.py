# Summary: Keeps old route imports working.
# Details: It supports the FastAPI backend by keeping one main piece of app behavior clear and reusable.
"""Legacy route-module compatibility shim.

The active route implementation lives in :mod:`app.routes`. This module remains
so older scripts/tests that import ``app.routes_v2.router`` keep working while
the app has a single maintained route source.
"""

from app.routes import router

__all__ = ["router"]
