"""Regression tests for the three v1 write bugs.

Each of these reproduces the exact v1 behaviour first, so the test documents
what was wrong as well as what is now right.
"""

from __future__ import annotations

import pytest

from plex_auto_genres.models import MediaItem, TagField
from plex_auto_genres.plexsvc.writer import PlexWriter, build_tag_edits, plan_tags
from plex_auto_genres.store import Store

from .conftest import FakePlexItem, FakeTag


# -- bug #1: only the last genre of the loop survived ----------------------


def test_v1_loop_behaviour_is_reproduced_by_plexapi():
    """Documents the original defect using plexapi's real editTags."""
    from plexapi.mixins import GenreMixin

    class StaleCachedItem(GenreMixin):
        def __init__(self, existing):
            self.genres = list(existing)
            self.sent = []

        def _edit(self, **kwargs):
            self.sent.append([v for k, v in sorted(kwargs.items()) if k.endswith(".tag.tag")])
            return self

    victim = StaleCachedItem(["Animation", "Comedy"])
    for genre in ["Action", "Fantasy", "Shounen"]:
        victim.addGenre(genre)  # what v1 did, once per genre

    assert len(victim.sent) == 3, "v1 issued one HTTP write per genre"
    assert victim.sent[-1] == ["Animation", "Comedy", "Shounen"]
    assert "Action" not in victim.sent[-1], "the earlier genres were overwritten"


def test_all_genres_are_written_in_one_request(store: Store, item):
    writer = PlexWriter(store, "run1", "Animes")
    outcome = writer.write_tags(
        item, TagField.GENRE, ["Action", "Fantasy", "Shounen"], clear=False
    )

    assert outcome.changed
    assert writer.requests == 1, "one PUT, however many genres"
    assert item.handle.last_tags == ["Animation", "Comedy", "Action", "Fantasy", "Shounen"]


def test_existing_tags_are_preserved_when_not_clearing(store: Store, item):
    writer = PlexWriter(store, "run1", "Animes")
    writer.write_tags(item, TagField.GENRE, ["Action"], clear=False)
    assert "Animation" in item.handle.last_tags
    assert "Comedy" in item.handle.last_tags


# -- bug #2: clearGenres did nothing ---------------------------------------


def test_clear_replaces_instead_of_appending(store: Store, item):
    writer = PlexWriter(store, "run1", "Animes")
    writer.write_tags(item, TagField.GENRE, ["Action", "Drama"], clear=True)

    assert item.handle.last_tags == ["Action", "Drama"]
    assert "Animation" not in item.handle.last_tags
    assert "Comedy" not in item.handle.last_tags


def test_clear_emits_an_explicit_removal_for_dropped_tags():
    edits = build_tag_edits(TagField.GENRE, ["Animation", "Comedy"], ["Action"])
    assert edits["genre[0].tag.tag"] == "Action"
    # Belt and braces: the dropped tags are also named in the remove param, so
    # the result is right whether Plex treats the indexed form as a replace or
    # as a merge.
    assert edits["genre[].tag.tag-"] == "Animation,Comedy"


def test_plan_tags_clear_true_is_a_replacement():
    assert plan_tags(["A", "B"], ["C"], clear=True) == ["C"]


def test_plan_tags_clear_false_is_a_union():
    assert plan_tags(["A", "B"], ["B", "C"], clear=False) == ["A", "B", "C"]


def test_plan_tags_deduplicates_case_insensitively():
    assert plan_tags(["Action"], ["action", "ACTION", "Drama"], clear=False) == ["Action", "Drama"]


def test_prefix_is_applied_to_new_tags_only():
    assert plan_tags([], ["Action", "Drama"], clear=True, prefix="*") == ["*Action", "*Drama"]


# -- bug #3: rate() was handed a string ------------------------------------


def test_rating_is_sent_as_a_float(store: Store, item):
    writer = PlexWriter(store, "run1", "Animes")
    assert writer.set_rating(item, 8.7) is True
    assert item.handle.ratings == [8.7]


