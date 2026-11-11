# Robustness audit: every input path

DESIGN goal 4 and §5. For each way bytes or requests reach the server: the bound, what happens on a
violation, and the test that shows it. Values live in `src/opencs/policy.py` (named there with their
reasons). "Counted" means only the connection's windowed bad-input count (below); what the city adds up in its statistics is "tallied".

Tests are `tests/unit/...` unless a directory is given; names are test functions or modules.

## Accounting: what is counted, and when a connection is closed

Every bucket and counter below belongs to the connection, not the phase (`session/budgets.py`, ADR 9): leaving the Chamber, re-entering it or starting another game never refills one. One rule per phase, no lifetime totals (a noisy modem must not be kicked for a few bad checksums a
minute; I §4.1 says a frame that does not decode is dropped, never a reason to close).

| phase | counted as bad | window and limit | on excess |
|---|---|---|---|
| login | a line over `MAX_LINE_BYTES`; more than `LOGIN_MAX_LINES` lines; more than `LOGIN_PREAMBLE_LIMIT` bytes before `HHH` | per connection, lifetime (the phase lasts seconds) | close |
| Chamber | a line past the token bucket, an overlong line included (`CHAMBER_BURST_LINES`, `CHAMBER_LINES_PER_SECOND`): dropped, one warning | `CHAMBER_FLOOD_DROPS` drops in `CHAMBER_FLOOD_WINDOW` s | close |
| in game | a frame failing LEN, CK or its checksum; a frame over the frame bucket (`FRAME_LIMIT`) | `MAX_BAD_INPUT` in `BAD_INPUT_WINDOW` s (`WindowedCounter`) | close ("too much bad input") |

A relayed message over its kind's bucket (`RELAY_LIMIT_*`) is dropped and tallied in the session
(`relay_drops`), **not** counted: an honest line that stalls for just under `SILENCE_SECONDS`
delivers its backlog at once and can exceed a budget, and the frame bucket already catches a flood.
A request the rules reject (an order for a module nobody stocks, a hit on an id naming nothing, a
shot of an unsafe KIND) is **not** counted: an honest client can send those (a stale view, a race),
the cost is bounded by the buckets above, and each is logged at debug or warned once
(`world/hits.py`). The choice of a windowed count over a lifetime one is recorded here; the numbers are
in policy.

## Network and login

A per-address limit keys a connection by its source: an IPv4 address (an IPv4-mapped IPv6 one as
that address), or the IPv6 /64 the address is in (`SOURCE_PREFIX_V6`, `net.listener.source_of`), since
a host is given a whole /64 and can use any address in it. That covers the connect rate, connections
not logged in, password checks, new accounts, wrong passwords per account and source (and the sources
an owner is known by), guests, chat and held `/RESUME` slots. Two exceptions: connections at once per
address count each full address, with the per-subnet limit (also a /64 for IPv6) above them; and the
refusal log is kept per full address. Log lines show the full address. Tests:
`net/test_listener::test_a_source_is_the_ipv4_address_or_the_ipv6_slash_64`,
`test_addresses_in_one_ipv6_slash_64_share_one_connect_rate`,
`test_two_ipv4_addresses_each_have_their_own_connect_rate`,
`test_connections_not_logged_in_are_counted_per_source`,
`session/test_login::test_guests_from_one_ipv6_slash_64_count_as_from_one_address`,
`session/test_loginlimits::test_addresses_in_one_ipv6_slash_64_share_one_allowance_of_password_checks`,
`session/test_loginlimits::test_the_owner_s_known_source_is_its_ipv6_slash_64_not_one_address`,
`session/test_chamber::test_chat_from_one_ipv6_slash_64_shares_one_budget`,
`session/test_line_drop_session::test_drops_from_one_ipv6_slash_64_hold_as_from_one_address`.

