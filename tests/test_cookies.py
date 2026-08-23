import pytest

from pagecapture.cookies import PREFER_ALLOW, ROLE_NAME, SKIP_LABEL, VISIBLE_LABEL


@pytest.mark.parametrize(
    "label",
    [
        "Accept all",
        "Allow all",
        "Accept additional cookies",
        "Reject additional cookies",
        "Allow required",
        "Accept cookies",
    ],
)
def test_visible_label_matches_banner_buttons(label: str):
    assert VISIBLE_LABEL.search(label)


@pytest.mark.parametrize(
    "label",
    [
        "Cookie policy",
        "View cookies",
        "Manage cookies",
        "Cookie settings",
        "Learn more",
        "Customise",
    ],
)
def test_skip_label_avoids_policy_and_settings(label: str):
    assert SKIP_LABEL.search(label)
    assert not VISIBLE_LABEL.search(label)


def test_role_name_matches_youtube_aria_label():
    name = "Accept the use of cookies and other data for the purposes described"
    assert ROLE_NAME.search(name)
    assert PREFER_ALLOW.search(name)


def test_prefer_allow_ranks_accept_over_reject():
    assert PREFER_ALLOW.search("Accept all")
    assert not PREFER_ALLOW.search("Reject all")
