from __future__ import annotations

from dataclasses import dataclass, field
import json
import signal
import sys
from typing import TYPE_CHECKING

from long_running_agent.process import DEVNULL, PIPE, run, spawn
from long_running_agent.runtime import codex
from pydantic import JsonValue
import pytest
from typer import Exit


if TYPE_CHECKING:
    from pathlib import Path

PRIVATE = "private-native-auth-must-not-appear"


@dataclass
class Peer:
    success: bool = True
    completed: bool = False
    calls: list[tuple[str, dict[str, JsonValue]]] = field(default_factory=list)

    def request(self, method: str, params: dict[str, JsonValue]) -> JsonValue:
        self.calls.append((method, params))
        if method == "account/read":
            return {
                "account": {
                    "type": "chatgpt",
                    "email": "new@example.invalid" if self.completed else "old@example.invalid",
                    "planType": "business",
                    "accessToken": PRIVATE,
                }
            }
        if method == "account/login/start":
            assert params == {"type": "chatgptDeviceCode"}
            return {
                "type": "chatgptDeviceCode",
                "loginId": "attempt",
                "verificationUrl": "https://auth.openai.com/codex/device",
                "userCode": "TEST-CODE",
            }
        assert method == "account/login/cancel"
        assert params == {"loginId": "attempt"}
        return {}

    def login_completed(self, login_id: str) -> bool:
        assert login_id == "attempt"
        self.completed = self.success
        return self.success


def test_switch_waits_for_native_completion_and_reads_native_account_before_returning() -> None:
    peer = Peer()
    shown: list[codex.DeviceCode] = []
    status = codex.NativeCodex(peer).switch_account(shown.append)
    assert status.email == "new@example.invalid"
    assert shown[0].user_code == "TEST-CODE"
    assert [method for method, _ in peer.calls] == ["account/read", "account/login/start", "account/read"]
    assert peer.calls[-1][1] == {"refreshToken": False}
    assert PRIVATE not in status.model_dump_json()


def test_failed_auth_does_not_logout_or_report_a_new_account() -> None:
    peer = Peer(success=False)
    with pytest.raises(codex.CodexError, match="sign-in did not complete"):
        codex.NativeCodex(peer).switch_account(lambda _: None)
    assert [method for method, _ in peer.calls] == ["account/read", "account/login/start"]
    assert not peer.completed


def test_interrupt_cancels_only_the_matching_native_login_without_logging_out() -> None:
    peer = Peer()

    def cancel(_: codex.DeviceCode) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        codex.NativeCodex(peer).switch_account(cancel)
    assert [method for method, _ in peer.calls] == ["account/read", "account/login/start", "account/login/cancel"]
    assert not peer.completed


def test_native_completion_without_verified_chatgpt_authentication_cannot_report_success() -> None:
    class SignedOutPeer(Peer):
        def request(self, method: str, params: dict[str, JsonValue]) -> JsonValue:
            if method == "account/read":
                return {"account": None}
            return super().request(method, params)

    with pytest.raises(codex.CodexError, match="could not be verified"):
        codex.NativeCodex(SignedOutPeer()).switch_account(lambda _: None)


@pytest.mark.parametrize("auth_method", ["apiKey", "futureUnknownMode"])
def test_unsupported_native_account_mode_is_rejected_before_device_login(auth_method: str) -> None:
    class UnsupportedPeer(Peer):
        def request(self, method: str, params: dict[str, JsonValue]) -> JsonValue:
            self.calls.append((method, params))
            assert method == "account/read"
            return {"account": {"type": auth_method, "accessToken": PRIVATE}}

    peer = UnsupportedPeer()
    with pytest.raises(codex.CodexError, match="manual account switch") as error:
        codex.NativeCodex(peer).switch_account(lambda _: pytest.fail("Unsupported auth must not start login"))
    assert [name for name, _ in peer.calls] == ["account/read"]
    assert PRIVATE not in str(error.value)


