package service

import (
	"fmt"
	"example.com/go-fixture/src/domain"
	"example.com/go-fixture/src/helper"
)

type Runner struct {
	helper *helper.Helper
	base   domain.Base
	domain.Embedded
}

func NewRunner() *Runner {
	return &Runner{helper: helper.New()}
}

func (r *Runner) Work(n int) error {
	local := helper.Helper{}
	r.helper.Save(n)
	local.Format()
	f := func(value int) int { return value + 1 }
	_ = f(n)
	fmt.Println(n)
	return nil
}

func (r *Runner) Close() error {
	return nil
}
