namespace Demo;

public interface IWorker
{
    void Run();
}

public class Worker : IWorker
{
    public void Run() { }

    public int Save() { return 1; }
}
