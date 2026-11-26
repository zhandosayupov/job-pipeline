# 0013. One Telegram bot

Date: 2026-11-26 · Status: accepted

## Decision
One bot (aiogram 3, long polling) answers only the owner's user id. It carries commands, approval
cards, alerts, worker status and, later, the approval Mini App (a Mini App is opened from the same
bot). Oneshot jobs send alerts through the Bot API directly (`adapters/notifier/telegram.py`) and
never receive updates, so only the resident process polls.
