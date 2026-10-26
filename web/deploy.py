"""Publish the screens as a static website on Azure Storage.

    py -m pip install azure-storage-blob
    py web/build_data.py
    py web/deploy.py

Needs STORAGE_CONNECTION_STRING in .env (storage account > Security + networking > Access keys).
It switches on "Static website" on the account if needed, then uploads everything in web/ to the
$web container with the right content types. Run it again after every change: it replaces the files.
The site's address is on the storage account, under Data management > Static website (Primary endpoint).
"""
from __future__ import annotations

import os
from pathlib import Path

from azure.storage.blob import BlobServiceClient, ContentSettings, StaticWebsite
from dotenv import load_dotenv

load_dotenv()
WEB = Path(__file__).resolve().parent
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
         ".json": "application/json; charset=utf-8", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8",
         ".png": "image/png", ".svg": "image/svg+xml", ".mp3": "audio/mpeg"}
SKIP = {"deploy.py", "build_data.py", "build_audio.py", "staticwebapp.config.json", "index.json"}


def main():
    service = BlobServiceClient.from_connection_string(os.environ["STORAGE_CONNECTION_STRING"])
    props = service.get_service_properties()
    if not (props.get("static_website") and props["static_website"].enabled):
        service.set_service_properties(static_website=StaticWebsite(enabled=True, index_document="index.html",
                                                                    error_document404_path="index.html"))
        print("Static website switched on.")
    container = service.get_container_client("$web")
    files = [p for p in WEB.rglob("*") if p.is_file() and p.name not in SKIP and "__pycache__" not in p.parts]
    for path in sorted(files):
        name = path.relative_to(WEB).as_posix()
        # The match data changes with every run of the agents: never let a browser keep an old copy.
        cache = "no-cache" if name.startswith("data/") or path.suffix in (".html", ".js", ".css") else "public, max-age=604800"
        with path.open("rb") as f:
            container.upload_blob(name, f, overwrite=True,
                                  content_settings=ContentSettings(content_type=TYPES.get(path.suffix, "application/octet-stream"),
                                                                   cache_control=cache))
        print(f"  uploaded {name}")
    print(f"\n{len(files)} files published. Address: storage account > Data management > Static website > Primary endpoint.")


if __name__ == "__main__":
    main()
