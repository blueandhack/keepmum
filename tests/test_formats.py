"""The rules without the model: provider formats, secrets in flags and prose, and personal data."""

import base64
import secrets
import string
import sys
from pathlib import Path

import pytest

from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector

sys.path.insert(0, str(Path(__file__).parent.parent / "bench"))
from negatives import HOLDOUT5_NEGATIVES  # noqa: E402

# Synthetic values only, built at run time: random strings in provider formats, never real credentials.
UPPER, LOWER, DIGITS = string.ascii_uppercase, string.ascii_lowercase, string.digits
ALNUM, HEX, B64URL = UPPER + LOWER + DIGITS, "0123456789abcdef", UPPER + LOWER + DIGITS + "-_"


def rnd(chars, n):
    return "".join(secrets.choice(chars) for _ in range(n))


@pytest.fixture(scope="module")
def rules():
    return RulesDetector()


def found(rules, text, pii=False):
    """(value, type, the rules that found it): merged spans join their sources with |."""
    p = Pipeline(rules, None, pii=pii)
    return [(text[s.start:s.end], s.type, set(s.source.split("|"))) for s in p.detect(text)]


PROVIDERS = [
    ("groq-api-key", lambda: "gsk_" + rnd(ALNUM, 52)),
    ("xai-api-key", lambda: "xai-" + rnd(ALNUM, 80)),
    ("openrouter-api-key", lambda: "sk-or-v1-" + rnd(HEX, 64)),
    ("replicate-api-token", lambda: "r8_" + rnd(ALNUM, 37)),
    ("huggingface-org-token", lambda: "api_org_" + rnd(UPPER + LOWER, 34)),
    ("langsmith-api-key", lambda: "lsv2_pt_" + rnd(HEX, 32) + "_" + rnd(HEX, 10)),
    ("aws-bedrock-api-key", lambda: "ABSK" + rnd(ALNUM + "+/", 132)),
    ("google-oauth-access-token", lambda: "ya29." + rnd(B64URL, 120)),
    ("azure-ad-client-secret", lambda: rnd(ALNUM, 3) + "8Q~" + rnd(ALNUM + "_~.-", 34)),
    ("alibaba-access-key-id", lambda: "LTAI" + rnd(ALNUM, 20)),
    ("gitlab-token", lambda: "glrt-" + rnd(B64URL, 20)),
    ("gitlab-token", lambda: "glptt-" + rnd(HEX, 40)),
    ("docker-hub-token", lambda: "dckr_pat_" + rnd(B64URL, 27)),
    ("pypi-token", lambda: "pypi-AgEIcHlwaS5vcmc" + rnd(B64URL, 70)),
    ("rubygems-token", lambda: "rubygems_" + rnd(HEX, 48)),
    ("supabase-access-token", lambda: "sbp_" + rnd(HEX, 40)),
    ("planetscale-token", lambda: "pscale_tkn_" + rnd(B64URL, 43)),
    ("databricks-token", lambda: "dapi" + rnd(HEX, 32)),
    ("heroku-api-key", lambda: "HRKU-AA" + rnd(B64URL, 58)),
    ("vault-token", lambda: "hvs." + rnd(B64URL, 95)),
    ("terraform-cloud-token", lambda: rnd(LOWER + DIGITS, 14) + ".atlasv1." + rnd(LOWER + DIGITS + "-_", 66)),
    ("pulumi-token", lambda: "pul-" + rnd(HEX, 40)),
    ("doppler-token", lambda: "dp.pt." + rnd(ALNUM, 43)),
    ("1password-token", lambda: "ops_eyJ" + rnd(ALNUM + "+/", 260)),
    ("age-secret-key", lambda: "AGE-SECRET-KEY-1" + rnd("QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L", 58)),
    ("grafana-token", lambda: "glsa_" + rnd(ALNUM, 32) + "_" + rnd(HEX, 8)),
    ("new-relic-key", lambda: "NRAK-" + rnd(UPPER + DIGITS, 27)),
    ("sentry-token", lambda: "sntryu_" + rnd(HEX, 64)),
    ("dynatrace-token", lambda: "dt0c01." + rnd(UPPER + DIGITS, 24) + "." + rnd(UPPER + DIGITS, 64)),
    ("postman-api-key", lambda: "PMAK-" + rnd(HEX, 24) + "-" + rnd(HEX, 34)),
    ("linear-api-key", lambda: "lin_api_" + rnd(ALNUM, 40)),
    ("notion-token", lambda: "ntn_" + rnd(DIGITS, 11) + rnd(ALNUM, 35)),
    ("shopify-token", lambda: "shpat_" + rnd(HEX, 32)),
    ("square-access-token", lambda: "EAAA" + rnd(B64URL, 60)),
    ("sendinblue-key", lambda: "xkeysib-" + rnd(HEX, 64) + "-" + rnd(ALNUM, 16)),
    ("slack-app-token", lambda: f"xapp-1-A{rnd(UPPER + DIGITS, 10)}-{rnd(DIGITS, 13)}-{rnd(HEX, 64)}"),
    ("telegram-bot-token", lambda: rnd(DIGITS, 10) + ":AA" + rnd(B64URL, 33)),
    ("mailchimp-key", lambda: rnd(HEX, 32) + "-us14"),
]


