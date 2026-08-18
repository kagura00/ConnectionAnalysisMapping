<?php

namespace Demo;

use Demo\Service\RunnerService;
use function Demo\Support\make_helper;

function start(RunnerService $runner): void
{
    $runner->run(1);
    $helper = make_helper();
    $helper->save('ready');
}

function make_runner(): RunnerService
{
    return RunnerService::create();
}
