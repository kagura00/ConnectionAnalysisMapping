using Demo.Domain;
using HelperAlias = Demo.Services.Helper;

namespace Demo.Services;

public class Runner : BaseEntity, IWorker
{
    private readonly Helper _helper = new Helper();

    public Runner() : base()
    {
    }

    public override void Save()
    {
        Work("save");
        _helper.Format("text");
        Helper local = new Helper();
        local.Format(1);
        Point point = new Point(1, 2);
        Formatter formatter = value => value.Trim();
        formatter("value");
        _ = point.X;
    }

    public void Work(string input)
    {
        Save();
        HelperAlias alias = new HelperAlias();
        alias.Format(input);
    }

    public string Execute(string input)
    {
        return _helper.Format(input);
    }
}
