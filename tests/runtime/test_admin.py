from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
from typing import Final

from long_running_agent.runtime import admin_worker as worker
from long_running_agent.runtime.admin import RegistrationInput
from pydantic import ValidationError
import pytest


WORKER: Final = Path(worker.__file__).resolve()


def registration(uid: int, browser: worker.Browser) -> worker.Registration:
    return worker.Registration(
        schema_version=1,
        uid=uid,
        gid=uid,
        username=f"user{uid}",
        home=f"/home/user{uid}",
        browser=browser,
        privileged_admin=False,
        limits=worker.ResourceLimits(memory_high="3G", tasks_max="2048", cpu_weight=100),
    )


def account(uid: int = 1001) -> pwd.struct_passwd:
    return pwd.struct_passwd((f"user{uid}", "x", uid, uid, "", f"/home/user{uid}", "/bin/bash"))


def test_legacy_allocation_and_new_collision_free_browser() -> None:
    legacy = worker.allocate_browser([], {}, legacy=True, uid=1001)
    assert legacy == {"display": 92, "cdp_port": 9222, "vnc_port": 5902, "viewer_port": 7900}
    first = worker.allocate_browser([registration(1001, legacy)], {20000: {9999}})
    assert first["display"] == 101
    second = worker.allocate_browser([registration(1001, legacy), registration(1002, first)], {})
    assert first["display"] != second["display"]
    assert set(first.values()).isdisjoint(second.values())


def test_legacy_browser_rejects_another_uid_listener() -> None:
    with pytest.raises(RuntimeError, match="belongs to another user"):
        worker.allocate_browser([], {9222: {1002}}, legacy=True, uid=1001)
    assert worker.allocate_browser([], {9222: {1001}}, legacy=True, uid=1001) == worker.LEGACY_BROWSER


@pytest.mark.parametrize(
    "invalid",
    [
        {**worker.LEGACY_BROWSER, "cdp_port": True},
        {**worker.LEGACY_BROWSER, "cdp_port": worker.LEGACY_BROWSER["vnc_port"]},
        {**worker.LEGACY_BROWSER, "viewer_port": 40000},
        {**worker.LEGACY_BROWSER, "display": -1},
    ],
    ids=["boolean-port", "duplicate-port", "ephemeral-port", "negative-display"],
)
def test_browser_rejects_boolean_duplicate_and_ephemeral_ports(invalid: object) -> None:
    with pytest.raises(RuntimeError, match=r"integer|Browser ports|Invalid X"):
        worker.validate_browser(invalid)


def test_firewall_protects_clients_and_prevents_endpoint_impersonation() -> None:
    rules = worker.firewall_rules([registration(1001, worker.LEGACY_BROWSER)])
    assert "table inet lra_browser" in rules
    assert rules.count("fib daddr type local") == 2
    for direction in ("dport", "sport"):
        assert f"tcp {direction} {{ 9222, 5902, 7900 }} meta skuid != {{ 0, 1001 }}" in rules
    for unrelated in ("flush ruleset", "ct state established accept", "tcp dport 22"):
        assert unrelated not in rules


def test_firewall_generation_is_stable_across_registration_order() -> None:
    first = registration(1001, worker.LEGACY_BROWSER)
    second = registration(1002, worker.allocate_browser([first], {}))
    assert worker.firewall_rules([first, second]) == worker.firewall_rules([second, first])


def test_connection_requires_matching_identity_and_loaded_ports() -> None:
    entry = registration(1001, worker.LEGACY_BROWSER)
    marker: dict[str, object] = {"schema_version": 1, "boot_id": "this-boot", "users": {"1001": worker.LEGACY_BROWSER}}
    assert worker.validate_connection(entry, marker, account(), "this-boot") == {"uid": 1001, **worker.LEGACY_BROWSER}
    invalid_markers: list[dict[str, object]] = [
        {**marker, "boot_id": "previous-boot"},
        {**marker, "users": {}},
        {**marker, "users": {"1001": {**worker.LEGACY_BROWSER, "cdp_port": 20000}}},
    ]
    for invalid in invalid_markers:
        with pytest.raises(RuntimeError, match="not ready"):
            worker.validate_connection(entry, invalid, account(), "this-boot")
    with pytest.raises(RuntimeError, match="does not match"):
        worker.validate_connection({**entry, "uid": 1002}, marker, account(), "this-boot")


