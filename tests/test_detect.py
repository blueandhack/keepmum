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
    assert set(values(f'private_key = "{key}"', rules.detect(f'private_key = "{key}"'))) == {key}


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

def test_a_search_tool_s_grounding_link_is_not_a_secret_but_a_webhook_is(rules):
    # agy's search_web returns Google's redirect links: masked, the model lost its sources (wf-03, Nov 18).
    link = ("https://vertexaisearch.cloud.google.com/grounding-api-redirect/"
            "AUZIYQEsslAtGzmpmdN8qEk2McvApB3VNMT2DlpHZtQZbEQBZNWE8Xt5rmJmT3Kw6DilGflr3UuooIMYc8AvpRAGx828_ojEM7iW8Y1TZF5Jj_0VUiM7ONsxGsg=")
    for text in (f"1. python.org: {link}\n", f'{{"web": {{"uri": "{link}", "title": "python.org"}}}}'):
        assert rules.detect(text) == []
    hook = "https://hooks.slack.com/services/" + "T0123ABCD/B0456EFGH/aB3dE5fG7hJ9kL1mN3pQ5rS7"  # split: GitHub's push protection takes a whole fake key for a real one
    elsewhere = link.replace("vertexaisearch.cloud.google.com/grounding-api-redirect", "example.com/share")
    assert rules.detect(f"post to {hook}") and rules.detect(f"see {elsewhere}")  # other hosts: still guessed at
    # The model takes pieces of the handle for secrets too (wf-03 live): a guess inside the link goes.
    for url in (link, elsewhere):
        text = f'"uri": "{url}"'
        piece = url[-60:-10]
        pf = _FakePF([Span(text.index(piece), text.index(piece) + len(piece), "SECRET", 0.9, "pf-serve")])
        got = values(text, Pipeline(rules, pf, pii=False).detect(text))
        assert (got == []) if url == link else any(piece in g or g in piece for g in got)


