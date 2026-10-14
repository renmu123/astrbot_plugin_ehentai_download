"""Build a clean AstrBot plugin archive without caches, downloads or credentials."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
NAME = "astrbot_plugin_ehentai_download"
FILES = ["main.py", "downloader.py", "metadata.yaml", "_conf_schema.json", "requirements.txt", "README.md", "LICENSE"]


def main():
    destination = ROOT / "dist" / f"{NAME}-v1.0.2.zip"
    destination.parent.mkdir(exist_ok=True)
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        for filename in FILES:
            archive.write(ROOT / filename, f"{NAME}/{filename}")
    with ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Plugin archive integrity check failed")
    print(destination)


if __name__ == "__main__":
    main()
