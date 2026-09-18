

# -- studeo RA shape --------------------------------------------------------
def test_a_correctly_formatted_ra_passes():
    from pap.ops.doctor import OK, _check_studeo_username

    assert _check_studeo_username("12345678-9").state == OK


def test_the_right_digits_with_the_hyphen_misplaced_is_caught():
    """Regression from five days of silent failure: STUDEO_USERNAME was
    `7654321-89` instead of `12345678-9`. Same nine digits, hyphen one position
    to the left, and Studeo answers a plain 401 — indistinguishable from a wrong
    password, which is where the search naturally goes."""
    from pap.ops.doctor import MISSING, _check_studeo_username

    check = _check_studeo_username("7654321-89")
    assert check.state == MISSING
    assert "nine digits are right" in check.detail
    assert "########-#" in check.detail


def test_the_report_never_prints_the_ra_itself():
    """doctor's output is safe to paste anywhere; that property is the whole
    point of it reporting shapes rather than values."""
    from pap.ops.doctor import _check_studeo_username

    detail = _check_studeo_username("7654321-89").detail
    assert "7654321" not in detail
    assert "#######-##" in detail


def test_a_missing_ra_is_reported_without_a_format_lecture():
    from pap.ops.doctor import MISSING, _check_studeo_username

    check = _check_studeo_username("")
    assert check.state == MISSING
    assert not check.detail


def test_a_wholly_wrong_ra_still_names_the_expected_shape():
    from pap.ops.doctor import MISSING, _check_studeo_username

    check = _check_studeo_username("emanuel@example.com")
    assert check.state == MISSING
    assert "########-#" in check.detail
