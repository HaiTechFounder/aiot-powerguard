#include "display_format.h"

#include <math.h>

namespace powerguard {
namespace display {
namespace {

void fill(uint8_t out[kGroupDigits], uint8_t code) {
  for (uint8_t i = 0; i < kGroupDigits; ++i) {
    out[i] = code;
  }
}

// Rounds to hundredths once, here, so the digits shown and the range check
// agree. Rounding after the check could turn an in-range 99.999 into 100.00.
bool toHundredths(float value, uint32_t& scaled) {
  const float rounded = value * 100.0f + 0.5f;
  if (rounded < 0.0f) {
    return false;
  }
  scaled = static_cast<uint32_t>(rounded);
  return true;
}

}  // namespace

void formatGroup(uint8_t out[kGroupDigits], float value, bool valid) {
  if (!valid) {
    fill(out, kCodeBDash);
    return;
  }
  // NaN fails every comparison, so it is excluded before any range test.
  if (isnan(value) || isinf(value)) {
    fill(out, kCodeBE);
    return;
  }

  const bool negative = value < 0.0f;
  const float magnitude = negative ? -value : value;
  const float limit = negative ? kMaxSignedDisplay : kMaxUnsignedDisplay;
  if (magnitude > limit) {
    fill(out, kCodeBE);
    return;
  }

  uint32_t scaled = 0;
  if (!toHundredths(magnitude, scaled)) {
    fill(out, kCodeBE);
    return;
  }

  const uint32_t whole = scaled / 100U;
  const uint32_t frac = scaled % 100U;

  if (negative) {
    // "-X.XX": the sign takes the tens position.
    out[0] = kCodeBDash;
    out[1] = static_cast<uint8_t>(whole % 10U) | kDecimalPointMask;
  } else {
    const uint8_t tens = static_cast<uint8_t>(whole / 10U);
    // A leading zero is noise on a seven-segment panel; blank it.
    out[0] = tens == 0 ? kCodeBBlank : tens;
    out[1] = static_cast<uint8_t>(whole % 10U) | kDecimalPointMask;
  }
  out[2] = static_cast<uint8_t>(frac / 10U);
  out[3] = static_cast<uint8_t>(frac % 10U);
}

void formatPanel(uint8_t out[kDigitCount],
                 float voltageV,
                 bool voltageValid,
                 float currentA,
                 bool currentValid) {
  uint8_t group[kGroupDigits];

  formatGroup(group, voltageV, voltageValid);
  // Leftmost group is DIG7..DIG4, most significant digit at DIG7.
  for (uint8_t i = 0; i < kGroupDigits; ++i) {
    out[7 - i] = group[i];
  }

  formatGroup(group, currentA, currentValid);
  for (uint8_t i = 0; i < kGroupDigits; ++i) {
    out[3 - i] = group[i];
  }
}

}  // namespace display
}  // namespace powerguard
