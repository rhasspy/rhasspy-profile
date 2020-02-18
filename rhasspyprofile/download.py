"""Methods for downloading profile artifacts"""
import asyncio
import logging
import os
import subprocess
import tempfile
import typing
from pathlib import Path

import aiohttp
import attr

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

        async with session.get(url) as response:
            with open(path, "wb") as out_file:
                async for chunk in response.content.iter_chunked(chunk_size):
                    out_file.write(chunk)
                    bytes_downloaded += len(chunk)

                    # Report status
                    if status_fun:
                        status_fun(
                            url, path, file_key, False, bytes_downloaded, bytes_expected
                        )

        # Final status
        if status_fun:
            status_fun(url, path, file_key, True, bytes_downloaded, bytes_expected)
    finally:
        if close_session:
            await session.close()

    return url, bytes_downloaded, bytes_expected


# -----------------------------------------------------------------------------


@attr.s(auto_attribs=True)
class MissingFile:
    """Details of a missing file in a profile."""

    file_key: str
    file_path: Path
    setting_name: str
    setting_value: str


@attr.s(auto_attribs=True)
class DownloadFailedException(Exception):
    """Exception raised when a file fails to download."""

    url: str
    reason: str


def get_missing_files(profile: Profile) -> typing.List[MissingFile]:
    """Return details of files that need to be downloaded."""
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
            compare_result = compare_func(actual_value)
            _LOGGER.debug(
                "%s %s %s = %s",
                condition_key,
                expected_value,
                actual_value,
                compare_result,
            )

            if compare_result:
                # file_key is the destination path (relative to profile directory).
                # file_value is the source name (in download.files).
                for file_key, file_value in condition_files.items():
                    file_path = profile.read_path(file_key)
                    need_download = True

                    # Check if a download is actually required
                    if file_path.exists():
                        if file_path.is_file():
                            # Check if file is empty or matches expected size
                            file_size = os.path.getsize(file_path)
                            if file_size > 0:
                                bytes_expected = files.get(file_value, {}).get(
                                    "bytes_expected"
                                )
                                if (bytes_expected is None) or (
                                    file_size == bytes_expected
                                ):
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
                                file_path=profile.write_path(file_key),
                                setting_name=condition_key,
                                setting_value=expected_value,
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
                file_details: typing.Dict[str, typing.Any] = files.get(
                    file_key
                )  # type: ignore
                assert file_details, f"Missing download details for {file_key}"

                # Number of bytes the final file should be (pre-unzip)
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

                # Check if file is split into multiple parts that must be concatenated
                file_parts: typing.List[
                    typing.Union[str, typing.Dict[str, typing.Any]]
                ] = file_details.get("parts", [])
                if file_parts:
                    # Multiple files
                    if not file_url.endswith("/"):
                        file_url += "/"

                    # Download all parts
                    awaitables = []
                    part_paths: typing.List[Path] = []
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
                        part_paths.append(part_path)

                        # Add to download queue
                        awaitables.append(
                            download_file(
                                part_url,
                                part_path,
                                file_key=f"{file_key}[{part_index}]",
                                chunk_size=chunk_size,
                                bytes_expected=part_bytes_expected,
                                session=session,
                                status_fun=status_fun,
                            )
                        )

                    # Wait for parts to download
                    for future in asyncio.as_completed(awaitables):
                        (
                            part_url,
                            part_bytes_downloaded,
                            part_bytes_expected,
                        ) = await future
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
                        for part_path in part_paths:
                            part_bytes: bytes = part_path.read_bytes()
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
                        bytes_expected=bytes_expected,
                        session=session,
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
        return lambda v: v >= f_value

    if value.startswith("<="):
        f_value = float(value[2:])
        return lambda v: v <= f_value

    if value.startswith(">"):
        f_value = float(value[1:])
        return lambda v: v > f_value

    if value.startswith("<"):
        f_value = float(value[1:])
        return lambda v: v < f_value

    if value.startswith("!"):
        return lambda v: v != value

    return lambda v: str(v) == value