def test_native_login_policy_remains_authoritative_after_chatgpt_account_projection() -> None:
    class DeniedPeer(Peer):
        def request(self, method: str, params: dict[str, JsonValue]) -> JsonValue:
            if method == "account/login/start":
                self.calls.append((method, params))
                message = "Native login policy rejected the attempt"
                raise codex.CodexError(message)
            return super().request(method, params)

    peer = DeniedPeer()
    with pytest.raises(codex.CodexError, match="Native login policy"):
        codex.NativeCodex(peer).switch_account(lambda _: pytest.fail("Native policy must gate the challenge"))
    assert [method for method, _ in peer.calls] == ["account/read", "account/login/start"]


def fake_server(tmp_path: Path, program: str, *, timeout: float = 2) -> codex.CodexProcess:
    script = tmp_path / "fake_server.py"
    script.write_text(program, encoding="utf-8")
    return codex.CodexProcess(
        spawn([sys.executable, str(script)], stdin=PIPE, stdout=PIPE, stderr=DEVNULL),
        timeout=timeout,
        login_timeout=timeout,
    )


def test_real_stdio_contract_handles_early_completion_unrelated_events_and_redacts_rpc_errors(tmp_path: Path) -> None:
    program = (
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line)\n"
        " if 'id' not in r: continue\n"
        " if r['method']=='initialize':\n"
        "  assert r['params']['clientInfo']['name']=='sarj_connect'\n"
        "  assert r['params']['clientInfo']['version']\n"
        "  assert r['params']['capabilities']=={'experimentalApi':True}\n"
        "  result={}\n"
        " elif r['method']=='account/login/start':\n"
        "  print(json.dumps({'method':'account/login/completed','params':{'loginId':'unrelated','success':False}}),flush=True)\n"
        "  print(json.dumps({'method':'account/login/completed','params':{'loginId':'attempt','success':True}}),flush=True)\n"
        "  result={}\n"
        " else:\n"
        f"  print(json.dumps({{'id':r['id'],'error':{{'code':-32001,'message':{PRIVATE!r}}}}}),flush=True)\n"
        "  continue\n"
        " print(json.dumps({'id':r['id'],'result':result}),flush=True)\n"
    )
    with fake_server(tmp_path, program) as peer:
        assert peer.request("account/login/start", {"type": "chatgptDeviceCode"}) == {}
        assert peer.login_completed("attempt")
        with pytest.raises(codex.CodexError, match="RPC -32001") as error:
            peer.request("account/read", {})
        assert PRIVATE not in str(error.value)
    assert peer.process.poll() == 0


@pytest.mark.parametrize(
    ("program", "expected"),
    [
        ("import sys\nfor line in sys.stdin: pass\n", "timed out"),
        (
            "import json,sys\nfor line in sys.stdin:\n print('not-valid-json-private',flush=True)\n",
            "unsupported protocol",
        ),
        ("pass\n", "authentication server disconnected"),
    ],
)
def test_transport_fails_closed_without_exposing_native_payloads(tmp_path: Path, program: str, expected: str) -> None:
    timeout = 0.2 if expected == "timed out" else 5
    with pytest.raises(codex.CodexError, match=expected) as error, fake_server(tmp_path, program, timeout=timeout):
        pytest.fail("An invalid native peer completed initialization")
    assert "not-valid-json-private" not in str(error.value)


def test_cleanup_reaps_only_owned_stubborn_server_and_is_idempotent(tmp_path: Path) -> None:
    program = (
        "import json,signal,sys,time\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "r=json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'id':r['id'],'result':{}}),flush=True)\n"
        "while True: time.sleep(1)\n"
    )
    with fake_server(tmp_path, program) as peer:
        assert peer.process.poll() is None
    assert peer.process.poll() == -signal.SIGKILL
    peer.close()


def native_executable(tmp_path: Path, program: str) -> Path:
    executable = tmp_path / "codex"
    executable.write_text(f"#!{sys.executable}\n{program}", encoding="utf-8")
    executable.chmod(0o700)
    return executable


