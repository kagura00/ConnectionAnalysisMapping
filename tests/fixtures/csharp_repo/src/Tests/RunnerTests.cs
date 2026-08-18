using Demo.Services;

namespace Demo.Tests;

public class RunnerTests
{
    public void Run()
    {
        new Runner().Execute("test");
    }
}
