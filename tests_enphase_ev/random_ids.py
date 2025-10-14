from __future__ import annotations

from random import Random

# Deterministic pseudo-random generator so tests are reproducible while
# avoiding hard-coded customer identifiers.
_rng = Random(0xEFC0DE2025)


def _rand_digits(min_value: int, max_value: int) -> str:
    """Return a random integer within range as a string."""
    return str(_rng.randrange(min_value, max_value))


# Public constants consumed across tests.
RANDOM_SITE_ID: str = _rand_digits(3_000_000, 9_999_999)
RANDOM_SERIAL: str = _rand_digits(400_000_000_000, 999_999_999_999)
RANDOM_SERIAL_ALT: str = _rand_digits(400_000_000_000, 999_999_999_999)


def generate_serial() -> str:
    """Return an additional pseudo-random serial string when tests need more."""
    return _rand_digits(400_000_000_000, 999_999_999_999)
