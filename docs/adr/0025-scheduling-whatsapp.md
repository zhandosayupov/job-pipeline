# 0025. Interview scheduling from iCal, WhatsApp through the bot, placeholders from Telegram

Date: 2026-11-27 · Status: accepted (S7) · Extends ADR 0023

## Decision
* **Free interview slots come from the calendar's secret iCal address** (`CALENDAR_ICS_URL`). The
  address is read-only, so no OAuth is needed.
  * When a message is an invitation or asks for times, the draft proposes 3 free slots: weekdays
    10:00–19:00 in Almaty, at most one per day, at least 18 hours ahead.
  * If the job's country gives a time zone, the slots are shown in that zone as well.
  * The slots are a fixed answer taken from the owner's own calendar, so they are not generated
    text. The owner still approves the draft.
  * If the calendar cannot be read, the `[[ВПИШИ]]` placeholder stays. Times that could not be
    checked are never proposed.
* **Booking links** (Calendly, cal.com, ...) are shown in the card; the owner books.
* **Calendar invitations** (`text/calendar` parts or `.ics` attachments) are read. Each one records
  the interview on the conversation and moves the application to `interview`.
* **Reminders** go to Telegram 24 hours and 1 hour before an interview, with the meeting link, the
  vacancy and the CV that was sent. They are sent by the inbox run and by the hourly selfcheck
  (which also runs at night).
* **Nothing is accepted, booked or created** in the calendar.
* **WhatsApp has no API for personal chats**, and unofficial clients risk a ban, so there is no
  automation on WhatsApp itself.
  * The owner pastes a message into the bot: `/wa <phone or name>` on the first line, the text below.
  * It is triaged, linked and drafted like mail.
  * The approved reply comes back as a `wa.me/<phone>?text=…` link: WhatsApp opens with the text
    filled in, and the owner sends it.
  * The conversation then counts as sent.
* **Placeholders can be filled from Telegram.** «✍️ Заполнить» makes the owner's next plain message
  replace the first `[[ВПИШИ]]` in the pending draft. That text is the owner's own words, so it is
  not truth-gated. The updated draft comes back with its buttons.
* **`jobpipe kb extract cv.md`** drafts `kb/facts.draft.yaml` from the CV, for the owner to review.
  * The request is pseudonymized, so it refuses to run without `profile.yaml`.
  * Every fact must quote a line that is really in the CV; its numbers and technologies must appear
    there too. Anything else is dropped and listed.
  * `facts.yaml` is never touched.
  * The CV is pseudonymized before the request and the answer is never restored, so the name,
    e-mail and phone from `profile.yaml` cannot reach the draft file or the console.
* **Selfcheck pings the LLM once a day**: one `ping` through the router. The result is cached for
  the day, so no quota is spent every hour.
* **Weekly Claude audit** (`.github/workflows/claude-audit.yml`, Mondays at 09:00 Almaty) runs
  `pip-audit`, the leak guard, the tests and ruff, and reviews the last 7 days of commits against
  the CLAUDE.md rules. Findings go into one issue labelled `claude-audit`; no issue is opened when
  nothing is found.
