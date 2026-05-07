from kebi.core.events.events import PlaceSaved


def test_place_saved_constructs_with_place_ids_list() -> None:
    event = PlaceSaved(user_id="user-1", place_ids=["place-1"])

    assert event.place_ids == ["place-1"]
    assert event.user_id == "user-1"


def test_place_saved_event_type_is_place_saved() -> None:
    event = PlaceSaved(user_id="user-1", place_ids=["place-1"])

    assert event.event_type == "place_saved"


def test_place_saved_place_metadata_defaults_to_empty_dict() -> None:
    event = PlaceSaved(user_id="user-1", place_ids=["place-1"])

    assert event.place_metadata == {}


def test_place_saved_accepts_multiple_place_ids() -> None:
    event = PlaceSaved(user_id="user-1", place_ids=["place-1", "place-2", "place-3"])

    assert len(event.place_ids) == 3
    assert "place-2" in event.place_ids


def test_place_saved_event_id_is_auto_generated() -> None:
    event = PlaceSaved(user_id="user-1", place_ids=["place-1"])

    assert isinstance(event.event_id, str)
    assert len(event.event_id) > 0
