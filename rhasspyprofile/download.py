"""Methods for downloading profile artifacts"""
import asyncio
import dataclasses
import logging
import os
import platform
import re
import ssl
import subprocess
import tempfile
import typing
from dataclasses import dataclass
from pathlib import Path

import aiofiles
import aiohttp

from .profile import Profile

_LOGGER = logging.getLogger(__name__)


# -----------------------------------------------------------------------------

# url, path, file_key, done, bytes_downloaded, bytes_expected
DownloadStatusType = typing.Callable[
    [str, Path, str, bool, int, typing.Optional[int]], None
]


async def download_file(
    url: str,
    path: Path,
    file_key: str = "",
    bytes_expected: typing.Optional[int] = None,
    session: typing.Optional[aiohttp.ClientSession] = None,
    ssl_context: typing.Optional[ssl.SSLContext] = None,
    chunk_size: int = 4096,
    status_fun: typing.Optional[DownloadStatusType] = None,
) -> typing.Tuple[str, int, typing.Optional[int]]:
    """
    Downloads a single file to a destination path.
    Optionally reports status by calling status_fun.

    Returns url, bytes downloaded, and bytes expected.
    """
    close_session = session is None
    session = session or aiohttp.ClientSession()
    assert session

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _LOGGER.debug("Downloading %s to %s", url, str(path))

        bytes_downloaded: int = 0

        if url.startswith("file://"):
            # Use file system
            file_path = Path(url[7:])
            async with aiofiles.open(file_path, "r") as local_file:
                with open(path, "wb") as out_file:
                    while True:
                        chunk = await local_file.read(chunk_size)
                        if not chunk:
                            break

                        out_file.write(chunk)
                        bytes_downloaded += len(chunk)

                        # Report status
                        if status_fun:
                            status_fun(
                                url,
                                path,
                                file_key,
                                False,
                                bytes_downloaded,
                                bytes_expected,
                            )
        else:
            # Actually download
            async with session.get(url, ssl=ssl_context) as response:
                with open(path, "wb") as out_file:
                    async for chunk in response.content.iter_chunked(chunk_size):
                        out_file.write(chunk)
                        bytes_downloaded += len(chunk)

                        # Report status
                        if status_fun:
                            status_fun(
                                url,
                                path,
                                file_key,
                                False,
                                bytes_downloaded,
                                bytes_expected,
                            )

        # Final status
        if status_fun:
            status_fun(url, path, file_key, True, bytes_downloaded, bytes_expected)
    finally:
        if close_session:
            await session.close()

    return url, bytes_downloaded, bytes_expected


# -----------------------------------------------------------------------------

# https://stackoverflow.com/questions/1094841/reusable-library-to-get-human-readable-version-of-file-size
def human_size(num: float, suffix="B") -> str:
    """Gets human read file size."""
    for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
        if abs(num) < 1024.0:
            return "%3.1f %s%s" % (num, unit, suffix)
        num /= 1024.0

    return "%.1f %s%s" % (num, "Yi", suffix)


@dataclass
class MissingFile:
    """Details of a missing file in a profile."""

    file_key: str
    file_path: Path
    setting_name: str
    setting_value: str
    bytes_expected: typing.Optional[int] = None

    def to_dict(self) -> typing.Dict[str, typing.Any]:
        """Convert to JSON serializable dictionary."""
        result = dataclasses.asdict(self)
        result["file_path"] = str(result["file_path"])

        result["human_size"] = ""
        if self.bytes_expected is not None:
            result["human_size"] = human_size(self.bytes_expected)

        return result


@dataclass
class MissingFilePart:
    """Details of a missing file split into parts."""

    index: int
    url: str
    path: Path
    file_key: str
    bytes_expected: typing.Optional[int] = None


@dataclass
class DownloadFailedException(Exception):
    """Exception raised when a file fails to download."""

    url: str
    reason: str


