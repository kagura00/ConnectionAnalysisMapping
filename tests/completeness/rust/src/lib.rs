pub struct Service;

pub trait Worker {
    fn run(&self);
}

impl Worker for Service {
    fn run(&self) {}
}

pub fn start() {
    helper();
}

fn helper() {}
