from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from long_running_agent.process import CommandError
from long_running_agent.runtime.setup import ensure_personal_registration
import pytest


if TYPE_CHECKING:
    from collections.abc import Sequence


def test_registered_user_is_never_reregistered() -> None:
    commands: list[list[str]] = []

    def runner(
        arguments: Sequence[str], *, capture_output: bool = False, check: bool = True, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        commands.append(list(arguments))
        assert capture_output
        assert timeout == 15
        assert not check
        return subprocess.CompletedProcess(list(arguments), 0, "{}", "")

    def unregistered() -> bool:
        pytest.fail("A valid existing registration must not be inspected or changed")

    ensure_personal_registration(runner=runner, unregistered=unregistered)
    assert commands == [["/usr/local/sbin/lra-user", "connection"]]


def test_only_verified_unregistered_user_gets_exact_self_registration() -> None:
    commands: list[list[str]] = []

    def runner(
        arguments: Sequence[str], *, capture_output: bool = False, check: bool = True, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        commands.append(list(arguments))
        assert capture_output
        assert timeout in {15, 60}
        code = 1 if len(commands) == 1 else 0
        assert check == (len(commands) != 1)
        return subprocess.CompletedProcess(list(arguments), code, "", "")

    ensure_personal_registration(runner=runner, unregistered=lambda: True)
    assert commands == [
        ["/usr/local/sbin/lra-user", "connection"],
        ["sudo", "-n", "/usr/local/sbin/lra-user", "register-self"],
        ["/usr/local/sbin/lra-user", "connection"],
    ]


def test_registered_but_unready_user_does_not_receive_sudo_fallback() -> None:
    commands: list[list[str]] = []

    def runner(
        arguments: Sequence[str], *, capture_output: bool = False, check: bool = True, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        commands.append(list(arguments))
        assert capture_output
        assert timeout == 15
        assert not check
        return subprocess.CompletedProcess(list(arguments), 1, "", "")

    with pytest.raises(RuntimeError, match="Existing registrations were preserved"):
        ensure_personal_registration(runner=runner, unregistered=lambda: False)
    assert len(commands) == 1


def test_denied_exact_self_registration_has_actionable_error_and_no_general_sudo() -> None:
    commands: list[list[str]] = []

    def runner(
        arguments: Sequence[str], *, capture_output: bool = False, check: bool = True, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        commands.append(list(arguments))
        assert capture_output
        assert timeout in {15, 60}
        if check:
            raise CommandError(arguments[0], 1)
        return subprocess.CompletedProcess(list(arguments), 1, "", "")

    with pytest.raises(RuntimeError, match="VM administrator"):
        ensure_personal_registration(runner=runner, unregistered=lambda: True)
    assert len(commands) == 2
    assert commands[-1] == ["sudo", "-n", "/usr/local/sbin/lra-user", "register-self"]
