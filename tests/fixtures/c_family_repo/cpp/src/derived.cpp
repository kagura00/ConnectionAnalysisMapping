#include "../include/base.hpp"

namespace demo {

int Derived::value() const {
    return base_value();
}

int add(int left, int right) {
    return left + right;
}

}
