import datetime
import json

import httpx
import pytest
import respx

from notifier.ai_banter import (
    COOLDOWN,
    MAX_LENGTH,
    MAX_TOKENS,
    OPENROUTER_URL,
    BanterError,
    Target,
    ai_allowed,
    build_messages,
    candidates,
    choose_target,
    clean_line,
    describe,
    parse_aliases,
    generate_line,
    link_roster,
)
from notifier.banter import RosterMember
from notifier.league import LeagueSnapshot, Move, Standing

NOW = datetime.datetime(2026, 8, 27, 18, 0, tzinfo=datetime.UTC)

YURI = Standing(entry=11, manager="Nils Berg", team_name="Just Bergs", rank=2,
                gw_points=41, total_points=41)
ROMAN = Standing(entry=22, manager="Rex Marlow", team_name="Rocket FC", rank=1,
                 gw_points=78, total_points=78)

ROSTER = (
    RosterMember("@just_bergs", "Liverpool"),
    RosterMember("@rexmarlow", "Manchester United"),
    RosterMember("@nobody_here", "Chelsea"),
)

CLASSIC = LeagueSnapshot(
    game="fpl",
    league_id=42,
    league_name="The Office",
    table=(ROMAN, YURI),
    moves=(
        Move(entry=11, gw=2, player_in="Haaland", player_out="Watkins", kind="transfer",
             when="2026-08-27T08:00:00Z"),
        Move(entry=22, gw=2, player_in="Salah", player_out="Saka", kind="transfer",
             when="2026-08-26T08:00:00Z"),
    ),
)

DRAFT = LeagueSnapshot(
    game="draft",
    league_id=7,
    league_name="Draft Dodgers",
    table=(
        Standing(entry=22, manager="Rex Marlow", team_name="Rocket FC", rank=1,
                 record="1-0-0", league_points=3, points_for=55),
        Standing(entry=11, manager="Nils Berg", team_name="Just Bergs", rank=2,
                 record="0-0-1", league_points=0, points_for=41),
    ),
    moves=(
        Move(entry=11, gw=2, player_in="Isak", player_out="Wood", kind="waiver",
             accepted=False, when="2026-08-27T09:00:00Z"),
    ),
    scoring="h2h",
)

# The same league scoring the other way: drafted squads, accumulated points, no matches.
DRAFT_CLASSIC = LeagueSnapshot(
    game="draft",
    league_id=8,
    league_name="The Testers",
    table=(Standing(entry=11, manager="Nils Berg", team_name="Just Bergs", rank=2,
                    gw_points=43, total_points=43),),
    moves=(),
    scoring="classic",
)


def _fixed(index):
    """A stand-in for `random.randrange` that always picks the same slot."""
    return lambda n: min(index, n - 1)


# ------------------------------------------------------------------- roster linking


def test_link_roster_matches_a_handle_to_a_team_name():
    """@just_bergs and "Just Bergs" are the same person typed twice."""
    linked = link_roster(CLASSIC.table, ROSTER)
    assert linked[11].handle == "@just_bergs"


def test_link_roster_matches_a_handle_to_a_manager_name():
    linked = link_roster((ROMAN,), ROSTER)
    assert linked[22].handle == "@rexmarlow"


def test_link_roster_ignores_a_too_short_name_inside_a_handle():
    """"Ada" is inside somebody's handle by chance often enough to matter, and the cost
    of a chance match is a personal joke aimed at the wrong mate."""
    short = Standing(entry=55, manager="Ada", team_name="", rank=5)
    roster = (RosterMember("@adamovich", "Everton"),)
    assert link_roster((short,), roster) == {}


def test_link_roster_matches_a_handle_on_a_surname_alone():
    """"@t_harrington" and "Tomas Harrington" share no whole string -- "tharrington" is
    not inside "tomasharrington" -- but they do share a surname. Observed live: without
    the per-word pass, three of four real mates went unlinked and lost their mention."""
    row = Standing(entry=99, manager="Tomas Harrington", team_name="Harringtons", rank=3)
    assert link_roster((row,), (RosterMember("@t_harrington", "Arsenal"),))[99].handle == "@t_harrington"


