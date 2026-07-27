"""
Shared vocabulary for "how fresh/real is this data" across the whole site.

This grew out of a series of one-off fixes (the roster/player/admin pages
going blank when the Clash Royale API hiccups, the War Fame leaderboard
showing a wall of zeroes between war days) that each invented their own
ad hoc boolean (`is_stale`, `is_last_known_data`, `is_last_race`, ...) to
describe a "this isn't quite live data" situation. Those situations are not
all the same thing, and conflating them makes it easy to slap the wrong
label on a banner. There are exactly three states any piece of data on this
site can be in:

CURRENT
    Fetched live, right now, from the Clash Royale API. This is the ground
    truth for "what does this look like at this exact moment." No banner
    needed -- this is the default, expected case.

MOST_RECENT
    The live fetch for this *exact* value failed (API down, timeout,
    rate-limited) or the value is intrinsically local-only right now (e.g.
    the harvester hasn't caught up yet), so we're showing the last
    successfully cached copy of that same value instead of nothing or an
    error. Semantically this is meant to represent "right now" -- it's just
    possibly a few minutes to a few hours stale. Sources: the `config`
    collection's `last_known_api::<endpoint>` cache, `player_profiles`,
    `war_tracking`, or `war_history`'s latest row when it's standing in for
    a failed *"most recent race"* query (not a different query).
    UI language: "last known" / "showing last known data".

HISTORICAL
    Not a failed-fetch fallback at all -- the live call succeeded, but the
    current value is legitimately empty/degenerate (e.g. war fame resets to
    0 for everyone between war days, because no race is active right now).
    Rather than show a misleading wall of zeroes, we substitute a real,
    different, past record (the last *completed* war from `war_history`)
    in its place. This is a cross-time-period substitution, not a same-value
    cache, and should never be labelled the same way as MOST_RECENT.
    UI language: "last race" / "last completed war".

Rule of thumb for a new route: if you're about to add another one-off
"was this stale/cached/old" boolean, ask which of these three situations
you're actually in and use these constants instead so the next person (or
the same person in six months) doesn't have to reverse-engineer it from the
variable name.
"""

CURRENT = "current"
MOST_RECENT = "most_recent"
HISTORICAL = "historical"

ALL_STATES = (CURRENT, MOST_RECENT, HISTORICAL)

# Human-readable copy for each state, for the handful of spots that want a
# ready-made label instead of branching on the raw string themselves.
LABELS = {
    CURRENT: None,  # no banner needed -- this is the expected, unremarkable case
    MOST_RECENT: "last known",
    HISTORICAL: "last race",
}

BANNER_TEXT = {
    CURRENT: None,
    MOST_RECENT: "Couldn't reach the Clash Royale API just now — showing the last known data instead of live stats.",
    HISTORICAL: "No active war right now — showing results from the last completed war instead.",
}


def is_current(freshness):
    """True only for the plain, expected, no-banner-needed case."""
    return freshness == CURRENT
