import random
import time

import pytest

from kb_assistant.adapters.local import masking
from kb_assistant.adapters.local.masking import HOLD, TRUNCATED, MaskingFilter, mask

# AWS's documentation examples, assembled at runtime so secret scanners do not flag them.
AWS_ACCESS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
AWS_SECRET_DIGITS = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPL1234"
SECRETS = {
    "card": ("Your card 4111 1111 1111 1111 expires soon.", "4111 1111 1111 1111"),
    "card-dashes": ("Card 5500-0000-0000-0004 on file", "5500-0000-0000-0004"),
    "iban": ("IBAN GB82 WEST 1234 5698 7654 32 done", "GB82 WEST 1234 5698 7654 32"),
    "access-key": (f"Key {AWS_ACCESS_KEY} and more", AWS_ACCESS_KEY),
    "secret-key": (f"the secret is {AWS_SECRET} ok", AWS_SECRET),
    # Labels glued to other letters, which partial matching used to miss.
    "secret-json": (f'{{"SecretAccessKey": "{AWS_SECRET}"}}', AWS_SECRET),
    "secrets-manager": (f"The Secrets Manager value is {AWS_SECRET}.", AWS_SECRET),
    "secret-digits": (f'{{"SecretAccessKey": "{AWS_SECRET_DIGITS}"}}', AWS_SECRET_DIGITS),
    "password": ("Use password: hunter2!! then log in", "hunter2!!"),
    "password-is": ("The pwd is Tr0ub4dor&3 for now", "Tr0ub4dor&3"),
    "ssn": ("SSN 123-45-6789 here", "123-45-6789"),
    "ni": ("NI number AB 12 34 56 C end", "AB 12 34 56 C"),
    "sin": ("SIN 046 454 286 end", "046 454 286"),
}
PLAIN = [
    "Email support@example.com or call +61 2 9999 8888 between 9 and 5.",
    "In 2024 we had 42 offices, 1234 staff and order 12345678 shipped.",
    "Room 101, level 3, 1 Martin Place, Sydney NSW 2000.",
    # Long digit runs are taken whole, so they never become a card number.
    "Serial 0000 0000 0000 0000 0000 0000 0000 0000 end",
]


def stream(text, cuts, success=True):
    masking_filter = MaskingFilter()
    pieces = [text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)])]
    outputs = [masking_filter.feed(piece) for piece in pieces]
    outputs.append(masking_filter.finish(success))
    return outputs


def tokens(text, rng, largest=6):
    cuts, position = [], 0
    while True:
        position += rng.randint(1, largest)
        if position >= len(text):
            return cuts
        cuts.append(position)


def leaked(secret, output, text):
    """Any four-character piece of the secret's value that is not also elsewhere in text."""
    elsewhere = text.replace(secret, "")
    return [
        secret[i : i + 4]
        for i in range(len(secret) - 3)
        if secret[i : i + 4] in output and secret[i : i + 4] not in elsewhere
    ]


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_every_split_position_matches_whole_text_and_never_leaks(name):
    text, secret = SECRETS[name]
    expected = mask(text)
    assert secret not in expected and "{" in expected
    for cut in range(len(text) + 1):
        output = "".join(stream(text, [cut]))
        assert output == expected
        assert not leaked(secret, output, text)


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_token_sized_streams_match_whole_text(name):
    text, secret = SECRETS[name]
    expected = mask(text)
    rng = random.Random(name)
    for _ in range(200):
        output = "".join(stream(text, tokens(text, rng)))
        assert output == expected and not leaked(secret, output, text)


@pytest.mark.parametrize("text", PLAIN)
def test_contact_details_and_ordinary_numbers_untouched(text):
    assert mask(text) == text
    for cut in range(len(text) + 1):
        assert "".join(stream(text, [cut])) == text


def test_cross_detector_overlap_never_bisects_a_span():
    text = "AKIA" + "A" * 12 + "1234 5678 9012 3456 tail"
    expected = mask(text)
    assert expected == "{AWS_ACCESS_KEY} 5678 9012 3456 tail"
    for cut in range(len(text) + 1):
        for second in range(cut, len(text) + 1, 3):
            assert "".join(stream(text, [cut, second])) == expected
    # Equal spans from two detectors merge into one placeholder.
    labelled = "password: 4111111111111111 next"
    assert mask(labelled) == "password: {CREDIT_DEBIT_CARD_NUMBER} next"
    for cut in range(len(labelled) + 1):
        assert "".join(stream(labelled, [cut])) == mask(labelled)


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_failure_at_every_prefix_length_releases_no_secret(name):
    text, secret = SECRETS[name]
    start = text.index(secret)
    for length in range(1, len(secret) + 1):
        released = "".join(stream(text[: start + length], tokens(text, random.Random(length)), success=False))
        assert not leaked(secret[:length], released, text[:start]), (length, released)


