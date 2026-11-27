-- 0004: S5 messages (Gmail, Telegram userbot), linked to conversations.
-- Only job-related mail is stored (ADR 0023); everything else in the mailbox is skipped unread.

CREATE TABLE messages (
  id               TEXT PRIMARY KEY,               -- <channel>:<sha1 of the external id>
  conversation_id  TEXT REFERENCES conversations(id),
  channel          TEXT NOT NULL,                  -- gmail | telegram
  direction        TEXT NOT NULL CHECK (direction IN ('in', 'out')),
  ext_id           TEXT NOT NULL,                  -- Message-ID header, or <chat>:<message id>
  thread_key       TEXT,                           -- Gmail X-GM-THRID / root Message-ID / Telegram chat id
  from_addr        TEXT,
  from_name        TEXT,
  subject          TEXT,
  ts               TEXT NOT NULL,
  category         TEXT,                           -- triage result; NULL = not processed yet
  body             TEXT NOT NULL DEFAULT '',
  data_json        TEXT NOT NULL DEFAULT '{}',
  created_at       TEXT NOT NULL,
  processed_at     TEXT
);
CREATE INDEX messages_conv ON messages(conversation_id);
CREATE INDEX messages_thread ON messages(channel, thread_key);
CREATE INDEX messages_pending ON messages(processed_at);
CREATE INDEX conversations_state ON conversations(state);
