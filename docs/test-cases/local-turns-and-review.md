# Test cases: local turns, the hand-back, and review

The features: what follows a turn the injection or harm check keeps on this Mac (`src/keepmum/handback.py`, `Proxy._hand_back`, `_hand_back_harm`), reviewing a hand-back before it goes (`src/keepmum/review.py`, the panel and the turn window in the Mac app), and the instructions and skills the local model gets for each step (`local.PLACES`).

Automated cases go through a real `Proxy` with a scripted check and local server: `tests/test_review.py`, `tests/test_turn_review.py`, `tests/test_reuse.py`, `tests/test_guard_scenarios.py`, `tests/test_handback.py`, `tests/test_logstore.py`. The live ones run real agents: `e2e/cases/local-*`, `harm-*`, `both-*` (`.venv/bin/python -m e2e run local- harm- both-`, with a local model server: `KEEPMUM_E2E_LOCAL_URL`, `KEEPMUM_E2E_LOCAL_MODEL`).

## 1. Both checks in one turn

| # | Case | Expected | Test |
|---|---|---|---|
| 1.1 | What you typed and a tool result both flagged, both checks local | Both score before keepmum decides; what was typed is handed back first, then the result; the turn goes once both are replaced | `test_both_checks_each_hand_back_their_part_and_the_turn_goes` |
| 1.2 | Either check in block mode | Refused, with both reasons; nothing sent | `test_one_check_in_block_mode_refuses_with_both_rows` |
| 1.3 | The harm rewrite stays local | The whole turn stays local; the tool result is not asked about | `test_both_checks_reviewed_what_was_typed_first_kept_local_asks_nothing_more` |
| 1.4 | Live, every agent, Qwen3.6 35B | Both parts replaced, nothing flagged reaches the provider | `e2e both-01-injection-and-harm-rewritten` |

## 2. The harm rewrite

| # | Case | Expected | Test |
|---|---|---|---|
| 2.1 | The local model takes the flagged part out | Goes to the cloud after keepmum's note | `test_approved_it_goes_as_the_local_model_wrote_it` and the e2e `harm-03` |
| 2.2 | It took out the wrong part (kept the flagged sentence) | keepmum scores each sentence itself and takes the flagged ones out; that goes | `test_a_rewrite_that_kept_the_flagged_sentence_is_redone_sentence_by_sentence` |
| 2.3 | Task and flagged part in one sentence | Sentence removal cannot help: stays local, or under review waits, told why | `test_a_failed_harm_rewrite_with_nobody_there_stays_local_unasked` |
| 2.4 | Reworded, not only removed | Not sent unless the user sends it | `test_a_reworded_rewrite_stays_local_even_when_the_check_passes_it` |
| 2.5 | The user's own instruction, and skills picked for the step | Instruction in place of keepmum's, only `harm_handback` skills after it, temperature 0 | `test_the_harm_rewrite_gets_the_users_instruction_and_only_the_skills_picked_for_it` |
| 2.6 | A skill with no places | Goes everywhere but the harm rewrite | `test_a_skill_with_no_places_goes_everywhere_but_the_harm_rewrite` |

## 3. The review panel

| # | Answer | Expected | Test |
|---|---|---|---|
| 3.1 | Send | The local model's text goes | `test_approved_it_goes_as_the_local_model_wrote_it` |
| 3.2 | Edit | The user's text goes, checked, a flag noted | `test_an_edit_goes_in_its_place` |
| 3.3 | Original | The flagged text goes unchanged, whole (past the 60,000 characters shown) | `test_the_original_tool_result_goes_as_it_was_when_the_user_chooses_it`, `test_what_was_typed_goes_as_typed_when_the_user_chooses_the_original` |
| 3.4 | Ask the local model | Its change replaces the box; the review pauses while it writes | `test_the_local_model_edits_the_box_and_the_review_waits_for_it` |
| 3.5 | No answer | What "If no answer" says; a rewrite that failed a check never goes on its own | `test_no_answer_does_what_the_timeout_says`, `test_a_failed_harm_rewrite_with_no_answer_stays_local_whatever_the_timeout` |