def get_missing_files(profile: Profile) -> typing.List[MissingFile]:
    """Return details of files that need to be downloaded."""

    # Cache machine type for later
    machine_type = platform.machine()

    # Load settings
    conditions: typing.Dict[str, typing.Any] = profile.get("download.conditions", {})
    files: typing.Dict[str, typing.Any] = profile.get("download.files", {})

    # Keys in download.files that should be downloaded
    missing_files: typing.List[MissingFile] = []

    # condition_key is a profile setting (e.g., speech_to_text.system).
    # condition_value has setting value(s) and files required for those values.
    for condition_key, condition_details in conditions.items():
        # Get the actual profile setting for comparison
        actual_value = profile.get(condition_key)
        for expected_value, condition_files in condition_details.items():
            # Allow comparisons like ">=0", etc.
            compare_func = get_compare_func(expected_value)

            try:
                compare_result = compare_func(actual_value)
                _LOGGER.debug(
                    "%s %s %s = %s",
                    condition_key,
                    expected_value,
                    actual_value,
                    compare_result,
                )
            except ValueError:
                _LOGGER.exception(
                    "%s %s %s = error", condition_key, expected_value, actual_value
                )
                compare_result = False

            if compare_result:
                # file_key is the destination path (relative to profile directory).
                # file_value is the source name (in download.files).
                for file_key, file_value in condition_files.items():
                    file_details = typing.cast(
                        typing.Dict[str, typing.Any], files.get(file_value)
                    )
                    assert file_details, f"Missing download details for {file_value}"
                    replace_filename = file_details.get("replace_filename", False)

                    if "platform" in file_details:
                        # Use platform-specific file
                        new_key = ""
                        for platform_regex, platform_file_key in file_details[
                            "platform"
                        ].items():
                            if not platform_regex:
                                # Empty regex matches any platform
                                new_key = platform_file_key
                            elif re.match(platform_regex, machine_type):
                                # Specific platform match
                                new_key = platform_file_key
                                break

                        assert new_key, "No new platform-specific file key"
                        _LOGGER.debug(
                            "Using platform-specific file for %s: %s",
                            machine_type,
                            new_key,
                        )

                        # Use new key
                        file_value = new_key
                        file_details = typing.cast(
                            typing.Dict[str, typing.Any], files.get(file_value)
                        )
                        assert (
                            file_details
                        ), f"Missing download details for {file_value}"

                    # ---------------------------------------------------------

                    file_key_path = file_key

                    if replace_filename:
                        # Use final component of target key as file name.
                        # Used for multi-platform paths.
                        file_key_path = "/".join(
                            file_key.split("/")[:-1] + [file_value.split("/")[-1]]
                        )

                    file_path = profile.read_path(file_key_path)

                    need_download = True
                    bytes_expected = file_details.get("bytes_expected")

                    # Check if a download is actually required
                    if file_path.exists():
                        if file_path.is_file():
                            # Check if file is empty or matches expected size
                            file_size = os.path.getsize(file_path)
                            if bytes_expected is not None:
                                if file_size == bytes_expected:
                                    # Matches expected size
                                    need_download = False
                            elif file_size > 0:
                                # Assume non-empty file is OK if we have no expectations
                                need_download = False
                        elif file_path.is_dir():
                            # Check if directory is empty
                            if list(file_path.glob("*")):
                                need_download = False

                    if need_download:
                        # Add to required file set
                        missing_files.append(
                            MissingFile(
                                file_key=file_value,
                                file_path=profile.write_path(file_key_path),
                                setting_name=condition_key,
                                setting_value=expected_value,
                                bytes_expected=bytes_expected,
                            )
                        )
                    else:
                        _LOGGER.debug("Skipping %s (%s)", file_key, str(file_path))

    return missing_files


