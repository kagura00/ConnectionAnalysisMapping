namespace Demo.Domain;

public abstract class BaseEntity
{
    public virtual string Id => "base";

    protected BaseEntity()
    {
    }

    public virtual void Save()
    {
    }
}
