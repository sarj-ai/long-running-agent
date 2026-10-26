from __future__ import annotations

import socket
import time
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


MAX_MESSAGE = 16384
PROTOCOL_VERSION = 1
MAX_URL = 8192


class HostRegistration(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)
    version: Literal[1] = PROTOCOL_VERSION
    connection: str = Field(pattern=r"^[a-f0-9]{32}$")
    socket: str = Field(min_length=1, max_length=4096)
    label: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")


class HostRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)
    version: Literal[1] = PROTOCOL_VERSION
    action: Literal["status", "browser", "image", "download"]
    path: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def selected_path(self) -> Self:
        if (self.action == "download") != (self.path is not None):
            message = "Only a download request accepts a selected VM path"
            raise ValueError(message)
        if self.path is not None and any(character in self.path for character in "\0\r\n"):
            message = "The selected VM path contains a control character"
            raise ValueError(message)
        return self


class HostReply(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)
    ok: bool
    message: str = Field(max_length=4096)
    paths: list[str] = Field(default_factory=list, max_length=16)


def read_message(channel: socket.socket) -> bytes:
    data = bytearray()
    timeout = channel.gettimeout()
    deadline = time.monotonic() + (3 if timeout is None else timeout)
    while len(data) <= MAX_MESSAGE:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            message = "The host connection message exceeded its deadline"
            raise TimeoutError(message)
        channel.settimeout(remaining)
        chunk = channel.recv(min(4096, MAX_MESSAGE + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if b"\n" in chunk:
            line, _, trailing = data.partition(b"\n")
            if trailing or len(line) > MAX_MESSAGE:
                break
            return bytes(line)
    message = "The host connection returned an incomplete or oversized message"
    raise ValueError(message)


def exchange(registration: HostRegistration, request: HostRequest, *, timeout: float = 360) -> HostReply:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(timeout)
        channel.connect(registration.socket)
        channel.sendall(request.model_dump_json().encode() + b"\n")
        return HostReply.model_validate_json(read_message(channel))


def website_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
        valid = valid and (parsed.port is None or parsed.port > 0)
    except ValueError:
        valid = False
    if (
        not valid
        or len(value) > MAX_URL
        or any(not character.isprintable() or character.isspace() for character in value)
    ):
        message = "Use a complete http:// or https:// website address without embedded credentials"
        raise ValueError(message)
    return value
