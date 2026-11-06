import pytest

from keepmum.detect import Span
from keepmum.detect.merge import merge
from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector, iban_valid, luhn_valid

# Synthetic values only: random strings in provider formats, never real credentials.
FAKE_GITHUB = "ghp_" + "Zq8vX2mN4pL7kR9tY3wB6cF1hJ5dG0sAe7Qw"
FAKE_AWS = "AKIA" + "QX7Z3M5N2P4R6T7V"


@pytest.fixture(scope="module")
def rules():
    return RulesDetector()


def values(text, spans):
    return [text[s.start : s.end] for s in spans]


def test_provider_keys(rules):
    text = f"export GITHUB_TOKEN={FAKE_GITHUB}\naws_access_key_id = {FAKE_AWS}\n"
    assert set(values(text, rules.detect(text))) >= {FAKE_GITHUB, FAKE_AWS}


def test_connection_string_marks_only_the_password(rules):
    text = "DATABASE_URL=postgres://app:Zx81!kq_pLm@db.internal:5432/orders"
    assert values(text, [s for s in rules.detect(text) if s.source.endswith("connection-string-password")]) == ["Zx81!kq_pLm"]


@pytest.mark.parametrize("text", [
    "aws_access_key_id = AKIAIOSFODNN7EXAMPLE",  # documented example
    "api_key = os.environ[\"OPENAI_API_KEY\"]",
    "STRIPE_SECRET_KEY=${STRIPE_SECRET_KEY}",
    "DB_PASSWORD=changeme",
    "SLACK_BOT_TOKEN=xoxb-your-token",
    "password: string;",
    "MAX_TOKENS = 4096",
    "postgres://user:password@localhost:5432/mydb",
    "KEEPMUM_SECRET_7f3a9c2e appears in a later turn",
    '"integrity": "sha512-v2kDEe57lecTulaDIuNTPy3Ry4gLGJ6Z1O3vE1krgXZNrsQ+LFTGHVxVjcXPs17LhbZVGedAJv8XZ1tvj5FvSg=="',
    "len(tok.encode(text, add_special_tokens=False))",  # a keyword argument, closed by brackets
    "secret_ref = None]",
])
def test_hard_negatives(rules, text):
    assert rules.detect(text) == []


def test_luhn():
    assert luhn_valid("4242424242424242")
    assert not luhn_valid("4242424242424241")


# The example IBANs from the ISO 13616 and national bank documentation.
@pytest.mark.parametrize("iban", ["GB82WEST12345698765432", "DE89370400440532013000", "FR1420041010050500013M02606",
                                  "NL91ABNA0417164300", "NO9386011117947"])
def test_iban_checksum(iban):
    assert iban_valid(iban)
    assert not iban_valid(iban[:2] + f"{(int(iban[2:4]) + 1) % 100:02d}" + iban[4:])


@pytest.mark.parametrize("text, found", [
    ("refund to GB82WEST12345698765432 please", "GB82WEST12345698765432"),
    ("IBAN: GB82 WEST 1234 5698 7654 32.", "GB82 WEST 1234 5698 7654 32"),
    ("Konto DE89 3704 0044 0532 0130 00, bitte", "DE89 3704 0044 0532 0130 00"),
    ("refund to GB83WEST12345698765432 please", None),  # wrong check digits
    ("ref ZZ82WEST12345698765432", None),  # no such country
    ("id=xGB82WEST12345698765432", None),  # part of a longer token
])
def test_iban_rule(rules, text, found):
    got = [text[s.start:s.end] for s in rules.detect(text) if s.source == "rules:iban"]
    assert got == ([found] if found else [])


def test_iban_is_personal_data(rules):
    text = "refund to GB82WEST12345698765432 please"
    assert Pipeline(rules, None, pii=False).detect(text) == []
    assert values(text, Pipeline(rules, None, pii=True).detect(text)) == ["GB82WEST12345698765432"]


def test_merge_keeps_widest_span_and_highest_priority_type():
    spans = [Span(0, 5, "PERSON", 0.6, "pf"), Span(3, 10, "SECRET", 0.99, "rules"), Span(20, 25, "EMAIL")]
    merged = merge(spans)
    assert [(s.start, s.end, s.type) for s in merged] == [(0, 10, "SECRET"), (20, 25, "EMAIL")]
    assert merged[0].confidence == 0.99


class _FakePF:
    def __init__(self, spans):
        self.spans = spans

    def detect(self, text):
        return self.spans


def test_refine_drops_identifiers_and_trims_labels(rules):
    text = "GET /api/v1/tokens/3f2b8c4e-9a1d-4e5f-8b7c-2d1e0f9a8b7c status=200 request_id=a01749ddb14f7101\n"
    pf = _FakePF([Span(text.index("/3f2b"), len(text) - 1, "SECRET", 0.9, "pf")])
    assert Pipeline(rules, pf).detect(text) == []


def test_refine_keeps_hex_after_a_keyword(rules):
    key = "b8714f20c6e93ad5" * 2
    text = f"the Datadog key for staging is {key}, rotate it"
    pf = _FakePF([Span(text.index(key) - 1, text.index(key) + len(key) + 1, "SECRET", 1.0, "pf")])
    assert values(text, Pipeline(rules, pf).detect(text)) == [key]


