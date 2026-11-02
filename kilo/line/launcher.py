"""
`kilo-line-browser`: starts the dedicated Chrome that Kilo reads and sends LINE messages through,
and keeps it running. Its profile (and LINE login) lives in <data dir>/line/chrome-profile, apart
from your own Chrome; the LINE extension is a pinned copy in <data dir>/line/extension, so
a Web Store update cannot change the page under Kilo. Chrome only loads an unpacked extension when
asked over its DevTools pipe, so this process stays attached to Chrome for as long as it runs.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from kilo.line.config import settings
from kilo.line.browser import EXTENSION_ID, PAGE_PREFIX

CHROMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
# Where Chrome keeps extensions installed from the Web Store, to copy the LINE extension from.
INSTALLED = Path("~/.config/google-chrome").expanduser()


def line_dir() -> Path:
    return settings.kilo_data_dir / "line"


def pin_extension(target: Path) -> Path:
    """Copy the newest installed LINE extension to `target`, without Web Store metadata Chrome refuses to load."""
    versions = sorted(INSTALLED.glob(f"*/Extensions/{EXTENSION_ID}/*/manifest.json"), key=lambda p: p.stat().st_mtime)
    if not versions:
        sys.exit(f"No LINE extension found under {INSTALLED}: install it from the Chrome Web Store in your own Chrome first.")
    shutil.copytree(versions[-1].parent, target, ignore=shutil.ignore_patterns("_metadata"))
    return target


class Pipe:
    """Chrome's DevTools pipe: JSON commands separated by NUL bytes, on file descriptors 3 (in) and 4 (out)."""

    def __init__(self, write_fd: int, read_fd: int):
        self._out, self._in = os.fdopen(write_fd, "wb", buffering=0), os.fdopen(read_fd, "rb", buffering=0)
        self._buffer, self._id = b"", 0

    def call(self, method: str, **params: Any) -> Dict[str, Any]:
        self._id += 1
        self._out.write(json.dumps({"id": self._id, "method": method, "params": params}).encode() + b"\0")
        while True:
            while b"\0" not in self._buffer:
                chunk = self._in.read(65536)
                if not chunk:
                    raise RuntimeError("Chrome closed its DevTools pipe.")
                self._buffer += chunk
            raw, self._buffer = self._buffer.split(b"\0", 1)
            message = json.loads(raw)
            if message.get("id") == self._id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error'].get('message')}")
                return message.get("result", {})


def launch(chrome: str, port: int, extension: Path, profile: Path) -> int:
    to_chrome, from_chrome = os.pipe(), os.pipe()

    def attach_pipe() -> None:  # in the child, before Chrome starts: the pipe on fds 3 and 4
        os.dup2(to_chrome[0], 3)
        os.dup2(from_chrome[1], 4)

    process = subprocess.Popen(
        [
            chrome,
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-pipe",
            "--enable-unsafe-extension-debugging",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1100,850",
        ],
        pass_fds=(3, 4),
        preexec_fn=attach_pipe,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(to_chrome[0])
    os.close(from_chrome[1])
    pipe = Pipe(to_chrome[1], from_chrome[0])
    try:
        loaded = pipe.call("Extensions.loadUnpacked", path=str(extension))["id"]
        if loaded != EXTENSION_ID:
            raise RuntimeError(f"The extension in {extension} has id {loaded}, not LINE's {EXTENSION_ID}.")
        targets = pipe.call("Target.getTargets")["targetInfos"]
        if not any(t["type"] == "page" and t["url"].startswith(PAGE_PREFIX) for t in targets):
            pipe.call("Target.createTarget", url=f"{PAGE_PREFIX}index.html")
    except Exception as e:
        process.terminate()
        sys.exit(f"Could not set up the LINE browser: {e}")

    print(f"LINE browser running (DevTools at 127.0.0.1:{port}, profile {profile}).")
    print("Log in to LINE in its window if asked. Keep this running; Ctrl+C or closing the window stops it.", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: process.terminate())
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        return process.wait()


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(prog="kilo-line-browser", description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--chrome", help="Chrome executable (default: the first of %s found)" % ", ".join(CHROMES))
    parser.add_argument("--update-extension", action="store_true",
                        help="replace the pinned LINE extension with the one installed in your Chrome now")
    args = parser.parse_args(argv)

    chrome = args.chrome or next((found for name in CHROMES if (found := shutil.which(name))), None)
    if not chrome:
        sys.exit("Chrome not found: install Google Chrome or pass --chrome.")
    url = urlparse(settings.kilo_line_cdp_url)
    if url.hostname not in ("127.0.0.1", "localhost", "::1") or not url.port:
        sys.exit(f"KILO_LINE_CDP_URL must be a local address with a port, e.g. http://127.0.0.1:9333 (got {url.geturl()}).")

    extension = line_dir() / "extension"
    if args.update_extension and extension.exists():
        shutil.rmtree(extension)
    if not extension.exists():
        pin_extension(extension)
        print(f"Pinned LINE extension {json.loads((extension / 'manifest.json').read_text())['version']} in {extension}.")
    profile = line_dir() / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    sys.exit(launch(chrome, url.port, extension, profile))


if __name__ == "__main__":
    main()