async def download_files(
    profile: Profile,
    missing_files: typing.Optional[typing.List[MissingFile]] = None,
    cache_dir: typing.Optional[Path] = None,
    session: typing.Optional[aiohttp.ClientSession] = None,
    ssl_context: typing.Optional[ssl.SSLContext] = None,
    chunk_size: int = 4096,
    status_fun: typing.Optional[DownloadStatusType] = None,
):
    """Download all required profile artifacts."""
    if not missing_files:
        missing_files = get_missing_files(profile)

    # Directory for caching download files
    temp_cache_dir: typing.Optional[tempfile.TemporaryDirectory] = None
    if not cache_dir:
        # Use temporary directory
        temp_cache_dir = tempfile.TemporaryDirectory()
        cache_dir = Path(temp_cache_dir.name)

    # Create or re-use session
    close_session = session is None
    session = session or aiohttp.ClientSession()
    assert session

    try:
        # Load settings
        files: typing.Dict[str, typing.Any] = profile.get("download.files", {})

        if missing_files:
            # URL base if prefixed to every file URL
            url_base: str = profile.get("download.url_base", "")
            if url_base and (not url_base.endswith("/")):
                url_base = url_base + "/"

            # Actually download files
            for missing_file in missing_files:
                file_key, download_path = missing_file.file_key, missing_file.file_path
                file_details = typing.cast(
                    typing.Dict[str, typing.Any], files.get(file_key)
                )
                assert file_details, f"Missing download details for {file_key}"
                assert (
                    "platform" not in file_details
                ), "Platform should be already be resolved"

                # Number of bytes the downloaded file should be (pre-unzip)
                zip_bytes_expected: typing.Optional[int] = file_details.get(
                    "zip_bytes_expected"
                )

                # Number of bytes the final file should be (post-unzip)
                bytes_expected: typing.Optional[int] = file_details.get(
                    "bytes_expected"
                )

                # Join with url base
                file_url: str = url_base + file_details["url"]

                # When True, downloaded file is automatically unzipped in place
                unzip = file_details.get("unzip", False)
                if unzip:
                    # Add .gz extension
                    download_path = download_path.with_suffix(
                        download_path.suffix + ".gz"
                    )
                else:
                    # No unzipping
                    zip_bytes_expected = bytes_expected

                # Check if file is split into multiple parts that must be concatenated
                file_parts: typing.List[
                    typing.Union[str, typing.Dict[str, typing.Any]]
                ] = file_details.get("parts", [])
                if file_parts:
                    # Multiple files
                    if not file_url.endswith("/"):
                        file_url += "/"

                    missing_parts: typing.List[MissingFilePart] = []
                    for part_index, part_details in enumerate(file_parts):
                        if isinstance(part_details, str):
                            part_fragment = part_details
                            part_bytes_expected = None
                        else:
                            part_fragment = part_details["fragment"]
                            part_bytes_expected = part_details.get("bytes_expected")

                        # Download to temporary file
                        part_url = file_url + part_fragment
                        part_path = Path(
                            tempfile.NamedTemporaryFile(
                                dir=cache_dir, delete=False
                            ).name
                        )
                        missing_parts.append(
                            MissingFilePart(
                                index=part_index,
                                url=part_url,
                                path=part_path,
                                file_key=f"{file_key}[{part_index}]",
                                bytes_expected=part_bytes_expected,
                            )
                        )

                    # Download all parts
                    awaitables = []
                    for part in missing_parts:
                        # Add to download queue
                        awaitables.append(
                            download_file(
                                part.url,
                                part.path,
                                file_key=part.file_key,
                                chunk_size=chunk_size,
                                bytes_expected=part.bytes_expected,
                                session=session,
                                ssl_context=ssl_context,
                                status_fun=None,
                            )
                        )

                    # Wait for parts to download
                    total_bytes_downloaded = 0
                    parts_left = len(awaitables)
                    for future in asyncio.as_completed(awaitables):
                        (
                            part_url,
                            part_bytes_downloaded,
                            part_bytes_expected,
                        ) = await future

                        parts_left -= 1
                        total_bytes_downloaded += part_bytes_downloaded
                        if status_fun:
                            # Report status when a part has finished
                            done = parts_left == 0
                            status_fun(
                                file_url,
                                download_path,
                                file_key,
                                done,
                                total_bytes_downloaded,
                                zip_bytes_expected,
                            )

                        if part_bytes_expected is not None:
                            if part_bytes_downloaded != part_bytes_expected:
                                _LOGGER.error(
                                    "Part download failure (%s, got %s byte(s), expected %s)",
                                    part_url,
                                    part_bytes_downloaded,
                                    part_bytes_expected,
                                )
                                raise DownloadFailedException(
                                    part_url,
                                    f"Part size mismatch (got {part_bytes_downloaded} byte(s), expected {part_bytes_expected})",
                                )

                    # Concatenate parts
                    final_size: int = 0
                    download_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(download_path, "wb") as final_file:
                        for part in missing_parts:
                            part_bytes: bytes = part.path.read_bytes()
                            final_file.write(part_bytes)
                            final_size += len(part_bytes)

                    file_url += download_path.name
                else:
                    # Single file
                    _, final_size, _ = await download_file(
                        file_url,
                        download_path,
                        file_key=file_key,
                        chunk_size=chunk_size,
                        bytes_expected=zip_bytes_expected,
                        session=session,
                        ssl_context=ssl_context,
                        status_fun=status_fun,
                    )

                # When True, downloaded file is automatically unzipped in place
                if unzip:
                    unzip_command = ["gunzip", "--force", str(download_path)]
                    _LOGGER.debug(unzip_command)
                    subprocess.check_call(unzip_command, cwd=download_path.parent)

                    # Fix path and size
                    download_path = download_path.with_suffix("")
                    final_size = os.path.getsize(download_path)

                # Verify size
                if bytes_expected is not None:
                    if final_size != bytes_expected:
                        download_path.unlink()
                        _LOGGER.error(
                            "Download failure (%s, got %s byte(s), expected %s)",
                            file_url,
                            final_size,
                            bytes_expected,
                        )
                        raise DownloadFailedException(
                            file_url,
                            f"File size mismatch (got {final_size} byte(s), expected {bytes_expected})",
                        )

                _LOGGER.debug(
                    "Successfully downloaded %s (%s)", file_key, str(download_path)
                )

    finally:
        # Clean up temporary directory
        if temp_cache_dir:
            temp_cache_dir.cleanup()

        if close_session:
            await session.close()


# -----------------------------------------------------------------------------


def get_compare_func(value: str):
    """Use mini-language to allow for profile setting value comparison."""
    if value.startswith(">="):
        f_value = float(value[2:])
        return lambda v: float(v) >= f_value

    if value.startswith("<="):
        f_value = float(value[2:])
        return lambda v: float(v) <= f_value

    if value.startswith(">"):
        f_value = float(value[1:])
        return lambda v: float(v) > f_value

    if value.startswith("<"):
        f_value = float(value[1:])
        return lambda v: float(v) < f_value

    if value.startswith("!"):
        return lambda v: v != value

    return lambda v: str(v) == value