def test_v1_string_score_would_have_raised():
    handle = FakePlexItem()
    with pytest.raises(ValueError):
        handle.rate("8.7")  # exactly what v1 passed


def test_out_of_range_and_missing_scores_are_skipped(store: Store, item):
    writer = PlexWriter(store, "run1", "Animes")
    assert writer.set_rating(item, None) is False
    assert writer.set_rating(item, 42.0) is False
    assert item.handle.ratings == []


# -- general write behaviour -----------------------------------------------


def test_no_request_when_tags_already_correct(store: Store, item):
    writer = PlexWriter(store, "run1", "Animes")
    outcome = writer.write_tags(item, TagField.GENRE, ["Animation", "Comedy"], clear=True)
    assert outcome.changed is False
    assert writer.requests == 0
    assert item.handle.edits == []


def test_dry_run_never_touches_plex(store: Store, item):
    writer = PlexWriter(store, "run1", "Animes", dry_run=True)
    outcome = writer.write_tags(item, TagField.GENRE, ["Action"], clear=True)
    assert outcome.changed is True          # still reports what would happen
    assert item.handle.edits == []          # but wrote nothing
    assert writer.requests == 0
    assert writer.set_rating(item, 9.0) is True
    assert item.handle.ratings == []


def test_writes_record_an_undo_snapshot(store: Store, item):
    writer = PlexWriter(store, "run-abc", "Animes")
    writer.write_tags(item, TagField.GENRE, ["Action"], clear=True)

    snaps = store.snapshots_for("run-abc")
    assert len(snaps) == 1
    import json
    assert json.loads(snaps[0]["before"]) == ["Animation", "Comedy"]
    assert json.loads(snaps[0]["after"]) == ["Action"]


# -- review regressions: undo restores locks and ratings -----------------------


def _server_for(handle):
    return type("Server", (), {"fetchItem": lambda self, key: handle})()


def test_undo_restores_the_lock_state_the_item_had(store: Store, item):
    from plex_auto_genres.plexsvc.writer import undo_run

    writer = PlexWriter(store, "run1", "Lib")
    writer.write_tags(item, TagField.GENRE, ["Action"], clear=True)
    assert item.handle.edits[-1]["genre.locked"] == 1

    assert undo_run(_server_for(item.handle), store, "run1") == (1, 0)
    assert item.handle.last_tags == ["Animation", "Comedy"]
    assert item.handle.edits[-1]["genre.locked"] == 0, "the field was unlocked before the run"


def test_undo_restores_a_previous_rating(store: Store, item):
    from plex_auto_genres.plexsvc.writer import undo_run

    item.handle.userRating = 6.0
    writer = PlexWriter(store, "run1", "Lib")
    assert writer.set_rating(item, 8.5) and item.handle.ratings == [8.5]

    assert undo_run(_server_for(item.handle), store, "run1") == (1, 0)
    assert item.handle.ratings[-1] == 6.0


def test_undo_clears_a_rating_that_did_not_exist(store: Store, item):
    from plex_auto_genres.plexsvc.writer import undo_run

    writer = PlexWriter(store, "run1", "Lib")
    assert writer.set_rating(item, 8.5)
    assert undo_run(_server_for(item.handle), store, "run1") == (1, 0)
    assert item.handle.ratings[-1] == -1.0, "plexapi's 'unrated'"


def test_a_tag_already_in_plex_is_matched_on_letters_not_punctuation():
    """The rules fold names, so the writer has to as well. Comparing case
    only appended "Rock n Roll" beside the "Rock 'n' Roll" an earlier run
    wrote, which is exactly the two collections the folding exists to
    prevent."""
    kept = plan_tags(current=["Rock 'n' Roll", "Action"],
                     incoming=["Rock n Roll", "Drama"], clear=False, prefix="")
    assert kept == ["Rock 'n' Roll", "Action", "Drama"]


