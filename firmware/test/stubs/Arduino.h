// Minimal Arduino stub for host (native) unit tests.
//
// Only the surface the tested modules actually use. Time and randomness are
// controllable so the schedule and jitter tests are deterministic.

#pragma once

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define F(x) (x)
#define HEX 16
#define DEC 10

// Deterministic stand-in for the ESP8266 hardware RNG.
#define RANDOM_REG32 (0x7fa31c09UL)

unsigned long millis();
void test_set_millis(unsigned long value);

long random(long howsmall, long howbig);
void test_set_random(long value);

char* dtostrf(double value, signed char width, unsigned char precision, char* out);

class Print {
 public:
  virtual ~Print() {}
  virtual size_t write(const char* text) = 0;

  size_t print(const char* text) { return write(text); }
  size_t print(char value);
  size_t print(int value);
  size_t print(unsigned int value);
  size_t print(long value);
  size_t print(unsigned long value);
  size_t print(unsigned long long value);
  size_t print(int value, int base);
  size_t print(unsigned long value, int base);
  size_t print(double value, int digits);

  size_t println();
  size_t println(const char* text);
  size_t println(int value);
  size_t println(unsigned int value);
  size_t println(unsigned long value);
  size_t println(unsigned long long value);
  size_t println(int value, int base);
  size_t println(unsigned long value, int base);
  size_t println(double value, int digits);
};
