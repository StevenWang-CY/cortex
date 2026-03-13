"""Tests for zombie-reading detection."""
import pytest
from cortex.services.state_engine.zombie_detector import ZombieReadingDetector


class TestZombieReadingDetector:
    def test_fires_after_sustained_hypo_browser_low_mouse_high_blink(self):
        """HYPO + browser + low mouse + blink above baseline for 120s -> fires."""
        detector = ZombieReadingDetector(
            blink_baseline=17.0,
            min_duration=90.0,
            cooldown=300.0,
        )
        base_time = 1000.0
        fired = False
        # Simulate updates every 1 second for 120 seconds
        for i in range(121):
            result = detector.update(
                state="HYPO",
                mouse_velocity=10.0,      # < 30 px/s threshold
                blink_rate=22.0,           # > 17.0 * 1.15 = 19.55
                active_app="Google Chrome",
                current_time=base_time + i,
            )
            if result:
                fired = True
                break

        assert fired, "Zombie detection should fire after sustained conditions"

    def test_does_not_fire_active_typing_high_mouse(self):
        """High mouse velocity (active interaction) should prevent firing."""
        detector = ZombieReadingDetector(
            blink_baseline=17.0,
            min_duration=90.0,
            cooldown=300.0,
        )
        base_time = 1000.0
        for i in range(150):
            result = detector.update(
                state="HYPO",
                mouse_velocity=100.0,     # > 30 px/s threshold
                blink_rate=22.0,
                active_app="Google Chrome",
                current_time=base_time + i,
            )
            assert not result, "Should not fire with high mouse velocity"

    def test_does_not_fire_in_flow_state(self):
        """FLOW state should prevent zombie detection from firing."""
        detector = ZombieReadingDetector(
            blink_baseline=17.0,
            min_duration=90.0,
            cooldown=300.0,
        )
        base_time = 1000.0
        for i in range(150):
            result = detector.update(
                state="FLOW",
                mouse_velocity=10.0,
                blink_rate=22.0,
                active_app="Google Chrome",
                current_time=base_time + i,
            )
            assert not result, "Should not fire in FLOW state"

    def test_does_not_fire_in_hyper_state(self):
        """HYPER state should prevent zombie detection from firing."""
        detector = ZombieReadingDetector(
            blink_baseline=17.0,
            min_duration=90.0,
            cooldown=300.0,
        )
        base_time = 1000.0
        for i in range(150):
            result = detector.update(
                state="HYPER",
                mouse_velocity=10.0,
                blink_rate=22.0,
                active_app="Google Chrome",
                current_time=base_time + i,
            )
            assert not result

    def test_does_not_fire_non_browser_app(self):
        """Non-browser app should prevent zombie detection."""
        detector = ZombieReadingDetector(
            blink_baseline=17.0,
            min_duration=90.0,
            cooldown=300.0,
        )
        base_time = 1000.0
        for i in range(150):
            result = detector.update(
                state="HYPO",
                mouse_velocity=10.0,
                blink_rate=22.0,
                active_app="Visual Studio Code",
                current_time=base_time + i,
            )
            assert not result, "Should not fire for non-browser app"

    def test_does_not_fire_blink_below_elevated_baseline(self):
        """Blink rate below 115% of baseline should prevent firing."""
        detector = ZombieReadingDetector(
            blink_baseline=17.0,
            min_duration=90.0,
            cooldown=300.0,
        )
        base_time = 1000.0
        for i in range(150):
            result = detector.update(
                state="HYPO",
                mouse_velocity=10.0,
                blink_rate=18.0,  # < 17 * 1.15 = 19.55
                active_app="Google Chrome",
                current_time=base_time + i,
            )
            assert not result, "Should not fire with low blink rate"

    def test_cooldown_prevents_rapid_refire(self):
        """After firing, cooldown should prevent re-firing for cooldown_seconds."""
        cooldown = 300.0
        detector = ZombieReadingDetector(
            blink_baseline=17.0,
            min_duration=90.0,
            cooldown=cooldown,
        )
        base_time = 1000.0

        # First: run until it fires
        fire_time = None
        for i in range(150):
            result = detector.update(
                state="HYPO",
                mouse_velocity=10.0,
                blink_rate=22.0,
                active_app="Google Chrome",
                current_time=base_time + i,
            )
            if result:
                fire_time = base_time + i
                break

        assert fire_time is not None, "Should have fired the first time"

        # Second: immediately try to fire again - should be blocked by cooldown
        for i in range(150):
            result = detector.update(
                state="HYPO",
                mouse_velocity=10.0,
                blink_rate=22.0,
                active_app="Google Chrome",
                current_time=fire_time + 1.0 + i,
            )
            assert not result, "Cooldown should prevent re-firing"

    def test_fires_again_after_cooldown_expires(self):
        """After cooldown expires, detection should be able to fire again."""
        cooldown = 300.0
        detector = ZombieReadingDetector(
            blink_baseline=17.0,
            min_duration=90.0,
            cooldown=cooldown,
        )
        base_time = 1000.0

        # First fire
        fire_time = None
        for i in range(150):
            result = detector.update(
                state="HYPO",
                mouse_velocity=10.0,
                blink_rate=22.0,
                active_app="Google Chrome",
                current_time=base_time + i,
            )
            if result:
                fire_time = base_time + i
                break

        assert fire_time is not None

        # Wait past cooldown and try again
        second_base = fire_time + cooldown + 1.0
        fired_again = False
        for i in range(150):
            result = detector.update(
                state="HYPO",
                mouse_velocity=10.0,
                blink_rate=22.0,
                active_app="Google Chrome",
                current_time=second_base + i,
            )
            if result:
                fired_again = True
                break

        assert fired_again, "Should fire again after cooldown expires"

    def test_resets_when_conditions_break(self):
        """If conditions stop being met, accumulation should reset."""
        detector = ZombieReadingDetector(
            blink_baseline=17.0,
            min_duration=90.0,
            cooldown=300.0,
        )
        base_time = 1000.0

        # Accumulate for 50 seconds
        for i in range(50):
            detector.update(
                state="HYPO",
                mouse_velocity=10.0,
                blink_rate=22.0,
                active_app="Google Chrome",
                current_time=base_time + i,
            )

        assert detector.is_accumulating

        # Break conditions (switch to FLOW)
        detector.update(
            state="FLOW",
            mouse_velocity=10.0,
            blink_rate=22.0,
            active_app="Google Chrome",
            current_time=base_time + 50,
        )

        assert not detector.is_accumulating

    def test_none_blink_rate_still_allows_detection(self):
        """When blink_rate is None, the blink check is skipped (passes)."""
        detector = ZombieReadingDetector(
            blink_baseline=17.0,
            min_duration=90.0,
            cooldown=300.0,
        )
        base_time = 1000.0
        fired = False
        for i in range(150):
            result = detector.update(
                state="HYPO",
                mouse_velocity=10.0,
                blink_rate=None,  # No blink data
                active_app="Google Chrome",
                current_time=base_time + i,
            )
            if result:
                fired = True
                break

        assert fired, "Should fire even without blink data"

    def test_various_browser_names(self):
        """Detection should work with all recognized browser app names."""
        browsers = ["Google Chrome", "Safari", "Firefox", "Arc",
                     "chrome", "safari", "firefox", "arc"]
        for browser in browsers:
            detector = ZombieReadingDetector(
                blink_baseline=17.0,
                min_duration=5.0,   # short for test
                cooldown=0.0,       # no cooldown for test
            )
            base_time = 1000.0
            fired = False
            for i in range(20):
                result = detector.update(
                    state="HYPO",
                    mouse_velocity=10.0,
                    blink_rate=22.0,
                    active_app=browser,
                    current_time=base_time + i,
                )
                if result:
                    fired = True
                    break
            assert fired, f"Should fire for browser '{browser}'"