def test_link_roster_leaves_an_unmatched_row_unlinked():
    """A bad link tags the wrong person in a personal joke, so no match beats a guess."""
    stranger = Standing(entry=33, manager="Ada Lovelace", team_name="Analytical XI", rank=3)
    assert link_roster((stranger,), ROSTER) == {}


# ------------------------------------------------------------------------- aliases


def test_parse_aliases_reads_handle_equals_name_pairs():
    raw = "@nine_iron=Ada Lovelace, @rexmarlow=Rex Marlow"
    assert parse_aliases(raw) == (
        ("@nine_iron", "Ada Lovelace"),
        ("@rexmarlow", "Rex Marlow"),
    )


@pytest.mark.parametrize("raw", ["", "   ", ",,", "@handle", "=Name", "@handle=", "@handle= "])
def test_parse_aliases_skips_what_it_cannot_use(raw):
    """One mate's typo costs that mate's mention, not the service -- the same call
    banter.parse_roster makes."""
    assert parse_aliases(raw) == ()


def test_parse_aliases_splits_on_the_first_equals_only():
    assert parse_aliases("@h=Name=Weird") == (("@h", "Name=Weird"),)


ALIASES = (
    ("@nine_iron", "Ada Lovelace"),
    ("@just_bergs", "Nils Berg"),
    ("@t_harrington", "Tomas Harington"),  # as the operator typed it: see below
)

ALIAS_ROSTER = (
    RosterMember("@nine_iron", "Chelsea"),
    RosterMember("@just_bergs", "Liverpool"),
    RosterMember("@t_harrington", "Arsenal"),
)


def test_an_alias_links_a_handle_that_shares_no_letters_with_the_name():
    """The whole point: no amount of letter-matching gets "@nine_iron" to "Ada
    Lovelace"."""
    row = Standing(entry=77, manager="Ada Lovelace", team_name="Analytical XI", rank=2)
    assert link_roster((row,), ALIAS_ROSTER, ALIASES)[77].handle == "@nine_iron"


def test_an_alias_still_links_when_the_operator_misspells_the_surname():
    """Configured "Tomas Harington" against the API's "Tomas Harrington" -- the real
    case. A shared four-letter word is enough on the second pass."""
    row = Standing(entry=88, manager="Tomas Harrington", team_name="Harringtons", rank=4)
    assert link_roster((row,), ALIAS_ROSTER, ALIASES)[88].handle == "@t_harrington"


def test_an_exact_alias_beats_a_shared_first_name():
    """Two mates called Sam: the one who spelled the surname the API's way wins, and
    the loose pass cannot steal the row from under them."""
    aliases = (("@sam_a", "Sam Alpha"), ("@sam_b", "Sam Beta"))
    roster = (RosterMember("@sam_a", "Chelsea"), RosterMember("@sam_b", "Arsenal"))
    table = (
        Standing(entry=1, manager="Sam Beta", team_name="B FC", rank=1),
        Standing(entry=2, manager="Sam Alpha", team_name="A FC", rank=2),
    )
    linked = link_roster(table, roster, aliases)
    assert linked[1].handle == "@sam_b"
    assert linked[2].handle == "@sam_a"


def test_an_alias_is_not_overridden_by_a_letter_match_guess():
    """A configured mapping is the operator's answer, not a hint."""
    row = Standing(entry=5, manager="Rex Marlow", team_name="Rocket FC", rank=3)
    roster = (RosterMember("@rexmarlow", "Man Utd"), RosterMember("@someone", "Spurs"))
    linked = link_roster((row,), roster, (("@someone", "Rex Marlow"),))
    assert linked[5].handle == "@someone"


