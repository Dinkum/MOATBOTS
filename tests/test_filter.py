from app.services.filter import decide, mention_names


def test_ignore_wins_when_feed_is_healthy():
    assert (
        decide(
            {"status": "ok", "message": "all green"},
            match_any=["fail"],
            ignore_any=["green"],
        )
        == "no"
    )


def test_match_wakes_on_failure():
    assert decide("billing crash loop", match_any=["crash", "fail"], ignore_any=["green"]) == "yes"


def test_empty_match_list_is_ambiguous():
    assert decide("random noise") == "ambiguous"


def test_mentions_are_unique_and_ordered():
    assert mention_names("hey @chief and @researcher and @chief") == ["chief", "researcher"]
