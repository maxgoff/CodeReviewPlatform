# Account service

Internal service for user accounts.

## Search

`search_users(name)` returns users whose name contains `name`.

## About this sample

This repository is a teaching fixture for ReviewBot. Its bugs are deliberate, including the
"hardcoded" `AWS_ACCESS_KEY_ID` in `app/users.py`. That value (ending in `EXAMPLE`) is the
placeholder Amazon publishes in its own documentation. It is not a credential and grants no access.
