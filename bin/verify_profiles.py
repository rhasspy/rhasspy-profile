#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(prog="verify_profiles.py")
    parser.add_argument("--url-base", help="Change base download URL")
    args = parser.parse_args()

    if os.isatty(sys.stdin.fileno()):
        print("Reading profile JSON from stdin...", file=sys.stderr)

    profile = json.load(sys.stdin)
    url_base = profile["download"]["url_base"]

    if args.url_base:
        url_base = args.url_base

    if not url_base.endswith("/"):
        url_base = url_base + "/"

    files = {}
    for file_key, file_info in profile["download"]["files"].items():
        try:
            if "url" not in file_info:
                # Must be a platform-specific file
                assert "platform" in file_info
                files[file_key] = {
                    "platform_files": set(file_info["platform"].values())
                }
                continue

            file_url = url_base + file_info["url"]
            files[file_key] = {
                "url": file_url,
                "size": file_info.get(
                    "zip_bytes_expected", file_info["bytes_expected"]
                ),
                "parts": "parts" in file_info,
            }

            # File is split into parts
            if "parts" in file_info:
                for i, part in enumerate(file_info["parts"]):
                    files[file_key + f"#{i}"] = {
                        "url": file_url + part["fragment"],
                        "size": part.get("zip_bytes_expected", part["bytes_expected"]),
                    }
        except Exception as e:
            print(file_key, file_info)
            raise e

    # Verify there is a file for every condition
    for condition, condition_info in profile["download"]["conditions"].items():
        for condition_files in condition_info.values():
            for download_key in condition_files.values():
                if isinstance(download_key, dict):
                    download_key = download_key["source"]

                assert download_key in files, f"Missing {download_key} from {condition}"

    # Verify that all platform files exist
    for file_key, file_info in files.items():
        for download_key in file_info.get("platform_files", []):
            assert download_key in files, f"Missing {download_key} from {file_key}"

    # Check each download HEAD (expecting content-length)
    for file_key, file_info in files.items():
        url = file_info.get("url")
        if not url or file_info.get("parts"):
            continue

        expected_size = int(file_info["size"])
        headers = (
            subprocess.check_output(["curl", "--silent", "--head", url])
            .decode()
            .splitlines()
        )
        actual_size = None
        for header in headers:
            header = header.strip()
            if header:
                if header.startswith("HTTP"):
                    assert header.split()[-1] in ("200", "302"), f"{url} {header}"
                    continue

                header_name, header_value = header.split(":", maxsplit=1)
                header_name = header_name.strip().lower()

                if header_name == "content-length":
                    actual_size = int(header_value)

        assert actual_size is not None, f"{url} (no size)"
        print(file_key, url, expected_size, actual_size)
        assert expected_size == actual_size, f"{file_key} (wrong size)"

    print("")
    print("OK")


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    main()
