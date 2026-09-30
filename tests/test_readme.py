import re
from pathlib import Path

README = (Path(__file__).resolve().parent.parent / "README.md").read_text()


def _slug(heading: str) -> str:
    text = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s", "-", text)


def test_readme_states_credentials_and_usage_policy():
    assert "Every profile must be an account that is yours" in README
    assert "https://www.anthropic.com/legal/aup" in README


def test_every_anchor_link_resolves_to_a_heading():
    headings = {_slug(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.+)$", README, re.M)}
    anchors = set(re.findall(r"\]\(#([^)]+)\)", README))
    assert anchors, "README has no anchor links"
    assert anchors <= headings, anchors - headings


def test_readme_says_what_the_update_check_sends_and_how_to_stop_it():
    assert "asks pypi.org for the latest version" in README
    assert "QUOTALENS_NO_UPDATE_CHECK=1" in README
    assert "It never updates itself." in README
