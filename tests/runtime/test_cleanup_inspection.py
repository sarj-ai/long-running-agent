from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Thread
import time
from typing import TYPE_CHECKING

from long_running_agent.process import run
from long_running_agent.runtime import cleanup_inspection
import pytest


if TYPE_CHECKING:
    from collections.abc import Callable, Generator


pytestmark = pytest.mark.skipif(not Path("/proc/self").exists(), reason="Linux peer credentials and systemd units")


def success(request: cleanup_inspection.ProbeRequest) -> dict[str, object]:
    return {
        "schema": 1,
        "nonce": request.nonce,
        "uid": os.getuid(),
        "protected": request.candidates,
        "package_writer": False,
        "zellij_running": True,
        "process_count": 1,
    }


@contextmanager
def responder(socket_dir: Path, factory: Callable[[cleanup_inspection.ProbeRequest], bytes]) -> Generator[Path]:
    endpoint = socket_dir / "probe.sock"
    errors: list[str] = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(endpoint))
        server.listen(1)
        server.settimeout(3)

        def respond(connection: socket.socket) -> None:
            with connection:
                connection.settimeout(3)
                payload = bytearray()
                while chunk := connection.recv(65536):
                    payload.extend(chunk)
                if payload:
                    request = cleanup_inspection.ProbeRequest.model_validate_json(payload)
                    connection.sendall(factory(request))

        def serve() -> None:
            try:
                connection = server.accept()[0]
                respond(connection)
            except OSError as error:
                errors.append(type(error).__name__)

        thread = Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield endpoint
        finally:
            thread.join(timeout=4)
            assert not thread.is_alive()
            assert not errors


@pytest.fixture
def socket_dir() -> Generator[Path]:
    # Keep real AF_UNIX paths short even when pytest uses a long temporary root.
    with TemporaryDirectory(prefix="lra-probe-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def local_trust(monkeypatch: pytest.MonkeyPatch, socket_dir: Path) -> None:
    def fixture_socket(endpoint: Path, *, owner: int) -> os.stat_result:
        assert endpoint.parent == socket_dir
        metadata = endpoint.lstat()
        assert metadata.st_uid == owner == os.getuid()
        return metadata

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Real unprivileged sockets test framing/peer credentials; only the root-owned filesystem anchor is substituted.
        cleanup_inspection, "trusted_socket", fixture_socket
    )


@pytest.mark.usefixtures("local_trust")
def test_native_client_checks_peer_and_returns_only_requested_matches(socket_dir: Path) -> None:
    request = cleanup_inspection.ProbeRequest(nonce="a" * 32, candidates=["/home/fixture/code/old"])

    def reply(value: cleanup_inspection.ProbeRequest) -> bytes:
        return json.dumps(success(value)).encode()

    with responder(socket_dir, reply) as endpoint:
        result = cleanup_inspection.exchange(request, endpoint, owner=os.getuid())
    assert result.protected == request.candidates
    assert result.zellij_running
    assert not result.package_writer


@pytest.mark.parametrize("mutation", ["nonce", "uid", "foreign", "duplicate"])
@pytest.mark.usefixtures("local_trust")
def test_native_client_rejects_misbound_or_unrequested_protections(socket_dir: Path, mutation: str) -> None:
    request = cleanup_inspection.ProbeRequest(nonce="a" * 32, candidates=["/home/fixture/code/old"])

    def reply(value: cleanup_inspection.ProbeRequest) -> bytes:
        payload = success(value)
        match mutation:
            case "nonce":
                payload["nonce"] = "b" * 32
            case "uid":
                payload["uid"] = os.getuid() + 1
            case "foreign":
                payload["protected"] = ["/home/another/code/private"]
            case _:
                payload["protected"] = [*value.candidates, *value.candidates]
        return json.dumps(payload).encode()

    with (
        responder(socket_dir, reply) as endpoint,
        pytest.raises(cleanup_inspection.ProbeUnavailableError, match="Read-only process inspection"),
    ):
        cleanup_inspection.exchange(request, endpoint, owner=os.getuid())


@pytest.mark.usefixtures("local_trust")
def test_native_client_rejects_worker_failure_instead_of_treating_it_as_empty(socket_dir: Path) -> None:
    request = cleanup_inspection.ProbeRequest(nonce="a" * 32, candidates=[])

    def failure(_value: cleanup_inspection.ProbeRequest) -> bytes:
        return b'{"error":"inspection incomplete"}\n'

    with responder(socket_dir, failure) as endpoint, pytest.raises(cleanup_inspection.ProbeUnavailableError) as error:
        cleanup_inspection.exchange(request, endpoint, owner=os.getuid())
    assert error.value.reason == "worker-rejected"


def test_unprivileged_socket_cannot_impersonate_the_production_probe(socket_dir: Path) -> None:
    endpoint = socket_dir / "probe.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(endpoint))
        with pytest.raises(cleanup_inspection.ProbeUnavailableError, match="Read-only process inspection"):
            cleanup_inspection.trusted_socket(endpoint, owner=0)