def test_refine_narrows_url_to_password(rules):
    text = "url: mysql://root:7sxjklEe%8YN3@mysql-primary:3306/shop"
    pf = _FakePF([Span(text.index("mysql://"), len(text), "SECRET", 0.8, "pf")])
    assert values(text, Pipeline(rules, pf).detect(text)) == ["7sxjklEe%8YN3"]


@pytest.mark.parametrize("text, value, kind, keep", [
    ("request_id=0d6dc8cff6403ab9", "0d6dc8cff6403ab9", "ACCOUNT", False),
    ("<total_tokens>14987873 tokens left</total_tokens>", "14987873", "ACCOUNT", False),
    ("-rw-r--r--  1 dev  staff  66060825 Oct 26 00:40 Keepmum.dmg", "66060825", "ACCOUNT", False),
    ('"f1": 0.9187279151943463', "9187279151943463", "ACCOUNT", False),
    ("wire it to account 48213907 today", "48213907", "ACCOUNT", True),
    ("card 4111 1111 1111 1111 exp 04/29", "4111 1111 1111 1111", "ACCOUNT", True),
    ("refund to GB33BUKB20201555555555", "GB33BUKB20201555555555", "ACCOUNT", True),
    ("acct 0012345678 at the credit union", "0012345678", "ACCOUNT", True),
    ("2026-10-21T09:14:03.112Z INFO", "2026-10-21T09:14:03.112", "DATE", False),
    ("born 03/14/1987", "03/14/1987", "DATE", True),
    ('"date_of_birth": "1987-03-14"', "1987-03-14", "DATE", True),
    ("Today's date: 2026-10-25", "2026-10-25", "DATE", False),
    ("knowledge cutoff is June 2026", "June 2026", "DATE", False),
    ("GET /api/v1/users/9dd52505", "/api/v1/users/9dd52505", "URL", False),
    ("profile: https://wiki.corp.internal/people/ada-l", "https://wiki.corp.internal/people/ada-l", "URL", True),
    ("mailer.send(to=user@example.com)", "user@example.com", "EMAIL", False),
    ("From: ada.l@corp.internal", "ada.l@corp.internal", "EMAIL", True),
    ("Co-Authored-By: Claude <noreply@anthropic.com>", "noreply@anthropic.com", "EMAIL", False),
    ("phone: 555-0100", "555-0100", "PHONE", False),
    ("phone: +1 415 555 0134", "+1 415 555 0134", "PHONE", False),  # 555-01xx is fictional in every area code
    ("phone: +1 370 867 2290", "+1 370 867 2290", "PHONE", True),
    ("- username: ops-admin", "ops-admin", "PERSON", False),
    ("Reported-by: Ada Lovelace", "Ada Lovelace", "PERSON", True),
])
def test_keep_pii(text, value, kind, keep):
    start = text.index(value)
    assert Pipeline.keep_pii(text, Span(start, start + len(value), kind)) is keep


@pytest.mark.parametrize("text", [
    "# OPENAI_API_KEY=sk-...\n# ANTHROPIC_API_KEY=sk-ant-...",
    'export const TOKEN_CONTRACT = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e";',
    "distributionSha256Sum=31c55713e40233a8303827ceb42ca48a47267a0ad4bab9177123121e71524c26",
])
def test_holdout_negatives_fixed(rules, text):
    assert rules.detect(text) == []


def test_private_key_hex_still_caught(rules):
    key = "0x" + "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
    assert values(f'private_key = "{key}"', rules.detect(f'private_key = "{key}"')) == [key]


def test_refine_drops_span_starting_inside_a_uuid(rules):
    text = "PUT /api/v1/accounts/e65d2c19-dc5d-4db6-98d6-9be4a5b85f36 status=200\n"
    start = text.index("65d2c19")
    pf = _FakePF([Span(start, text.index(" status"), "SECRET", 0.9, "pf")])
    assert Pipeline(rules, pf).detect(text) == []


def test_refine_drops_public_address_from_model(rules):
    text = 'export const TOKEN_CONTRACT = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e";'
    a = text.index("0x")
    pf = _FakePF([Span(a, a + 42, "SECRET", 0.9, "pf")])
    assert Pipeline(rules, pf).detect(text) == []


def test_prose_is_not_an_assignment(rules):
    assert rules.detect("Append BACKUP_GITHUB_TOKEN= followed by exactly the same value") == []


@pytest.mark.parametrize("text, value", [
    ("password: hunter2x", "hunter2x"),
    ('password = "correcthorse"', "correcthorse"),
    ("DB_PASSWORD=sunshineday\n", "sunshineday"),
])
def test_real_assignments_still_caught(rules, text, value):
    assert value in values(text, rules.detect(text))


