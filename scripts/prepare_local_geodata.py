#!/usr/bin/env python3
"""Download free local geocoding extracts for offline reverse geocoding."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile

import shapefile  # type: ignore[import-untyped]


GEONAMES_BASE = "https://download.geonames.org/export/dump"
NATURAL_EARTH_COUNTRIES = (
    "https://naciscdn.org/naturalearth/110m/cultural/ne_110m_admin_0_countries.zip"
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare GeoNames + Natural Earth data for Photome.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("model_cache/geodata"),
        help="Output geodata root. Default: model_cache/geodata",
    )
    parser.add_argument(
        "--cities",
        default="cities1000",
        choices=("cities1000", "cities15000"),
        help="GeoNames city extract. cities1000 is better coverage; cities15000 is smaller.",
    )
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    geonames = root / "geonames"
    naturalearth = root / "naturalearth"
    geonames.mkdir(parents=True, exist_ok=True)
    naturalearth.mkdir(parents=True, exist_ok=True)

    _download_geonames_zip(f"{GEONAMES_BASE}/{args.cities}.zip", geonames / f"{args.cities}.txt")
    _download_text(f"{GEONAMES_BASE}/admin1CodesASCII.txt", geonames / "admin1CodesASCII.txt")
    _download_text(f"{GEONAMES_BASE}/countryInfo.txt", geonames / "countryInfo.txt")
    _download_natural_earth(naturalearth / "countries.geojson")

    print(f"geodata ready: {root}")


def _download_geonames_zip(url: str, output: Path) -> None:
    if output.is_file():
        print(f"exists: {output}")
        return
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "geonames.zip"
        _download(url, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            member = next(name for name in zf.namelist() if name.endswith(".txt"))
            with zf.open(member) as src, output.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    print(f"wrote: {output}")


def _download_text(url: str, output: Path) -> None:
    if output.is_file():
        print(f"exists: {output}")
        return
    _download(url, output)
    print(f"wrote: {output}")


def _download_natural_earth(output: Path) -> None:
    if output.is_file():
        print(f"exists: {output}")
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / "naturalearth.zip"
        extract_root = tmp_path / "naturalearth"
        extract_root.mkdir()
        _download(NATURAL_EARTH_COUNTRIES, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_root)
        shp = next(extract_root.glob("*.shp"))
        reader = shapefile.Reader(str(shp), encoding="latin1")
        fields = [field[0] for field in reader.fields[1:]]
        features = []
        for shape_record in reader.iterShapeRecords():
            props = dict(zip(fields, shape_record.record))
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        key: props.get(key)
                        for key in (
                            "ISO_A2",
                            "WB_A2",
                            "SU_A2",
                            "NAME",
                            "NAME_LONG",
                            "ADMIN",
                            "NAME_EN",
                            "NAME_KO",
                        )
                    },
                    "geometry": shape_record.shape.__geo_interface__,
                }
            )
        output.write_text(
            json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False),
            encoding="utf-8",
        )
    print(f"wrote: {output}")


def _download(url: str, output: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "photome-local-geodata/1.0"})
    with urllib.request.urlopen(req, timeout=60) as response, output.open("wb") as fh:
        shutil.copyfileobj(response, fh)


if __name__ == "__main__":
    main()
