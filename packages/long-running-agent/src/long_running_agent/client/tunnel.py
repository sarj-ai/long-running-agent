from __future__ import annotations

import sys
from typing import TYPE_CHECKING, NoReturn

from long_running_agent.client.cloud import Cloud, sanitized_environment
from long_running_agent.process import environment_snapshot, exec_command


if TYPE_CHECKING:
    from long_running_agent.config import Settings


MAX_PORT = 65535


def tunnel(settings: Settings, port: int) -> NoReturn:
    if not 1 <= port <= MAX_PORT:
        message = "Tunnel port must be between 1 and MAX_PORT"
        raise ValueError(message)
    # Cloud SDK imports the installed NumPy accelerator in its own process.
    environment = sanitized_environment(environment_snapshot())
    environment.update(CLOUDSDK_PYTHON=sys.executable, CLOUDSDK_PYTHON_SITEPACKAGES="1")
    arguments = Cloud(settings).arguments(
        [
            "compute",
            "start-iap-tunnel",
            settings.instance,
            str(port),
            "--listen-on-stdin",
            "--zone=" + settings.zone,
            "--verbosity=error",
        ]
    )
    # Replacing this process leaves SSH's raw streaming transport unbuffered.
    exec_command(arguments, env=environment)
