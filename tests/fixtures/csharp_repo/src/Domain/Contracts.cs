namespace Demo.Domain;

public delegate string Formatter(string value);

public record Snapshot(string Value);

public struct Counter
{
    public int Value;
}