@pytest.mark.parametrize("rule,make", PROVIDERS, ids=[f"{r}-{i}" for i, (r, _) in enumerate(PROVIDERS)])
def test_provider_formats_with_no_telling_name(rules, rule, make):
    value = make()
    for text in (f"export SVC={value}\n", f"here it is: {value} (rotate it after the demo)"):
        assert any(v == value and k == "SECRET" and f"rules:{rule}" in src for v, k, src in found(rules, text))


@pytest.mark.parametrize("template,rule", [
    ("./migrate --url https://ops.acme.dev --token {v} --dry-run", "secret-flag"),
    ("mysqldump -u backup --password={v} shop", "secret-flag"),
    ("acme deploy --api-key {v}", "secret-flag"),
    ("curl -u admin:{v} https://jenkins.acme.dev/api/json", "curl-user-password"),
    ("fyi the password for the build box is {v}", "password-in-prose"),
    ("The staging API key is {v}, it expires on Friday.", "key-in-prose"),
    ("Hey, the Datadog key for staging is {v} - please rotate it", "key-in-prose"),
    ("machine api.heroku.com\n  login me\n  password {v}\n", "netrc-password"),
    ('{{"auths": {{"ghcr.io": {{"auth": "{v}"}}}}}}', "docker-config-auth"),
    ("//registry.npmjs.org/:_auth={v}", "npmrc-auth"),
    ("users:\n- name: admin\n  user:\n    client-key-data: {v}", "kubeconfig-client-key"),
    ("DefaultEndpointsProtocol=https;AccountName=acme;AccountKey={v};EndpointSuffix=core.windows.net", "azure-storage-key"),
    ("https://acme.s3.amazonaws.com/q3.csv?X-Amz-Expires=3600&X-Amz-Signature={v}", "signed-url-signature"),
    ("GET / HTTP/1.1\nCookie: theme=dark; session_id={v}", "session-cookie"),
])
def test_secrets_in_flags_prose_and_config_files(rules, template, rule):
    shapes = {"docker-config-auth": lambda: base64.b64encode(f"bot:{rnd(ALNUM, 24)}".encode()).decode(),
              "kubeconfig-client-key": lambda: base64.b64encode(secrets.token_bytes(120)).decode(),
              "azure-storage-key": lambda: base64.b64encode(secrets.token_bytes(64)).decode(),
              "npmrc-auth": lambda: base64.b64encode(f"bot:{rnd(ALNUM, 24)}".encode()).decode(),
              "signed-url-signature": lambda: rnd(HEX, 64)}
    value = shapes.get(rule, lambda: rnd(LOWER, 8) + rnd(DIGITS, 4) + rnd(UPPER, 8))()
    text = template.format(v=value)
    assert any(v == value and f"rules:{rule}" in src for v, _, src in found(rules, text)), found(rules, text)


@pytest.mark.parametrize("name,text", HOLDOUT5_NEGATIVES)
def test_holdout5_negatives(rules, name, text):
    assert found(rules, text, pii=True) == []


@pytest.mark.parametrize("text,value,kind", [
    ("email=jane.roe@fastmail.com plan=pro", "jane.roe@fastmail.com", "EMAIL"),
    ("Reported-by: Jane Roe <jane_roe@protonmail.com>", "jane_roe@protonmail.com", "EMAIL"),
    ('<a href="mailto:jane.roe@fastmail.com">mail</a>', "jane.roe@fastmail.com", "EMAIL"),
    ("call (415) 555-2671 after six", "(415) 555-2671", "PHONE"),
    ("phone=+1 415 555 2671", "+1 415 555 2671", "PHONE"),
    ("reach me on +49 30 1234567", "+49 30 1234567", "PHONE"),
    ("mobile: 4155552671", "4155552671", "PHONE"),
    ("SSN: 123-45-6789", "123-45-6789", "ACCOUNT"),
    ('"date_of_birth": "1985-03-02"', "1985-03-02", "DATE"),
    ("I was born March 3, 1990 in case you need it", "March 3, 1990", "DATE"),
])
def test_personal_data_without_the_model(rules, text, value, kind):
    assert [(v, k) for v, k, _ in found(rules, text, pii=True)] == [(value, kind)]
    assert all(k == "SECRET" for _, k, _ in found(rules, text, pii=False))  # personal data only with pii = true


