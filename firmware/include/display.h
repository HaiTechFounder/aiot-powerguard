// AIoT PowerGuard - MAX7219 local readout (8-digit, 7-segment).
//
// Shows the most recent validated INA226 sample on the panel wired to the
// NodeMCU. Local display only: it reads the same sample telemetry already
// carries and publishes nothing, so MQTT, Wi-Fi and reconnect behaviour are
// untouched by design - this module has no reference to any of them.
//
// The driver is ~50 lines of bit-banged SPI rather than a library. A MAX7219 in
// Code B decode mode needs three GPIOs and a 16-bit shift; pulling in a
// matrix-oriented dependency for that would add more surface than it removes,
// and `lib_deps` stays pinned to the two direct dependencies it had.
//
// Every write is a fixed number of instructions with no delay() and no loop
// that waits on anything, so a refresh cannot stall the main loop, starve the
// watchdog, or shift the sampling and telemetry deadlines.

#pragma once

#include <stdint.h>

#include "display_format.h"
#include "measurement.h"

namespace powerguard {
namespace display {

// NodeMCU labels, per the wiring this firmware expects.
constexpr uint8_t kPinDin = 14;   // D5 / GPIO14
constexpr uint8_t kPinLoad = 12;  // D6 / GPIO12  (CS)
constexpr uint8_t kPinClk = 13;   // D7 / GPIO13

// 0..15. Mid-scale: legible indoors without needlessly loading the 3V3 rail.
constexpr uint8_t kDefaultIntensity = 7;

// kRefreshIntervalMs (comfortably inside the required 500-1000 ms) and
// kSampleStaleAfterMs (a reading older than this is no longer "now" and shows
// as dashes) live in display_format.h, so the host tests can pin them.

// MAX7219 register addresses.
constexpr uint8_t kRegNoOp = 0x00;
constexpr uint8_t kRegDecodeMode = 0x09;
constexpr uint8_t kRegIntensity = 0x0A;
constexpr uint8_t kRegScanLimit = 0x0B;
constexpr uint8_t kRegShutdown = 0x0C;
constexpr uint8_t kRegDisplayTest = 0x0F;

class Max7219Display {
 public:
  Max7219Display(uint8_t dinPin = kPinDin,
                 uint8_t loadPin = kPinLoad,
                 uint8_t clkPin = kPinClk);

  // Drives the pins, wakes the chip, selects Code B decoding for all eight
  // digits and blanks the panel. Safe to call when no panel is attached: the
  // bus is write-only, so a missing MAX7219 costs three idle GPIOs and
  // nothing else.
  void begin(uint8_t intensity = kDefaultIntensity);

  // Refreshes at most once per kRefreshIntervalMs. `sample` is the last
  // validated reading and `sampleFresh` says whether it still describes now;
  // when it does not, the panel shows dashes instead of a stale number.
  // Returns true when a refresh was written on this call.
  bool tick(uint32_t nowMs, const measurement::PowerMeasurement& sample, bool sampleFresh);

  // Writes the eight digits immediately, ignoring the refresh interval.
  void showMeasurement(const measurement::PowerMeasurement& sample, bool sampleFresh);

  // Eight dashes: "not measured", distinct from any value.
  void showUnavailable();

  void setIntensity(uint8_t intensity);
  bool begun() const { return begun_; }
  uint32_t refreshCount() const { return refreshCount_; }

 private:
  void send(uint8_t address, uint8_t value);
  void writePanel(const uint8_t digits[kDigitCount]);

  uint8_t dinPin_;
  uint8_t loadPin_;
  uint8_t clkPin_;
  bool begun_;
  bool started_;
  uint32_t lastRefreshMs_;
  uint32_t refreshCount_;
};

}  // namespace display
}  // namespace powerguard
