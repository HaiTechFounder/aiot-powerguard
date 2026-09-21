#include "display.h"

#include <Arduino.h>

namespace powerguard {
namespace display {

Max7219Display::Max7219Display(uint8_t dinPin, uint8_t loadPin, uint8_t clkPin)
    : dinPin_(dinPin),
      loadPin_(loadPin),
      clkPin_(clkPin),
      begun_(false),
      started_(false),
      lastRefreshMs_(0),
      refreshCount_(0) {}

void Max7219Display::begin(uint8_t intensity) {
  pinMode(dinPin_, OUTPUT);
  pinMode(clkPin_, OUTPUT);
  pinMode(loadPin_, OUTPUT);
  digitalWrite(clkPin_, LOW);
  digitalWrite(loadPin_, HIGH);  // idle high; a falling edge latches

  send(kRegDisplayTest, 0x00);  // leave the power-on lamp test
  send(kRegScanLimit, 0x07);    // all eight digits
  send(kRegDecodeMode, 0xFF);   // Code B on every digit
  setIntensity(intensity);
  send(kRegShutdown, 0x01);  // normal operation

  begun_ = true;
  showUnavailable();  // nothing has been measured yet, and the panel says so
}

// One 16-bit frame, MSB first: address then data, latched on LOAD's rising
// edge. digitalWrite on the ESP8266 is a register write, so the whole frame is
// a few microseconds with no busy-wait anywhere.
void Max7219Display::send(uint8_t address, uint8_t value) {
  const uint16_t frame = (static_cast<uint16_t>(address) << 8) | value;
  digitalWrite(loadPin_, LOW);
  for (int8_t bit = 15; bit >= 0; --bit) {
    digitalWrite(clkPin_, LOW);
    digitalWrite(dinPin_, (frame >> bit) & 0x01 ? HIGH : LOW);
    digitalWrite(clkPin_, HIGH);
  }
  digitalWrite(loadPin_, HIGH);
}

void Max7219Display::setIntensity(uint8_t intensity) {
  send(kRegIntensity, intensity > 0x0F ? 0x0F : intensity);
}

void Max7219Display::writePanel(const uint8_t digits[kDigitCount]) {
  // Digit registers are 1-based: DIG0 is register 0x01.
  for (uint8_t i = 0; i < kDigitCount; ++i) {
    send(static_cast<uint8_t>(i + 1), digits[i]);
  }
  ++refreshCount_;
}

void Max7219Display::showUnavailable() {
  if (!begun_) {
    return;
  }
  uint8_t digits[kDigitCount];
  formatPanel(digits, 0.0f, false, 0.0f, false);
  writePanel(digits);
}

void Max7219Display::showMeasurement(const measurement::PowerMeasurement& sample,
                                     bool sampleFresh) {
  if (!begun_) {
    return;
  }
  // One gate for both groups: the pair is one reading, and showing half of a
  // reading the sensor did not produce would be worse than showing neither.
  const bool usable = sampleFresh && sample.valid;
  uint8_t digits[kDigitCount];
  formatPanel(digits, sample.voltageV, usable, sample.currentA, usable);
  writePanel(digits);
}

bool Max7219Display::tick(uint32_t nowMs,
                          const measurement::PowerMeasurement& sample,
                          bool sampleFresh) {
  if (!begun_) {
    return false;
  }
  // Unsigned subtraction wraps correctly, so millis() rollover is a non-event.
  if (started_ && (nowMs - lastRefreshMs_) < kRefreshIntervalMs) {
    return false;
  }
  lastRefreshMs_ = nowMs;
  started_ = true;
  showMeasurement(sample, sampleFresh);
  return true;
}

}  // namespace display
}  // namespace powerguard