## 4. The turn window (`--review turn`)

| # | Case | Expected | Test |
|---|---|---|---|
| 4.1 | A turn with what was typed and a tool result | One window, every piece drafted meanwhile; the defaults go | `test_one_window_shows_every_flagged_piece_with_its_draft_and_the_defaults_go` |
| 4.2 | Each piece answered differently | Original, removed, rewrite and summary each as picked | `test_each_piece_takes_its_own_answer`, `test_the_users_own_text_goes_for_a_piece_and_is_checked` |
| 4.3 | Keep the turn on this Mac | Nothing goes; the conversation's later turns stay local | `test_kept_local_nothing_reaches_the_cloud_and_later_turns_stay` |
| 4.4 | Settings on stay | Defaults to local; the user can still send | `test_with_the_settings_on_stay_the_window_defaults_to_local_but_the_user_can_send` |
| 4.5 | No answer | Defaults go only if every draft passed, under "send" | `test_no_answer_sends_only_defaults_that_passed` |
| 4.6 | Write Again, Ask the local model | A new draft (sampled), or the box changed, checked the same way | `test_a_piece_is_drafted_again_or_changed_as_the_user_asks`, `test_write_again_of_what_was_typed_is_sampled` |
| 4.7 | No local model answers | What was typed is still drafted by sentence removal | `test_with_no_local_model_keepmum_still_drafts_what_was_typed_itself` |
| 4.8 | Nobody there to review | The turn goes as the settings say | `test_with_nobody_there_a_turn_goes_as_the_settings_say` |

## 5. The clock

| # | Case | Expected | Test |
|---|---|---|---|
| 5.1 | The local model writes for 0.2 s | The time left stands still, and comes back after | `test_a_pause_gives_back_the_time_it_took` |
| 5.2 | It writes past the limit | The review ends at its hard deadline (wait + 2 min of pauses) | `test_pauses_never_go_past_the_hard_deadline` |
| 5.3 | The agent would give up first | The review ends 10 s before (Claude Code 6 min, pi and opencode 5, measured) | `test_a_review_ends_before_its_agent_gives_up`, `test_the_proxy_tells_a_review_which_agent_waits` |

## 6. When an answer is used again

| # | Case | Expected | Test |
|---|---|---|---|
| 6.1 | The same message typed again, later | Asked about again | `test_the_same_message_typed_again_later_is_asked_about_again` |
| 6.2 | The same tool result returned again, later | Asked about again | `test_a_tool_result_returned_again_later_is_asked_about_again` |
| 6.3 | The same text within 30 s (Codex's title request, a retry) | The answer stands, and the log says so | `test_within_moments_the_answer_stands_and_is_logged`, `test_a_tool_result_retried_within_moments_goes_as_answered` |
| 6.4 | The conversation's own history | Keeps the answer, every later turn | `test_a_conversations_history_keeps_its_answer` |
| 6.5 | An original, sent once | New again: asked about again | `test_an_original_sent_once_is_asked_about_again_when_new` |
| 6.6 | Which input is new, per API | The last message (Messages, Gemini, Code Assist); every Responses item after the model's last | `test_the_newest_input_of_each_api` |

## 7. Logs kept across restarts

| # | Case | Expected | Test |
|---|---|---|---|
| 7.1 | A hit, what came of it, a masked value | Rows on disk, never text or previews; facts merged on load | `test_rows_outlast_a_restart_with_what_came_later_and_never_any_text` |
| 7.2 | A kept row after a restart | Never takes the new run's text | `test_a_kept_row_never_takes_a_new_runs_text` |
| 7.3 | Older than `keep_days`; off | Dropped; off deletes the file | `test_rows_older_than_keep_days_are_dropped`, `test_off_keeps_nothing_on_disk_and_deletes_what_was_kept` |
