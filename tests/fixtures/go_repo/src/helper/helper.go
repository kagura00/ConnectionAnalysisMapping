package helper

import "strings"

type Helper struct {
	Value int
}

func New() *Helper {
	return &Helper{}
}

func (h *Helper) Save(value int) error {
	h.Value = value
	return nil
}

func (h *Helper) Format() string {
	return strings.TrimSpace("helper")
}
