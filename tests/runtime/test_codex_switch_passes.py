from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path

from long_running_agent.runtime import codex
import pytest

from tests.runtime.test_codex import fake_server


@pytest.mark.parametrize("fragment", range(1, 51), ids=[f"account-pass-{value:02}" for value in range(1, 51)])
def test_native_account_switch_over_fragmented_stdio(
    fragment: int, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    program = """
import json,os,sys
fragment = FRAGMENT
complete = False
def send(value):
 payload=(json.dumps(value)+"\\n").encode()
 for start in range(0,len(payload),fragment):
  os.write(sys.stdout.fileno(),payload[start:start+fragment])
for line in sys.stdin:
 request=json.loads(line)
 method=request.get("method")
 if method=="initialized":continue
 if method=="initialize":result={}
 elif method=="account/read":
  result={"account":{"type":"chatgpt","email":"new@example.invalid" if complete else "old@example.invalid","planType":"business","accessToken":"fixture-secret"}}
 elif method=="account/login/start":
  assert request["params"]=={"type":"chatgptDeviceCode"}
  send({"method":"account/login/completed","params":{"loginId":"unrelated","success":False}})
  send({"method":"account/login/completed","params":{"loginId":"fixture-login","success":True}})
  result={"loginId":"fixture-login","verificationUrl":"https://auth.openai.com/codex/device","userCode":"TEST-CODE"}
  complete=True
 else:raise AssertionError(method)
 send({"id":request["id"],"result":result})
""".replace("FRAGMENT", str(fragment))
    shown: list[codex.DeviceCode] = []
    with fake_server(tmp_path, program) as peer:
        account = codex.NativeCodex(peer).switch_account(shown.append)
    assert account.email == "new@example.invalid"
    assert account.method == "chatgpt"
    assert account.plan == "business"
    assert shown[0].verification_url == "https://auth.openai.com/codex/device"
    assert shown[0].user_code == "TEST-CODE"
    assert "fixture-secret" not in account.model_dump_json()
    output = capsys.readouterr()
    assert "fixture-secret" not in output.out + output.err
