"""Safe asynchronous MediaInfo CLI execution with raw-byte output."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class MediaInfoError(RuntimeError):
    """Raised when MediaInfo cannot inspect a media file."""


class CommandResult(Protocol):
    @property
    def returncode(self) -> int: ...

    @property
    def stdout(self) -> bytes: ...

    @property
    def stderr(self) -> bytes: ...


class CommandRunner(Protocol):
    async def run(self, *command: str) -> CommandResult: ...


@dataclass(frozen=True, slots=True)
class SubprocessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class AsyncSubprocessRunner:
    """Execute an argv vector directly, without a shell."""

    async def run(self, *command: str) -> SubprocessResult:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        return SubprocessResult(
            returncode=process.returncode or 0,
            stdout=stdout,
            stderr=stderr,
        )


class MediaInfoRunner:
    """Return MediaInfo stdout exactly as emitted by the subprocess."""

    def __init__(
        self,
        *,
        executable: str = "mediainfo",
        command_runner: CommandRunner | None = None,
        timeout: float = 120.0,
    ) -> None:
        if not executable or "\x00" in executable:
            raise ValueError("mediainfo executable cannot be blank")
        if timeout <= 0:
            raise ValueError("mediainfo timeout must be positive")
        self._executable = executable
        self._command_runner: CommandRunner = command_runner or AsyncSubprocessRunner()
        self._timeout = timeout

    async def inspect(self, media_path: Path) -> bytes:
        path = Path(media_path)
        if not await asyncio.to_thread(path.is_file):
            raise MediaInfoError("media file unavailable")
        try:
            result = await asyncio.wait_for(
                self._command_runner.run(
                    self._executable,
                    "--Output=JSON",
                    str(path),
                ),
                timeout=self._timeout,
            )
        except asyncio.CancelledError:
            raise
        except FileNotFoundError as error:
            raise MediaInfoError("mediainfo executable unavailable") from error
        except TimeoutError as error:
            raise MediaInfoError("mediainfo timed out") from error
        if result.returncode != 0:
            raise MediaInfoError(
                f"mediainfo exited unsuccessfully ({result.returncode})"
            )
        if not isinstance(result.stdout, bytes):
            raise MediaInfoError("mediainfo returned non-byte output")
        return result.stdout
