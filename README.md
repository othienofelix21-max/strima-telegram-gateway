# STRIMA Telegram Gateway

Experimental one-movie proof for:

Telegram channel -> STRIMA gateway -> Bunny Magic Containers CDN endpoint -> STRIMA.

## Important

Do **not** commit Telegram secrets into this repository.

The container expects these runtime environment variables in Bunny Magic Containers:

- `TG_API_ID`
- `TG_API_HASH`
- `TG_BOT_TOKEN`
- `TG_CHANNEL_ID`
- `PORT=80` (optional; defaults to 80)

The Telegram bot must be a member of the source channel and able to read the test movie message.

## Endpoints

- `/health` — verifies the container and Telegram connection.
- `/movie/<telegram_message_id>` — streams the Telegram media and supports HTTP byte ranges.

This test stores no complete movie on container disk.
