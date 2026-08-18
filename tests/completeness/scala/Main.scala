package demo

trait Worker {
  def run(value: Int): String
}

class Service extends Worker {
  def run(value: Int): String = value.toString
}

object Main {
  def start(): Unit = new Service().run(1)
}
