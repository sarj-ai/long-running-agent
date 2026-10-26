from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import override

from long_running_agent import Client
from long_running_agent.client import transfer
from long_running_agent.config import Settings
from long_running_agent.process import DEVNULL, spawn
import pytest


@dataclass(frozen=True)
class NativeHost:
    vm: Path
    downloads: Path
    controller: Client


class NativeController(Client):
    @override
    def ensure_connection(self) -> None:
        pass


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NativeHost:
    vm = tmp_path / "vm home"
    binaries = vm / ".local/bin"
    binaries.mkdir(parents=True, mode=0o700)
    vm.chmod(0o700)
    runtime = binaries / "lra"
    runtime.write_text(
        f"#!{sys.executable}\nimport sys\nfrom long_running_agent.runtime.transfer import app\n"
        "sys.argv=[sys.argv[0],*sys.argv[3:]]\napp()\n"
    )
    runtime.chmod(0o700)
    local = tmp_path / "native-bin"
    local.mkdir()
    ssh = local / "ssh"
    ssh.write_text(
        f"#!{sys.executable}\nimport subprocess,sys\n"
        f"sys.exit(subprocess.run(['/bin/sh','-c',sys.argv[-1]],env={{'HOME':{str(vm)!r},'PATH':'/usr/bin:/bin'}},check=False).returncode)\n"
    )
    ssh.chmod(0o700)
    monkeypatch.setenv("PATH", f"{local!s}:/usr/bin:/bin")
    downloads = tmp_path / "Mac Downloads"
    downloads.mkdir(mode=0o700)
    return NativeHost(vm, downloads, NativeController(Settings()))


def test_selected_images_round_trip_over_actual_binary_subprocess_pipes(host: NativeHost, tmp_path: Path) -> None:
    selected = tmp_path / "image with 雪 ; $(touch marker).png"
    payload = bytes(range(256)) * 400
    selected.write_bytes(payload)
    receipts = transfer.send(host.controller, [selected])
    assert len(receipts) == 1
    uploaded = receipts[0]
    assert Path(uploaded.path).is_relative_to(host.vm)
    assert Path(uploaded.path).read_bytes() == payload
    downloaded = transfer.get(host.controller, uploaded.path, host.downloads)
    assert Path(downloaded.path).read_bytes() == payload
    assert downloaded.size == len(payload)
    assert downloaded.sha256 == hashlib.sha256(payload).hexdigest()
    assert Path(downloaded.path).stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "marker").exists()


def test_upload_batch_normalizes_control_characters_without_changing_image_bytes(
    host: NativeHost, tmp_path: Path
) -> None:
    first = tmp_path / "line\nbreak.png"
    second = tmp_path / "empty.png"
    first.write_bytes(b"\x89PNG\0\xff\r\n")
    second.touch()
    receipts = transfer.send(host.controller, [first, second])
    assert [Path(receipt.path).name for receipt in receipts] == ["line_break.png", "empty.png"]
    assert Path(receipts[0].path).read_bytes() == first.read_bytes()
    assert receipts[1].size == 0


@pytest.mark.parametrize("count", [0, 17], ids=["empty-selection", "over-batch-limit"])
def test_upload_count_rejects_invalid_selection_before_connecting(tmp_path: Path, count: int) -> None:
    class DisconnectedController(Client):
        @override
        def ensure_connection(self) -> None:
            pytest.fail("Invalid selection attempted a connection")

    with pytest.raises(ValueError, match="one and sixteen"):
        transfer.send(DisconnectedController(Settings()), [tmp_path / "unused"] * count)


def test_colliding_normalized_upload_names_fail_before_any_vm_inbox_file(host: NativeHost, tmp_path: Path) -> None:
    first = tmp_path / "line\nbreak.png"
    second = tmp_path / "line_break.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    with pytest.raises(ValueError, match="distinct after normalization"):
        transfer.send(host.controller, [first, second])
    assert not (host.vm / ".local/share/long-running-agent/inbox").exists()


