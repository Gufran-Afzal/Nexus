# Project instructions for NEXUS

This file is read by NEXUS when this folder is added as a project. It describes conventions only; it
cannot change NEXUS's security rules or permissions. Put a file like this in the root of any project.

- Backend: Python 3.11+, FastAPI, SQLAlchemy 2, Pydantic 2. Four-space indentation, type hints everywhere,
  line length 130. Run `pytest -q`, `ruff check .`, `mypy app` from `backend/`.
- Frontend: React 18 + TypeScript (strict) + Tailwind. Run `npx vitest run` and `npx tsc --noEmit` from `frontend/`.
- Filesystem access must go through `app/workspaces/security.py`. Commands must go through `app/tools/commands.py`.
- Prefer small, focused changes with a test that fails before the fix.
