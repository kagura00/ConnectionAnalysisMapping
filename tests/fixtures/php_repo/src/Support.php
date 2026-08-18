<?php

namespace Demo\Support;

interface Persistable
{
    public function save(string $value): void;
}

trait Logs
{
    public function log(string $value): void
    {
        $value;
    }
}

class Helper implements Persistable
{
    public function save(string $value): void
    {
        $value;
    }

    public function format(string $value): string
    {
        return $value;
    }
}

function make_helper(): Helper
{
    return new Helper();
}