| input | bound | on violation | test |
|---|---|---|---|
| TCP accept, total | `MAX_CONNECTIONS` | refused at once | `net/test_listener::test_total_connection_limit_refuses_the_extra_one_at_once` |
| TCP accept rate, per source | `CONNECT_RATE_PER_ADDRESS` (every attempt spends, refused or not) | closed before a session is made | `net/test_listener::test_an_address_that_connects_too_often_is_refused_until_its_rate_refills` |
| log lines from refusals | one per address per `REFUSAL_LOG_INTERVAL`, the rest summed up in the next (`RefusalLog`, bounded by `MAX_TRACKED_SOURCES`); accepts log at DEBUG, logins at INFO with the address | held back and counted | `net/test_listener::test_refusals_are_logged_once_a_minute_per_address_and_summed_up`, `test_the_refusal_log_remembers_a_bounded_number_of_addresses` |
| TCP accept, per address | `MAX_CONNECTIONS_PER_ADDRESS`; an IPv4-mapped IPv6 address counts as its IPv4 one | refused at once | `net/test_listener::test_per_address_limit`, `test_addresses_are_keyed_as_one_host_and_grouped_by_subnet` |
| TCP accept, per subnet | `MAX_CONNECTIONS_PER_SUBNET` (/24, /64: `SUBNET_PREFIX_*`) | refused at once | `net/test_listener::test_per_subnet_limit` |
| connections not logged in | `MAX_UNAUTHENTICATED_PER_ADDRESS` per source; `MAX_UNAUTHENTICATED` in all | per source: refused at once; in all: the oldest not logged in is cut to make room | `net/test_listener::test_an_address_may_have_only_so_many_connections_not_logged_in`, `test_past_the_total_not_logged_in_the_oldest_such_connection_is_cut` |
| held `/RESUME` slots, per source | `MAX_HELD_PER_ADDRESS` (< connections per address; seats per address = `MAX_SEATS_PER_ADDRESS`) | the drop holds nothing (a death still applies) | `session/test_line_drop_session::test_the_limit_is_per_address`, `test_policy_limits` |
| dead peers | TCP keepalive (`KEEPALIVE_*`) | the socket ends, the player is dropped | `net/test_listener::test_accepted_sockets_get_keepalive` |
| time to log in | `LOGIN_TIMEOUT` | close | `session/test_login` (timeout tests) |
| silence at the `*` prompt | `GENIE_IDLE_TIMEOUT` | close | `session/test_login::test_a_silent_star_prompt_times_out_much_later` |
| login preamble before `HHH` | `LOGIN_PREAMBLE_LIMIT` bytes | close | `session/test_login`; `fuzz/test_fuzz_net::test_noise_at_the_login_never_raises_and_stays_bounded` |
| login lines | `LOGIN_MAX_LINES` | close | `session/test_login::test_too_many_lines_close` |
| guests online (no password, so the cheapest seat) | `MAX_GUESTS_PER_ADDRESS`, `MAX_GUESTS` | refused with a line, close | `session/test_login::test_an_address_may_have_only_so_many_guests_online`, `test_the_server_has_only_so_many_guests_online` |
| login line length | `MAX_LINE_BYTES` (the line buffer never holds more) | close | `session/test_login::test_an_overlong_account_line_closes`, `fuzz/test_fuzz_net::test_a_login_line_without_a_terminator_is_not_buffered_for_ever` |
| account name / password length | `MAX_ACCOUNT_NAME_LENGTH`, `MAX_PASSWORD_LENGTH` | refused with a line | `lobby/test_identity::test_name_bounds`, `test_the_password_may_hold_commas_and_must_not_be_empty_or_huge` |
| password checks, per address and global | `LOGIN_ATTEMPTS_PER_ADDRESS`, `LOGIN_ATTEMPTS_GLOBAL`; password hashes (Argon2id, `ARGON2_*`) run on `HASH_WORKERS` threads (`app.ThreadHasher`), never on the loop, and the logon holds what arrives meanwhile (within `LOGIN_PREAMBLE_LIMIT`) | refused, close | `session/test_loginlimits`, `session/test_login::test_lines_sent_during_the_password_check_are_read_once_it_is_done`, `test_a_flood_during_the_password_check_closes`, `test_app::test_password_hashes_run_on_a_thread_and_report_back_on_the_loop` |
| wrong passwords for one account | `FAILED_LOGINS_PER_ACCOUNT_AND_SOURCE` for each source, spent as each check starts (so checks at once cannot all pass), given back by a right password or a refusal before hashing, and refilled by a right one; and `FAILED_LOGINS_PER_ACCOUNT` shared by every source the account has not logged in from since the server started (the latest `KNOWN_SOURCES_PER_ACCOUNT`, in memory only); refuses without hashing; `reset-password` forgets both | refused, close | `session/test_loginlimits::test_wrong_passwords_for_one_account_are_limited_whatever_the_address`, `test_a_guesser_cannot_lock_the_owner_out_where_the_owner_has_logged_in`, `test_one_source_s_wrong_passwords_spend_nothing_of_another_known_source`, `test_a_locked_account_opens_again_in_minutes`, `test_guesses_made_at_once_are_charged_before_any_is_hashed`, `test_a_right_password_gives_its_charge_back`, `test_a_check_the_address_may_not_make_charges_nothing_to_the_account`, `test_control::test_a_reset_password_opens_an_account_the_guessers_locked` |
| a check that finishes after the player left, or after a second login of the account | `Identity.complete` re-checks who is online; nothing is stored or admitted for a session that has closed | refused / ignored | `session/test_login::test_a_check_that_finishes_after_the_player_hung_up_does_nothing`, `test_a_registration_whose_player_hung_up_stores_nothing`, `test_two_logins_of_one_account_racing_through_the_check_admit_one` |
| new accounts, per address and global | `NEW_ACCOUNTS_PER_ADDRESS`, `NEW_ACCOUNTS_GLOBAL` | refused, close | `session/test_loginlimits` |
| limiter tables | `MAX_TRACKED_SOURCES` addresses (or accounts, for the sources each was logged in from) | oldest forgotten | `session/test_loginlimits::test_the_table_of_sources_is_bounded`, `test_the_table_of_known_sources_is_bounded` |
| new account names and passwords | 3-12 of `ACCOUNT_NAME_CHARACTERS`, not `RESERVED_ACCOUNT_NAMES` nor a guest name, also as read with look-alike digits taken for letters and `_` `-` dropped (`ADM1N`, `ADM-IN`: `LOOK_ALIKE_LETTERS`); passwords `MIN_PASSWORD_LENGTH`..`MAX_PASSWORD_LENGTH`, not in `COMMON_PASSWORDS` (compared in upper case) and not the account name (accounts made before keep working) | refused with a line, close | `lobby/test_identity::test_a_new_account_must_have_a_plain_name_and_a_real_password`, `test_a_common_password_is_refused_whatever_its_case`, `test_a_password_equal_to_the_account_name_is_refused`, `test_an_account_made_before_the_rules_still_logs_in` |
| accounts stored | `MAX_ACCOUNTS`; unplayed ones (no progress, no login) deleted after `UNPLAYED_ACCOUNT_DAYS`, checked every `ACCOUNT_PRUNE_INTERVAL`; `registration = "closed"` turns first-login registration off | registration refused / account removed | `lobby/test_identity::test_past_the_cap_no_account_is_registered_but_existing_ones_log_in`, `test_unplayed_accounts_are_pruned_but_not_played_or_online_ones`, `test_with_registration_closed_an_unknown_name_is_refused`, `test_app::test_unplayed_accounts_are_pruned_at_the_interval_and_played_ones_kept` |