def test_status_cli_only_queries_metadata_and_withholds_native_tokens(tmp_path: Path) -> None:
    native_executable(
        tmp_path,
        "import json,sys\n"
        "assert sys.argv[1:]==['app-server','--stdio']\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line)\n"
        " if 'id' not in r: continue\n"
        " if r['method']=='initialize': result={}\n"
        " else:\n"
        "  assert r['method']=='account/read'\n"
        "  assert r['params']=={'refreshToken':False}\n"
        f"  result={{'account':{{'type':'chatgpt','email':'old@example.invalid','planType':'business','accessToken':{PRIVATE!r}}}}}\n"
        " print(json.dumps({'id':r['id'],'result':result}),flush=True)\n",
    )
    result = run(
        [sys.executable, "-m", "long_running_agent.cli", "runtime", "codex", "status"],
        env={"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path / "private-store"), "PATH": str(tmp_path)},
        capture_output=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == {"method": "chatgpt", "email": "old@example.invalid", "plan": "business"}
    assert PRIVATE not in result.stdout


def test_connection_owns_ephemeral_stdio_server_and_disables_only_child_remote_control(tmp_path: Path) -> None:
    environment = {"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path / "native"), "POLICY": "preserved"}
    selected_home = tmp_path / "validated-store"
    observed = tmp_path / "child-metadata.json"
    selected_executable = native_executable(
        tmp_path,
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "metadata={'arguments':sys.argv[1:],'home':os.environ['HOME'],'store':os.environ['CODEX_HOME'],"
        "'policy':os.environ['POLICY'],'remote':os.environ['CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED']}\n"
        f"Path({str(observed)!r}).write_text(json.dumps(metadata))\n"
        "for line in sys.stdin: pass\n",
    )
    peer = codex.connect(selected_home, selected_executable, environment=environment)
    peer.close()
    assert peer.process.poll() == 0
    assert json.loads(observed.read_text(encoding="utf-8")) == {
        "arguments": ["app-server", "--stdio"],
        "home": str(tmp_path),
        "store": str(selected_home),
        "policy": "preserved",
        "remote": "1",
    }
    assert "CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED" not in environment
    assert environment["CODEX_HOME"] == str(tmp_path / "native")


def test_native_switch_uses_preflight_home_and_returns_only_verified_account(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    executable = native_executable(
        tmp_path,
        "import json,os,sys\n"
        f"assert os.environ['CODEX_HOME']=={str(tmp_path)!r}\n"
        "assert sys.argv[1:]==['app-server','--stdio']\n"
        "completed=False\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line)\n"
        " if 'id' not in r: continue\n"
        " if r['method']=='initialize': result={}\n"
        " elif r['method']=='account/read':\n"
        "  email='new@example.invalid' if completed else 'old@example.invalid'\n"
        f"  result={{'account':{{'type':'chatgpt','email':email,'accessToken':{PRIVATE!r}}}}}\n"
        " else:\n"
        "  assert r['method']=='account/login/start'\n"
        "  assert r['params']=={'type':'chatgptDeviceCode'}\n"
        "  result={'loginId':'attempt','verificationUrl':'https://auth.openai.com/codex/device','userCode':'TEST-CODE'}\n"
        "  print(json.dumps({'method':'account/login/completed','params':{'loginId':'attempt','success':True}}),flush=True)\n"
        "  completed=True\n"
        " print(json.dumps({'id':r['id'],'result':result}),flush=True)\n",
    )
    account = codex.native_switch(tmp_path, executable)
    assert account.email == "new@example.invalid"
    assert PRIVATE not in account.model_dump_json()
    output = capsys.readouterr()
    assert "https://auth.openai.com/codex/device" in output.err
    assert "TEST-CODE" in output.err
    assert not output.out


def test_switch_refuses_preflight_before_authentication_or_restart() -> None:
    calls: list[str] = []

    def preflight() -> codex.codex_panes.PanePlan:
        calls.append("preflight")
        message = "Unsupported pane settings"
        raise codex.codex_panes.PaneError(message)

    def authenticate(_home: Path, _executable: Path) -> codex.AccountStatus:
        pytest.fail("Authentication must wait for a valid pane plan")

    def restart(_plan: codex.codex_panes.PanePlan) -> codex.codex_panes.RestartReport:
        pytest.fail("Invalid panes must never restart")

    with pytest.raises(codex.codex_panes.PaneError, match="Unsupported pane settings"):
        codex.switch_panes(preflight, authenticate, restart)
    assert calls == ["preflight"]


def test_rejected_native_authentication_never_restarts_a_pane(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = codex.codex_panes.PanePlan(tmp_path, tmp_path / "native-codex", (), tmp_path / "checkpoint.json")

    def reject(_home: Path, _executable: Path) -> codex.AccountStatus:
        message = "Native login policy rejected the attempt"
        raise codex.CodexError(message)

    def restart(_plan: codex.codex_panes.PanePlan) -> codex.codex_panes.RestartReport:
        pytest.fail("No pane may restart after failed authentication")

    with pytest.raises(codex.CodexError, match="Native login policy rejected"):
        codex.switch_panes(lambda: plan, reject, restart)
    assert not capsys.readouterr().out


@pytest.mark.parametrize("state", ["reopened", "held"])
def test_switch_reports_verified_account_then_each_pane_outcome(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    state: str,
) -> None:
    plan = codex.codex_panes.PanePlan(tmp_path, tmp_path / "native-codex", (), tmp_path / "checkpoint.json")
    calls: list[str] = []

    def preflight() -> codex.codex_panes.PanePlan:
        calls.append("preflight")
        return plan

    def authenticate(home: Path, executable: Path) -> codex.AccountStatus:
        assert (home, executable) == (plan.home, plan.executable)
        calls.append("auth")
        return codex.AccountStatus(method="chatgpt", email="new@example.invalid")

    def restart(selected: codex.codex_panes.PanePlan) -> codex.codex_panes.RestartReport:
        assert selected is plan
        calls.append("restart")
        return codex.codex_panes.RestartReport(
            (codex.codex_panes.PaneOutcome("work", codex.codex_panes.PaneId(7), state, "Native pane result"),),
            plan.checkpoint,
        )

    if state == "reopened":
        codex.switch_panes(preflight, authenticate, restart)
    else:
        with pytest.raises(Exit) as error:
            codex.switch_panes(preflight, authenticate, restart)
        assert error.value.exit_code == 1
    assert calls == ["preflight", "auth", "restart"]
    output = capsys.readouterr()
    account_line, panes_line = output.out.splitlines()
    assert json.loads(account_line) == {
        "event": "account_switched",
        "account": {"method": "chatgpt", "email": "new@example.invalid", "plan": None},
    }
    assert json.loads(panes_line) == {
        "event": "pane_results",
        "outcomes": [{"session": "work", "pane_id": 7, "state": state, "detail": "Native pane result"}],
        "checkpoint": str(plan.checkpoint),
        "failed": int(state != "reopened"),
        "reopened": int(state == "reopened"),
    }
    assert "from saved checkpoints" in output.err
    assert "native-codex" not in output.out


def test_restart_failure_does_not_hide_successful_native_account_commit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = codex.codex_panes.PanePlan(tmp_path, tmp_path / "native-codex", (), tmp_path / "checkpoint.json")

    def authenticate(_home: Path, _executable: Path) -> codex.AccountStatus:
        return codex.AccountStatus(method="chatgpt", email="new@example.invalid")

    def changed(_plan: codex.codex_panes.PanePlan) -> codex.codex_panes.RestartReport:
        message = "Pane changed before restart; existing panes were left running"
        raise codex.codex_panes.PaneError(message)

    with pytest.raises(codex.codex_panes.PaneError, match="Pane changed"):
        codex.switch_panes(lambda: plan, authenticate, changed)
    assert json.loads(capsys.readouterr().out)["account"]["email"] == "new@example.invalid"
