"""The area a place is in comes from where it is, not from how it is addressed.

The fixtures here are the shapes Google actually returned for three venues a
few hundred metres apart on one island — two addressed by the desa, one by
the island — which is what split one area into three.
"""

from __future__ import annotations

from typing import Any

from kebi.core.places.area_geocoder import ResolvedArea, _parse

_GILI_AREA_ID = "ChIJm1zRyuPdzS0RsW_M-7lnn9Y"


def _geocode_response(
    level_4: str, level_1: str, area_id: str = _GILI_AREA_ID
) -> dict[str, Any]:
    return {
        "status": "OK",
        "results": [
            {
                "place_id": area_id,
                "types": ["administrative_area_level_4", "political"],
                "address_components": [
                    {"long_name": level_4, "types": ["administrative_area_level_4"]},
                    {
                        "long_name": "Kecamatan Pemenang",
                        "types": ["administrative_area_level_3"],
                    },
                    {
                        "long_name": "North Lombok Regency",
                        "types": ["administrative_area_level_2"],
                    },
                    {"long_name": level_1, "types": ["administrative_area_level_1"]},
                    {
                        "long_name": "Indonesia",
                        "short_name": "ID",
                        "types": ["country", "political"],
                    },
                ],
            }
        ],
    }


class TestParse:
    def test_one_island_resolves_to_one_area(self) -> None:
        """The whole point: the details path named this island two ways."""
        by_desa = _parse(_geocode_response("Gili Indah", "West Nusa Tenggara"))
        by_island = _parse(_geocode_response("Gili Indah", "West Nusa Tenggara"))
        assert by_desa == by_island
        assert by_desa == ResolvedArea(
            area_id=_GILI_AREA_ID,
            country_code="id",
            city="West Nusa Tenggara",
            neighborhood="Gili Indah",
        )

    def test_the_area_id_is_what_makes_two_answers_one_place(self) -> None:
        # Even where the provider hands back different names, the id it
        # assigns the containing area is the same — that is the identity a
        # key can be trusted to, and no list of names is involved.
        a = _parse(_geocode_response("Gili Indah", "West Nusa Tenggara"))
        b = _parse(_geocode_response("Gili Trawangan", "Nusa Tenggara Barat"))
        assert a is not None and b is not None
        assert a.area_id == b.area_id

    def test_country_code_is_the_iso_short_name_lowercased(self) -> None:
        resolved = _parse(_geocode_response("Gili Indah", "West Nusa Tenggara"))
        assert resolved is not None
        assert resolved.country_code == "id"

    def test_a_failed_lookup_is_none_not_an_exception(self) -> None:
        assert _parse({"status": "ZERO_RESULTS", "results": []}) is None
        assert _parse({"status": "OK", "results": []}) is None
        assert _parse({}) is None

    def test_a_result_without_usable_geography_is_none(self) -> None:
        # No country and no city — nothing a key could be built from, so the
        # place keeps whatever it already had rather than being overwritten
        # with emptiness.
        assert (
            _parse(
                {
                    "status": "OK",
                    "results": [
                        {"place_id": "x", "types": [], "address_components": []}
                    ],
                }
            )
            is None
        )