## Chamber

| input | bound | on violation | test |
|---|---|---|---|
| line length | `MAX_LINE_BYTES` (client armed buffer 512, I §5.2) | line discarded and charged to the line bucket below (so a stream of them closes the connection); within the budget, "too long" | `session/test_chamber::test_an_overlong_line_is_refused_not_relayed`, `test_a_stream_of_overlong_lines_is_a_flood_and_closes_the_connection`, `wire/test_text` |
| line rate | token bucket, then `CHAMBER_FLOOD_*` | dropped with one warning; close on a sustained flood | `session/test_chamber::test_a_burst_of_chat_is_cut_to_the_budget_and_the_sender_is_warned_once`, `test_sustained_flooding_closes_the_connection`, `test_honest_pace_is_never_limited` |
| chat, per source | `CHAT_PER_ADDRESS`, shared by every connection of the source, in the Chamber and in game | dropped (the Chamber warns once), not counted | `session/test_chamber::test_chat_from_one_address_shares_one_budget_over_its_connections`, `session/test_ingame::test_chat_from_one_address_shares_one_budget_over_its_connections` |
| chat to a client that reads slowly | over `DROPPABLE_WATERMARK` queued items, chat (`Event.droppable`, `Member.congested`) is skipped for that client; game state never is | skipped and tallied (`Lobby.chat_dropped`, `Router.dropped`), so many senders within their budgets cannot overflow a 9600-baud reader's outbox and cut it off | `session/test_chamber::test_a_listener_that_has_stopped_reading_is_skipped_not_cut_off`, `lobby/test_lobby::test_chat_skips_a_member_whose_connection_is_too_far_behind`, `test_routing::test_a_droppable_event_skips_a_congested_player_and_nothing_else_does`, `net/test_connection::test_the_backlog_is_what_is_queued_and_not_yet_written` |
| chat text | `CHAT_MAX_CHARS`; bytes outside `0x20..0x7e` stripped | truncated, sanitized | `lobby/test_commands`, `wire/test_text` |
| leaving and re-entering (`/QUIT`, `M 1380;3`) | `CHAMBER_REENTRY_LIMIT` trips; the line budget is the session's, so a trip does not refill it | close | `session/test_chamber::test_leaving_and_reentering_the_chamber_does_not_refill_the_line_budget`, `test_a_flood_spread_over_trips_to_the_star_prompt_still_closes`, `test_leaving_the_chamber_too_often_closes_the_connection`, `test_a_player_who_leaves_the_chamber_now_and_then_is_never_closed` |
| idle | `CHAMBER_IDLE_TIMEOUT` (an hour), `GUEST_CHAMBER_IDLE_TIMEOUT` (15 min) for a guest; only a non-empty line restarts it, not bytes the reader drops | told, closed; the account is freed | `session/test_chamber::test_a_silent_chamber_connection_is_closed_after_the_idle_timeout`, `test_a_silent_guest_is_closed_sooner`, `test_bytes_that_make_no_line_do_not_restart_the_idle_clock`, `test_a_stale_chamber_connection_frees_its_account` |
| launch (`/GO`) never arrives | `GO_WAIT` | back to the lobby | `session/test_chamber` (launch tests) |
| arbitrary bytes | all of the above | no exception, buffers bounded | `fuzz/test_fuzz_net::test_noise_in_the_chamber_never_raises_and_stays_bounded` |