def test_retired_endpoint_metadata_does_not_block_browser_registration() -> None:
    entry = registration(1001, worker.LEGACY_BROWSER)
    saved = {**entry, "t3_port": 24000}
    assert worker.validate_registration(saved) == saved
    assert saved["t3_port"] == 24000
    marker: dict[str, object] = {
        "schema_version": 1,
        "boot_id": "this-boot",
        "users": {"1001": worker.LEGACY_BROWSER},
    }
    assert worker.validate_connection(saved, marker, account(), "this-boot") == {"uid": 1001, **worker.LEGACY_BROWSER}


def test_retired_listener_keeps_cross_user_isolation_until_its_registration_is_removed() -> None:
    entry = registration(1001, worker.LEGACY_BROWSER)
    entry["t3_port"] = 24000
    rules = worker.firewall_rules([entry])
    for direction in ("dport", "sport"):
        assert f"tcp {direction} {{ 9222, 5902, 7900, 24000 }} meta skuid != {{ 0, 1001 }}" in rules


@pytest.mark.parametrize("value", [None, [], True, {1: "private-invalid-field"}])
def test_registry_boundary_rejects_nonobjects_and_nontext_keys_without_echoing_values(value: object) -> None:
    with pytest.raises(RuntimeError, match=r"Expected a registry object|Expected text registry keys") as caught:
        worker.object_fields(value)
    assert "private-invalid-field" not in str(caught.value)


def test_registration_rejects_unsafe_field_types_and_paths() -> None:
    entry = registration(1001, worker.LEGACY_BROWSER)
    for invalid in (
        {**entry, "uid": True},
        {**entry, "gid": "1001"},
        {**entry, "home": "/root"},
        {**entry, "limits": {**entry["limits"], "cpu_weight": True}},
        {**entry, "unexpected": 1},
    ):
        with pytest.raises(RuntimeError, match=r"Expected|Unexpected"):
            worker.validate_registration(invalid)


def test_writable_registration_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "registration.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o666)
    with pytest.raises(RuntimeError, match="root-owned"):
        worker.root_owned(path)


def test_self_registration_identity_is_bound_to_the_sudo_caller() -> None:
    assert worker.validate_sudo_identity({"SUDO_UID": "1001", "SUDO_USER": "user1001"}, account()) == "user1001"
    for environment in (
        {},
        {"SUDO_UID": "0", "SUDO_USER": "root"},
        {"SUDO_UID": "1002", "SUDO_USER": "user1001"},
        {"SUDO_UID": "1001", "SUDO_USER": "other"},
    ):
        with pytest.raises(RuntimeError, match=r"sudo caller UID|Sudo caller identity"):
            worker.validate_sudo_identity(environment, account())


def test_private_login_defaults_preserve_other_settings_and_are_idempotent() -> None:
    updated = worker.private_login_defaults("# baseline\nPASS_MAX_DAYS 99999\nUMASK 022\n")
    assert "PASS_MAX_DAYS 99999" in updated
    assert "UMASK 077" in updated
    assert "HOME_MODE 0700" in updated
    assert worker.private_login_defaults(updated) == updated


def test_advisory_slice_controls_do_not_add_a_memory_kill_limit() -> None:
    policy = worker.slice_policy(worker.ResourceLimits(memory_high="3G", tasks_max="2048", cpu_weight=100))
    assert all(value in policy for value in ("MemoryHigh=3G", "TasksMax=2048", "CPUWeight=100"))
    assert "MemoryMax" not in policy


def test_self_registration_sudo_policy_is_one_exact_command() -> None:
    assert worker.SELF_REGISTRATION_POLICY == "ALL ALL=(root) NOPASSWD: /usr/local/sbin/lra-user register-self\n"