def test_candidates_uses_aliases_for_the_label():
    snapshot = LeagueSnapshot("fpl", 1, "x", (
        Standing(entry=77, manager="Ada Lovelace", team_name="Analytical XI", rank=2),
    ), ())
    target = candidates((snapshot,), ALIAS_ROSTER, ALIASES)[0]
    assert (target.label, target.handle) == ("@nine_iron", "@nine_iron")


def test_choose_target_passes_aliases_through():
    snapshot = LeagueSnapshot("fpl", 1, "x", (
        Standing(entry=77, manager="Ada Lovelace", team_name="Analytical XI", rank=2),
    ), ())
    assert choose_target((snapshot,), ALIAS_ROSTER, None, _fixed(0), ALIASES).label == "@nine_iron"


# ------------------------------------------------------------------ target selection


def test_candidates_prefers_the_handle_where_one_was_linked():
    labels = {target.label for target in candidates((CLASSIC,), ROSTER)}
    assert labels == {"@just_bergs", "@rexmarlow"}


def test_candidates_falls_back_to_the_manager_name():
    labels = {target.label for target in candidates((CLASSIC,), ())}
    assert labels == {"Nils Berg", "Rex Marlow"}


def test_candidates_counts_a_manager_in_both_games_once():
    """Otherwise whoever plays both is twice as likely to be roasted as whoever
    plays one."""
    pool = candidates((CLASSIC, DRAFT), ROSTER)
    assert len(pool) == 2


def test_candidates_keeps_both_games_ids_for_the_same_person():
    """The two games issue different entry ids to one manager. Keeping only the first
    filed the target's own draft waiver under "other managers' moves"."""
    draft_other_id = LeagueSnapshot("draft", 7, "d", (
        Standing(entry=555, manager="Nils Berg", team_name="Just Bergs", rank=1,
                 gw_points=43, total_points=43),
    ), (Move(entry=555, gw=2, player_in="Isak", player_out="Wood", kind="waiver",
             accepted=False, when="2026-08-27T09:00:00Z"),))
    target = next(t for t in candidates((CLASSIC, draft_other_id), ROSTER)
                  if t.label == "@just_bergs")
    assert target.entry_for("fpl") == 11
    assert target.entry_for("draft") == 555
    text = describe((CLASSIC, draft_other_id), target)
    assert "Other managers' latest moves" not in text.split("Draft PL")[1]
    assert "REJECTED" in text


def test_entry_for_returns_none_for_a_game_the_target_does_not_play():
    target = Target("@a", "@a", "T", 11, "fpl", entries=(("fpl", 11),))
    assert target.entry_for("draft") is None


def test_entry_for_falls_back_to_entry_when_no_per_game_ids_were_recorded():
    """Every Target built before this field existed -- and every test that builds one by
    hand -- keeps working."""
    assert Target("@a", "@a", "T", 11, "fpl").entry_for("draft") == 11


def test_candidates_skips_a_row_with_no_name_at_all():
    nameless = Standing(entry=44, manager="", team_name="", rank=4)
    snapshot = LeagueSnapshot("fpl", 42, "x", (nameless,), ())
    assert candidates((snapshot,), ()) == ()


def test_choose_target_never_repeats_the_previous_target():
    target = choose_target((CLASSIC,), ROSTER, last_target="@rexmarlow", rand=_fixed(0))
    assert target.label == "@just_bergs"


def test_choose_target_repeats_when_there_is_only_one_manager():
    """A league of one cannot honour the alternation rule; repeating beats returning
    nothing, exactly as banter._pick_target decides for the static lines."""
    solo = LeagueSnapshot("fpl", 42, "x", (YURI,), ())
    target = choose_target((solo,), ROSTER, last_target="@just_bergs", rand=_fixed(0))
    assert target.label == "@just_bergs"


def test_choose_target_is_none_with_no_snapshots():
    assert choose_target((), ROSTER, None, _fixed(0)) is None


