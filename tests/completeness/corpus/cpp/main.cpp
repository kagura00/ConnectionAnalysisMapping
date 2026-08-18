namespace demo {
class Base {
public:
    int value() const { return 1; }
};

class Derived : public Base {
public:
    int run() { return value(); }
};

int start() {
    Derived item;
    return item.run();
}
}
