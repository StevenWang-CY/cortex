"""Tests for ZombieReadingDetector — passive reading interception."""
import pytest
from cortex.services.state_engine.zombie_detector import ZombieReadingDetector


# All tests start at t=1000 to avoid the cooldown check (last_trigger=0.0, cooldown=300)

class TestZombieReadingDetector:
    def test_triggers_after_sustained_hypo_in_browser(self):
        """120s HYPO + browser + low mouse + elevated blink → fires."""
        detector = ZombieReadingDetector(blink_baseline=17.0, min_duration=90.0, cooldown=0.0)
        base_t = 1000.0
        for i in range(400):
            t = base_t + float(i * 0.5)
            result = detector.update(
                state="HYPO",
                mouse_velocity=10.0,
                blink_rate=22.0,  # above 17 * 1.15 = 19.55
                active_app="Google Chrome",
                current_time=t,
            )
            if result:
                assert (t - base_t) >= 90.0
                return
        assert False, "Zombie reading should have triggered after 90s"

    def test_does_not_trigger_in_flow(self):
        detector = ZombieReadingDetector(min_duration=5.0, cooldown=0.0)
        for i in range(100):
            result = detector.update(
                state="FLOW",
                mouse_velocity=10.0,
                blink_rate=22.0,
                active_app="Google Chrome",
                current_time=1000.0 + float(i),
            )
            assert result is False

    def test_does_not_trigger_non_browser(self):
        detector = ZombieReadingDetector(min_duration=5.0, cooldown=0.0)
        for i in range(100):
            result = detector.update(
                state="HYPO",
                mouse_velocity=10.0,
                blink_rate=22.0,
                active_app="Code",  # not a browser
                current_time=1000.0 + float(i),
            )
            assert result is False

    def test_resets_on_high_mouse_velocity(self):
        detector = ZombieReadingDetector(min_duration=5.0, blink_baseline=17.0, cooldown=0.0)
        base_t = 1000.0
        # Accumulate
        for i in range(8):
            detector.update("HYPO", 10.0, 22.0, "Google Chrome", base_t + float(i))
        assert detector.is_accumulating
        # High mouse velocity resets
        detector.update("HYPO", 100.0, 22.0, "Google Chrome", base_t + 8.0)
        assert not detector.is_accumulating

    def test_cooldown_prevents_rapid_retrigger(self):
        detector = ZombieReadingDetector(
            min_duration=5.0, cooldown=300.0, blink_baseline=17.0,
        )
        base_t = 1000.0
        # First trigger
        triggered = False
        for i in range(20):
            if detector.update("HYPO", 10.0, 22.0, "Google Chrome", base_t + float(i)):
                triggered = True
                trigger_time = base_t + float(i)
                break
        assert triggered
        # Immediately after: cooldown blocks
        result = detector.update("HYPO", 10.0, 22.0, "Google Chrome", trigger_time + 1.0)
        assert result is False

    def test_blink_below_baseline_no_trigger(self):
        """Low blink rate (not glazed) → no trigger."""
        detector = ZombieReadingDetector(min_duration=5.0, blink_baseline=20.0, cooldown=0.0)
        for i in range(100):
            result = detector.update(
                state="HYPO",
                mouse_velocity=10.0,
                blink_rate=15.0,  # below 20 * 1.15 = 23
                active_app="Google Chrome",
                current_time=1000.0 + float(i),
            )
            assert result is False
