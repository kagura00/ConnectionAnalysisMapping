use crate::domain::{Helper, Worker};

pub struct Runner {
    helper: Helper,
}

impl Runner {
    pub fn new(helper: Helper) -> Self {
        Self { helper }
    }

    pub fn work(&self) -> String {
        let callback = |value: i32| value + 1;
        self.helper.save();
        callback(1).to_string()
    }
}

impl Worker for Runner {
    type Output = String;

    fn run(&self) -> String {
        self.work()
    }
}

impl Runner {
    fn close(&mut self) {}
}
