from __future__ import annotations

import argparse  # ruff: ignore[banned-api] — isolated Python 3.12 root worker cannot import user-installed Typer.
from datetime import UTC, datetime
import fcntl
import grp
import hashlib
import json
from operator import itemgetter
import os
from pathlib import Path
import pwd
import re
import subprocess  # ruff: ignore[suspicious-subprocess-import] — stdlib-only root worker; privileged executables are allowlisted below.
import sys
import tempfile
from typing import TYPE_CHECKING, Final, Never, NotRequired, TypedDict, TypeGuard


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


class Browser(TypedDict):
    display: int
    cdp_port: int
    vnc_port: int
    viewer_port: int


class ResourceLimits(TypedDict):
    memory_high: str
    tasks_max: str
    cpu_weight: int


class Registration(TypedDict):
    schema_version: int
    uid: int
    gid: int
    username: str
    home: str
    browser: Browser
    limits: ResourceLimits
    privileged_admin: bool
    t3_port: NotRequired[int]


class Connection(Browser):
    uid: int


class WorkerArguments(argparse.Namespace):
    action: str = ""
    username: str = ""
    uid: int | None = None
    legacy_browser: bool = False
    allow_privileged: bool = False
    memory_high: str | None = None
    tasks_max: str | None = None
    cpu_weight: int = 100


MIN_USER_UID: Final = 1000
MIN_BROWSER_PORT: Final = 1024
MAX_BROWSER_PORT: Final = 32767
MAX_DISPLAY: Final = 999
MAX_CPU_WEIGHT: Final = 10000
LISTEN_STATE: Final = "0A"
PROC_SOCKET_UID_COLUMN: Final = 7

CONFIG: Final = Path("/etc/long-running-agent")
USERS: Final = CONFIG / "users"
RUNTIME: Final = Path("/run/long-running-agent")
READY: Final = RUNTIME / "browser-guard.json"
EXECUTABLE: Final = "/usr/local/sbin/lra-user"
TABLE: Final = "lra_browser"
LEGACY_BROWSER: Final[Browser] = {"display": 92, "cdp_port": 9222, "vnc_port": 5902, "viewer_port": 7900}
PRIVILEGED_GROUPS: Final[frozenset[str]] = frozenset(
    {"root", "sudo", "admin", "wheel", "docker", "lxd", "incus-admin", "google-sudoers", "disk", "libvirt"}
)
SELF_REGISTRATION_POLICY: Final = "ALL ALL=(root) NOPASSWD: /usr/local/sbin/lra-user register-self\n"
GUARD_UNIT: Final = """[Unit]
Description=Per-user local browser network isolation
After=local-fs.target nftables.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/lra-user firewall-load
ExecReload=/usr/local/sbin/lra-user firewall-load
[Install]
WantedBy=multi-user.target
"""
USER_ORDERING: Final = """[Unit]
Requires=lra-browser-guard.service
After=lra-browser-guard.service
"""


def root_owned(path: Path, *, directory: bool = False) -> None:
    metadata = path.lstat()
    if path.is_symlink() or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        fail(f"Expected a root-owned, non-writable managed path: {path}")
    if directory != path.is_dir():
        fail(f"Unexpected managed path type: {path}")


def fail(message: str) -> Never:
    raise RuntimeError(message)


def emit(value: object) -> None:
    sys.stdout.write(json.dumps(value) + "\n")


def root_directory(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, mode=0o755)
        path.chmod(0o755)
    root_owned(path, directory=True)


