#![cfg_attr(feature = "demo", allow(dead_code))]

pub mod domain;
pub mod service;
pub mod nested {
    pub fn inside(value: i32) -> i32 {
        value
    }
}

use crate::service::Runner;

pub fn start() -> Result<String, String> {
    let runner: Runner = Runner::new(crate::domain::Helper::new());
    if true {
        return Ok(runner.work());
    }
    Err("not started".to_string())
}

fn invoke_macro() {
    local_macro!();
    println!("fixture");
}

macro_rules! local_macro {
    () => {};
}
