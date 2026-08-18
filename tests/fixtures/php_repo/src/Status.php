<?php

namespace Demo;

enum Status: string
{
    case Ready = 'ready';

    public function label(): string
    {
        return $this->value;
    }
}
