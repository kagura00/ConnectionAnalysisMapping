#pragma once

namespace demo {

template <typename T>
class Base {
public:
    int base_value() const { return 1; }
};

class Derived : public Base<int> {
public:
    int value() const;
};

struct Counter {
    int value;
    int increment() const { return value + 1; }
};

int add(int left, int right);

}
