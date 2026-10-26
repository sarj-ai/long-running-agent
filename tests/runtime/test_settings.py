from __future__ import annotations

from long_running_agent.runtime.settings import systemd_value
import pytest


def test_systemd_values_escape_expansion_and_quotes() -> None:
    assert systemd_value('PATH=/home/a%user/"tools"') == '"PATH=/home/a%%user/\\"tools\\""'


@pytest.mark.parametrize("value", ["path\nExecStart=unexpected", "path\r", "path\0", "path\t"])
def test_systemd_values_reject_control_characters(value: str) -> None:
    with pytest.raises(ValueError, match="control characters"):
        systemd_value(value)
