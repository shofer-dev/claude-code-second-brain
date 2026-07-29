"""Second Brain — a background observer for a Claude Code session.

The package is laid out along the path an observation takes, and each module owns
exactly one stage of it:

    transcript → projection → spool          what is observed, and how it is trimmed
    window + ledger                          what the observer remembers, and for how long
    detectors + fork + tools + provider      how a pass thinks
    gate + advice + mailbox                  what is allowed to be said
    loop + worker                            when any of it happens

`paths`, `config`, `constants` and `lock` are the shared floor: where state lives,
what is tunable, and the only coordination primitive (a file lock).

Nothing here writes to the repository it watches, and nothing listens on a socket.
"""

__version__ = "0.1.0"
