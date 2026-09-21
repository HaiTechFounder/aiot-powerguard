// Arduino stub implementation for host unit tests.

#include <Arduino.h>

namespace {
unsigned long g_millis = 0;
long g_random = 0;
}  // namespace

unsigned long millis() { return g_millis; }
void test_set_millis(unsigned long value) { g_millis = value; }

long random(long howsmall, long howbig) {
  if (howbig <= howsmall) {
    return howsmall;
  }
  const long span = howbig - howsmall;
  return howsmall + (g_random % span);
}
void test_set_random(long value) { g_random = value < 0 ? -value : value; }

char* dtostrf(double value, signed char width, unsigned char precision, char* out) {
  char format[16];
  snprintf(format, sizeof(format), "%%%d.%uf", static_cast<int>(width),
           static_cast<unsigned>(precision));
  sprintf(out, format, value);
  return out;
}

size_t Print::print(char value) {
  const char text[2] = {value, '\0'};
  return write(text);
}

size_t Print::print(int value) {
  char text[24];
  snprintf(text, sizeof(text), "%d", value);
  return write(text);
}

size_t Print::print(unsigned int value) {
  char text[24];
  snprintf(text, sizeof(text), "%u", value);
  return write(text);
}

size_t Print::print(long value) {
  char text[32];
  snprintf(text, sizeof(text), "%ld", value);
  return write(text);
}

size_t Print::print(unsigned long value) {
  char text[32];
  snprintf(text, sizeof(text), "%lu", value);
  return write(text);
}

size_t Print::print(unsigned long long value) {
  char text[32];
  snprintf(text, sizeof(text), "%llu", value);
  return write(text);
}

size_t Print::print(int value, int base) {
  char text[32];
  snprintf(text, sizeof(text), base == HEX ? "%X" : "%d", value);
  return write(text);
}

size_t Print::print(unsigned long value, int base) {
  char text[32];
  snprintf(text, sizeof(text), base == HEX ? "%lX" : "%lu", value);
  return write(text);
}

size_t Print::print(double value, int digits) {
  char text[48];
  snprintf(text, sizeof(text), "%.*f", digits, value);
  return write(text);
}

size_t Print::println() { return write("\n"); }
size_t Print::println(const char* text) { return print(text) + write("\n"); }
size_t Print::println(int value) { return print(value) + write("\n"); }
size_t Print::println(unsigned int value) { return print(value) + write("\n"); }
size_t Print::println(unsigned long value) { return print(value) + write("\n"); }
size_t Print::println(unsigned long long value) { return print(value) + write("\n"); }
size_t Print::println(int value, int base) { return print(value, base) + write("\n"); }
size_t Print::println(unsigned long value, int base) { return print(value, base) + write("\n"); }
size_t Print::println(double value, int digits) { return print(value, digits) + write("\n"); }
