// AIoT PowerGuard - local display formatting (pure logic).
//
// Turns one measurement into the eight MAX7219 Code B digit codes the panel
// shows. Deliberately free of Arduino and of the driver, so the code the device
// runs is exactly the code the host tests exercise - the same split as
// scheduler and measurement.
//
// Layout, left to right:
//
//     digit 7 6 5 4   3 2 1 0
//           X X.X X   X X.X X
//           voltage    current
//
// A group is four digits, which is the whole constraint. "XX.XX" fits; a sign
// costs one of the two integer digits, so a negative value is rendered
// "-X.XX". Reverse current is real and must be visible, and on this hardware
// the measurable range is +/-8.19 A, so one integer digit is always enough for
// a value the sensor could legitimately report. Anything wider is refused
// rather than truncated into a plausible lie.

#pragma once

#include <stdint.h>

namespace powerguard {
namespace display {

// MAX7219 Code B decode values. 0-9 are themselves; the rest are the font's
// own symbols. Bit 7 of a written digit lights the decimal point.
constexpr uint8_t kCodeBDash = 0x0A;
constexpr uint8_t kCodeBE = 0x0B;
constexpr uint8_t kCodeBBlank = 0x0F;
constexpr uint8_t kDecimalPointMask = 0x80;

constexpr uint8_t kDigitCount = 8;
constexpr uint8_t kGroupDigits = 4;

// Refresh cadence, and the age past which a reading stops being "now".
// Pure policy, kept here so the host tests can hold the firmware to it.
constexpr uint32_t kRefreshIntervalMs = 500UL;
constexpr uint32_t kSampleStaleAfterMs = 5000UL;

// Largest magnitude representable as "XX.XX", and as "-X.XX" with a sign.
constexpr float kMaxUnsignedDisplay = 99.995f;
constexpr float kMaxSignedDisplay = 9.995f;

// One four-digit group, most significant digit first.
//
// `value` is rendered "XX.XX", or "-X.XX" when negative. `valid` false yields
// four dashes: the panel says "not measured", never a stale or invented
// number. A value too wide for the group, or not finite, yields "EEEE".
void formatGroup(uint8_t out[kGroupDigits], float value, bool valid);

// The whole panel: voltage in digits 7..4, current in digits 3..0.
//
// `out` is indexed by MAX7219 digit number, so out[0] is DIG0. The two groups
// are independent: a valid voltage still shows while an invalid current shows
// dashes, because they are two separate claims.
void formatPanel(uint8_t out[kDigitCount],
                 float voltageV,
                 bool voltageValid,
                 float currentA,
                 bool currentValid);

}  // namespace display
}  // namespace powerguard