def test_prepared_units_parse_and_restrict_the_privileged_surface(tmp_path: Path) -> None:
    unit = tmp_path / cleanup_inspection.SERVICE_UNIT
    unit.write_text(cleanup_inspection.service_text())
    listener = tmp_path / cleanup_inspection.SOCKET_UNIT
    listener.write_text(cleanup_inspection.socket_text())
    result = run(["systemd-analyze", "verify", "--man=no", str(unit), str(listener)], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "CapabilityBoundingSet=CAP_SYS_PTRACE CAP_DAC_READ_SEARCH" in unit.read_text()
    assert "NoNewPrivileges=true" in unit.read_text()
    assert "ProtectSystem=strict" in unit.read_text()
    commands = cleanup_inspection.installation_commands(tmp_path)
    service_changes = [command for command in commands if "/usr/bin/systemctl" in command]
    assert service_changes == [
        ["sudo", "--", "/usr/bin/systemctl", "daemon-reload"],
        ["sudo", "--", "/usr/bin/systemctl", "enable", "--now", cleanup_inspection.SOCKET_UNIT],
        ["sudo", "--", "/usr/bin/systemctl", "is-active", "--quiet", cleanup_inspection.SOCKET_UNIT],
    ]


def recovered_client_exchange(socket_dir: Path) -> cleanup_inspection.ProbeReply:
    request = cleanup_inspection.ProbeRequest(nonce="b" * 32, candidates=["/home/fixture/code/old"])

    def valid(value: cleanup_inspection.ProbeRequest) -> bytes:
        return json.dumps(success(value)).encode()

    with responder(socket_dir, valid) as endpoint:
        result = cleanup_inspection.exchange(request, endpoint, owner=os.getuid())
    endpoint.unlink()
    assert result.nonce == request.nonce
    assert result.uid == os.getuid()
    assert result.protected == request.candidates
    assert result.zellij_running
    return result


def failure_then_fresh_client(
    socket_dir: Path,
    factory: Callable[[cleanup_inspection.ProbeRequest], bytes],
    reason: cleanup_inspection.ProbeFailureReason,
) -> None:
    request = cleanup_inspection.ProbeRequest(nonce="a" * 32, candidates=["/home/fixture/code/old"])
    with responder(socket_dir, factory) as endpoint, pytest.raises(cleanup_inspection.ProbeUnavailableError) as error:
        cleanup_inspection.exchange(request, endpoint, owner=os.getuid())
    assert error.value.reason == reason
    assert "private-fixture" not in str(error.value)
    endpoint.unlink()
    recovered_client_exchange(socket_dir)


@pytest.mark.usefixtures("local_trust")
def test_known_worker_budget_codes_and_legacy_errors_recover_only_on_fresh_success(socket_dir: Path) -> None:
    cases: list[tuple[bytes, cleanup_inspection.ProbeFailureReason]] = [
        (b'{"error":"inspection incomplete"}', "worker-rejected"),
        (b'{"error":"inspection incomplete","code":"incomplete"}', "worker-incomplete"),
        (b'{"error":"inspection incomplete","code":"deadline"}', "worker-deadline"),
        (b'{"error":"inspection incomplete","code":"process-limit"}', "worker-process-limit"),
        (b'{"error":"inspection incomplete","code":"descriptor-limit"}', "worker-descriptor-limit"),
        (b'{"error":"inspection incomplete","code":"byte-budget"}', "worker-byte-budget"),
        (b'{"error":"inspection incomplete","code":"maps-too-large"}', "worker-maps-too-large"),
        (b'{"code":"metadata-too-large","error":"inspection incomplete"}', "worker-metadata-too-large"),
    ]
    for payload, reason in cases:

        def failure(_value: cleanup_inspection.ProbeRequest, payload: bytes = payload) -> bytes:
            return payload

        failure_then_fresh_client(socket_dir, failure, reason)


@pytest.mark.usefixtures("local_trust")
def test_unknown_duplicate_or_extra_failure_fields_are_private_and_recoverable(socket_dir: Path) -> None:
    payloads = [
        b'{"error":"inspection incomplete","code":"private-fixture"}',
        b'{"error":"inspection incomplete","code":"maps-too-large","private-fixture":1}',
        b'{"error":"private-fixture","code":"maps-too-large"}',
        b'{"error":"inspection incomplete","code":"maps-too-large","code":"deadline"}',
        b'{"error":"inspection incomplete","code":"maps-too-large","protected":["/private-fixture"]}',
    ]
    for payload in payloads:

        def failure(_value: cleanup_inspection.ProbeRequest, payload: bytes = payload) -> bytes:
            return payload

        failure_then_fresh_client(socket_dir, failure, "reply-invalid")


@pytest.mark.usefixtures("local_trust")
def test_empty_worker_reply_is_not_an_empty_inventory_and_fresh_reply_recovers(socket_dir: Path) -> None:
    def empty(_value: cleanup_inspection.ProbeRequest) -> bytes:
        return b""

    failure_then_fresh_client(socket_dir, empty, "reply-empty")


@pytest.mark.usefixtures("local_trust")
def test_oversized_worker_reply_is_bounded_and_fresh_reply_recovers(socket_dir: Path) -> None:
    def oversized(_value: cleanup_inspection.ProbeRequest) -> bytes:
        return b"x" * (cleanup_inspection.MAX_MESSAGE + 1)

    failure_then_fresh_client(socket_dir, oversized, "reply-oversize")


@pytest.mark.usefixtures("local_trust")
def test_invalid_success_schema_never_leaks_validation_input_then_fresh_reply_recovers(socket_dir: Path) -> None:
    for mutation in ("json", "boolean-count", "extra", "protected-type"):

        def malformed(value: cleanup_inspection.ProbeRequest, mutation: str = mutation) -> bytes:
            if mutation == "json":
                return b'{"private-fixture":'
            payload = success(value)
            match mutation:
                case "boolean-count":
                    payload["process_count"] = False
                case "extra":
                    payload["private-fixture"] = "private-fixture"
                case _:
                    payload["protected"] = "private-fixture"
            return json.dumps(payload).encode()

        failure_then_fresh_client(socket_dir, malformed, "reply-invalid")


@pytest.mark.usefixtures("local_trust")
def test_misbound_identity_and_scope_reject_then_fresh_bound_reply_recovers(socket_dir: Path) -> None:
    for mutation in ("nonce", "uid", "foreign", "duplicate"):

        def misbound(value: cleanup_inspection.ProbeRequest, mutation: str = mutation) -> bytes:
            payload = success(value)
            match mutation:
                case "nonce":
                    payload["nonce"] = "c" * 32
                case "uid":
                    payload["uid"] = os.getuid() + 1
                case "foreign":
                    payload["protected"] = ["/home/foreign/code/private-fixture"]
                case _:
                    payload["protected"] = [*value.candidates, *value.candidates]
            return json.dumps(payload).encode()

        reason: cleanup_inspection.ProbeFailureReason = (
            "reply-identity" if mutation in {"nonce", "uid"} else "reply-scope"
        )
        failure_then_fresh_client(socket_dir, misbound, reason)


@pytest.mark.usefixtures("local_trust")
def test_missing_and_refused_socket_classification_cannot_prevent_later_fresh_success(socket_dir: Path) -> None:
    endpoint = socket_dir / "probe.sock"
    request = cleanup_inspection.ProbeRequest(nonce="a" * 32, candidates=[])
    with pytest.raises(cleanup_inspection.ProbeUnavailableError) as missing:
        cleanup_inspection.exchange(request, endpoint, owner=os.getuid())
    assert missing.value.reason == "connect-missing"
    recovered_client_exchange(socket_dir)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as inactive:
        inactive.bind(str(endpoint))
        with pytest.raises(cleanup_inspection.ProbeUnavailableError) as refused:
            cleanup_inspection.exchange(request, endpoint, owner=os.getuid())
    assert refused.value.reason == "connect-refused"
    endpoint.unlink()
    recovered_client_exchange(socket_dir)


@pytest.mark.usefixtures("local_trust")
def test_outgoing_byte_bound_and_deadline_fail_without_retries_then_fresh_reply_recovers(
    socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = cleanup_inspection.ProbeRequest(nonce="a" * 32, candidates=["/home/fixture/code/" + "🧪" * 4000] * 512)
    with pytest.raises(cleanup_inspection.ProbeUnavailableError) as oversized:
        cleanup_inspection.exchange(request, socket_dir / "never-connected.sock", owner=os.getuid())
    assert oversized.value.reason == "request-invalid"
    moments = iter((0.0, cleanup_inspection.TIMEOUT + 1.0))

    def expired_clock() -> float:
        return next(moments, cleanup_inspection.TIMEOUT + 1.0)

    def never_called(_value: cleanup_inspection.ProbeRequest) -> bytes:
        pytest.fail("Expired client must close before sending a request")

    request = cleanup_inspection.ProbeRequest(nonce="a" * 32, candidates=[])
    with responder(socket_dir, never_called) as endpoint, monkeypatch.context() as clock:
        clock.setattr(  # sarj-noqa: SARJ445 -- Advance only the OS monotonic clock to expire a real socket after authenticated connect without sleeping75seconds.
            time, "monotonic", expired_clock
        )
        with pytest.raises(cleanup_inspection.ProbeUnavailableError) as expired:
            cleanup_inspection.exchange(request, endpoint, owner=os.getuid())
    assert expired.value.reason == "deadline"
    endpoint.unlink()
    recovered_client_exchange(socket_dir)
