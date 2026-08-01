from guvcfd import app as guvcfd_app
from guvcfd.scenario_runs import _ach_label, _subdir_name


def test_sealed_room_error_none_when_ach_positive():
    assert guvcfd_app._sealed_room_error("decay", 3.0, False) is None
    assert guvcfd_app._sealed_room_error("steady_state", [1.0, 2.0], True) is None


def test_sealed_room_error_rejects_steady_state():
    msg = guvcfd_app._sealed_room_error("steady_state", 0.0, True)
    assert msg is not None and "Decay" in msg


def test_sealed_room_error_requires_fan_in_decay_mode():
    msg = guvcfd_app._sealed_room_error("decay", 0.0, False)
    assert msg is not None and "fan" in msg.lower()

    assert guvcfd_app._sealed_room_error("decay", 0.0, True) is None


def test_sealed_room_error_checks_any_value_in_a_sweep_list():
    # A sweep with a mix of positive and sealed ACH values is still
    # rejected/accepted based on the sealed one(s).
    assert guvcfd_app._sealed_room_error("decay", [3.0, 0.0], True) is None
    assert guvcfd_app._sealed_room_error("decay", [3.0, 0.0], False) is not None
    assert guvcfd_app._sealed_room_error("steady_state", [3.0, -1.0], True) is not None


def test_ach_label_and_subdir_name_use_sealed_not_zero():
    assert _ach_label(0.0) == "sealed"
    assert _ach_label(-1.0) == "sealed"
    assert _ach_label(3.0) == "3"
    assert _subdir_name(2.0, 0.0) == "Z2_ACHsealed"