def test_target_key_is_the_handle_when_linked_and_the_label_otherwise():
    assert Target("@a", "@a", "T", 1, "fpl").key == "@a"
    assert Target("Ada Lovelace", None, "T", 1, "fpl").key == "Ada Lovelace"


# ------------------------------------------------------------------------- the prompt


def _target(label="@just_bergs", entry=11):
    return Target(label=label, handle=label, team_name="Just Bergs", entry=entry, game="fpl")


def test_describe_puts_the_targets_own_latest_move_under_their_name():
    text = describe((CLASSIC,), _target())
    assert "@just_bergs's latest moves" in text
    assert "GW2 transfer: in Haaland, out Watkins" in text


def test_describe_marks_a_rejected_waiver_as_rejected():
    """Losing a waiver is the most mockable thing in the payload; the model cannot use
    it unless the prompt says it failed."""
    text = describe((DRAFT,), _target())
    assert "in Isak, out Wood -- REJECTED, lost the waiver" in text


def test_describe_separates_other_managers_moves():
    text = describe((CLASSIC,), _target())
    assert "Other managers' latest moves:" in text
    assert "Rex Marlow: GW2 transfer: in Salah, out Saka" in text


def test_describe_says_so_when_the_target_has_not_moved():
    quiet = LeagueSnapshot("fpl", 42, "The Office", (ROMAN, YURI), ())
    assert "has made no transfers or claims here yet" in describe((quiet,), _target())


def test_describe_carries_both_games_scoring_terms():
    text = describe((CLASSIC, DRAFT), _target())
    assert "41 pts total, 41 this GW" in text          # classic
    assert "W-D-L 1-0-0, 3 league pts, 55 scored" in text  # draft


def test_describe_labels_a_classic_scoring_draft_league_as_such():
    """Calling it head-to-head invites a joke about a fixture that was never played."""
    text = describe((DRAFT_CLASSIC,), _target())
    assert "no head-to-head" in text
    assert "43 pts total, 43 this GW" in text


def test_describe_still_labels_a_real_h2h_draft_league_head_to_head():
    assert "Draft PL (head-to-head)" in describe((DRAFT,), _target())


def test_build_messages_names_the_target_and_bans_the_off_limits_material():
    messages = build_messages((CLASSIC,), _target(), NOW)
    system, user = messages[0]["content"], messages[1]["content"]
    assert messages[0]["role"] == "system" and messages[1]["role"] == "user"
    assert "MUST be about that move" in system
    assert "Never their race" in system
    assert "hair" in system  # appearance ban, spelled out: a model reached for "bald"
    # The mention is the whole point of NOTIFIER_BANTER_ALIASES, so the label being in
    # the line is a rule, not a hope.
    assert "must contain that @handle" in system
    assert "Target: @just_bergs" in user
    assert "2026-08-27" in user
    assert "in Haaland, out Watkins" in user


# -------------------------------------------------------------------- output cleanup


@pytest.mark.parametrize("raw,expected", [
    ("A line.", "A line."),
    ('  "A quoted line."  ', "A quoted line."),
    ("Here's one: A line.", "A line."),
    ("Banter: A line.", "A line."),
    ("First line.\nSecond line.", "First line."),
    ("*A bolded line.*", "A bolded line."),
    ("A line\r\nwith a carriage return.", "A line"),
])
def test_clean_line_flattens_what_models_actually_return(raw, expected):
    assert clean_line(raw) == expected


def test_clean_line_refuses_an_empty_completion():
    with pytest.raises(BanterError):
        clean_line("   \n  ")


def test_clean_line_truncates_at_a_sentence_end_where_there_is_one():
    long = ("Sentence one is quite long and goes on for a while here. " * 4) + "Tail."
    cleaned = clean_line(long)
    assert len(cleaned) <= MAX_LENGTH
    assert cleaned.endswith(".")
    assert "..." not in cleaned


