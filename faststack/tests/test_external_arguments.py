import pytest

from faststack.io.arguments import parse_external_arguments


def test_windows_arguments_remove_quotes_and_preserve_unicode_and_backslashes():
    value = (
        r'--profile "C:\Program Files\RawTherapee\設定 profile.pp3" '
        r'--source C:\photos\stack --label ""'
    )

    assert parse_external_arguments(value, windows=True) == [
        "--profile",
        r"C:\Program Files\RawTherapee\設定 profile.pp3",
        "--source",
        r"C:\photos\stack",
        "--label",
        "",
    ]


def test_windows_quoted_path_is_one_exact_argv_element():
    assert parse_external_arguments(
        r'"D:\Tool Profiles\wide gamut.pp3"', windows=True
    ) == [r"D:\Tool Profiles\wide gamut.pp3"]


def test_windows_backslashes_before_literal_quote_follow_crt_rules():
    assert parse_external_arguments(r'--name "say \"hello\""', windows=True) == [
        "--name",
        'say "hello"',
    ]


def test_posix_arguments_keep_empty_and_unicode_elements():
    assert parse_external_arguments('--label "" --name "café image"') == [
        "--label",
        "",
        "--name",
        "café image",
    ]


def test_unclosed_quote_is_rejected():
    with pytest.raises(ValueError, match="closing quotation"):
        parse_external_arguments('"C:\\Program Files', windows=True)
