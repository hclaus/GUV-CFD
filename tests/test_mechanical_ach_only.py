from guvcfd import app as guvcfd_app
from guvcfd.qtapp import helpers as qtapp_helpers


def test_mechanical_ach_only_error_none_when_disabled():
    assert guvcfd_app._mechanical_ach_only_error("decay", 0.0, False) is None
    assert guvcfd_app._mechanical_ach_only_error("steady_state", -1.0, False) is None


def test_mechanical_ach_only_error_rejects_steady_state():
    msg = guvcfd_app._mechanical_ach_only_error("steady_state", 3.0, True)
    assert msg is not None and "Decay" in msg


def test_mechanical_ach_only_error_rejects_ach_at_or_below_zero():
    msg = guvcfd_app._mechanical_ach_only_error("decay", 0.0, True)
    assert msg is not None and "ventilation" in msg.lower()

    assert guvcfd_app._mechanical_ach_only_error("decay", 3.0, True) is None


def test_mechanical_ach_only_error_checks_any_value_in_a_sweep_list():
    assert guvcfd_app._mechanical_ach_only_error("decay", [3.0, 6.0], True) is None
    assert guvcfd_app._mechanical_ach_only_error("decay", [3.0, 0.0], True) is not None


def test_qtapp_mechanical_ach_only_error_mirrors_app():
    assert qtapp_helpers.mechanical_ach_only_error("decay", 3.0, False) is None
    assert qtapp_helpers.mechanical_ach_only_error("steady_state", 3.0, True) is not None
    assert qtapp_helpers.mechanical_ach_only_error("decay", 0.0, True) is not None
    assert qtapp_helpers.mechanical_ach_only_error("decay", 3.0, True) is None
