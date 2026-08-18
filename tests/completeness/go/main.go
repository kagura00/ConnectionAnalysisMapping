package demo

type Worker interface {
    Run()
}

type Service struct{}

func (Service) Run() {}

func Start() { helper() }

func helper() {}