@pytest.mark.parametrize("symlink", [False, True], ids=["existing-file", "existing-symlink"])
def test_download_never_overwrites_existing_file_or_symlink(host: NativeHost, *, symlink: bool) -> None:
    source = host.vm / "image.png"
    source.write_bytes(b"selected binary")
    sentinel = host.downloads / "keep"
    sentinel.write_bytes(b"existing")
    selected = host.downloads / source.name
    if symlink:
        selected.symlink_to(sentinel)
    else:
        selected.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        transfer.get(host.controller, str(source), host.downloads)
    assert sentinel.read_bytes() == b"existing"
    assert selected.is_symlink() if symlink else selected.read_bytes() == b"existing"
    assert not list(host.downloads.glob(".incoming-*"))


def test_download_uses_literal_remote_path_and_never_executes_filename(host: NativeHost) -> None:
    source = host.vm / "$(touch injected); 'quote'.png"
    source.write_bytes(b"explicit")
    receipt = transfer.get(host.controller, str(source), host.downloads)
    assert Path(receipt.path).read_bytes() == b"explicit"
    assert not (host.vm / "injected").exists()


def test_download_rejects_source_outside_vm_home_without_publishing(host: NativeHost, tmp_path: Path) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep")
    with pytest.raises(RuntimeError, match="metadata was interrupted"):
        transfer.get(host.controller, str(outside), host.downloads)
    assert not list(host.downloads.iterdir())


def test_download_directory_symlink_is_rejected_before_connection(host: NativeHost, tmp_path: Path) -> None:
    alias = tmp_path / "downloads-alias"
    alias.symlink_to(host.downloads, target_is_directory=True)
    with pytest.raises(OSError, match=r"Not a directory|Too many levels"):
        transfer.get(host.controller, str(host.vm / "unused"), alias)
    assert not list(host.downloads.iterdir())


def test_missing_download_directory_is_created_privately_without_parent_tree_creation(
    host: NativeHost, tmp_path: Path
) -> None:
    source = host.vm / "image.png"
    source.write_bytes(b"selected")
    destination = tmp_path / "New Downloads"
    receipt = transfer.get(host.controller, str(source), destination)
    assert Path(receipt.path).read_bytes() == b"selected"
    assert destination.stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileNotFoundError):
        transfer.get(host.controller, str(source), tmp_path / "missing/implicit/tree")


@pytest.mark.parametrize("kind", ["outside-inbox", "wrong-batch", "control-character", "parent-segment"])
def test_uploaded_receipt_must_name_private_inbox_and_never_return_terminal_controls(
    host: NativeHost, tmp_path: Path, kind: str
) -> None:
    selected = tmp_path / "image.png"
    selected.write_bytes(b"selected")
    batch = "a" * 32
    path = host.vm / ".local/share/long-running-agent/inbox" / batch / selected.name
    match kind:
        case "outside-inbox":
            path = host.vm / "other" / selected.name
        case "wrong-batch":
            path = path.parent.parent / "not-a-private-batch" / selected.name
        case "control-character":
            path = Path(f"{host.vm!s}\x1b[2J") / ".local/share/long-running-agent/inbox" / batch / selected.name
        case _:
            path = host.vm / "other/.." / ".local/share/long-running-agent/inbox" / batch / selected.name
    receipts = [
        {
            "path": str(path),
            "size": selected.stat().st_size,
            "sha256": hashlib.sha256(selected.read_bytes()).hexdigest(),
        }
    ]
    runtime = host.vm / ".local/bin/lra"
    runtime.write_text(f"#!{sys.executable}\nimport sys\nsys.stdin.buffer.read()\nprint({json.dumps(receipts)!r})\n")
    with pytest.raises(RuntimeError, match="outside a private inbox"):
        transfer.send(host.controller, [selected])


