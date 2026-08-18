use std::fmt::Display;

#[cfg(feature = "domain")]
pub struct Base {
    pub id: i32,
}

pub trait BaseTrait: Display {
    fn base(&self);
}

pub trait Worker: BaseTrait {
    type Output;
    fn run(&self) -> Self::Output;
}

pub type Alias = Base;

pub struct Helper;

impl Helper {
    pub fn new() -> Self {
        Self
    }

    pub fn save(&self) {}
}

pub enum State {
    Ready,
    Busy(u32),
}

pub union Raw {
    pub number: u32,
    pub decimal: f32,
}
