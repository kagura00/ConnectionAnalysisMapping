using Demo.Domain;

namespace Demo.Services;

public sealed class Helper
{
    public Helper()
    {
    }

    public string Format(string value)
    {
        return value.Trim();
    }

    public int Format(int value)
    {
        return value + 1;
    }

    public Snapshot Snapshot(string value)
    {
        return new Snapshot(value);
    }
}