@pytest.mark.parametrize("text, piece, kept", [
    # What the model took for secrets in wf-01 live (Nov 18): macOS's temp folder, an agent's own folder.
    ("Active Workspaces:\\n- /private/var/folders/ng/f6k2b2nj0k92xndsgmh6bqrh0000gn/T/keepmum-e2e-9l1ld436/w/ws\\n",
     "ng/f6k2b2nj0k92xndsgmh6bqrh0000gn/T/keepmum-e2e-9l1ld436/w", False),
    ("File: file:///private/var/folders/ng/f6k2b2nj0k92xndsgmh6bqrh0000gn/T/x/photos/a.png", "f6k2b2nj0k92xndsgmh6bqrh0000gn", False),
    ('{"path": "/Users/me/.gemini/antigravity/brain/23594f88-5e6f-42a5-bbd4-5e59d395f5f8/x.png"}',
     "brain/23594f88-5e6f-42a5-bbd4-5e59d395f5f8", False),
    # Not a path on this Mac: a URL's path, a key with slashes, a loose token next to a path.
    ("see https://example.com/Users/AUZIYQEsslAtGzmpmdN8qEk2McvApB3VNMT2DlpHZtQ now", "AUZIYQEsslAtGzmpmdN8qEk2McvApB3VNMT2DlpHZtQ", True),
    ("aws_secret = wJalrXUtnFEMI/K7MDENG/bPxRfiCYzq8vX2mN4pL7k", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzq8vX2mN4pL7k", True),
    ("token: Zq3bX8mL2vT9wR4kY7nP1sD6fH0jC5aE8gUi and /tmp/x", "Zq3bX8mL2vT9wR4kY7nP1sD6fH0jC5aE8gUi", True),
])
def test_a_guess_inside_a_path_on_this_mac_is_not_a_secret(rules, text, piece, kept):
    pf = _FakePF([Span(text.index(piece), text.index(piece) + len(piece), "SECRET", 0.9, "pf-serve")])
    got = values(text, Pipeline(rules, pf, pii=False).detect(text))
    assert any(piece in g or g in piece for g in got) is kept


def test_a_csp_nonce_is_not_a_secret_but_an_assigned_key_is(rules):
    # A page's nonce goes to every browser that loads it (fe-01, Nov 18): the entropy check took it.
    value = "r4nd0mN0nc3Vb8xQ2kL9pZ7wT5yH"
    for text in (f'<script nonce="{value}" src="app.mjs">', f"'nonce-{value}'"):
        assert rules.detect(text) == []
    assert values(f"API_KEY = '{value}'", rules.detect(f"API_KEY = '{value}'")) == [value, value]


@pytest.mark.parametrize("text, piece, kept", [
    # What the model took for secrets live (Nov 18): pins by hash, and a page's nonce.
    ("FROM python:3.12-slim@sha256:2f7e1b0d3f1a6c5e4b8a9d0c7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f\n",
     "python:3.12-slim@sha256:2f7e1b0d3f1a6c5e4b8a9d0c7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f", False),
    ("      - uses: actions/setup-node@16d7db0cade58ea39e455f285a3fdc3d0b9bdb73 # v4.1.0",
     "actions/setup-node@16d7db0cade58ea39e455f285a3fdc3d0b9bdb73", False),
    ("      - uses: actions/setup-node@16d7db0cade58ea39e455f285a3fdc3d0b9bdb73", "@16d7db0cade58ea39e455f285a3fdc3d0b9bdb73", False),
    ('<script nonce="q7Xv2LmP9kR4tW8z" src="app.mjs">', "q7Xv2LmP9kR4tW8z", False),
    ('12:   <script nonce=\\"q7Xv2LmP9kR4tW8z\\" type=\\"module\\">', "q7Xv2LmP9kR4tW8z", False),  # JSON text in a string
    ("script-src 'self' 'nonce-q7Xv2LmP9kR4tW8z'", "nonce-q7Xv2LmP9kR4tW8z", False),
    # Not a pin or a nonce: a token after an @, a value assigned to a key.
    ("ping me @Zq3bX8mL2vT9wR4kY7nP1sD6fH0jC5aE8gUi", "Zq3bX8mL2vT9wR4kY7nP1sD6fH0jC5aE8gUi", True),
    ("key='q7Xv2LmP9kR4tW8z'", "q7Xv2LmP9kR4tW8z", True),
    # A commit on its own is a hash; the same hex named a token is not.
    ("16d7db0cade58ea39e455f285a3fdc3d0b9bdb73 fix: handle an empty cart", "16d7db0cade58ea39e455f285a3fdc3d0b9bdb73", False),
    # A transaction or block hash: the model flags every one (crypto-01 live, Nov 18).
    ("| sepolia | 0x6a84a34c24c5132c69d6fae0b47916ba6486bc8427e304216412576744186407 | 9fd39b0 |",
     "0x6a84a34c24c5132c69d6fae0b47916ba6486bc8427e304216412576744186407", False),
    ("the wallet key: 0x6a84a34c24c5132c69d6fae0b47916ba6486bc8427e304216412576744186407",
     "0x6a84a34c24c5132c69d6fae0b47916ba6486bc8427e304216412576744186407", True),
    ("the deploy token is 16d7db0cade58ea39e455f285a3fdc3d0b9bdb73", "16d7db0cade58ea39e455f285a3fdc3d0b9bdb73", True),
    # A plugin or package by name: Codex's plugin list (wf-01 on Codex, Nov 18), npm, Homebrew.
    ("- Figma (figma@openai-curated-remote)\n", "figma@openai-curated-remote", False),
    ("- Codex Security (codex-security@openai-curated-remote)\n", "codex-security@openai-curated-remote", False),
    ("npm i react@18.3.1 @types/node@20.11.5", "react@18.3.1", False),
    ("brew install node@20", "node@20", False),
    # Not one: a token after a name.
    ("deploy with ci@Zq3bX8mL2vT9wR4kY7nP1sD6", "ci@Zq3bX8mL2vT9wR4kY7nP1sD6", True),
])
def test_a_pin_by_hash_or_a_nonce_is_not_a_secret(rules, text, piece, kept):
    pf = _FakePF([Span(text.index(piece), text.index(piece) + len(piece), "SECRET", 0.9, "pf-serve")])
    got = values(text, Pipeline(rules, pf, pii=False).detect(text))
    assert any(piece in g or g in piece for g in got) is kept


_K64 = "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"


@pytest.mark.parametrize("text, masked", [
    # A key in hex, named or decoded as one: too few symbols for the entropy check (Nov 18).
    (f"Use the wallet's private key 0x{_K64} to sign it", True),
    (f'{{"signing_key": "{_K64}"}}', True),
    (f'KEY = bytes.fromhex("{_K64}")', True),
    (f'const key = Buffer.from("{_K64}", "hex");', True),
    (f"AES key: {_K64[:32]}", True),
    # The same digits as a hash: a transaction, a file's digest, a cache key, a public key.
    (f"Sent. Transaction hash: 0x{_K64}", False),
    (f"{_K64}  dist/app.tar.gz", False),
    (f"Cache restored from key: node-cache-Linux-{_K64}", False),
    (f"compressed public key 02{_K64}", False),
])
def test_a_key_in_hex_is_masked_and_a_hash_is_not(rules, text, masked):
    got = values(text, Pipeline(rules, None, pii=False).detect(text))
    assert any(_K64[:32] in g for g in got) is masked


@pytest.mark.parametrize("text", [
    # An f-string or format reference is not the value (self-01 live, Nov 18).
    'self.assertEqual(scan(f"token = {GITHUB}"), [("github-token", GITHUB)])',
    'url = f"https://api.example.com/?token={self.token}"',
    'headers = {"Authorization": f"Bearer {token}"}; token = {cfg["token"]}',
    # keepmum's own placeholder, cut short by the model.
    "token = KEEPMUM_SECRET_a8",
])
def test_a_reference_or_a_cut_placeholder_is_not_a_secret(rules, text):
    assert Pipeline(rules, None, pii=False).detect(text) == []


@pytest.mark.parametrize("text, value", [
    ('token = "{Zq3bX8mL2vT9wR4kY7nP1sD6}"', "Zq3bX8mL2vT9wR4kY7nP1sD6"),  # a value in braces is still one
    ('payload = {"token": "ghp_8XVYhnYbsusja9YDeSu4dN3xO1tmeSYEyoVS"}', "ghp_8XVYhnYbsusja9YDeSu4dN3xO1tmeSYEyoVS"),
])
def test_a_secret_near_braces_is_still_masked(rules, text, value):
    assert any(value in v for v in values(text, Pipeline(rules, None, pii=False).detect(text)))


@pytest.mark.parametrize("text, masked", [
    # A fake token made of the alphabet in order, as an agent writes for a test (self-01, Nov 18).
    ('SLACK = "xoxb-' '123456789012-123456789012-AbCdEfGhIjKlMnOpQrStUvWx"', False),  # split: GitHub's push protection takes a whole fake key for a real one
    ("api_key = abcdefghijklmnopqrstuvwxyz0123456789", False),
    # One such run in a real password does not make it a fake.
    ("password = Password0123456789!", True),
    ("api_key = Zq3bX8mL2vT9wR4kY7nP1sD6abcdef", True),
])
def test_a_value_made_of_the_alphabet_in_order_is_a_placeholder(rules, text, masked):
    assert bool(Pipeline(rules, None, pii=False).detect(text)) is masked


def _guess(text: str, piece: str) -> list[Span]:
    return [Span(text.index(piece), text.index(piece) + len(piece), "SECRET", 0.9, "pf-serve")]


def test_a_guess_that_adds_the_host_to_a_password_gives_way_to_it(rules):
    # The model ran a span from the password to the end of the URL (k8s-01, Nov 18).
    pw = "h0pCNu6g3HluMilUuS"
    text = f"DATABASE_URL=postgres://orders:{pw}@db.internal:5432/orders\n"
    got = values(text, Pipeline(rules, _FakePF(_guess(text, f"{pw}@db.internal:5432/orders")), pii=False).detect(text))
    assert got == [pw]
    # From one character into the password (agy, k8s-01).
    got = values(text, Pipeline(rules, _FakePF(_guess(text, f"{pw[1:]}@db.internal:5432/orders")), pii=False).detect(text))
    assert got == [pw]


def test_a_guess_with_more_than_the_host_around_a_rule_span_is_kept(rules):
    pw = "h0pCNu6g3HluMilUuS"
    text = f"postgres://orders:{pw}@db.internal:5432/orders?sslkey=Zq3bX8mL2vT9wR4kY7nP1sD6\n"
    piece = f"{pw}@db.internal:5432/orders?sslkey=Zq3bX8mL2vT9wR4kY7nP1sD6"
    got = values(text, Pipeline(rules, _FakePF(_guess(text, piece)), pii=False).detect(text))
    assert any("Zq3bX8mL2vT9wR4kY7nP1sD6" in g for g in got)


@pytest.mark.parametrize("text, piece, kept", [
    # A constant's name the model took for a secret (k8s-01, Nov 18).
    ("data:\n  STRIPE_SECRET_KEY: info\n", "STRIPE_SECRET_KEY", None),
    # A CSV row: the id is not a secret, and no field is.
    ("2: 5e1f0c9a-2d7b-4c3e-9a8f-0b6d4e2c1f73,us,12000,paid", "5e1f0c9a-2d7b-4c3e-9a8f-0b6d4e2c1f73,us", None),
    # A key in JSON text keeps none of the escapes after it (nb-01, Nov 18).
    ('os.environ[\\"KEY\\"] = \\"Zq3bX8mL2vT9wR4kY7nP1sD6fH0jC5aE\\"\\n", "df',
     'Zq3bX8mL2vT9wR4kY7nP1sD6fH0jC5aE\\"\\n', "Zq3bX8mL2vT9wR4kY7nP1sD6fH0jC5aE"),
    ('key = \\"Zq3bX8mL2vT9wR4kY7nP1sD6fH0jC5aE\\\\', 'Zq3bX8mL2vT9wR4kY7nP1sD6fH0jC5aE\\\\', "Zq3bX8mL2vT9wR4kY7nP1sD6fH0jC5aE"),
])
def test_model_spans_are_cut_to_the_value(rules, text, piece, kept):
    got = values(text, Pipeline(rules, _FakePF(_guess(text, piece)), pii=False).detect(text))
    assert got == ([kept] if kept else [])


@pytest.mark.parametrize("text, masked", [
    ("secret = directory / 'secret.yaml'", False),  # a variable in an expression (k8s-01 on Codex)
    ("token = base + suffix", False),
    ("password = sunshine", True),  # a lone word is still a password
])
def test_a_variable_in_an_expression_is_not_a_password(rules, text, masked):
    assert bool(Pipeline(rules, None, pii=False).detect(text)) is masked


def test_a_token_in_a_git_remote_is_masked_alone(rules):
    # `git remote -v` with a token in the URL: the host and repository stay readable (git-01, Nov 18).
    tok = "ghp_6TzJ7vdMTJmVIR0kiTOK1Fi6mP2TuhaaGwUc"
    text = f"origin\thttps://x-access-token:{tok}@github.com/acme/orders.git (fetch)\n"
    assert values(text, Pipeline(rules, _FakePF(_guess(text, f"{tok}@github.com/acme/orders.git")), pii=False).detect(text)) == [tok]


@pytest.mark.parametrize("text, piece", [
    ("  integrity sha512-Z4jtVdSClePIEelBxFvNUshyE0MlW0ctYN2sR+wZeJbR01ScVkrUAGoUh0TMvJJOGeIcUPajaB7Fq0mcoKtGhw==",
     "sha512-Z4jtVdSClePIEelBxFvNUshyE0MlW0ctYN2sR+wZeJbR01ScVkrUAGoUh0TMvJJOGeIcUPaja"),
    ("golang.org/x/crypto v0.31.0/go.mod h1:kLxQiBxrJ0JAHbCXUGC7/dwLyB1GRmqHQFMDA9Jan1Q=", "h1:kLxQiBxrJ0JAHbCXUGC7/dwLyB1GRmqHQFMDA9Jan1Q"),
    ('stripe.api_key = os.environ["STRIPE_SECRET_KEY"]', 'os.environ["STRIPE_SECRET_KEY'),
    ("< set-cookie: session=…; Path=/", "set-cookie"),
    ("GET /v2/orders/ord_7Hc2 HTTP/2", "ord_7Hc2"),
    ("9: contexts:\n10: - name: orders-prod", "contexts"),
    ('"endpoint": "orders-prod.c8x2kq1v9wze.us-west-2.rds.amazonaws.com:5432"', "orders-prod.c8x2kq1v9wze.us-west-2.rds.amazonaws.com:5432"),
])
def test_public_text_the_model_took_for_secrets(rules, text, piece):
    assert Pipeline(rules, _FakePF(_guess(text, piece)), pii=False).detect(text) == []


@pytest.mark.parametrize("text, piece, kind", [
    ("drwxr-xr-x  3 yoga staff  96 Nov 18 18:01 .codex", ".codex", "PERSON"),
    ("- Gmail (gmail@openai-curated-remote)", "gmail@openai-curated-remote", "EMAIL"),
    ("-rw-r--r--  1 yoga staff  128 Nov 18 18:01 report.md", "128 Nov 18", "ADDRESS"),
    ("| 北京 | 3 |", "北京", "ADDRESS"),
    ("| 北京 | 3 |", "北京", "PERSON"),
    ("total 8\\n-rw-r--r--  1 yoga  staff  1418 Nov 18 report.md", "n-rw-r--r", "PERSON"),  # JSON text: an escaped new line
])
def test_personal_data_guesses_that_are_not(rules, text, piece, kind):
    pf = _FakePF([Span(text.index(piece), text.index(piece) + len(piece), kind, 0.9, "pf-serve")])
    assert Pipeline(rules, pf, pii=True).detect(text) == []


@pytest.mark.parametrize("text, piece, kind", [
    ("Ship to 北京市朝阳区建国路88号", "北京市朝阳区建国路88号", "ADDRESS"),
    ("Contact 张伟 for access", "张伟", "PERSON"),
    ("mail margaret.thornbury@gmail.com today", "margaret.thornbury@gmail.com", "EMAIL"),
])
def test_personal_data_guesses_that_are(rules, text, piece, kind):
    pf = _FakePF([Span(text.index(piece), text.index(piece) + len(piece), kind, 0.9, "pf-serve")])
    assert values(text, Pipeline(rules, pf, pii=True).detect(text)) == [piece]


@pytest.mark.parametrize("text", [
    # A session cookie is a login (debug-01 on Codex, Nov 18).
    "> Cookie: session=Kq7vX2mLp9RtW4zB8n; theme=dark",
    "< set-cookie: sessionid=Kq7vX2mLp9RtW4zB8n; Path=/; HttpOnly",
    'curl -b "connect.sid=s%3AKq7vX2mLp9RtW4zB8n.abc" https://x.example',
    "Set-Cookie: _shop_session=Kq7vX2mLp9RtW4zB8nQ; path=/",
])
def test_a_session_cookie_is_masked(rules, text):
    got = values(text, Pipeline(rules, None, pii=False).detect(text))
    assert any("Kq7vX2mLp9RtW4zB8n" in g for g in got) and not any("theme" in g or "Path" in g for g in got)


@pytest.mark.parametrize("text, piece, kept", [
    # The model's span over a whole .npmrc line, and over code with an escaped line break (git-01).
    ("//registry.npmjs.org/:_authToken=npm_8Hi4ikv5wNJXBooVCaQr60IS6UmSVl4kWB5w\n",
     "registry.npmjs.org/:_authToken=npm_8Hi4ikv5wNJXBooVCaQr60IS6UmSVl4kWB5w", "npm_8Hi4ikv5wNJXBooVCaQr60IS6UmSVl4kWB5w"),
    ('"    return None\\nfor line in lines:"', "None\\nfor", None),
])
def test_a_model_span_is_cut_at_a_name_and_at_escaped_breaks(rules, text, piece, kept):
    got = values(text, Pipeline(rules, _FakePF(_guess(text, piece)), pii=False).detect(text))
    assert got == ([kept] if kept else [])


@pytest.mark.parametrize("text, piece, kind", [
    # Found by the live matrix of Nov 18.
    ('"endpoint": "orders-prod.c8x2kq1v9wze.us-west-2.rds.amazonaws.com:5432"', "orders-prod.c8x2kq1v9wze.us-west-2.rds", "SECRET"),
    ("golang.org/x/crypto v0.31.0 h1:68RbgsSwMRnuFwGRYM5pbHUGTV0kGOXOutW9t/kK3sk=", "68RbgsSwMRnuFwGRYM5pbHUGTV0kGOXOutW9t/kK3sk", "SECRET"),
    ("integrity sha512-Z4jtVdSClePIEelBxFvNUshyE0MlW0ctYN2sR+wZeJbR01ScVkrUAGoUh0TMvJJOGeIcUPajaB7Fq0mcoKtGhw==",
     "BxFvNUshyE0MlW0ctYN2sR+wZeJbR01ScVkrUAGoUh0TMvJJOGeIcUPajaB7Fq0mcoKtGhw", "SECRET"),
    ("rg -n 'STRIPE_KEY|STRIPE_SECRET_KEY|sk_(live|test)_' .", "STRIPE_KEY|STRIPE_SECRET_KEY|sk_(live|test)_", "SECRET"),
    ("grep -oE 'sha512?-?[A-Za-z0-9+/=]+' web/yarn.lock", "?-?[A-Za-z0-9+/=]+", "SECRET"),
    ("< set-cookie: session=x; Path=/; HttpOnly; Secure", "HttpOnly", "SECRET"),
    # Claude Code's own instructions, a nonce begun at its hyphen, a key file's path begun mid-word (Nov 18).
    ("OR `grep -rE 'openai|langchain_openai|google.generativeai|genai|mistralai|cohere|ollama'` over the project",
     "openai|langchain_openai|google.generativeai|genai|mistralai|cohere|ollama", "SECRET"),
    ("script-src 'self' 'nonce-q7Xv2LmP9kR4tW8z'", "-q7Xv2LmP9kR4tW8z", "SECRET"),
    ("- Key: keys/deploy_ed25519", "ploy_ed25519", "SECRET"),
    ("256 SHA256:tsr02mISijUYvTRENTacI6TVaZdJLqUJp02U0osx4Xs deploy@ci (ED25519)", "tsr02mISijUYvTRENTacI6TVaZdJLqUJp02U0osx4Xs", "SECRET"),
    ("13:> Authorization: Bearer [omitted]\\n18:< x-request-id: 4e8a1c2f-7b3d-4a6e-9f05-c1d2b3a4e5f6\\n",
     "4e8a1c2f-7b3d-4a6e-9f05-c1d2b3a4e5f6", "SECRET"),
])
def test_more_public_text_the_model_took_for_secrets(rules, text, piece, kind):
    pf = _FakePF([Span(text.index(piece), text.index(piece) + len(piece), kind, 0.9, "pf-serve")])
    assert Pipeline(rules, pf, pii=False).detect(text) == []


@pytest.mark.parametrize("text, piece", [
    # Documents (doc-01..03, Nov 18).
    ("</JETSKI_OUTPUT></STEP_9>", "JETSKI_OUTPUT></STEP_9"),
    ("Invoice INV-2026-0412, due 2026-12-04", "INV-2026-0412"),
    ("- OPS-2291: search results rank exact SKU matches first", "OPS-2291"),
    ("The key starts with sk_live_ and must stay in .env", "sk_live_"),
])
def test_document_text_the_model_took_for_secrets(rules, text, piece):
    assert Pipeline(rules, _FakePF(_guess(text, piece)), pii=False).detect(text) == []


def test_a_password_in_word_xml_ends_at_its_tag(rules):
    pw = "Kq7vX2mLp9RtW4zB8n"
    text = f'<w:p><w:r><w:t xml:space="preserve">Staging admin: admin / password: {pw}</w:t></w:r></w:p></w:body>'
    assert values(text, Pipeline(rules, None, pii=False).detect(text)) == [pw]


def test_a_stand_in_password_is_no_password(rules):
    assert Pipeline(rules, None, pii=False).detect("PASSWORD=mypassword") == []


def test_a_known_value_at_a_line_end_drops_the_dollar(rules):
    # `cat -A .env`: a key pasted in the prompt, masked before, then printed with the line's `$` (crypto-01).
    from keepmum.redact import Redactor
    from keepmum.vault import Vault

    key = "0x6f5a82662d9f886a8a081921597c2886db57f9ffa02253037040ddb171698574"
    r = Redactor(Pipeline(rules, None, pii=False), Vault(b"k" * 32))
    r.redact_text(f"The staging signing key is {key} - add it to .env")
    out = r.redact_text(f"STAGING_SIGNING_KEY={key}$\nAPP_ENV=dev$")
    assert out.endswith("$\nAPP_ENV=dev$") and key not in out


def test_a_token_made_of_letters_is_still_a_secret(rules):
    text = "here: QmXkZpRtWvBnLsJd"
    assert values(text, Pipeline(rules, _FakePF(_guess(text, "QmXkZpRtWvBnLsJd")), pii=False).detect(text)) == ["QmXkZpRtWvBnLsJd"]


def test_a_request_id_after_a_secret_word_on_its_line_is_still_one(rules):
    text = "token: 4e8a1c2f-7b3d-4a6e-9f05-c1d2b3a4e5f6"
    assert values(text, Pipeline(rules, _FakePF(_guess(text, "4e8a1c2f-7b3d-4a6e-9f05-c1d2b3a4e5f6")), pii=False).detect(text))


def test_a_key_in_a_sed_expression_is_masked_alone(rules):
    sk = "sk_live_" + "qwEOgtmUFB7ZdVOlpTzVnW32"  # split: GitHub's push protection takes a whole fake key for a real one
    text = f"sed -i 's/{sk}/$STRIPE_SECRET_KEY/' app/payments.py"
    assert values(text, Pipeline(rules, _FakePF(_guess(text, f"s/{sk}/")), pii=False).detect(text)) == [sk]


@pytest.mark.parametrize("text, piece, kind, kept", [
    ("张伟,+86 138 0013 8000,110101199003071234", "张伟,+86 138 0013 8000", "PHONE", "+86 138 0013 8000"),
    # A row with its line breaks escaped (JSON text): one ADDRESS over three fields (i18n-01 on Claude Code).
    ("地址\\n张伟,+86 138 0013 8000,110101199003071234,北京", "张伟,+86 138 0013 8000,110101199003071234", "ADDRESS", "+86 138 0013 8000"),
    ("北京市朝阳区建国路88号\\n王芳,+86 139", "北京市朝阳区建国路88号\\n王芳", "PERSON", "王芳"),
    ("Ship to 10 Downing St, London SW1A 2AA", "10 Downing St, London SW1A 2AA", "ADDRESS", "10 Downing St, London SW1A 2AA"),
    ("Customers: Beijing (3), Shanghai (2), Shenzhen (1)", "Beijing (3), Shanghai", "ADDRESS", None),
])
def test_personal_data_guesses_are_cut_or_dropped(rules, text, piece, kind, kept):
    pf = _FakePF([Span(text.index(piece), text.index(piece) + len(piece), kind, 0.9, "pf-serve")])
    got = values(text, Pipeline(rules, pf, pii=True).detect(text))
    assert (kept in got and piece not in [g for g in got if g != kept]) if kept else not any(piece in g for g in got)


def test_a_token_followed_by_an_at_sign_is_masked_alone(rules):
    tok = "ghp_6TzJ7vdMTJmVIR0kiTOK1Fi6mP2TuhaaGwUc"
    text = f"https://x-access-token:{tok}@github.com/acme/orders.git"
    assert values(text, Pipeline(rules, _FakePF(_guess(text, f"{tok}@")), pii=False).detect(text)) == [tok]


def _png_b64(lines: int, sep: str) -> str:
    import base64, random
    rnd = random.Random(7)
    data = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(rnd.randrange(256) for _ in range(45 * lines))
    b = base64.b64encode(data).decode()
    return sep.join(b[i:i + 60] for i in range(0, len(b), 60))


@pytest.mark.parametrize("sep", ["\n", "\\n"])
def test_a_picture_in_base64_is_not_a_secret(rules, sep):
    # GitHub's contents API wraps base64 at 60; in JSON text the wraps are escaped. The model flagged
    # the last line of a PNG Codex fetched (wf-02 on Codex, Nov 18).
    blob = _png_b64(6, sep)
    text = '{"structuredContent":{"content":"' + blob + '"}}'
    last = blob.rsplit(sep, 1)[1].rstrip("=")
    pf = _FakePF([Span(text.index(last), text.index(last) + len(last), "SECRET", 0.9, "pf-serve")])
    assert Pipeline(rules, pf, pii=False).detect(text) == []


def test_a_key_on_the_line_after_a_picture_is_still_masked(rules):
    blob = _png_b64(3, "\n")
    blob = blob.rstrip("=") + "=="  # padding ends the file
    key = "Zq3bX8mL2vT9wR4kY7nP1sD6fH0jC5aE8gUiW2"
    text = blob + "\n" + key + "\n"
    pf = _FakePF([Span(text.index(key), text.index(key) + len(key), "SECRET", 0.9, "pf-serve")])
    assert key in values(text, Pipeline(rules, pf, pii=False).detect(text))


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


def _assigned(rules, text):
    return values(text, [s for s in rules.detect(text) if s.source.endswith("secret-assignment")])


@pytest.mark.parametrize("text, value", [
    # Quoted, with spaces and shell characters: masked whole (it was not masked at all).
    ('DB_PASSWORD="p@ss w0rd $HOME!"', "p@ss w0rd $HOME!"),
    ("export DB_PASSWORD='correct horse battery staple'", "correct horse battery staple"),
    # A quote inside an unquoted password: masked whole (it was cut at the quote, the rest sent).
    ('ADMIN_PASSWORD=Tr0ub4dor"&\\3x', 'Tr0ub4dor"&\\3x'),
    ("API_SECRET=c0rrect\\horse9Zq", "c0rrect\\horse9Zq"),
    ('echo "DB_PASSWORD=Xk9$mQ2vL7pZw4" >> .env', "Xk9$mQ2vL7pZw4"),
    ('{"password":"abc123XYZ","user":"bob"}', "abc123XYZ"),
    ('conn = "Server=x;Password=s3cr3tPw!;User=y"', "s3cr3tPw!"),
    # A quote opened in another word, or an apostrophe in prose, does not hide a value.
    ("don't forget: export DB_PASSWORD='hunter2xyz9'", "hunter2xyz9"),
    ('{"env": "API_TOKEN=\'Zq8vLm2pXw4k\'"}', "Zq8vLm2pXw4k"),
])
def test_assignment_values_are_masked_whole(rules, text, value):
    assert _assigned(rules, text) == [value]


def test_a_value_ends_at_an_escaped_new_line():
    """Codex sends command output as JSON text: a value must not run on into the next lines."""
    import json

    r = RulesDetector()
    text = json.dumps({"output": f"GITHUB_TOKEN={FAKE_GITHUB}\nLOG_LEVEL=info\nDB_HOST=db.internal\n"})
    assert all(v == FAKE_GITHUB for v in values(text, r.detect(text)))
    twice = json.dumps(json.dumps({"output": "API_SECRET=c0rrect\\horse9Zq\nX=1"}))
    assert _assigned(r, twice) == ["c0rrect\\\\\\\\horse9Zq"]
    patch = 'await tools.apply_patch("*** Begin Patch\\n+API_KEY=KEEPMUM_SECRET_5a47cafc\\n+REGION=eu\\n*** End Patch\\n");'
    assert _assigned(r, patch) == []  # a placeholder is not masked again, with or without what follows
    escaped = json.dumps({"output": 'DB_PASSWORD="p@ss w0rd $HOME!"\nADMIN_PASSWORD=Tr0ub4dor"&\\3x\n'})
    assert _assigned(r, escaped) == ["p@ss w0rd $HOME!", 'Tr0ub4dor\\"&\\\\3x']


@pytest.mark.parametrize("text", [
    '"passwordHint": "Use at least 8 characters"',
    '"password": "Enter your password"',
    'password: "${{ secrets.DB_PASSWORD }}"',
    'if password == "hunter22x":',
    'f"PASSWORD={pw}"',
    # The quote after `=` closes a pattern the word opened (a live Claude Code command, base-01).
    "mkdir -p config && grep '^GITHUB_TOKEN=' .env > config/github.env && echo 'REGION=eu' >> config/github.env",
    'grep "^API_KEY=" .env | cut -d= -f2 > "out file"',
    '{"command": "grep \\"^DB_PASSWORD=\\" .env > x && echo \\"done\\""}',
])
def test_assignment_text_that_is_not_a_secret(rules, text):
    assert _assigned(rules, text) == []


def test_a_path_in_prose_is_not_a_guessed_secret():
    from keepmum.detect.rules import is_path

    assert is_path("bench/results/PHASE0") and is_path("./scripts/run-all") and is_path("docs/test-cases/")
    assert not is_path("wJalrXUtnFEMI/K7MDENG/bPxRfiCYzz9KeyQ") and not is_path("PHASE0")
    p = Pipeline(RulesDetector())
    text = "The numbers are in bench/results/PHASE0, see there. AWS: wJalrXUtnFEMI/K7MDENG/bPxRfiCYzz9KeyQ"
    assert values(text, p.detect(text)) == ["wJalrXUtnFEMI/K7MDENG/bPxRfiCYzz9KeyQ"]


def test_the_model_reads_names_in_file_names_as_words():
    from keepmum.detect.rules import file_view

    text = "clients/Margaret Thornbury_resume.md and backup-ghp_x.txt; my_var = 1"
    assert file_view(text) == "clients/Margaret Thornbury resume.md and backup-ghp x.txt; my_var = 1"
    assert len(file_view(text)) == len(text)


@pytest.mark.parametrize("text, found, name", [
    ("clients/Margaret Thornbury_resume.md", "clients/Margaret Thornbury_resume.md", "Margaret Thornbury"),
    ("Margaret_Thornbury_cover_letter.md", "Margaret_Thornbury_cover_letter.md", "Margaret_Thornbury"),
    ("Margaret Thornbury.pdf", "Margaret Thornbury.pdf", "Margaret Thornbury"),
    ("Ask Mary-Jane O'Neil.", "Mary-Jane O'Neil", "Mary-Jane O'Neil"),
])
def test_a_name_found_in_a_file_name_is_only_the_name(text, found, name):
    start = text.index(found)
    span = Pipeline.trim_pii(text, Span(start, start + len(found), "PERSON", 0.9, "pf-serve"))
    assert text[span.start:span.end] == name


def test_no_personal_data_span_runs_into_a_file_name():
    """The model may give a second span (an address) over the file name; trimmed too, it cannot
    merge the name back into `Margaret Thornbury_cover`."""
    text = "clients/Margaret Thornbury_cover_letter.md"
    start = text.index("Thornbury")
    span = Pipeline.trim_pii(text, Span(start, text.index(".md"), "ADDRESS", 0.6, "pf-serve"))
    assert text[span.start:span.end] == "Thornbury"
    email = "write to margaret_t@example.org"
    e = Pipeline.trim_pii(email, Span(9, len(email), "EMAIL", 0.9, "pf-serve"))
    assert email[e.start:e.end] == "margaret_t@example.org"
