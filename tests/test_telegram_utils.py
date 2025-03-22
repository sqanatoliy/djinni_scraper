import pytest
from djinni_scraper.utils.telegram_utils import Telegram


@pytest.mark.parametrize("input_text, expected_output", [
    ("Simple text", "Simple text"),
    ("*Text with asterix", r"\*Text with asterix"),
    ("*Bold*", r"\*Bold\*"),
    ("Escape: [link](url)", r"Escape: \[link\]\(url\)"),
    ("`Code` and ’apostrophe’", "'Code' and 'apostrophe'"),
])
def test_clean_text_for_telegram(input_text, expected_output):
    assert Telegram._clean_text_for_telegram(input_text) == expected_output