def test_failed_remote_exit_discards_complete_but_unconfirmed_download(host: NativeHost) -> None:
    selected = host.vm / "image.png"
    selected.write_bytes(b"valid bytes")
    runtime = host.vm / ".local/bin/lra"
    runtime.write_text(runtime.read_text().replace("app()", "app(standalone_mode=False)") + "raise SystemExit(3)\n")
    with pytest.raises(RuntimeError, match="Private SSH file transfer failed"):
        transfer.get(host.controller, str(selected), host.downloads)
    assert not list(host.downloads.iterdir())


def test_local_symlink_is_rejected_without_upload(host: NativeHost, tmp_path: Path) -> None:
    selected = tmp_path / "image.png"
    selected.write_bytes(b"explicit")
    alias = tmp_path / "alias.png"
    alias.symlink_to(selected)
    with pytest.raises(OSError, match="Too many levels"):
        transfer.send(host.controller, [alias])
    assert not (host.vm / ".local/share/long-running-agent/inbox").exists()


class EstablishedController(Client):
    @override
    def ensure_connection(self) -> None:
        pytest.fail("A terminal-owned transfer must not start an untracked SSH health probe")


def test_terminal_transfer_control_reuses_connection_without_health_probe(host: NativeHost, tmp_path: Path) -> None:
    selected = tmp_path / "selected.png"
    selected.write_bytes(b"explicit binary\0\xff")
    controller = EstablishedController(Settings())
    control = transfer.TransferControl()
    uploaded = transfer.send(controller, [selected], control=control)[0]
    downloaded = transfer.get(controller, uploaded.path, host.downloads, control=control)
    assert Path(downloaded.path).read_bytes() == selected.read_bytes()
    control.cancel()


def test_cancelled_control_rejects_new_transfers_before_spawning_or_creating_paths(tmp_path: Path) -> None:
    control = transfer.TransferControl()
    control.cancel()
    controller = EstablishedController(Settings())
    with pytest.raises(InterruptedError, match="transfer cancelled"):
        transfer.send(controller, [tmp_path / "unused"], control=control)
    with pytest.raises(InterruptedError, match="transfer cancelled"):
        transfer.get(controller, "/home/unused", tmp_path / "not-created", control=control)
    assert not (tmp_path / "not-created").exists()
    control.cancel()


@pytest.mark.parametrize("direction", ["upload", "download"], ids=["blocked-upload", "partial-download"])
def test_terminal_cancellation_reaps_only_owned_stalled_ssh_and_discards_partial_download(
    host: NativeHost, tmp_path: Path, direction: str
) -> None:
    marker = tmp_path / "owned-ssh-started"
    selected = tmp_path / "selected.png"
    selected.write_bytes(b"x" * 1024 * 1024)
    payload = b"x" * 100
    header = json.dumps({"name": "partial.png", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    script = (
        f"#!{sys.executable}\nimport pathlib,signal,sys,time\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        f"pathlib.Path({str(marker)!r}).write_text('started')\n"
    )
    if direction == "download":
        script += f"print({header!r},flush=True)\nsys.stdout.buffer.write(b'x');sys.stdout.buffer.flush()\n"
    script += "time.sleep(30)\n"
    (host.vm.parent / "native-bin/ssh").write_text(script)
    control = transfer.TransferControl()
    controller = EstablishedController(Settings())

    def selected_transfer() -> list[transfer.Receipt] | transfer.Receipt:
        match direction:
            case "upload":
                return transfer.send(controller, [selected], control=control)
            case _:
                return transfer.get(controller, str(host.vm / "selected.png"), host.downloads, control=control)

    unrelated = spawn(
        [sys.executable, "-c", "import time;time.sleep(30)"], stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(selected_transfer)
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert marker.exists()
                start = time.monotonic()
                control.cancel()
                with pytest.raises(InterruptedError, match="transfer cancelled"):
                    pending.result(timeout=5)
                assert time.monotonic() - start < 5
                assert unrelated.poll() is None
                assert not list(host.downloads.iterdir())
            finally:
                control.cancel()
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=2)