## In game: frames

| input | bound | on violation | test |
|---|---|---|---|
| bad CK / ~CK / LEN, payload checksum | the reader skips and counts; buffer at most `MAX_PENDING` | counted | `wire/test_frame`, `fuzz/test_fuzz_net::test_damaged_frames_in_game_never_raise_and_are_counted` |
| a header that never completes | `FRAME_STALL_SECONDS` | given up, counted | `session/test_ingame::test_a_growing_partial_frame_is_still_given_up_when_the_stall_began_long_ago`, `fuzz/test_fuzz_net::test_a_partial_frame_that_never_completes_cannot_hold_the_buffer_past_its_bound` |
| undecodable payload, unknown type or sub | `DecodeError` only | dropped and tallied (`decode_drops`), not counted (I §4.1) | `session/test_ingame`, `messages/test_base` |
| frames per second | `FRAME_LIMIT` (200 burst, 100/s; the captured clients send at most 15 frames in any second and 123 in any 10 s, and I §3's ~50/s is what the client *reads*, not what it sends) | dropped undecoded, counted | `session/test_ingame_session::test_frames_past_the_burst_are_dropped_and_counted_as_bad_input`, `test_a_flood_of_frames_closes_the_connection`, `test_a_rate_far_above_the_real_clients_send_rate_is_never_limited` |
| sustained garbage | `MAX_BAD_INPUT` per `BAD_INPUT_WINDOW` | close | `fuzz/test_fuzz_net::test_a_sustained_flood_of_garbage_in_game_closes_the_connection` |
| a noisy line | a few bad frames a minute | never closes | `fuzz/test_fuzz_net::test_a_trickle_of_damaged_frames_does_not_close_an_honest_connection`, `session/test_ingame::test_undecodable_real_client_b_frames_at_one_a_second_do_not_close` |
| silence | `SILENCE_SECONDS` after the first frame | taken as a line drop (`/RESUME` rules) | `session/test_line_drop_session::test_silence_for_15_seconds_after_a_frame_is_a_line_drop` |
| quit handshake | `QUIT_WAIT`, `QUIT_SECOND_WAIT`, `RETURN_SETTLE` | close | `session/test_ingame_session` (quit tests) |

## In game: what the world does with a client's fields

| input | bound | on violation | test |
|---|---|---|---|
| relayed kinds (`'b'`, `'c'`, `'I'`, chat, Grenade, the bare `'J'` and `'A'` of a death and its drop) | `RELAY_LIMIT_*` buckets; a kind the city is handed without one is listed with its reason (`session/test_ingame::UNBUDGETED`) | dropped and tallied (`relay_drops`, not bad input); a dropped shot is still logged for attribution, a dropped order is answered | `session/test_ingame` (budget tests), `session/test_ingame_session::test_alternating_deaths_and_drop_requests_reach_the_others_only_within_the_budget`, `session/test_ingame_session::test_a_shot_over_the_budget_is_not_relayed_but_still_reaches_the_shot_log`, `test_an_honest_stall_of_just_under_the_silence_limit_then_its_backlog_never_closes` |
| orders, pickups, ejects, effects | `RELAY_LIMIT_ORDER`, `_MODULE_OPS`, `_EFFECT`; queues `MAX_WAITING_*` | refused or dropped | `world/test_orders`, `world/test_module_orders`, `world/test_effects` |
| ids that name nothing, ids of the wrong kind, absurd positions and values | every handler ignores or logs; none raises | ignored (debug log) or tallied in the city's statistics (`unsafe_shots` and friends; not bad input) | `session/test_ingame_session::test_module_and_effect_messages_that_name_nothing_never_raise_out_of_the_session`, `fuzz/test_fuzz_session` (every A.2 type with random fields, ids that name something live or nothing, huge coordinates) |
| unsafe shot KIND; a pod state other than 0 or 1 | a shot of an unsafe KIND is never relayed; a state is clamped to 0 or 1 (`relay.pod_state`) and relayed | the shot is dropped and tallied in the city's statistics (`unsafe_shots`), not bad input; the state is clamped, nothing tallied | `world/test_shots`, `fuzz/test_fuzz_session` (`ClientModel`) |
| a client's report about a leg it was not sent | `owed` counts per client | ignored | `world/test_guppy_flight`, `world/test_munchers` |
| chat bytes | stripped to printable | sanitized | `world/test_chat` |

## Memory per connection and per city

| state | bound | test |
|---|---|---|
| inbound line buffer | `MAX_LINE_BYTES` | `fuzz/test_fuzz_net` |
| inbound frame buffer | `MAX_PENDING` (238 bytes) | `fuzz/test_fuzz_net` |
| outbox | `OUTBOX_LIMIT` items, then that connection is aborted; the others are not delayed. Chat stops being queued at `DROPPABLE_WATERMARK` | `net/test_connection::test_overflow_at_exactly_limit_plus_one`, `test_outbox_isolation` (real sockets) |
| outbox, a pod's reports | one `'b'` a pod: a newer one replaces the queued one where it sits, up to a queued change in that pod's life; nothing else is replaced | `net/test_coalescing::test_fifteen_moving_pods_over_a_real_socket_stay_fresh_and_are_not_cut`, `test_a_report_after_a_barrier_stays_behind_it` |
| bytes a client in a game has not acknowledged | `CLIENT_UNACKED_BYTES`, under half of its 4096-byte receive ring, which loses bytes when full; then the pump waits for acks, for at most `ACK_STALL_SECONDS`, as the frame awaited may be lost | `net/test_flow::test_the_pump_never_has_more_than_the_limit_unacknowledged_while_the_client_acks`, `test_acks_that_stop_coming_do_not_stop_the_pump_for_ever` |
| tags in one acknowledgement frame | the last `MAX_ACK_TAGS` read (the client sends at most 11, a frame holds 233); a tag matching nothing outstanding costs one look-up, not a search of the window. Not budgeted: dropping an honest ack would stall the pump | `net/test_flow::test_a_frame_of_unknown_tags_costs_no_search_of_a_full_window`, `test_only_the_last_tags_of_a_frame_are_read` |
| shots to a client that is behind | past `SHOTS_SHED_FAR_BACKLOG` items those from beyond `SHOT_KEEP_FEET` are not queued, past `SHOTS_SHED_ALL_BACKLOG` none is; tallied (`Router.shots_shed`). The shooter's client reports the hit, so damage still lands | `test_routing::test_a_player_behind_is_not_sent_far_shots_but_is_sent_near_ones`, `test_outbox_isolation::test_shots_at_a_client_that_never_reads_are_shed_and_do_not_cut_it` |
| bytes written to a connection | `CLIENT_BYTES_PER_SECOND` (`[limits] client_bytes_per_second`), so a backlog stays in the outbox and these limits see it; a close is not paced | `net/test_pacing::test_while_the_pump_holds_back_the_rest_is_a_backlog_the_server_can_see`, `test_a_close_is_not_paced_so_a_long_backlog_still_says_goodbye` |
| shot log | `SHOT_LOG_MAX` | `world/test_combat`; ceiling in `fuzz/test_fuzz_session::test_a_long_hostile_run_leaves_each_players_records_bounded` |
| hit set, loadout and score ledgers, attack history | aged out or fixed size | same ceiling test |
| a client's view | `ever_held` is bounded by the id ranges; a relayed shot adds its shooter only when that is the sender's pod or a known NPC, so a client naming other ids marks none as held | `longrun/test_city_48h`, `world/test_shots::test_a_shooter_id_that_is_not_the_senders_marks_nothing_as_held` |
| player ids held by long-connected clients | a pilot who stays through every id's introduction holds the 240-id range (I §6.2: one byte, a registry that never unlinks); inherent in II §8 | the join is refused ("NO FREE PLAYER ID") until one leaves; a warning names the longest-connected players | `world/test_seating::test_a_join_refused_for_want_of_a_player_id_is_logged_with_who_holds_the_range` |
| dropped modules, mines, called Sentinels, Munchers | `MAX_DROPS`, `MAX_MINES`, `MAX_CALLED_SENTINELS`, `MAX_MUNCHERS` | `world/test_drops`, `test_effects`, `test_sentinels`, `test_munchers` |
| waiting guppy jobs | `MAX_WAITING_JOBS`, `MAX_WAITING_TOWER_ORDERS` | `world/test_orders`, `test_module_orders` |
| released-id pool | at most the range's size; double releases ignored | `world/test_ids` |
| held `/RESUME` records | one per identity, expiring | `session/test_held` |
| everything the server remembers over two simulated days | ceiling, and no growth across resets | `longrun/test_city_48h` |

## No exception escapes

| entry point | containment | test |
|---|---|---|
| `Session.feed`, `tick`, `closed` | `Session._guard`: log with context, close that connection | `session/test_session`, `test_containment::test_a_session_handler_that_raises_closes_only_its_own_connection` |
| `Connection` pumps | `Connection._guard`: abort that connection | `net/test_connection::test_a_handler_that_raises_closes_this_connection_cleanly` |
| a city call from a session | `InGamePhase._to_city`: logged with the request, dropped | `session/test_ingame_session::test_an_exception_in_the_city_drops_the_request_and_keeps_the_connection`, `test_containment::test_a_city_bug_on_one_players_message_leaves_the_other_player_and_the_city_running` |
| join, leave, line drop | caught; the player is gone either way, and after a failed line drop the others are still told and the city saved, each step guarded on its own; a `/RESUME` whose join fails or is refused holds the game again, for `TYPE_SECONDS` from then | `session/test_ingame_session` (join and leave tests), `session/test_line_drop_session`, `session/test_ingame_session::test_a_line_drop_that_raises_marks_the_city_and_still_removes_the_player`, `session/test_chamber_resume::test_a_resume_whose_join_raises_keeps_the_game_held_for_another_try`, `test_a_resume_whose_join_is_refused_keeps_the_game_held_for_another_try` |
| the city ticker | `cities.run_city`: a failed tick or a failure in the work after it is logged and counted as a city failure, and the ticker goes on | `test_app::test_the_ticker_notes_a_slow_tick_and_a_failed_one`, `test_a_failure_after_a_tick_is_noted_and_the_ticker_goes_on`, `test_cities` |
| a long-lived task (a city ticker, the daily prune, a shutdown countdown) ending anyway | `tasks.watch`: logged when it ends | `test_app::test_a_city_ticker_that_ends_with_an_exception_is_logged` |
| a city left inconsistent by any of those city calls | counted in `session.cityhealth`, rebuilt from its configuration when next empty, never while played | `test_cities::test_a_city_that_failed_is_kept_while_played_and_rebuilt_once_empty`, `test_control::test_status_shows_a_city_that_has_failed_and_whether_a_rebuild_waits` |
| delivery to a sink | `Router.deliver` | `test_routing`, `test_containment::test_a_sink_that_raises_does_not_stop_delivery_to_the_rest` |
| server messages | constructors refuse crash-prone values (`'c'` KIND, `'R'` ranges, `'T'` classes, chat bytes) | `messages/test_server`; every message the 48-hour run emits is encoded into a frame (`longrun/test_city_48h`) |

## Operations (M12)

| input / event | bound | on violation | test |
|---|---|---|---|
| admin connection | loopback peers only; bound to `127.0.0.1`; `ADMIN_MAX_CONNECTIONS` | closed at once | `test_admin::test_it_listens_on_loopback_only`, `test_only_a_loopback_peer_is_served`, `test_more_connections_than_the_limit_are_closed_at_once` |
| admin token | at least `ADMIN_TOKEN_MIN_LENGTH`; constant-time compare; `ADMIN_AUTH_TIMEOUT`; `ADMIN_AUTH_LIMIT` attempts over all connections; never logged | denied and closed; no command is run | `test_admin::test_a_wrong_token_is_denied_and_closed`, `test_token_attempts_are_metered_across_connections`, `test_no_token_in_time_closes_without_a_reply`, `test_the_token_never_reaches_a_log_record` |
| the admin token written in a `server.toml` that other users can read | a warning at start naming the file and its mode, not the token; the token still works | logged once per start | `test_admin::test_a_readable_file_holding_the_token_is_warned_about`, `test_the_server_warns_at_start_about_a_readable_file_with_the_token` |
| no token configured for an admin port | the server does not start | exit code 2 | `test_admin::test_a_server_whose_admin_port_has_no_token_refuses_to_start`, `test_no_usable_token_means_the_channel_does_not_start` |
| a local user exhausting the admin channel | accepted trade-off: one shared token-attempt bucket and four slots taken before authentication; see OPERATING.md "The admin channel" | the operator is refused for a while; the game is unaffected | none (a decision, not a bound) |
| admin line length, rate, silence | `ADMIN_MAX_LINE`, `ADMIN_COMMAND_LIMIT`, `ADMIN_IDLE_TIMEOUT` | closed | `test_admin::test_an_overlong_line_closes_before_and_after_the_token`, `test_commands_over_the_rate_close_the_connection`, `test_a_silent_operator_is_dropped_after_the_idle_timeout` |
| operator input | every command answers in text and never raises | an error line | `test_control::test_no_operator_input_raises` |
| a password the operator makes (`create-account`, `reset-password`) | shown once in the reply, never logged; account changes refused while the player is online or held | refused with a line | `test_control::test_the_operator_creates_an_account_and_its_password_is_shown_once_never_logged`, `test_the_operator_resets_a_password`, `test_progress_and_accounts_are_wiped_only_while_the_player_is_offline` |
| a callsign or text in an admin reply | printable ASCII only | other bytes shown as `?` | `test_admin::test_replies_cannot_carry_control_characters` |
| announce text | printable ASCII, `MAX_CHAT_BYTES` | made printable and cut | `test_control::test_announce_text_is_made_printable_and_cut_to_what_the_client_takes` |
| shutdown | `SHUTDOWN_DEADLINE`, then connections are aborted | aborted | `net/test_listener::test_stop_aborts_what_outlasts_the_deadline_and_returns`, `test_shutdown::test_a_player_who_never_reads_cannot_hold_the_shutdown_and_is_still_saved` |
| a shutdown or a kick of a player in a fight | not a death, nothing held | clean leave | `session/test_removal::test_a_kick_under_fire_is_not_a_death_and_holds_nothing` |
| wire capture | `max_bytes` per file and `CAPTURE_DIR_MAX_BYTES` for the directory (oldest of our files removed first); never the logon; a write error ends the capture only | stops with a note | `net/test_capture::test_a_capture_stops_at_its_cap_with_a_note`, `test_the_logon_is_never_recorded`, `test_an_unwritable_directory_ends_the_capture_not_the_connection`, `test_the_oldest_files_go_when_the_directory_would_pass_its_cap` |
| the database file (password hashes) | the data directory is made 0700 and `opencs.db` 0600 (`DATA_DIR_MODE`, `DATABASE_FILE_MODE`); the unit's `UMask=0077` covers the WAL files and logs | a warning if it cannot be made private | `store/test_stores::test_the_data_directory_and_database_are_private_to_the_server` |
| an invalid city file at reload | validated whole first | nothing changes | `test_cities::test_an_invalid_file_changes_nothing_not_even_the_valid_parts` |

## Threat model

`tests/e2e/test_abuse.py` plays the main abuses against a real server on loopback: a chat flood spread
over trips through the `*` prompt, chat aimed past a client that reads slowly, a guess storm on one
account (and that the event loop keeps running while the hashes do), a reconnect loop, and guests from
one address.

The game port is public, and anything may connect to it and send anything. The tables above bound what
one connection, one address, one subnet, one account and the whole server can make the server do. This
section lists what those bounds do **not** cover, and why.

| risk | why it stays | what limits it |
|---|---|---|
| passwords in plain text on the wire | the DOS client speaks the protocol over a modem bridge and cannot do TLS | players are told to use a password of their own (PLAYING.md, OPERATING.md); only salted hashes are stored |
| cheating: scores, bays, hits and positions come from the client | the client runs the game and reports it (I §9); the server cannot recompute it | the server only notices: counters in the city's statistics (`shooter_mismatches`, `unsafe_shots`, `hits_without_a_shot` and `distant_hits`, the last two for a hit report with no shot before it and for one from farther than `HIT_PLAUSIBLE_RANGE`) and a warning once per player. A counted report is still applied, so the counters do not limit damage. `owed` legs and `reset-progress` for the operator |
| account names can be probed (an unknown name registers, a known one says WRONG PASSWORD) | the logon has no other way to tell a new player from a returning one (II §3) | the per-address, per-account and global login limits; `registration = "closed"` |
| a botnet with many /24s | per-address and per-subnet limits only go so far against many sources | total caps (`max_connections`, `max_unauthenticated`, `max_guests`, `LOGIN_ATTEMPTS_GLOBAL`), eviction of the oldest connection not logged in, and the firewall in front |
| a volumetric flood (bandwidth, SYN) | it never reaches the process | the hosting provider; the kernel's SYN cookies; the firewall rate limit (OPERATING.md, "Checklist: a public server") |
| a local user on the host and the admin channel | loopback only, by design | the token, its attempt limit; see "Operations" above |
| locking an account with wrong passwords | a per-account limit is what stops distributed guessing | it binds only sources the owner has not logged in from since the server started, so the owner's usual address stays open; from a new address, or after a restart during an attack, the owner is refused until the guessing stops (it refills in minutes, `FAILED_LOGINS_PER_ACCOUNT`); `reset-password` clears it |
