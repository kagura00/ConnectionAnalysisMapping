<?php

namespace Demo;

interface Worker
{
    public function run(): void;
}

class Service implements Worker
{
    public function run(): void {}
}

function start(): void {}