def is_dictionary(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def object_fields(value: object) -> dict[str, object]:
    if not is_dictionary(value):
        fail("Expected a registry object")
    fields: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            fail("Expected text registry keys")
        fields[key] = item
    return fields


def integer(value: object) -> int:
    if type(value) is not int:
        message = "Expected an integer registry field"
        raise RuntimeError(message)
    return value


def text(value: object) -> str:
    if not isinstance(value, str):
        fail("Expected a text registry field")
    return value


def read_root_json(path: Path) -> dict[str, object]:
    root_owned(path.parent, directory=True)
    root_owned(path)
    return decode_object(path.read_text(encoding="utf-8"))


def decode_object(content: str, *, loader: Callable[[str], object] = json.loads) -> dict[str, object]:
    return object_fields(loader(content))


def atomic_root_write(path: Path, content: str, *, mode: int = 0o644) -> None:
    root_directory(path.parent)
    if path.exists() or path.is_symlink():
        root_owned(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".lra-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_browser(value: object) -> Browser:
    fields = object_fields(value)
    if set(fields) != {"display", "cdp_port", "vnc_port", "viewer_port"}:
        fail("Unexpected browser registration fields")
    browser = Browser(
        display=integer(fields["display"]),
        cdp_port=integer(fields["cdp_port"]),
        vnc_port=integer(fields["vnc_port"]),
        viewer_port=integer(fields["viewer_port"]),
    )
    ports = [browser["cdp_port"], browser["vnc_port"], browser["viewer_port"]]
    if len(set(ports)) != len(ports) or any(not MIN_BROWSER_PORT <= port <= MAX_BROWSER_PORT for port in ports):
        fail("Browser ports must be distinct non-ephemeral user ports")
    if not 1 <= browser["display"] <= MAX_DISPLAY:
        fail("Invalid X display registration")
    return browser


def validate_limits(value: object) -> ResourceLimits:
    fields = object_fields(value)
    if set(fields) != {"memory_high", "tasks_max", "cpu_weight"}:
        message = "Unexpected resource-control fields"
        raise RuntimeError(message)
    limits = ResourceLimits(
        memory_high=text(fields["memory_high"]),
        tasks_max=text(fields["tasks_max"]),
        cpu_weight=integer(fields["cpu_weight"]),
    )
    if not re.fullmatch(r"(?:[1-9][0-9]*[KMGT]?|infinity)", limits["memory_high"]):
        message = "Invalid advisory memory setting"
        raise RuntimeError(message)
    if not re.fullmatch(r"(?:[1-9][0-9]*|infinity)", limits["tasks_max"]):
        message = "Invalid task-count setting"
        raise RuntimeError(message)
    if not 1 <= limits["cpu_weight"] <= MAX_CPU_WEIGHT:
        message = "Invalid CPU weight"
        raise RuntimeError(message)
    return limits


def validate_registration(value: object) -> Registration:
    entry = object_fields(value)
    # The first reviewed registry format had only the legacy owner exception.
    if set(entry) == {"schema_version", "uid", "gid", "username", "home", "browser", "limits"}:
        entry = {**entry, "privileged_admin": entry["browser"] == LEGACY_BROWSER}
    required = {"schema_version", "uid", "gid", "username", "home", "browser", "limits", "privileged_admin"}
    if set(entry) not in (required, required | {"t3_port"}):
        fail("Unexpected user registration fields")
    uid, gid = integer(entry["uid"]), integer(entry["gid"])
    if integer(entry["schema_version"]) != 1 or uid < MIN_USER_UID or gid < 0:
        fail("Invalid registered user identity")
    username, home = text(entry["username"]), text(entry["home"])
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", username):
        fail("Invalid registered username")
    if Path(home).parent != Path("/home"):
        message = "Expected an OS Login home under /home"
        raise RuntimeError(message)
    privileged = entry["privileged_admin"]
    if type(privileged) is not bool:
        fail("Invalid administrator exception")
    registration = Registration(
        schema_version=1,
        uid=uid,
        gid=gid,
        username=username,
        home=home,
        browser=validate_browser(entry["browser"]),
        limits=validate_limits(entry["limits"]),
        privileged_admin=privileged,
    )
    # Keep retired listener protection until its root-owned registration is removed.
    if "t3_port" in entry:
        port = integer(entry["t3_port"])
        if not MIN_BROWSER_PORT <= port <= MAX_BROWSER_PORT or port in browser_ports(registration):
            fail("Invalid retired endpoint port")
        registration["t3_port"] = port
    return registration


def browser_ports(entry: Registration) -> list[int]:
    return [entry["browser"][key] for key in ("cdp_port", "vnc_port", "viewer_port")]


def protected_ports(entry: Registration) -> list[int]:
    ports = browser_ports(entry)
    retired = entry.get("t3_port")
    return ports if retired is None else [*ports, retired]


def registrations() -> list[Registration]:
    root_owned(CONFIG, directory=True)
    root_owned(USERS, directory=True)
    entries: list[Registration] = []
    for path in sorted(USERS.glob("*.json")):
        entry = validate_registration(read_root_json(path))
        if path.name != str(entry["uid"]) + ".json":
            fail("Registration filename differs from its UID")
        entries.append(entry)
    ports = [port for entry in entries for port in protected_ports(entry)]
    displays = [entry["browser"]["display"] for entry in entries]
    if len(ports) != len(set(ports)) or len(displays) != len(set(displays)):
        fail("Browser registrations collide")
    return entries


def firewall_rules(entries: Sequence[Registration]) -> str:
    lines = [
        f"add table inet {TABLE}",
        f"flush table inet {TABLE}",
        f"table inet {TABLE} {{",
        "  chain output {",
        "    type filter hook output priority -150; policy accept;",
    ]
    for entry in sorted(entries, key=itemgetter("uid")):
        uid = entry["uid"]
        ports = ", ".join(str(port) for port in protected_ports(entry))
        # Source checks also prevent another UID from impersonating an unused endpoint.
        lines.extend(
            f"    fib daddr type local tcp {field} {{ {ports} }} "
            f"meta skuid != {{ 0, {uid} }} reject with tcp reset "
            f'comment "lra-uid-{uid}-{field}"'
            for field in ("dport", "sport")
        )
    return "\n".join([*lines, "  }", "}", ""])


def listening_ports() -> dict[int, set[int]]:
    listeners: dict[int, set[int]] = {}
    for name in ("tcp", "tcp6"):
        path = Path("/proc/net") / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines()[1:]:
            fields = line.split()
            if len(fields) > PROC_SOCKET_UID_COLUMN and fields[3] == LISTEN_STATE:
                port = int(fields[1].rsplit(":", 1)[1], 16)
                listeners.setdefault(port, set()).add(int(fields[7]))
    return listeners


def allocate_browser(
    entries: Sequence[Registration], listeners: Mapping[int, set[int]], *, legacy: bool = False, uid: int | None = None
) -> Browser:
    used_ports = {port for entry in entries for port in protected_ports(entry)}
    used_displays = {entry["browser"]["display"] for entry in entries}
    candidates = (
        [LEGACY_BROWSER]
        if legacy
        else (
            Browser(
                display=display,
                cdp_port=20000 + (display - 100) * 3,
                vnc_port=20001 + (display - 100) * 3,
                viewer_port=20002 + (display - 100) * 3,
            )
            for display in range(100, 1000)
        )
    )
    for browser in candidates:
        ports = {browser[key] for key in ("cdp_port", "vnc_port", "viewer_port")}
        if not used_ports.isdisjoint(ports) or browser["display"] in used_displays:
            continue
        if not browser_endpoints_available(browser, listeners, legacy=legacy, uid=uid):
            continue
        return browser.copy()
    fail("No collision-free browser registration is available")


def browser_endpoints_available(
    browser: Browser, listeners: Mapping[int, set[int]], *, legacy: bool, uid: int | None
) -> bool:
    ports = {browser[key] for key in ("cdp_port", "vnc_port", "viewer_port")}
    if legacy:
        if any(listeners.get(port, set()) - {0, uid} for port in ports):
            fail("An existing legacy browser port belongs to another user")
        return True
    return (
        ports.isdisjoint(listeners)
        and not Path(f"/tmp/.X11-unix/X{browser['display']}").exists()  # ruff: ignore[hardcoded-temp-file] — read-only X server collision check at its required global path.
        and not Path(f"/tmp/.X{browser['display']}-lock").exists()  # ruff: ignore[hardcoded-temp-file] — read-only X server collision check at its required global path.
    )


def run(*arguments: str, input_text: str | None = None, capture: bool = False) -> subprocess.CompletedProcess[str]:
    if not arguments or arguments[0] not in {
        "/usr/sbin/nft",
        "/usr/bin/systemctl",
        "/usr/bin/loginctl",
        "/usr/sbin/visudo",
    }:
        message = "Unsupported privileged executable"
        raise RuntimeError(message)
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] — privileged executable allowlist; arguments never use a shell.
        arguments, input=input_text, text=True, check=True, capture_output=capture
    )


