import json
import textwrap
from pathlib import Path

import pytest


REQ_ACT = textwrap.dedent(
    """\
    # Domain: activation

    ## Rules

    | # | Rule | Evidence |
    |---|---|---|
    | ACT-R1 | funnelStage is derived on every write — never self-reported | `activation.types.ts:5-6`; `org-activation.repository.ts:170-178` |
    | ACT-R2 | Empty organization → all flags false, never 404 | `activation.service.ts:19` — covered by `activation.service.spec.ts` |
    | ACT-R3 | Milestone write is idempotent (see ACT-R1) · DONE | `repo.ts:156` |
    """
)

REQ_INV = textwrap.dedent(
    """\
    # Domain: invitations

    - INV-R1 · Admin creates an invitation; role limited to grantable list · TODO
    - INV-R2 · Invitation e-mail must not already be a member
    """
)

FLOWS = textwrap.dedent(
    """\
    # Processes

    ## Process 1 — From signup to activation

    ```
    signup → setup → first location → ACTIVATION
    ```

    | # | Step | Evidence |
    |---|---|---|
    | 1.1 | Company URL availability check (ACT-R2) | `onboarding.service.ts:11` |
    | 1.2 | Milestone write is idempotent (ACT-R3) | `repo.ts:156` |

    ## Process 2 — Employee joins organization

    1. Admin creates invitation (INV-R1)
    2. Employee accepts (INV-R9)
    """
)

DOC_A = textwrap.dedent(
    """\
    # Hosting

    ## Domains

    The application is served from api.dev.example.com behind a gateway.

    ## Activation funnel

    Organization activation is two-sided: owner publishes a schedule and a member reports availability.
    """
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "docs" / "requirements" / "domains").mkdir(parents=True)
    (tmp_path / "docs" / "flows").mkdir(parents=True)
    (tmp_path / "docs" / "requirements" / "domains" / "activation.md").write_text(REQ_ACT)
    (tmp_path / "docs" / "requirements" / "domains" / "invitations.md").write_text(REQ_INV)
    (tmp_path / "docs" / "flows" / "PROCESSES.md").write_text(FLOWS)
    (tmp_path / "docs" / "HOSTING.md").write_text(DOC_A)
    cfg = {
        "docs": {"globs": ["docs/**/*.md"]},
        "requirements": {
            "globs": ["docs/requirements/domains/*.md"],
            "id_pattern": r"\b[A-Z]{2,6}-R\d{1,3}\b",
            "write_file": "docs/requirements/requirements.json",
        },
        "flows": {
            "globs": ["docs/flows/**/*.md"],
            "heading_pattern": r"^##\s+Process\s+(.+)$",
            "write_file": "docs/flows/flows.json",
        },
    }
    (tmp_path / ".spec-mcp.json").write_text(json.dumps(cfg))
    return tmp_path
