import os
import stat
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import grpc
import requests
from pydantic import BaseModel

from ray.rllib.examples.envs.classes.multi_agent.footsies.game.proto import (
    footsies_service_pb2 as footsies_pb2,
)
from ray.rllib.examples.envs.classes.multi_agent.footsies.game.proto import (
    footsies_service_pb2_grpc as footsies_pb2_grpc,
)


@dataclass
class BinaryUrls:
    # Uploaded 07.28.2025
    S3_ROOT = "https://ray-example-data.s3.us-west-2.amazonaws.com/rllib/env-footsies/binaries/"

    # Zip file names
    ZIP_LINUX_SERVER = "footsies_linux_server_021725.zip"
    ZIP_LINUX_WINDOWED = "footsies_linux_windowed_021725.zip"
    ZIP_MAC_HEADLESS = "footsies_mac_headless_5709b6d.zip"
    ZIP_MAC_WINDOWED = "footsies_mac_windowed_5709b6d.zip"

    # Full URLs
    URL_LINUX_SERVER_BINARIES = S3_ROOT + ZIP_LINUX_SERVER
    URL_LINUX_WINDOWED_BINARIES = S3_ROOT + ZIP_LINUX_WINDOWED
    URL_MAC_HEADLESS_BINARIES = S3_ROOT + ZIP_MAC_HEADLESS
    URL_MAC_WINDOWED_BINARIES = S3_ROOT + ZIP_MAC_WINDOWED


class Config(BaseModel):
    download_dir: Path = Path("/tmp/ray/binaries/footsies")
    extract_dir: Path = Path("/tmp/ray/binaries/footsies")
    target_binary: Literal[
        "linux_server", "linux_windowed", "mac_headless", "mac_windowed"
    ] = "linux_server"


class FootsiesBinary:
    def __init__(self, config: Config):
        self._urls = BinaryUrls()
        self.config = config
        self.target_binary = config.target_binary
        if self.target_binary == "linux_server":
            self.url = self._urls.URL_LINUX_SERVER_BINARIES
        elif self.target_binary == "linux_windowed":
            self.url = self._urls.URL_LINUX_WINDOWED_BINARIES
        elif self.target_binary == "mac_headless":
            self.url = self._urls.URL_MAC_HEADLESS_BINARIES
        elif self.target_binary == "mac_windowed":
            self.url = self._urls.URL_MAC_WINDOWED_BINARIES
        else:
            raise ValueError(f"Invalid target binary: {self.target_binary}")

        self.full_download_dir = config.download_dir.resolve()
        self.full_download_path = (
            self.full_download_dir / str.split(self.url, sep="/")[-1]
        )
        self.full_extract_dir = config.extract_dir.resolve()
        self.renamed_path = ""

    def _download_game_binary(self):
        chunk_size = 1024 * 1024  # 1MB

        if Path(self.full_download_path).exists():
            print(
                f"Game binary already exists at {self.full_download_path}, skipping download."
            )
        else:
            try:
                with requests.get(self.url, stream=True) as response:
                    response.raise_for_status()
                    self.full_download_dir.mkdir(parents=True, exist_ok=True)
                    with open(self.full_download_path, "wb") as f:
                        for chunk in response.iter_content(chunk_size=chunk_size):
                            if chunk:
                                f.write(chunk)
                print(
                    f"Downloaded game binary to {self.full_download_path}\n"
                    f"Binary size: {self.full_download_path.stat().st_size / 1024 / 1024:.1f} MB\n"
                )
            except requests.exceptions.RequestException as e:
                print(f"Failed to download binary from {self.url}: {e}")

    def _unzip_game_binary(self):
        self.renamed_path = self.full_extract_dir / "footsies_binaries"

        if Path(self.renamed_path).exists():
            print(
                f"Game binary already extracted at {self.renamed_path}, skipping extraction."
            )
        else:
            self.full_extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(self.full_download_path, mode="r") as zip_ref:
                zip_ref.extractall(self.full_extract_dir)

            if self.target_binary == "mac_windowed":
                self.full_download_path.with_suffix(".app").rename(self.renamed_path)
            else:
                self.full_download_path.with_suffix("").rename(self.renamed_path)
            print(f"Extracted game binary to {self.renamed_path}")

    def start_game_server(self, port: int) -> None:
        self._download_game_binary()
        self._unzip_game_binary()

        if self.target_binary == "mac_windowed":
            game_binary_path = (
                Path(self.renamed_path) / "Contents" / "MacOS" / "FOOTSIES"
            )
        elif self.target_binary == "mac_headless":
            game_binary_path = Path(self.renamed_path) / "FOOTSIES"
        else:
            game_binary_path = Path(self.renamed_path) / "footsies.x86_64"

        if os.access(game_binary_path, os.X_OK):
            print(f"Game binary has an executable permission: {game_binary_path}")
        else:
            self._add_executable_permission(game_binary_path)
        print(f"Game binary path: {game_binary_path}")

        if (
            self.target_binary == "linux_server"
            or self.target_binary == "linux_windowed"
        ):
            subprocess.Popen([game_binary_path, "--port", str(port)])
        else:
            subprocess.Popen(
                [
                    "arch",
                    "-x86_64",
                    game_binary_path,
                    "--port",
                    str(port),
                ],
            )
        time.sleep(10)  # Grace period for the server to start

        # check if the game server is running correctly
        _t0 = time.time()
        _timeout_duration = 10  # seconds

        channel = grpc.insecure_channel(f"localhost:{port}")
        try:
            stub = footsies_pb2_grpc.FootsiesGameServiceStub(channel)
            stub.StartGame(footsies_pb2.Empty())
            ready = stub.IsReady(footsies_pb2.Empty()).value
            while not ready and time.time() - _t0 < _timeout_duration:
                time.sleep(1)
                ready = stub.IsReady(footsies_pb2.Empty()).value
                if time.time() - _t0 > _timeout_duration:
                    raise TimeoutError(
                        f"Game server did not become ready within {_timeout_duration} seconds."
                    )
                print("Game not ready...")
            print("Game ready!")
        finally:
            channel.close()

    @staticmethod
    def _add_executable_permission(binary_path: Path) -> None:
        binary_path.chmod(binary_path.stat().st_mode | stat.S_IXUSR)