def test_folding_does_not_merge_names_that_only_look_alike():
    assert plan_tags(current=["Action"], incoming=["Adventure"], clear=False, prefix="") == [
        "Action", "Adventure"
    ]


def test_a_manual_removal_takes_off_a_tag_plex_already_has():
    """Merging keeps what Plex holds, so a removal has to reach into it too:
    otherwise it could only ever stop a tag being added, never take one off."""
    kept = plan_tags(current=["Action", "Kids", "Drama"], incoming=["Comedy"],
                     clear=False, prefix="", remove=["kids"])
    assert kept == ["Action", "Drama", "Comedy"]


def test_a_manual_removal_matches_a_prefixed_collection():
    kept = plan_tags(current=["_Action", "_Kids"], incoming=["Drama"],
                     clear=False, prefix="_", remove=["Kids"])
    assert kept == ["_Action", "_Drama"]


def test_decisions_match_a_tag_with_the_prefix_or_without_it():
    """A person may name a tag the app wrote ("PAG-Action", or just "Action")
    or one it did not (Plex's "Kids", their "My Favourites"): both must hit."""
    kept = plan_tags(current=["Kids", "PAG-Action", "My Favourites"], incoming=["Drama"],
                     clear=False, prefix="PAG-", remove=["Kids", "Action"],
                     extra=["my favourites", "Mecha"])
    assert kept == ["My Favourites", "PAG-Drama", "PAG-Mecha"]


def test_a_refusal_beats_an_answer_and_an_addition_alike():
    assert plan_tags(current=[], incoming=["Drama", "Kids"], clear=True, prefix="PAG-",
                     remove=["PAG-Kids"], extra=["kids"]) == ["PAG-Drama"]


class _Listed(FakePlexItem):
    """A Plex item as a library listing hands it over: only some of its tags."""

    def __init__(self, shown, full):
        super().__init__(1, "Anime", 2001, genres=shown)
        self._full, self.reloads = list(full), 0

    def isPartialObject(self):
        return self.reloads == 0

    def reload(self):
        self.reloads += 1
        self.genres = [FakeTag(g) for g in self._full]
        return self


def _listed_item(shown, full) -> MediaItem:
    handle = _Listed(shown, full)
    return MediaItem(rating_key=1, title="Anime", year=2001, guids=[],
                     current_genres=list(shown), handle=handle)


def test_replacing_takes_off_the_tags_a_listing_did_not_show(store: Store):
    """Plex lists two genres of four; clearGenres removed only those two, and
    every older tag past the cut -- a renamed one, an ignored one -- stayed."""
    item = _listed_item(["Drama", "otaku culture"], ["Drama", "otaku culture", "Kids", "sister"])
    writer = PlexWriter(store, "run", "Anime")

    outcome = writer.write_tags(item, TagField.GENRE, ["Drama", "otaku", "sisters"], clear=True)

    removed = item.handle.edits[-1]["genre[].tag.tag-"].split(",")
    assert sorted(removed) == ["Kids", "otaku%20culture", "sister"]
    assert outcome.before == ["Drama", "otaku culture", "Kids", "sister"], "the undo record too"


def test_a_plan_that_takes_nothing_off_reads_no_more_than_the_listing(store: Store):
    """Merging only adds, so tags past the cut cannot change the result: no
    extra request per item for the passes that run over a whole library."""
    item = _listed_item(["Drama"], ["Drama", "Kids"])
    writer = PlexWriter(store, "run", "Anime")

    writer.write_tags(item, TagField.GENRE, ["Action"], clear=False)

    assert item.handle.reloads == 0


def test_a_refusal_reaches_a_tag_past_the_listings_cut(store: Store):
    item = _listed_item(["Drama"], ["Drama", "Kids"])
    writer = PlexWriter(store, "run", "Anime")

    writer.write_tags(item, TagField.GENRE, ["Action"], clear=False, remove=["kids"])

    assert item.handle.last_tags == ["Drama", "Action"]
    assert item.handle.edits[-1]["genre[].tag.tag-"] == "Kids"