@pytest.mark.parametrize("text, value, masked", [
    # The model labels an API key in prose an account number: a secret keyword before it makes it a secret.
    ("Hey, the Datadog key for staging is 3f9a1c7e5b2d8f4a6c0e9b1d3f5a7c2e - please rotate it", "3f9a1c7e5b2d8f4a6c0e9b1d3f5a7c2e", True),
    ("the auth token is 9Kx2Lm7Qp4Rt8Vw3 for now", "9Kx2Lm7Qp4Rt8Vw3", True),
    # Card and bank digits stay account numbers, even after a keyword.
    ("card for the api key billing: 4242 4242 4242 4242", "4242 4242 4242 4242", False),
    # A header name after `type: apiKey` is not a credential.
    ("    apiKeyAuth:\n      type: apiKey\n      in: header\n      name: X-API-Key", "X-API-Key", False),
    # No secret keyword: still an account number (personal data, off by default).
    ("refund to account 3f9a1c7e5b2d8f4a6c0e9b1d3f5a7c2e please", "3f9a1c7e5b2d8f4a6c0e9b1d3f5a7c2e", False),
])
def test_model_account_spans_after_a_secret_keyword_are_secrets(rules, text, value, masked):
    a = text.index(value)
    pf = _FakePF([Span(a, a + len(value), "ACCOUNT", 0.9, "pf")])
    got = values(text, Pipeline(RulesDetector(), pf, pii=False).detect(text))
    assert (value in got) == masked


def test_username_in_a_path_is_not_a_person(rules):
    text = "Working directory: /Users/alex/keepmum-test"
    a = text.index("/alex")
    pf = _FakePF([Span(a, a + 5, "PERSON", 0.9, "pf")])
    assert Pipeline(rules, pf, pii=True).detect(text) == []


def test_real_name_survives_trimming(rules):
    text = "OWNER=Priya Raman <priya.raman@corp.internal>"
    a = text.index("=Priya")
    pf = _FakePF([Span(a, a + len("=Priya Raman"), "PERSON", 0.9, "pf")])
    assert values(text, Pipeline(rules, pf, pii=True).detect(text)) == ["Priya Raman", "priya.raman@corp.internal"]


@pytest.mark.parametrize("text, name", [
    ("You are Claude Code, Anthropic's official CLI for Claude.", "Claude Code"),
    ("Co-Authored-By: Claude Opus 5.5", "Claude Opus 5.5"),
])
def test_claude_product_names_are_not_people(rules, text, name):
    a = text.index(name)
    pf = _FakePF([Span(a, a + len(name), "PERSON", 0.9, "pf")])
    assert Pipeline(rules, pf, pii=True).detect(text) == []


# --- File names are not secrets ------------------------------------------------------------------

def test_file_names_the_entropy_check_or_the_model_took_for_secrets_are_not_masked(rules):
    names = ["bench/results/PHASE0", "bench/results/guard-20261104-231405.md", "docs/Zq7Xw2Lm9Pk4Rt6Vb1Nc8.json"]
    text = f"Read {names[0]}.md, then {names[1]}, and {names[2]}.\n"
    # What the model flagged in a real session: dated report names, as SECRET.
    pf = _FakePF([Span(text.index(n), text.index(n) + len(n), "SECRET", 0.9, "pf-serve") for n in names])
    assert Pipeline(rules, pf, pii=False).detect(text) == []


def test_a_key_is_still_masked_next_to_a_file_name_or_inside_one(rules):
    key = "ghp_" + "Zq7Xw2Lm9Pk4Rt6Vb1Nc8Hd3Jf5Gs0Ya2Ue7"
    loose = "Xq3k9ZpL2mN8vB4cR7tY1wE5aQ"
    text = f"token {key}.md and {loose} in notes/plan.md\n"
    pf = _FakePF([Span(text.index(loose), text.index(loose) + len(loose), "SECRET", 0.9, "pf-serve")])
    got = values(text, Pipeline(rules, pf, pii=False).detect(text))
    assert key in got  # a token rule knows the format: masked even as a file name
    assert loose in got  # no extension of its own: still a secret


def test_a_file_name_masked_earlier_is_let_through_now(rules):
    p = Pipeline(rules, None, pii=False)
    assert p.lets_through("bench/results/guard-20261104-231405.md", frozenset({"pf-serve"}))
    assert not p.lets_through("bench/results/guard-20261104-231405.md", frozenset({"rules:github-token"}))
    assert not p.lets_through("Xq3k9ZpL2mN8vB4cR7tY1wE5aQ", frozenset({"rules:entropy"}))


def test_a_path_in_json_text_and_an_email_without_an_at_are_not_masked(rules):
    text = r'{"file_path": "notes/plan.md\n", "out": "bench/guard-20261104-231405.md"}'
    path = text.index("notes")
    dated = text.index("bench")
    pf = _FakePF([Span(path, text.index('", "out"'), "SECRET", 0.9, "pf-serve"),
                  Span(dated, dated + len("bench/guard-20261104-231405.md"), "EMAIL", 0.9, "pf-serve")])
    assert Pipeline(rules, pf, pii=True).detect(text) == []
    mail = "write to ana@keepmum.app"
    pf = _FakePF([Span(9, len(mail), "EMAIL", 0.9, "pf-serve")])
    assert values(mail, Pipeline(rules, pf, pii=True).detect(mail)) == ["ana@keepmum.app"]