def test_failure_drops_only_the_held_tail():
    text = "Plain words. " * 40
    released = "".join(stream(text, [len(text) // 2], success=False))
    assert released.endswith(TRUNCATED)
    assert text.startswith(released.removesuffix(TRUNCATED))
    assert len(text) - len(released.removesuffix(TRUNCATED)) <= HOLD


def test_successful_finish_releases_non_matching_prefix_as_is():
    assert "".join(stream("Your key prefix is AKIA12", [10])) == "Your key prefix is AKIA12"


def test_adjacent_and_trailing_secrets():
    text = "SSN 123-45-6789 SSN 987-65-4320 and card 4111111111111111"
    expected = (
        "SSN {US_SOCIAL_SECURITY_NUMBER} SSN {US_SOCIAL_SECURITY_NUMBER} "
        "and card {CREDIT_DEBIT_CARD_NUMBER}"
    )
    assert mask(text) == expected
    for cut in range(len(text) + 1):
        assert "".join(stream(text, [cut])) == expected


def test_detectors_stay_within_their_bounds():
    value = "x" * 120
    # Only the first 64 characters of a very long labelled value are covered.
    assert mask(f"password = {value} done") == "password = {PASSWORD}" + "x" * 56 + " done"
    for detector in masking.DETECTORS:
        assert detector.max_length < HOLD


def test_hold_back_is_bounded_and_streams_stay_fast():
    text = "0000 " * 2000 + "card 4111 1111 1111 1111 end " + "word " * 2000
    masking_filter = MaskingFilter()
    started = time.monotonic()
    largest = 0
    output = []
    for index in range(0, len(text), 4):
        output.append(masking_filter.feed(text[index : index + 4]))
        largest = max(largest, len(masking_filter.pending))
    output.append(masking_filter.finish())
    assert "".join(output) == mask(text) and "4111" not in "".join(output)
    assert largest <= HOLD + masking.MAX_LENGTH + 4
    assert time.monotonic() - started < 5


def test_forced_overflow_cut_still_masks_the_rest_of_the_span(monkeypatch):
    # Force the safety valve by making any held span count as overflow.
    monkeypatch.setattr(masking, "OVERFLOW", HOLD)
    text = "x" * 200 + " card 4111 1111 1111 1111 end " + "y" * 200
    for size in (1, 3, 7):
        output = "".join(stream(text, list(range(size, len(text), size))))
        assert not leaked("4111 1111 1111 1111", output, text)
        assert "{CREDIT_DEBIT_CARD_NUMBER}" in output
        assert output.startswith("x" * 200) and output.endswith("y" * 200)


def test_differential_fuzz_against_whole_text_masking():
    rng = random.Random(20261025)
    pieces = [
        "word", "the", "Secret", "secrets", "password", "pwd", "is", ":", "=", " ", " ", "\n",
        "4111 1111 1111 1111", "5500-0000-0000-0004", "0000", "1234", "123-45-6789",
        AWS_ACCESS_KEY, AWS_SECRET, "GB82 WEST 1234 5698 7654 32", "AB 12 34 56 C",
        "046 454 286", '"', "{", "}", "x" * 30,
    ]
    for _ in range(1500):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 40)))
        expected = mask(text)
        output = "".join(stream(text, tokens(text, rng, largest=rng.choice([1, 3, 8, 30]))))
        assert output == expected, text


CHAINED = {
    # Two 40-character paths ending in "secret" put a non-overlapping scan out of phase.
    "aws-natural": (
        "The secret used by the nightly payments job is in team/finance/config/prod/payments/secret "
        "(production), and the copy kept under team/finance/config/test/payments/secret for staging "
        f"tests is the access key {AWS_SECRET}. Rotate it every 90 days.",
        AWS_SECRET,
    ),
    "iban-chain": ("AB12 " * 48 + "GB82 WEST 1234 5698 7654 32. " + "z " * 60, "5698 7654"),
    "password-chain": ("pwd=" * 61 + "Tr0ub4dor&3Zz end " + "x " * 60, "Tr0ub4dor&3Zz"),
}


@pytest.mark.parametrize("name", sorted(CHAINED))
def test_chained_labels_longer_than_the_context_never_leak(name):
    text, secret = CHAINED[name]
    assert len(text) > masking.CONTEXT and secret not in mask(text)
    rng = random.Random(name)
    for largest in (1, 3, 5, 8):
        for _ in range(60):
            output = "".join(stream(text, tokens(text, rng, largest)))
            assert secret not in output
            assert output == mask(text)
