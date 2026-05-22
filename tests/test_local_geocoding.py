from __future__ import annotations

import json
from pathlib import Path

from app.services.geocoding.local import LocalGazetteerProvider


def test_local_gazetteer_maps_gps_to_multilingual_place_names(tmp_path: Path) -> None:
    geodata = tmp_path / "geodata"
    geonames = geodata / "geonames"
    naturalearth = geodata / "naturalearth"
    geonames.mkdir(parents=True)
    naturalearth.mkdir(parents=True)

    (geonames / "countryInfo.txt").write_text(
        "KR\tKOR\t410\tKS\tSouth Korea\tSeoul\t...\n",
        encoding="utf-8",
    )
    (geonames / "admin1CodesASCII.txt").write_text("KR.11\tSeoul\tSeoul\t1835847\n", encoding="utf-8")
    (geonames / "cities1000.txt").write_text(
        "\t".join(
            [
                "1835848",
                "Seoul",
                "Seoul",
                "서울,SEOUL",
                "37.5660",
                "126.9784",
                "P",
                "PPLC",
                "KR",
                "",
                "11",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (naturalearth / "countries.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "ISO_A2": "KR",
                            "NAME": "South Korea",
                            "NAME_EN": "South Korea",
                            "NAME_KO": "대한민국",
                        },
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[124, 33], [132, 33], [132, 39], [124, 39], [124, 33]]],
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = LocalGazetteerProvider(geodata).reverse(37.55, 127.0)

    assert result is not None
    tags = result.place_tags()
    assert "Seoul" in tags
    assert "서울" in tags
    assert "대한민국" in tags
    assert "한국" in tags