@pytest.mark.parametrize(
    "values",
    [
        {"username": "../root"},
        {"username": "user1001", "uid": 0},
        {"username": "user1001", "cpu_weight": 10001},
        {"username": "user1001", "memory_high": "3G\nMemoryMax=1"},
    ],
    ids=["unsafe-username", "privileged-uid", "excessive-cpu-weight", "memory-policy-injection"],
)
def test_public_registration_rejects_invalid_fields(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RegistrationInput.model_validate(values)


def test_worker_is_standalone_python312_compatible() -> None:
    ast.parse(WORKER.read_text(encoding="utf-8"), feature_version=(3, 12))


def test_register_self_accepts_no_identity_or_permission_arguments() -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(WORKER), "register-self", "--legacy-browser"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="requires an ordinary process")
@pytest.mark.parametrize(
    "arguments", [["register-self"], ["register", "nobody"]], ids=["self-registration", "named-registration"]
)
def test_forged_sudo_environment_cannot_cross_the_root_guard(arguments: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(WORKER), *arguments],
        env={"SUDO_UID": "1001", "SUDO_USER": "user1001"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "require root" in result.stderr


@pytest.mark.skipif(
    sys.platform != "linux"
    or os.geteuid() != 0
    or not all(shutil.which(program) for program in ("nft", "unshare", "ip")),
    reason="requires root and nft/ip/unshare in an isolated Linux network namespace",
)
def test_kernel_owner_and_root_access_and_cross_uid_denial() -> None:
    unshare = shutil.which("unshare")
    assert unshare is not None
    result = subprocess.run(
        [unshare, "--net", sys.executable, __file__, "--namespace-worker"],
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    if "Operation not permitted" in result.stderr:
        pytest.skip("This container cannot create an isolated network namespace")
    assert result.returncode == 0, result.stderr
    report = worker.decode_object(result.stdout)
    assert report["cross_uid_connections_allowed"] == 0
    assert report["owner_allowed"] is True
    assert report["root_allowed"] is True
    assert report["impersonation_allowed"] is False
    assert all(
        report[key] is True
        for key in (
            "unrelated_port_allowed",
            "ipv4",
            "ipv6",
            "other_local_address",
        )
    )


def namespace_worker() -> None:
    ip, nft = shutil.which("ip"), shutil.which("nft")
    if ip is None or nft is None:
        message = "Isolated Linux fixture requires ip and nft"
        raise RuntimeError(message)
    subprocess.run([ip, "link", "set", "lo", "up"], check=True)
    subprocess.run([ip, "address", "add", "10.254.254.1/32", "dev", "lo"], check=True)
    subprocess.run([ip, "-6", "address", "add", "fd00::1/128", "dev", "lo"], check=True)
    browser = worker.Browser(display=100, cdp_port=20000, vnc_port=20001, viewer_port=20002)
    entry = registration(1001, browser)
    rules = worker.firewall_rules([entry])
    subprocess.run([nft, "--check", "--file", "-"], input=rules, text=True, check=True)
    subprocess.run([nft, "--file", "-"], input=rules, text=True, check=True)
    subprocess.run([nft, "--file", "-"], input=rules, text=True, check=True)
    server_code = """import socket,sys
s=socket.socket(int(sys.argv[1]),socket.SOCK_STREAM)
if s.family==socket.AF_INET6:s.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind((sys.argv[2],int(sys.argv[3])));s.listen();print('ready',flush=True)
while True:
 c,a=s.accept();c.close()
"""
    client_code = """import socket,sys
try:
 s=socket.create_connection((sys.argv[1],int(sys.argv[2])),timeout=1);s.close()
except OSError:sys.exit(1)
"""
    children: list[subprocess.Popen[str]] = []

    def server(uid: int, family: int, host: str, port: int) -> None:
        child: subprocess.Popen[str] = subprocess.Popen(
            ["/usr/bin/python3", "-c", server_code, str(family), host, str(port)],
            user=uid,
            group=uid,
            extra_groups=[],
            stdout=subprocess.PIPE,
            text=True,
        )
        children.append(child)
        if child.stdout is None or os.read(child.stdout.fileno(), len(b"ready\n")) != b"ready\n":
            message = "Isolated socket fixture failed to start"
            raise RuntimeError(message)

    def connect(uid: int, host: str, port: int) -> bool:
        return (
            subprocess.run(
                ["/usr/bin/python3", "-c", client_code, host, str(port)],
                user=uid,
                group=uid,
                extra_groups=[],
                check=False,
            ).returncode
            == 0
        )

    try:
        server(1001, 2, "0.0.0.0", 20000)  # ruff: ignore[hardcoded-bind-all-interfaces] — root-only isolated network namespace fixture.
        server(1001, 10, "::", 20000)
        server(1002, 2, "127.0.0.1", 20001)
        server(1002, 2, "127.0.0.1", 25000)
        report: dict[str, bool | int] = {
            "owner_allowed": connect(1001, "127.0.0.1", 20000),
            "root_allowed": connect(0, "127.0.0.1", 20000),
            "ipv4": not connect(1002, "127.0.0.1", 20000),
            "ipv6": not connect(1002, "::1", 20000),
            "other_local_address": not connect(1002, "10.254.254.1", 20000) and not connect(1002, "fd00::1", 20000),
            "impersonation_allowed": connect(1001, "127.0.0.1", 20001),
            "unrelated_port_allowed": connect(1001, "127.0.0.1", 25000),
        }
        report["cross_uid_connections_allowed"] = sum(
            not report[name] for name in ("ipv4", "ipv6", "other_local_address")
        )
        sys.stdout.write(json.dumps(report) + "\n")
    finally:
        for child in children:
            child.terminate()
            child.wait(timeout=5)


if __name__ == "__main__" and sys.argv[1:] == ["--namespace-worker"]:
    namespace_worker()
