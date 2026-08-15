from __future__ import annotations

from server.accounts import AccountStore


def test_first_login_for_a_username_creates_the_account():
    # The simplest real auth flow for a small self-hosted game - no
    # separate signup step, first login registers.
    store = AccountStore()
    result = store.authenticate("rowan", "correct horse battery staple")

    assert result.success
    assert result.is_new_account
    assert result.player_id
    assert result.error is None


def test_a_returning_username_gets_back_the_same_player_id():
    # Preserves the "reconnect as the same character" property the old
    # .player_id file used to provide, just proven by a password instead.
    store = AccountStore()
    first = store.authenticate("rowan", "correct horse battery staple")
    second = store.authenticate("rowan", "correct horse battery staple")

    assert second.success
    assert not second.is_new_account
    assert second.player_id == first.player_id


def test_an_existing_username_with_the_wrong_password_is_refused():
    store = AccountStore()
    store.authenticate("rowan", "correct horse battery staple")

    result = store.authenticate("rowan", "wrong password")

    assert not result.success
    assert result.player_id is None
    assert result.error


def test_blank_username_or_password_is_refused_without_registering_anything():
    store = AccountStore()

    blank_username = store.authenticate("", "some password")
    blank_password = store.authenticate("rowan", "")

    assert not blank_username.success
    assert not blank_password.success
    # Neither call should have registered "rowan" - a real login with the
    # real password still succeeds as a genuinely first-time account
    # afterward, not blocked by a half-registered stray entry.
    real_login = store.authenticate("rowan", "correct horse battery staple")
    assert real_login.is_new_account


def test_two_different_usernames_get_two_different_player_ids():
    store = AccountStore()
    rowan = store.authenticate("rowan", "password-one")
    elowen = store.authenticate("elowen", "password-two")

    assert rowan.player_id != elowen.player_id


def test_persists_across_separate_store_instances_against_the_same_file(tmp_path):
    # The real, file-backed path - server/main.py's actual production
    # wiring, as opposed to every other test above's in-memory-only
    # default (path=None), which deliberately never touches disk.
    path = tmp_path / "accounts.json"

    first_instance = AccountStore(path)
    original = first_instance.authenticate("rowan", "correct horse battery staple")

    second_instance = AccountStore(path)
    reloaded = second_instance.authenticate("rowan", "correct horse battery staple")

    assert reloaded.success
    assert not reloaded.is_new_account
    assert reloaded.player_id == original.player_id


def test_password_is_never_stored_in_plaintext_on_disk(tmp_path):
    path = tmp_path / "accounts.json"
    store = AccountStore(path)
    store.authenticate("rowan", "correct horse battery staple")

    raw = path.read_text()
    assert "correct horse battery staple" not in raw


def test_in_memory_store_never_touches_disk(tmp_path, monkeypatch):
    # path=None (Transport's own default when no AccountStore is
    # explicitly supplied) must genuinely never write a file - the whole
    # reason it exists is to keep every existing test/call site that
    # constructs a bare Transport free of stray real-file risk.
    monkeypatch.chdir(tmp_path)
    store = AccountStore()
    store.authenticate("rowan", "correct horse battery staple")

    assert list(tmp_path.iterdir()) == []


def test_new_account_starts_with_no_recent_sessions():
    store = AccountStore()
    result = store.authenticate("rowan", "correct horse battery staple")

    assert result.recent_sessions == []


def test_record_session_joined_shows_up_on_a_later_login():
    store = AccountStore()
    first = store.authenticate("rowan", "correct horse battery staple")

    store.record_session_joined(first.player_id, "the-tavern-of-doom")

    second = store.authenticate("rowan", "correct horse battery staple")
    assert second.recent_sessions == ["the-tavern-of-doom"]


def test_record_session_joined_moves_a_repeated_session_to_the_front_not_duplicating_it():
    store = AccountStore()
    result = store.authenticate("rowan", "correct horse battery staple")
    player_id = result.player_id

    store.record_session_joined(player_id, "session-a")
    store.record_session_joined(player_id, "session-b")
    store.record_session_joined(player_id, "session-a")  # rejoining an earlier one

    reloaded = store.authenticate("rowan", "correct horse battery staple")
    assert reloaded.recent_sessions == ["session-a", "session-b"]


def test_record_session_joined_caps_at_ten_most_recent():
    store = AccountStore()
    result = store.authenticate("rowan", "correct horse battery staple")
    player_id = result.player_id

    for i in range(12):
        store.record_session_joined(player_id, f"session-{i}")

    reloaded = store.authenticate("rowan", "correct horse battery staple")
    assert len(reloaded.recent_sessions) == 10
    assert reloaded.recent_sessions[0] == "session-11"  # most recent first


def test_record_session_joined_for_an_unknown_player_id_is_a_silent_no_op():
    store = AccountStore()
    store.record_session_joined("not-a-real-player-id", "some-session")  # should not raise
