<?php

namespace Demo\Service;

interface RunnerContract
{
    public function run(int $value): \Demo\Support\Helper;
}

class BaseService
{
    protected function base(): void
    {
    }
}
