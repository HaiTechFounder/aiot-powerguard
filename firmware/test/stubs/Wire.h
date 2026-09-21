// Minimal TwoWire stub for host (native) unit tests.

#pragma once

#include <stdint.h>

class TwoWire {
 public:
  void begin(int sda, int scl);
  void setClock(uint32_t clockHz);
};

extern TwoWire Wire;