def load_firewall(entries: Sequence[Registration]) -> None:
    rules = firewall_rules(entries)
    run("/usr/sbin/nft", "--check", "--file", "-", input_text=rules)
    # One nft transaction replaces only our dedicated table, never the host ruleset.
    run("/usr/sbin/nft", "--file", "-", input_text=rules)
    marker = {
        "schema_version": 1,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip(),
        "rules_sha256": hashlib.sha256(rules.encode()).hexdigest(),
        "loaded_at": datetime.now(UTC).isoformat(),
        "users": {str(entry["uid"]): entry["browser"] for entry in entries},
    }
    atomic_root_write(READY, json.dumps(marker, indent=2) + "\n")


def reload_firewall() -> None:
    root_directory(RUNTIME)
    with (RUNTIME / "admin.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Read the registry inside the same lock used by registration. A delayed
        # boot load must never replace a newly registered user's guard rules.
        load_firewall(registrations())


def validate_connection(
    entry: object, marker: Mapping[str, object], account: pwd.struct_passwd, boot_id: str
) -> Connection:
    registration = validate_registration(entry)
    uid = account.pw_uid
    if (
        registration["uid"] != uid
        or registration["username"] != account.pw_name
        or registration["home"] != account.pw_dir
    ):
        fail("Browser registration does not match this OS Login account")
    loaded_users = object_fields(marker.get("users"))
    if (
        marker.get("schema_version") != 1
        or marker.get("boot_id") != boot_id
        or loaded_users.get(str(uid)) != registration["browser"]
    ):
        fail("The browser UID firewall is not ready for this user")
    return Connection(uid=uid, **registration["browser"])


def connection_info() -> Connection:
    uid = os.getuid()
    shared = read_root_json(CONFIG / "shared.json")
    if shared != {"schema_version": 1, "browser_isolation": "uid-nftables"}:
        fail("Shared browser isolation has not been configured")
    entry = read_root_json(USERS / f"{uid}.json")
    marker = read_root_json(READY)
    return validate_connection(
        entry, marker, pwd.getpwuid(uid), Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    )


def private_home(account: pwd.struct_passwd) -> None:
    home = Path(account.pw_dir)
    if home.parent != Path("/home"):
        fail("Expected the existing user-owned OS Login home under /home")
    root_owned(Path("/home"), directory=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(home, flags)
    try:
        if os.fstat(descriptor).st_uid != account.pw_uid:
            fail("The OS Login home belongs to a different UID")
        os.fchmod(descriptor, 0o700)
        for name in (".ssh", ".codex", ".config", ".local"):
            created = False
            try:
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
                created = True
            except FileExistsError:
                pass
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                owner = os.fstat(child).st_uid
                if owner != account.pw_uid and not (created and owner == 0):
                    fail(f"Unsafe private user directory: {name}")
                if created:
                    os.fchown(child, account.pw_uid, account.pw_gid)
                os.fchmod(child, 0o700)
            finally:
                os.close(child)
    finally:
        os.close(descriptor)


def account_for_registration(args: WorkerArguments) -> pwd.struct_passwd:
    account = pwd.getpwnam(args.username)
    if account.pw_uid < MIN_USER_UID or (args.uid is not None and account.pw_uid != args.uid):
        fail("Resolved OS Login UID differs from the requested ordinary account")
    groups = privileged_groups(account)
    privileged_admin = args.legacy_browser or args.allow_privileged
    if not privileged_admin and groups:
        fail(
            "This account already has privileged local groups ("
            + ", ".join(sorted(groups))
            + "); an administrator must explicitly review and use --allow-privileged"
        )
    return account


def registration_browser(
    args: WorkerArguments, account: pwd.struct_passwd, existing: Registration | None, entries: Sequence[Registration]
) -> Browser:
    if not existing:
        return allocate_browser(entries, listening_ports(), legacy=args.legacy_browser, uid=account.pw_uid)
    if existing["username"] != account.pw_name or existing["home"] != account.pw_dir:
        fail("Existing registration belongs to a different account")
    if args.legacy_browser and existing["browser"] != LEGACY_BROWSER:
        fail("Refusing to change an existing browser allocation")
    return existing["browser"]


def registration_limits(args: WorkerArguments, existing: Registration | None) -> ResourceLimits:
    limits = ResourceLimits(
        memory_high=args.memory_high or ("infinity" if args.legacy_browser else "3G"),
        tasks_max=args.tasks_max or ("infinity" if args.legacy_browser else "2048"),
        cpu_weight=args.cpu_weight,
    )
    if existing and args.memory_high is None and args.tasks_max is None:
        limits = existing["limits"]
    return validate_limits(limits)


def register(args: WorkerArguments) -> None:
    account = account_for_registration(args)
    root_directory(CONFIG)
    root_directory(USERS)
    root_directory(RUNTIME)
    with (RUNTIME / "admin.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        entries = registrations()
        existing = next((entry for entry in entries if entry["uid"] == account.pw_uid), None)
        browser = registration_browser(args, account, existing, entries)
        limits = registration_limits(args, existing)
        entry = Registration(
            schema_version=1,
            uid=account.pw_uid,
            gid=account.pw_gid,
            username=account.pw_name,
            home=account.pw_dir,
            browser=browser,
            limits=limits,
            privileged_admin=args.legacy_browser or args.allow_privileged,
        )
        if existing is not None and "t3_port" in existing:
            entry["t3_port"] = existing["t3_port"]
        entries = [value for value in entries if value["uid"] != account.pw_uid] + [entry]
        run("/usr/sbin/nft", "--check", "--file", "-", input_text=firewall_rules(entries))
        private_home(account)
        atomic_root_write(USERS / f"{account.pw_uid}.json", json.dumps(entry, indent=2) + "\n")
        atomic_root_write(
            CONFIG / "shared.json", json.dumps({"schema_version": 1, "browser_isolation": "uid-nftables"}) + "\n"
        )
        atomic_root_write(Path("/etc/systemd/system/lra-browser-guard.service"), GUARD_UNIT)
        atomic_root_write(Path("/etc/systemd/system/user@.service.d/long-running-agent.conf"), USER_ORDERING)
        atomic_root_write(
            Path(f"/etc/systemd/system/user-{account.pw_uid}.slice.d/long-running-agent.conf"), slice_policy(limits)
        )
        load_firewall(entries)
    # Starting linger can start user@ and its guard dependency. Release the
    # registry lock first so that service can read/load the completed registry.
    run("/usr/bin/systemctl", "daemon-reload")
    run("/usr/bin/systemctl", "enable", "lra-browser-guard.service")
    run("/usr/bin/loginctl", "enable-linger", account.pw_name)
    emit(
        {
            "registered": account.pw_name,
            "uid": account.pw_uid,
            "browser": browser,
            "resource_controls": limits,
            "restarted_user_services": False,
            "privileges_granted": [],
        }
    )


def privileged_groups(account: pwd.struct_passwd) -> set[str]:
    groups = {grp.getgrgid(gid).gr_name for gid in os.getgrouplist(account.pw_name, account.pw_gid)}
    return groups & PRIVILEGED_GROUPS


def slice_policy(limits: ResourceLimits) -> str:
    return f"[Slice]\nMemoryHigh={limits['memory_high']}\nTasksMax={limits['tasks_max']}\nCPUWeight={limits['cpu_weight']}\n"


def validate_sudo_identity(environment: Mapping[str, str], account: pwd.struct_passwd) -> str:
    value = environment.get("SUDO_UID", "")
    if not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) < MIN_USER_UID:
        fail("register-self requires an ordinary sudo caller UID")
    if int(value) != account.pw_uid or environment.get("SUDO_USER") != account.pw_name:
        fail("Sudo caller identity does not match the resolved account")
    return account.pw_name


def register_self() -> None:
    environment = os.environ  # ruff: ignore[banned-api] — isolated stdlib root worker must verify sudo's process identity.
    value = environment.get("SUDO_UID", "")
    if not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) < MIN_USER_UID:
        fail("register-self must be invoked through sudo by an ordinary user")
    account = pwd.getpwuid(int(value))
    username = validate_sudo_identity(environment, account)
    register(
        WorkerArguments(
            username=username,
            uid=account.pw_uid,
            legacy_browser=False,
            allow_privileged=False,
            memory_high=None,
            tasks_max=None,
            cpu_weight=100,
        )
    )


def private_login_defaults(original: str) -> str:
    updated = original
    for key, value in (("UMASK", "077"), ("HOME_MODE", "0700")):
        expression = r"(?m)^\s*" + key + r"\s+[^\n]*$"
        replacement = key + " " + value
        updated = (
            re.sub(expression, replacement, updated)
            if re.search(expression, updated)
            else updated.rstrip() + "\n" + replacement + "\n"
        )
    return updated


def install_self_registration() -> None:
    root_owned(Path(EXECUTABLE).parent, directory=True)
    root_owned(Path(EXECUTABLE))
    root_directory(RUNTIME)
    with tempfile.TemporaryDirectory(prefix="sudoers-", dir=RUNTIME) as directory:
        candidate = Path(directory) / "long-running-agent-self-registration"
        candidate.write_text(SELF_REGISTRATION_POLICY, encoding="utf-8")
        candidate.chmod(0o440)
        run("/usr/sbin/visudo", "--check", "--file", str(candidate))
        atomic_root_write(
            Path("/etc/sudoers.d/long-running-agent-self-registration"), SELF_REGISTRATION_POLICY, mode=0o440
        )
    login = Path("/etc/login.defs")
    root_owned(login)
    original = login.read_text(encoding="utf-8")
    updated = private_login_defaults(original)
    if updated != original:
        backup = Path("/etc/login.defs.long-running-agent-before")
        if not backup.exists():
            atomic_root_write(backup, original)
        atomic_root_write(login, updated)
    atomic_root_write(
        Path("/etc/profile.d/long-running-agent-umask.sh"), "# Private defaults for new login shells.\numask 077\n"
    )
    emit(
        {
            "self_registration": "exact register-self command only",
            "sudoers_validated": True,
            "new_home_mode": "0700",
            "new_login_umask": "077",
            "existing_sessions_restarted": False,
        }
    )


def check_private_home(entry: Registration) -> None:
    for relative in ("", ".ssh", ".codex", ".config", ".local"):
        path = Path(entry["home"]) / relative
        metadata = path.lstat()
        if path.is_symlink() or not path.is_dir() or metadata.st_uid != entry["uid"] or metadata.st_mode & 0o077:
            fail("A registered user home or credential directory is not private")


def check_registered_user(entry: Registration, rules: str) -> None:
    account = pwd.getpwuid(entry["uid"])
    if account.pw_name != entry["username"] or account.pw_dir != entry["home"]:
        fail("A registration no longer matches its OS Login identity")
    if not entry["privileged_admin"] and privileged_groups(account):
        fail("An ordinary registered user now has privileged local groups: " + account.pw_name)
    check_private_home(entry)
    policy = Path(f"/etc/systemd/system/user-{entry['uid']}.slice.d/long-running-agent.conf")
    root_owned(policy)
    if policy.read_text(encoding="utf-8") != slice_policy(entry["limits"]):
        fail("A user resource-control policy differs from its registration")
    if (
        run(
            "/usr/bin/loginctl", "show-user", str(entry["uid"]), "--property=Linger", "--value", capture=True
        ).stdout.strip()
        != "yes"
    ):
        fail("User lingering is disabled for " + account.pw_name)
    for field in ("dport", "sport"):
        if f"lra-uid-{entry['uid']}-{field}" not in rules:
            fail("A registered user is missing a browser UID firewall rule")


def check() -> None:
    entries = registrations()
    rules = run("/usr/sbin/nft", "list", "table", "inet", TABLE, capture=True).stdout
    for entry in entries:
        check_registered_user(entry, rules)
    marker = read_root_json(READY)
    expected = hashlib.sha256(firewall_rules(entries).encode()).hexdigest()
    if (
        marker.get("rules_sha256") != expected
        or marker.get("boot_id") != Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    ):
        fail("The loaded browser guard receipt is stale")
    emit(
        {
            "registered_users": len(entries),
            "browser_guard_loaded": True,
            "shared_profiles": False,
            "privileges_granted": [],
            "explicit_administrator_exceptions": [entry["username"] for entry in entries if entry["privileged_admin"]],
        }
    )


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Manage ordinary OS Login registrations and UID browser isolation.")
    commands = parser.add_subparsers(dest="action", required=True)
    registration = commands.add_parser("register", help="Register an already resolved OS Login account")
    registration.add_argument("username")
    registration.add_argument("--uid", type=int)
    registration.add_argument(
        "--legacy-browser", action="store_true", help="Preserve the existing privileged owner browser ports and limits"
    )
    registration.add_argument(
        "--allow-privileged",
        action="store_true",
        help="Explicitly acknowledge an existing trusted administrator; grants no new privilege",
    )
    registration.add_argument("--memory-high")
    registration.add_argument("--tasks-max")
    registration.add_argument("--cpu-weight", type=int, default=100)
    commands.add_parser("firewall-load", help="Load only the dedicated UID guard table")
    commands.add_parser("check", help="Check root registration and active guard receipts")
    commands.add_parser("connection", help="Read only the current UID browser allocation")
    commands.add_parser(
        "register-self", help="Register only the ordinary account identified by sudo; accepts no arguments"
    )
    commands.add_parser(
        "install-self-registration", help="Install the validated exact-command sudoers rule and private login defaults"
    )
    args = parser.parse_args(None if arguments is None else list(arguments), namespace=WorkerArguments())
    os.umask(0o077)
    if args.action == "connection":
        emit(connection_info())
        return
    if os.geteuid() != 0:
        fail("User registration and firewall administration require root")
    match args.action:
        case "register":
            register(args)
        case "register-self":
            register_self()
        case "install-self-registration":
            install_self_registration()
        case "check":
            check()
        case _:
            reload_firewall()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(f"lra-user: {error}. Ask the VM administrator to check user registration.")