@pytest.mark.parametrize("text", [
    "Reply-To: support@acme-shop.com",  # a role mailbox (RFC 2142)
    "From: <noreply@github.com>",
    "contact jane@example.com",  # reserved for examples (RFC 2606)
    "call 212-555-0147",  # fictional (555-0100..0199)
    "ssn = 000-12-3456",  # no SSN starts with 000
])
def test_personal_data_that_is_not_a_person(rules, text):
    assert found(rules, text, pii=True) == []


def test_an_email_inside_a_password_is_part_of_the_secret(rules):
    text = 'DATABASE_URL="mongodb+srv://svc:a.cI%9x#Qp7Lmz@cluster0.x7k2p.mongodb.net/prod"'
    assert [(v, k) for v, k, _ in found(rules, text, pii=True)] == [("a.cI%9x#Qp7Lmz", "SECRET")]


# A real token can hold a run of one character by chance; a placeholder is mostly such runs. The
# filter used to drop any value with six of a kind in a row: every token below went unmasked, one
# Slack app token in about 6,700 among random ones (Nov 17, 2026).
# Built from pieces, as the tokens above are made at run time: a whole token in a provider's format
# written in the source is, to GitHub's push protection, a secret committed.
RUN_TOKENS = [
    ("slack-app-token", "xapp" + "-1-ALVPJUUMHP0-8444444642701-381b256fd9f5aef869674b3148ab1fedae4f07eec431b1424da578ac4c9381c7"),
    ("slack-app-token", "xapp" + "-1-ATT9OJ0IXLN-3427304062570-aebcaa054edd3977eea00b8de0d192ffae0d146f6a8cd00e4369bfffffff8822"),
    ("openrouter-api-key", "sk-or" + "-v1-0c8f2e00000003b7d9a51c6e04f8b2a7d3c9e1f5b0a8d6c4e2f7a9b1c3d5e7f9"),
    ("groq-api-key", "gsk" + "_Qm7ZxR2pLkW9vN4tYcH8jSaaaaaaaB3fD6gU1oE5iP0rT2wX7qMn"),
]
PLACEHOLDERS = [
    "ghp" + "_" + "x" * 36, "sk-proj" + "-abc123" + "x" * 30, "AKIA" + "X" * 16,
    "xoxb" + "-000000000000-000000000000-" + "x" * 24, "glpat" + "-" + "x" * 20, "sk_live" + "_" + "0" * 24,
    "xapp" + "-1-AXXXXXXXXXX-0000000000000-" + "x" * 64, "*" * 24,
]


@pytest.mark.parametrize("rule,value", RUN_TOKENS, ids=[f"{r}-{i}" for i, (r, _) in enumerate(RUN_TOKENS)])
def test_a_token_with_a_run_of_one_character_is_still_a_secret(rules, rule, value):
    for text in (f"export SVC={value}\n", f"here it is: {value} (rotate it after the demo)"):
        assert any(v == value and k == "SECRET" and f"rules:{rule}" in src for v, k, src in found(rules, text))


@pytest.mark.parametrize("value", PLACEHOLDERS)
def test_a_placeholder_made_of_runs_is_still_left_alone(rules, value):
    for text in (f"export SVC={value}\n", f"GITHUB_TOKEN={value}\n", f'api_key = "{value}"\n', f"here it is: {value} (rotate it after the demo)"):
        assert not [v for v, k, _ in found(rules, text) if k == "SECRET" and value in v], text


def test_runs_mark_a_placeholder_from_a_third_of_the_value(rules):
    from keepmum.detect import rules as rules_mod

    assert rules_mod.REPEATED_SHARE == pytest.approx(1 / 3)
    assert rules.looks_like_placeholder("xxxxxx") and rules.looks_like_placeholder("abcdefghijkl000000")  # 6 of 18
    assert not rules.looks_like_placeholder("abcdefghijklmnopq000000")  # 6 of 23
    assert not rules.looks_like_placeholder("abcdefghijklmnop00000")  # a run of 5 is no run
