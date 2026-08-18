package demo;

import java.util.ArrayList;
import java.util.List;
import demo.support.Helper;

public class Derived extends Base implements Runnable, Marker {
    private final Helper helper = new Helper();
    private int value;

    public Derived(int value) {
        this.value = value;
    }

    @Override
    public void run() {
        local();
        helper.help();
    }

    private int local() {
        return baseValue();
    }

    public List<String> values() {
        return new ArrayList<>();
    }

    @Override
    public void mark() {
    }
}
