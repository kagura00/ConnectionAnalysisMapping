package domain

type Base struct {
	ID int
}

type Worker interface {
	Work(int) error
	Close() error
}

type Kind string

type Alias = Kind

type Embedded struct {
	Name string
}
