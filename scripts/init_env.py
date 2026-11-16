"""Create a private local .env without overwriting existing settings or printing secrets.

Run this yourself on the deployment machine before starting Compose.
"""

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def initialize(template: Path, output: Path, token_factory=secrets.token_urlsafe):
    content = template.read_text()
    # Validate the template before generating credentials or opening the destination.
    for key in ("LAB_TOKEN", "GRAFANA_ADMIN_PASSWORD"):
        if content.splitlines().count(f"{key}=") != 1:
            raise ValueError(f"Expected exactly one blank {key} in template")
    if output.exists():
        raise FileExistsError("Existing .env was not changed")
    values = {key: token_factory(32) for key in ("LAB_TOKEN", "GRAFANA_ADMIN_PASSWORD")}
    content = (
        "\n".join(
            f"{line[:-1]}={values[line[:-1]]}"
            if line in ("LAB_TOKEN=", "GRAFANA_ADMIN_PASSWORD=")
            else line
            for line in content.splitlines()
        )
        + "\n"
    )
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)


if __name__ == "__main__":
    try:
        initialize(ROOT / ".env.example", ROOT / ".env")
    except (FileExistsError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    print("Created private .env; credentials were not printed. Keep this file out of Git.")
