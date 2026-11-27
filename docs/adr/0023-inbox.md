# 0023. Inbox: job-related mail only, triage, funnel moves, drafts that the owner sends

Date: 2026-11-27 · Status: accepted (S5) · Implements ADR 0010, 0016

## Decision
* **Code never sends a message.** An approved reply goes to one of two places, and the owner presses send there:
  * **Gmail:** a draft in Drafts (IMAP `APPEND`), threaded with `In-Reply-To`/`References`;
  * **Telegram:** a chat draft saved by the userbot (`messages.saveDraft`).

  This keeps ADR 0010 ("never sends mail in v1") and makes every outgoing message an owner action.
* **Only job-related mail is read further.** A message is kept only if one of these holds:
  * it belongs to a tracked thread;
  * it names a live application's company;
  * its sender is a hiring platform (`inbox.ats_domains`) or a job alert (`inbox.alert_senders`);
  * it has job words and is not bulk mail.

  Everything else is skipped and never stored, and never reaches an LLM. Mail is fetched with `BODY.PEEK` from read-only mailboxes, so its flags never change. UID cursors are kept per folder and reset when `UIDVALIDITY` changes. The first run looks back `first_run_days` days, never through the whole mailbox.
* **Triage, rules first.** Rules recognise rejections, invitations, "application received" mail and job alerts, after hard line breaks are normalised. The LLM (task `triage`, pseudonymized) handles the rest. Its output is checked:
  * questions must appear verbatim in the message;
  * topics are the union of the LLM's and the regex topics, so a missed sensitive topic still forces Approve.

  Without an LLM, a message with questions counts as a screening; anything else is `other`, and the owner is told about it.
* **Linking.** By thread first (Gmail `X-GM-THRID`, `In-Reply-To`/`References`, the Telegram chat). Otherwise by the company name in the sender, subject or text, among live applications. When several match, the title words decide, and the card marks the link as ambiguous.
* **The funnel moves forward only.** The path is the shortest one in the transition table. It stays within the funnel states and never goes back into the apply flow:

  | mail | application moves to |
  |---|---|
  | rejection | `rejected` |
  | invite | `interview` |
  | screening question | `screening` |
  | info request | `replied` |
  | "application received" | `applied` (and resolves a `submit_unknown`: the side-effect key becomes `done`) |

  An application in `applied` with no reply for `ghosted_after_days` becomes `ghosted`. A late rejection after silence is allowed (`ghosted → rejected`).
* **Reply drafts** (`services/reply.py`):
  * **Sensitive questions** are never answered with generated text. They get one of: the owner's verbatim `answers.yaml` entry, the visa sentence derived from the job's country, the citizenship from the profile, or a `[[ВПИШИ: …]]` placeholder. Compensation (`ask`), interview times and offers are always placeholders.
  * **The LLM** writes only the connecting text. Any fixed answer or placeholder it drops is put back, and the remaining generated text goes through the truth gate (one retry).
  * **A draft that fails the gate** shows no approve button.
  * **No-reply senders** get a draft to copy into the site, with no Gmail button.
  * **Without an LLM**, a template is used.
* **Approval and at-most-once.** Every reply is L1 by default (`decide_message`).
  * Approve moves the conversation to `approved` and queues `inbox.draft` (Gmail) or `tg.draft` (userbot). Both use the side-effect key `draft:<conversation>:<n>`.
  * The Gmail handler logs in first, then reserves the key, then calls `APPEND`. A failure after the key is reserved becomes `unknown` for the owner and is never retried.
  * The worker API reserves the `tg.draft` key before handing the task out.
* **Closing the loop.** The owner's sent reply is read from the `\Sent` folder (or seen by the userbot) and moves the conversation `approved → sent → waiting`. Whether the draft went out unedited is recorded as a `reply_outcome` event (`/inbox` shows the count). That count is the evidence for `l2_min_clean_approvals` before replies could move to L2.
* **Telegram userbot.** It runs on the laptop, on the secondary account, using Telethon (optional group `userbot`).
  * It reads private chats only (no groups, channels or bots) and posts them to `/worker/inbound`, which stores them and queues `inbox.process`.
  * The owner logs in once, interactively. The session file stays in the worker directory.
* **Queue runner fix (found while building this).** The VM queue run takes only the task types it has handlers for. Before the fix it would have taken `apply.*` and `tg.draft` tasks meant for the laptop and dead-lettered them.
* **Retention.** After `messages_compact_days` (120), a message keeps its headers, category and triage; the body and links are deleted.

## Consequences
* Rules and the hh alert parser were written from public knowledge of these emails and checked against synthetic fixtures, not real mail. The first live runs should be read in `/inbox` before anyone relies on them.
* hh.kz chat messages are not read directly. The hh e-mail notification about a new message is triaged like any other mail, but the reply is given on hh (a no-reply sender, so the draft is for copying). Reading the hh chat in the browser worker is parked in `docs/TODO.md`.