def test_clean_line_ellipsises_when_there_is_no_sentence_break():
    cleaned = clean_line("word " * 100)
    assert len(cleaned) <= MAX_LENGTH + 3
    assert cleaned.endswith("...")


# ---------------------------------------------------------------------- the cooldown


def test_ai_allowed_permits_the_first_line():
    assert ai_allowed(None, NOW) is True


def test_ai_allowed_blocks_inside_the_window():
    assert ai_allowed(NOW - COOLDOWN + datetime.timedelta(seconds=1), NOW) is False


def test_ai_allowed_permits_once_the_window_has_passed():
    assert ai_allowed(NOW - COOLDOWN, NOW) is True


def test_ai_allowed_treats_a_future_stamp_as_just_now():
    """A clock stepped backwards or a hand-edited state file must not read as licence
    to spend."""
    assert ai_allowed(NOW + datetime.timedelta(hours=1), NOW) is False


# ------------------------------------------------------------------------- the call


def _reply(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


@respx.mock
def test_generate_line_sends_the_key_and_model_and_returns_the_line():
    route = respx.post(OPENROUTER_URL).mock(return_value=_reply("Haaland for Watkins. Bold."))
    with httpx.Client() as client:
        line = generate_line(client, "sk-test", "anthropic/claude-sonnet-5", (CLASSIC,), _target(), NOW)
    assert line == "Haaland for Watkins. Bold."
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer sk-test"
    body = request.read().decode()
    assert '"model":"anthropic/claude-sonnet-5"' in body.replace(" ", "")
    assert "Haaland" in body


@respx.mock
def test_generate_line_asks_for_no_reasoning_and_room_for_the_line():
    """A reasoning model bills its thinking against max_tokens before writing anything;
    with reasoning off and headroom, a one-line answer cannot be starved."""
    route = respx.post(OPENROUTER_URL).mock(return_value=_reply("A line."))
    with httpx.Client() as client:
        generate_line(client, "sk-test", "deepseek/deepseek-v4-pro-0813", (CLASSIC,), _target(), NOW)
    body = json.loads(route.calls[0].request.read())
    assert body["reasoning"] == {"enabled": False}
    assert body["max_tokens"] == MAX_TOKENS >= 300


@respx.mock
def test_generate_line_names_the_finish_reason_when_the_content_is_null():
    """The reasoning-model failure: budget spent thinking, content null. The fix is a
    setting, so the error has to carry enough to tell that from a transport failure."""
    respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": None}, "finish_reason": "stop"}],
        "usage": {"completion_tokens_details": {"reasoning_tokens": 756}},
    }))
    with httpx.Client() as client, pytest.raises(BanterError) as excinfo:
        generate_line(client, "sk-test", "m", (CLASSIC,), _target(), NOW)
    assert "reasoning_tokens" in str(excinfo.value)


@respx.mock
def test_generate_line_raises_banter_error_on_a_non_200():
    respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(402, json={"error": "no credit"}))
    with httpx.Client() as client, pytest.raises(BanterError):
        generate_line(client, "sk-test", "m", (CLASSIC,), _target(), NOW)


@respx.mock
def test_generate_line_raises_banter_error_on_a_transport_failure():
    respx.post(OPENROUTER_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    with httpx.Client() as client, pytest.raises(BanterError):
        generate_line(client, "sk-test", "m", (CLASSIC,), _target(), NOW)


@respx.mock
def test_generate_line_raises_banter_error_on_a_body_it_cannot_read():
    respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json={"choices": []}))
    with httpx.Client() as client, pytest.raises(BanterError):
        generate_line(client, "sk-test", "m", (CLASSIC,), _target(), NOW)


@respx.mock
def test_generate_line_raises_banter_error_when_the_content_is_not_text():
    respx.post(OPENROUTER_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": None}}]})
    )
    with httpx.Client() as client, pytest.raises(BanterError):
        generate_line(client, "sk-test", "m", (CLASSIC,), _target(), NOW)
