<?php

namespace Demo\Service;

use Demo\Support\{Helper as H, Logs};
use function Demo\Support\make_helper as make;

final class RunnerService extends BaseService implements RunnerContract
{
    use Logs;

    private H $helper;

    public function __construct(H $helper)
    {
        $this->helper = $helper;
    }

    public function run(int $value): H
    {
        $worker = $this->helper;
        $worker->save((string) $value);
        $this->log(make()->format((string) $value));
        return $worker;
    }

    public static function create(): self
    {
        return new self(make());
    }
}
