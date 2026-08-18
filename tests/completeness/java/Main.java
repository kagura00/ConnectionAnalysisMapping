package demo;

interface Marker {
    void mark();
}

class Base {
    int value() { return 1; }
}

public class Main extends Base implements Marker {
    public void mark() { }

    int run() { return value(); }
}
